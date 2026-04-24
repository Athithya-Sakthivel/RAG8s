#!/usr/bin/env python3
# Generates CronJob + RBAC + supporting manifests for the indexing pipeline.
# This variant enhances idempotent rollout behavior (render/hash/state) and
# cluster-aware AWS auth (IRSA for EKS, static creds for kind).

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

# --- Defaults and runtime keys ------------------------------------------------

DEFAULTS: dict[str, str] = {
    "NAMESPACE": "indexing",
    "CRONJOB_NAME": "indexing-backup-cronjob",
    "INDEXING_BACKUP_CRON_EXPRESSION": "0 */6 * * *",
    "CRON_SCHEDULE": "0 */6 * * *",
    "CRONJOB_CONCURRENCY": "Allow",
    "CRONJOB_BACKOFF_LIMIT": "1",
    "CRONJOB_PARALLELISM": "3",
    "CRONJOB_COMPLETIONS": "1",
    "CRONJOB_DEBUG_KEEP_POD": "false",
    "CRONJOB_TIMEZONE": "",
    "SUCCESSFUL_JOBS_HISTORY_LIMIT": "3",
    "FAILED_JOBS_HISTORY_LIMIT": "1",
    "SERVICE_ACCOUNT_NAME": "indexer-cron-sa",
    "ROLE_NAME": "indexer-cron-role",
    "ROLEBINDING_NAME": "indexer-cron-rb",
    "INDEXING_PIPELINE_CPU_IMAGE_REPO": "athithya5354/indexing_pipeline_cpu",
    "INDEXING_PIPELINE_CPU_IMAGE_TAG": "v12",
    "INDEXING_BACKUP_CRONJOB_CPU_REQUEST": "2",
    "INDEXING_BACKUP_CRONJOB_CPU_LIMIT": "4",
    "INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST": "1Gi",
    "INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT": "2Gi",
    "LOG_LEVEL": "INFO",
    "HTTP_TIMEOUT": "60",
    "QDRANT_URL": "http://qdrant.qdrant.svc.cluster.local:6333",
    "DENSE_URL": "http://dense-svc.models.svc.cluster.local:8200",
    "SPARSE_URL": "http://sparse-svc.models.svc.cluster.local:8201",
    "PYTHONUNBUFFERED": "1",
    "MANIFESTS_DIR": "src/manifests/indexing_cronjob",
    # AWS-specific defaults
    "DATA_S3_BUCKET": "",
    "AWS_REGION": "us-east-1",
    "STORAGE_RAW_PREFIX": "data/raw/",
    "STORAGE_CHUNKED_PREFIX": "data/chunked/",
    # IRSA role annotation key default (empty unless provided)
    "IRSA_ROLE_ARN": "",
    # state dir for idempotency
    "STATE_DIR": ".state",
}

SENSITIVE_KEYS = {
    "QDRANT_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
}

NAMED_SECRET_MAP = {
    "QDRANT_API_KEY": "qdrant-api-key",
    "AWS_SECRET_ACCESS_KEY": "indexer-aws-creds",
    "AWS_SESSION_TOKEN": "indexer-aws-creds",
    "AWS_ACCESS_KEY_ID": "indexer-aws-creds",
}

RUNTIME_KEYS = set(DEFAULTS.keys()).union(
    {
        "DATA_S3_BUCKET",
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "IRSA_ROLE_ARN",
        "QDRANT_API_KEY",
        "QDRANT_SECRET_NAME",
        "BATCH_SIZE",
        "MAX_TOKENS_PER_CHUNK",
        "MIN_TOKENS_PER_CHUNK",
        "CSV_TARGET_TOKENS_PER_CHUNK",
        "JSONL_TARGET_TOKENS_PER_CHUNK",
        "UPSERT_CHUNK",
        "DENSE_DIM",
        "SPARSE_BATCH_FALLBACK",
        "OVERWRITE_DOC_DOCX_TO_PDF",
        "OVERWRITE_ALL_AUDIO_FILES",
        "OVERWRITE_SPREADSHEETS_WITH_CSV",
        "OVERWRITE_PPT_WITH_PPTS",
        "USE_IRSA",
        "ENV",
        "STORAGE_RAW_PREFIX",
        "STORAGE_CHUNKED_PREFIX",
    }
)

# --- Utilities ----------------------------------------------------------------


def run_cmd(cmd: list[str],
            input_bytes: bytes | None = None,
            timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return proc.returncode, out, err
    except subprocess.TimeoutExpired as e:
        return 124, getattr(e, "stdout", "") or "", getattr(e, "stderr",
                                                             "") or f"timeout after {timeout}s"


def ensure_kubectl_available():
    rc, out, err = run_cmd(["kubectl", "version", "--client=true"])
    if rc != 0:
        print("ERROR: kubectl not available or not in PATH. details:",
              err or out,
              file=sys.stderr)
        raise SystemExit(2)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_state_dir(manifests_dir: Path, state_dir_name: str = DEFAULTS["STATE_DIR"]) -> Path:
    state_dir = manifests_dir / state_dir_name
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def read_previous_hash(state_dir: Path) -> str | None:
    hpath = state_dir / "rendered.sha256"
    if not hpath.exists():
        return None
    return hpath.read_text(encoding="utf-8").strip()


def write_state_files(state_dir: Path, rendered_yaml: str, digest: str) -> None:
    (state_dir / "rendered.yaml").write_text(rendered_yaml, encoding="utf-8")
    (state_dir / "rendered.sha256").write_text(digest + "\n", encoding="utf-8")


# --- Manifest builders -------------------------------------------------------


def is_cron_key(k: str) -> bool:
    up = k.upper()
    for p in ("CRON", "CRONJOB", "INDEXING_BACKUP_CRON",
              "SUCCESSFUL_JOBS_HISTORY_LIMIT",
              "FAILED_JOBS_HISTORY_LIMIT"):
        if p in up:
            return True
    return False


def collect_runtime_env_map() -> dict[str, str]:
    out: dict[str, str] = {}
    keys = sorted(RUNTIME_KEYS.union(DEFAULTS.keys()))
    for k in keys:
        if is_cron_key(k):
            continue
        v = os.environ.get(k)
        if v is None:
            v = DEFAULTS.get(k, "")
        out[k] = "" if v is None else str(v)
    if out.get("INDEXING_BACKUP_CRON_EXPRESSION") and not out.get("CRON_SCHEDULE"):
        out["CRON_SCHEDULE"] = out["INDEXING_BACKUP_CRON_EXPRESSION"]
    return out


def ns_manifest(ns: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": ns}}


def serviceaccount_manifest(ns: str, name: str,
                            annotate_use_irsa: bool, irsa_role_arn: str) -> dict[str, Any]:
    meta = {"name": name, "namespace": ns}
    if annotate_use_irsa and irsa_role_arn:
        meta.setdefault("annotations", {})["eks.amazonaws.com/role-arn"] = irsa_role_arn
    return {"apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": meta}


def role_manifest(ns: str, name: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": ns},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "verbs": ["get", "list", "watch", "create", "update", "patch"],
            },
        ],
    }


def rolebinding_manifest(ns: str, name: str, role_name: str,
                         sa_name: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": ns},
        "subjects": [{
            "kind": "ServiceAccount",
            "name": sa_name,
            "namespace": ns,
        }],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": role_name,
        },
    }


def cronjob_manifest(cfg: dict[str, str],
                     env_map: dict[str, str]) -> dict[str, Any]:
    ns = cfg["NAMESPACE"]
    cron_name = cfg["CRONJOB_NAME"]
    image = (
        f"{cfg.get('INDEXING_PIPELINE_CPU_IMAGE_REPO')}:"
        f"{cfg.get('INDEXING_PIPELINE_CPU_IMAGE_TAG')}"
    )
    sa_name = cfg["SERVICE_ACCOUNT_NAME"]
    aws_secret = cfg.get("AWS_SECRET_NAME",
                         NAMED_SECRET_MAP.get("AWS_SECRET_ACCESS_KEY",
                                              "indexer-aws-creds"))
    qdrant_secret = cfg.get("QDRANT_SECRET_NAME",
                            NAMED_SECRET_MAP.get("QDRANT_API_KEY",
                                                 "qdrant-api-key"))
    extra_secret = cfg.get("EXTRA_SECRET_NAME", "indexer-extra-secrets")

    env_list: list[dict[str, Any]] = []
    use_irsa = cfg.get("USE_IRSA", "0") in ("1", "true", "yes")

    # Ensure S3-related envs are present in env_map (may be empty strings)
    for required in ("DATA_S3_BUCKET", "AWS_REGION", "STORAGE_RAW_PREFIX", "STORAGE_CHUNKED_PREFIX"):
        env_map.setdefault(required, cfg.get(required, DEFAULTS.get(required, "")))

    # Build env list; sensitive keys come from secrets if present in environment
    for k in sorted(env_map.keys()):
        if is_cron_key(k):
            continue
        v = env_map[k] or ""
        if k in SENSITIVE_KEYS:
            # If the sensitive value is present in the environment, create a secretRef
            if os.environ.get(k):
                if k == "QDRANT_API_KEY":
                    env_list.append({
                        "name": k,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": qdrant_secret,
                                "key": "QDRANT_API_KEY",
                            }
                        }
                    })
                elif k in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
                    # For non-IRSA mode we mount AWS creds from a secret
                    env_list.append({
                        "name": k,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": aws_secret,
                                "key": k,
                            }
                        }
                    })
                else:
                    env_list.append({
                        "name": k,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": extra_secret,
                                "key": k,
                            }
                        }
                    })
                continue
            # If not present in env, fall back to literal (could be empty)
            env_list.append({"name": k, "value": v})
            continue
        env_list.append({"name": k, "value": v})

    # Ensure HTTP_TIMEOUT present
    if not any(e.get("name") == "HTTP_TIMEOUT" for e in env_list):
        env_list.append({
            "name": "HTTP_TIMEOUT",
            "value": cfg.get("HTTP_TIMEOUT", DEFAULTS["HTTP_TIMEOUT"]),
        })

    pod_annotations: dict[str, str] = {}
    if use_irsa and cfg.get("IRSA_ROLE_ARN"):
        pod_annotations["eks.amazonaws.com/role-arn"] = cfg["IRSA_ROLE_ARN"]

    # Wrapper script to override ENTRYPOINT
    wrapper_lines = [
        "set -e",
        "DESIRED=\"${DESIRED_NOFILE:-262144}\"",
        "ulimit -n \"$DESIRED\" 2>/dev/null || true",
        "echo \"nofile limit: $(ulimit -n 2>/dev/null || echo unknown)\"",
        "exec /opt/venv/bin/python indexing_pipeline.py",
    ]
    wrapper_script = "\n".join(wrapper_lines)

    container_spec: dict[str, Any] = {
        "name": "indexer",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/bin/sh", "-c"],
        "args": [wrapper_script],
        "env": env_list,
        "resources": {
            "requests": {
                "cpu": cfg.get("INDEXING_BACKUP_CRONJOB_CPU_REQUEST",
                               DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"]),
                "memory": cfg.get("INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST",
                                  DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"]),
            },
            "limits": {
                "cpu": cfg.get("INDEXING_BACKUP_CRONJOB_CPU_LIMIT",
                               DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"]),
                "memory": cfg.get("INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT",
                                  DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"]),
            },
        },
    }

    job_spec: dict[str, Any] = {
        "backoffLimit": int(cfg.get("CRONJOB_BACKOFF_LIMIT",
                                    DEFAULTS["CRONJOB_BACKOFF_LIMIT"])),
        "parallelism": int(cfg.get("CRONJOB_PARALLELISM",
                                   DEFAULTS["CRONJOB_PARALLELISM"])),
        "completions": int(cfg.get("CRONJOB_COMPLETIONS",
                                   DEFAULTS["CRONJOB_COMPLETIONS"])),
        "template": {
            "metadata": {
                "labels": {"app": cron_name},
                **({"annotations": pod_annotations} if pod_annotations else {}),
            },
            "spec": {
                "serviceAccountName": sa_name,
                "restartPolicy": "Never",
                "containers": [container_spec],
            },
        },
    }

    cron: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": cron_name, "namespace": ns},
        "spec": {
            "schedule": cfg.get("CRON_SCHEDULE",
                                DEFAULTS["INDEXING_BACKUP_CRON_EXPRESSION"]),
            "concurrencyPolicy": cfg.get("CRONJOB_CONCURRENCY",
                                        DEFAULTS["CRONJOB_CONCURRENCY"]),
            "successfulJobsHistoryLimit": int(
                cfg.get("SUCCESSFUL_JOBS_HISTORY_LIMIT",
                        DEFAULTS["SUCCESSFUL_JOBS_HISTORY_LIMIT"])),
            "failedJobsHistoryLimit": int(
                cfg.get("FAILED_JOBS_HISTORY_LIMIT",
                        DEFAULTS["FAILED_JOBS_HISTORY_LIMIT"])),
            "jobTemplate": {
                "spec": job_spec
            },
        },
    }

    if cfg.get("CRONJOB_TIMEZONE"):
        cron["spec"]["timeZone"] = cfg["CRONJOB_TIMEZONE"]

    return cron


# --- Secret helpers ----------------------------------------------------------


def kubectl_create_secret_inline(name: str, namespace: str,
                                 literals: dict[str, str]) -> tuple[bool, str]:
    if not literals:
        return False, "no-literals"
    cmd = [
        "kubectl", "create", "secret", "generic", name, "-n", namespace,
        "--dry-run=client", "-o", "yaml"
    ]
    for k, v in literals.items():
        if not k:
            continue
        cmd += ["--from-literal", f"{k}={v}"]
    rc, out, err = run_cmd(cmd, timeout=20)
    if rc != 0:
        return False, err or out
    rc2, out2, err2 = run_cmd(["kubectl", "apply", "-f", "-"],
                              input_bytes=(out.encode("utf-8")),
                              timeout=20)
    if rc2 != 0:
        return False, err2 or out2
    return True, ""


# --- Config load / validation -----------------------------------------------


def validate_aws_creds_present(cfg: dict[str, str]) -> bool:
    use_irsa = cfg.get("USE_IRSA", "0") in ("1", "true", "yes")
    if use_irsa:
        return bool(cfg.get("IRSA_ROLE_ARN"))
    # Non-IRSA: require at least AWS_SECRET_ACCESS_KEY and AWS_ACCESS_KEY_ID or session token
    if os.environ.get("AWS_SECRET_ACCESS_KEY") and os.environ.get("AWS_ACCESS_KEY_ID"):
        return True
    if os.environ.get("AWS_SESSION_TOKEN") and os.environ.get("AWS_ACCESS_KEY_ID"):
        return True
    return False


def load_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    cfg["NAMESPACE"] = os.environ.get("NAMESPACE", DEFAULTS["NAMESPACE"])
    cfg["CRONJOB_NAME"] = os.environ.get("CRONJOB_NAME",
                                        DEFAULTS["CRONJOB_NAME"]).lower()
    cfg["CRON_SCHEDULE"] = os.environ.get(
        "INDEXING_BACKUP_CRON_EXPRESSION",
        os.environ.get("CRON_SCHEDULE",
                       DEFAULTS["INDEXING_BACKUP_CRON_EXPRESSION"]))
    cfg["SERVICE_ACCOUNT_NAME"] = os.environ.get("SERVICE_ACCOUNT_NAME",
                                                DEFAULTS["SERVICE_ACCOUNT_NAME"])
    cfg["ROLE_NAME"] = os.environ.get("ROLE_NAME", DEFAULTS["ROLE_NAME"])
    cfg["ROLEBINDING_NAME"] = os.environ.get("ROLEBINDING_NAME",
                                            DEFAULTS["ROLEBINDING_NAME"])
    cfg["QDRANT_SECRET_NAME"] = os.environ.get(
        "QDRANT_SECRET_NAME",
        NAMED_SECRET_MAP.get("QDRANT_API_KEY", "qdrant-api-key"))
    cfg["AWS_SECRET_NAME"] = os.environ.get(
        "AWS_SECRET_NAME",
        NAMED_SECRET_MAP.get("AWS_SECRET_ACCESS_KEY",
                             "indexer-aws-creds"))
    cfg["EXTRA_SECRET_NAME"] = os.environ.get("EXTRA_SECRET_NAME",
                                             "indexer-extra-secrets")
    cfg["INDEXING_PIPELINE_CPU_IMAGE_REPO"] = os.environ.get(
        "INDEXING_PIPELINE_CPU_IMAGE_REPO",
        DEFAULTS["INDEXING_PIPELINE_CPU_IMAGE_REPO"])
    cfg["INDEXING_PIPELINE_CPU_IMAGE_TAG"] = os.environ.get(
        "INDEXING_PIPELINE_CPU_IMAGE_TAG",
        DEFAULTS["INDEXING_PIPELINE_CPU_IMAGE_TAG"])
    cfg["CRONJOB_BACKOFF_LIMIT"] = os.environ.get(
        "CRONJOB_BACKOFF_LIMIT", DEFAULTS["CRONJOB_BACKOFF_LIMIT"])
    cfg["CRONJOB_CONCURRENCY"] = os.environ.get(
        "CRONJOB_CONCURRENCY", DEFAULTS["CRONJOB_CONCURRENCY"])
    cfg["SUCCESSFUL_JOBS_HISTORY_LIMIT"] = os.environ.get(
        "SUCCESSFUL_JOBS_HISTORY_LIMIT",
        DEFAULTS["SUCCESSFUL_JOBS_HISTORY_LIMIT"])
    cfg["FAILED_JOBS_HISTORY_LIMIT"] = os.environ.get(
        "FAILED_JOBS_HISTORY_LIMIT", DEFAULTS["FAILED_JOBS_HISTORY_LIMIT"])
    cfg["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"] = os.environ.get(
        "INDEXING_BACKUP_CRONJOB_CPU_REQUEST",
        DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"])
    cfg["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"] = os.environ.get(
        "INDEXING_BACKUP_CRONJOB_CPU_LIMIT",
        DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"])
    cfg["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"] = os.environ.get(
        "INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST",
        DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"])
    cfg["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"] = os.environ.get(
        "INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT",
        DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"])
    cfg["MANIFESTS_DIR"] = os.environ.get("MANIFESTS_DIR",
                                         DEFAULTS["MANIFESTS_DIR"])
    use_irsa_env = os.environ.get("AWS_USE_IRSA",
                                  os.environ.get("USE_IRSA",
                                                 "")).strip().lower() in (
                                                   "1", "true", "yes")
    cfg["USE_IRSA"] = "1" if use_irsa_env else "0"
    cfg["IRSA_ROLE_ARN"] = os.environ.get("IRSA_ROLE_ARN", "")
    cfg["AWS_REGION"] = os.environ.get("AWS_REGION", DEFAULTS["AWS_REGION"])
    cfg["DATA_S3_BUCKET"] = os.environ.get("DATA_S3_BUCKET", DEFAULTS.get("DATA_S3_BUCKET", ""))
    cfg["STORAGE_RAW_PREFIX"] = os.environ.get("STORAGE_RAW_PREFIX", DEFAULTS["STORAGE_RAW_PREFIX"])
    cfg["STORAGE_CHUNKED_PREFIX"] = os.environ.get("STORAGE_CHUNKED_PREFIX", DEFAULTS["STORAGE_CHUNKED_PREFIX"])
    cfg["STATE_DIR"] = os.environ.get("STATE_DIR", DEFAULTS["STATE_DIR"])
    env_map = collect_runtime_env_map()
    for k, v in env_map.items():
        if k not in cfg:
            cfg[k] = v
    return cfg


def validate_cfg(cfg: dict[str, str]):
    # Validate IRSA vs static creds
    if cfg.get("USE_IRSA", "0") in ("1", "true", "yes"):
        required: list[str] = []
        if not cfg.get("IRSA_ROLE_ARN"):
            required.append("IRSA_ROLE_ARN")
        if not cfg.get("AWS_REGION"):
            required.append("AWS_REGION")
        if not cfg.get("DATA_S3_BUCKET"):
            required.append("DATA_S3_BUCKET")
        if required:
            print("ERROR: When USE_IRSA enabled, the following envs are required:", ", ".join(required), file=sys.stderr)
            raise SystemExit(2) from None
    else:
        # Non-IRSA: require AWS creds or QDRANT_API_KEY
        if not (
            (os.environ.get("AWS_SECRET_ACCESS_KEY") and os.environ.get("AWS_ACCESS_KEY_ID"))
            or os.environ.get("AWS_SESSION_TOKEN")
            or os.environ.get("QDRANT_API_KEY")
        ):
            print(
                "ERROR: non-IRSA mode requires AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY or AWS_SESSION_TOKEN or QDRANT_API_KEY",
                file=sys.stderr,
            )
            raise SystemExit(2) from None

    for k in ("CRONJOB_PARALLELISM", "CRONJOB_COMPLETIONS"):
        v = cfg.get(k, DEFAULTS.get(k, ""))
        if v is None or v == "":
            continue
        try:
            ival = int(str(v))
            if ival < 1:
                print(f"ERROR: {k} must be a positive integer",
                      file=sys.stderr)
                raise SystemExit(2) from None
        except Exception:
            print(f"ERROR: {k} must be an integer", file=sys.stderr)
            raise SystemExit(2) from None


# --- File helpers ------------------------------------------------------------


def write_manifest_file(manifests_dir: Path, filename: str,
                        manifest: dict[str, Any]) -> Path:
    path = manifests_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(manifest,
                       fh,
                       sort_keys=False,
                       default_flow_style=False,
                       allow_unicode=True)
    return path


def recreate_manifests_dir(manifests_dir: Path):
    if manifests_dir.exists():
        shutil.rmtree(manifests_dir)
    manifests_dir.mkdir(parents=True, exist_ok=True)


# --- Apply / idempotent rollout ---------------------------------------------


def render_all_manifests(manifests: list[tuple[str, dict[str, Any]]]) -> str:
    docs: list[dict[str, Any]] = []
    for _, m in manifests:
        docs.append(m)
    return yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False, explicit_start=True)


def apply(cfg: dict[str, str], dry_run: bool = False):
    ensure_kubectl_available()
    ns = cfg["NAMESPACE"]
    sa_name = cfg["SERVICE_ACCOUNT_NAME"]
    role_name = cfg["ROLE_NAME"]
    rb_name = cfg["ROLEBINDING_NAME"]
    manifests_dir = Path(cfg.get("MANIFESTS_DIR", DEFAULTS["MANIFESTS_DIR"]))
    state_dir_name = cfg.get("STATE_DIR", DEFAULTS["STATE_DIR"])
    env_map = {k: v for k, v in cfg.items()}

    # Optional strict runtime check
    if os.environ.get("REQUIRE_ALL_RUNTIME_ENVS", "").lower() in ("1", "true", "yes"):
        required_runtime = ["QDRANT_URL", "DENSE_URL", "SPARSE_URL", "DATA_S3_BUCKET", "AWS_REGION"]
        missing = [k for k in required_runtime if not env_map.get(k) and not os.environ.get(k)]
        if missing:
            print("ERROR: missing required runtime envs:", ", ".join(missing), file=sys.stderr)
            raise SystemExit(2) from None

    manifests: list[tuple[str, dict[str, Any]]] = []
    manifests.append(("00-namespace.yaml", ns_manifest(ns)))
    manifests.append(("10-serviceaccount.yaml",
                      serviceaccount_manifest(ns, sa_name,
                                              annotate_use_irsa=(cfg.get("USE_IRSA", "0") == "1"),
                                              irsa_role_arn=cfg.get("IRSA_ROLE_ARN", ""))))
    manifests.append(("20-role.yaml", role_manifest(ns, role_name)))
    manifests.append(("30-rolebinding.yaml",
                      rolebinding_manifest(ns, rb_name, role_name, sa_name)))

    # Placeholder secrets for Qdrant and AWS (only placeholders written to manifests dir)
    if os.environ.get("QDRANT_API_KEY"):
        qname = cfg.get("QDRANT_SECRET_NAME", NAMED_SECRET_MAP.get("QDRANT_API_KEY", "qdrant-api-key"))
        manifests.append(("41-secret-qdrant-placeholder.yaml",
                          {
                              "apiVersion": "v1",
                              "kind": "Secret",
                              "metadata": {"name": qname, "namespace": ns},
                              "type": "Opaque",
                              "stringData": {"QDRANT_API_KEY": "REPLACE_WITH_REAL_KEY"},
                          }))

    aws_placeholders: dict[str, str] = {}
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        aws_placeholders["AWS_ACCESS_KEY_ID"] = os.environ["AWS_ACCESS_KEY_ID"]
    if os.environ.get("AWS_SECRET_ACCESS_KEY"):
        aws_placeholders["AWS_SECRET_ACCESS_KEY"] = "REPLACE_WITH_REAL_VALUE"
    if os.environ.get("AWS_SESSION_TOKEN"):
        aws_placeholders["AWS_SESSION_TOKEN"] = "REPLACE_WITH_REAL_VALUE"
    if aws_placeholders:
        aname = cfg.get("AWS_SECRET_NAME", NAMED_SECRET_MAP.get("AWS_SECRET_ACCESS_KEY", "indexer-aws-creds"))
        manifests.append(("40-secret-aws-placeholder.yaml",
                          {
                              "apiVersion": "v1",
                              "kind": "Secret",
                              "metadata": {"name": aname, "namespace": ns},
                              "type": "Opaque",
                              "stringData": aws_placeholders,
                          }))

    extras: dict[str, str] = {}
    for k in sorted(SENSITIVE_KEYS):
        if k in ("QDRANT_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
            continue
        if os.environ.get(k):
            extras[k] = "REPLACE_WITH_REAL_VALUE"
    if extras:
        ename = cfg.get("EXTRA_SECRET_NAME", "indexer-extra-secrets")
        manifests.append(("42-secret-extra-placeholder.yaml",
                          {
                              "apiVersion": "v1",
                              "kind": "Secret",
                              "metadata": {"name": ename, "namespace": ns},
                              "type": "Opaque",
                              "stringData": extras,
                          }))

    cron = cronjob_manifest(cfg, {k: v for k, v in cfg.items()})
    manifests.append(("50-cronjob.yaml", cron))

    # Write manifests to disk (manifests_dir)
    recreate_manifests_dir(Path(manifests_dir))
    written_files: list[Path] = []
    for fname, m in manifests:
        p = write_manifest_file(manifests_dir, fname, m)
        written_files.append(p)

    # Render combined YAML for hashing
    rendered = render_all_manifests(manifests)
    digest = sha256_text(rendered)

    # Ensure state dir exists and read previous hash
    state_dir = ensure_state_dir(Path(manifests_dir), state_dir_name)
    prev_hash = read_previous_hash(state_dir)

    if dry_run:
        print("--- DRY RUN: wrote placeholders to", str(manifests_dir))
        for p in written_files:
            print(p)
        print("--- Rendered digest:", digest)
        return

    # If nothing changed, skip apply (idempotent)
    if prev_hash == digest:
        print("No changes detected (rendered manifest hash unchanged). Skipping kubectl apply.")
        return

    # Apply namespace first (idempotent)
    ns_file = manifests_dir / "00-namespace.yaml"
    if ns_file.exists():
        rc, out, err = run_cmd(["kubectl", "apply", "-f", str(ns_file)], timeout=20)
    else:
        ns_yaml = yaml.safe_dump(ns_manifest(ns), sort_keys=False)
        rc, out, err = run_cmd(["kubectl", "apply", "-f", "-"],
                               input_bytes=ns_yaml.encode("utf-8"),
                               timeout=20)
    if rc != 0:
        print("ERROR: applying namespace failed:", err or out, file=sys.stderr)
        raise SystemExit(4) from None

    # Wait for namespace to be ready
    waited = 0
    max_wait = 30
    while True:
        rc2, out2, err2 = run_cmd(["kubectl", "get", "namespace", ns])
        if rc2 == 0:
            break
        time.sleep(1)
        waited += 1
        if waited >= max_wait:
            print(
                f"ERROR: namespace '{ns}' not ready after {max_wait}s. kubectl get ns returned: {err2 or out2}",
                file=sys.stderr,
            )
            raise SystemExit(5) from None

    # Create live secrets only when static creds are present (non-IRSA)
    created_secret_names: list[str] = []
    if os.environ.get("QDRANT_API_KEY"):
        qname = cfg.get("QDRANT_SECRET_NAME", NAMED_SECRET_MAP.get("QDRANT_API_KEY", "qdrant-api-key"))
        ok, err = kubectl_create_secret_inline(qname, ns, {"QDRANT_API_KEY": os.environ["QDRANT_API_KEY"]})
        if not ok:
            print("ERROR creating qdrant secret:", err, file=sys.stderr)
            raise SystemExit(3) from None
        created_secret_names.append(qname)

    # If IRSA is disabled, create AWS secret from env if present
    use_irsa = cfg.get("USE_IRSA", "0") in ("1", "true", "yes")
    aws_literals_live: dict[str, str] = {}
    if not use_irsa:
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            aws_literals_live["AWS_ACCESS_KEY_ID"] = os.environ["AWS_ACCESS_KEY_ID"]
        if os.environ.get("AWS_SECRET_ACCESS_KEY"):
            aws_literals_live["AWS_SECRET_ACCESS_KEY"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        if os.environ.get("AWS_SESSION_TOKEN"):
            aws_literals_live["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
    if aws_literals_live:
        aname = cfg.get("AWS_SECRET_NAME", NAMED_SECRET_MAP.get("AWS_SECRET_ACCESS_KEY", "indexer-aws-creds"))
        ok, err = kubectl_create_secret_inline(aname, ns, aws_literals_live)
        if not ok:
            print("ERROR creating aws secret:", err, file=sys.stderr)
            raise SystemExit(3) from None
        created_secret_names.append(aname)

    # Extras
    extras_live: dict[str, str] = {}
    for k in sorted(SENSITIVE_KEYS):
        if k in ("QDRANT_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
            continue
        if os.environ.get(k):
            extras_live[k] = os.environ[k]
    if extras_live:
        ename = cfg.get("EXTRA_SECRET_NAME", "indexer-extra-secrets")
        ok, err = kubectl_create_secret_inline(ename, ns, extras_live)
        if not ok:
            print("ERROR creating extra secret:", err, file=sys.stderr)
            raise SystemExit(3) from None
        created_secret_names.append(ename)

    # Apply remaining manifests (serviceaccount, role, rolebinding, secrets placeholders, cronjob)
    to_apply_docs = []
    for fname, _ in manifests:
        if fname == "00-namespace.yaml":
            continue
        p = manifests_dir / fname
        if not p.exists():
            continue
        to_apply_docs.append(p.read_text(encoding="utf-8"))
    if to_apply_docs:
        docs_combined = "\n---\n".join(to_apply_docs)
        rc3, out3, err3 = run_cmd(["kubectl", "apply", "-f", "-"],
                                  input_bytes=docs_combined.encode("utf-8"),
                                  timeout=60)
        if rc3 != 0:
            print("ERROR: applying manifests failed:", err3 or out3, file=sys.stderr)
            raise SystemExit(6) from None

    # Persist state (rendered YAML + hash) for future idempotency checks
    write_state_files(state_dir, rendered, digest)

    print("Applied manifests successfully.")
    if created_secret_names:
        print("Created secrets:", ", ".join(created_secret_names))


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str]) -> tuple[bool, bool]:
    # Simple arg parsing: --dry-run and --force
    dry = False
    force = False
    for a in argv:
        if a in ("--dry-run", "-n"):
            dry = True
        if a in ("--force", "-f"):
            force = True
    return dry, force


if __name__ == "__main__":
    try:
        cfg = load_cfg()
        validate_cfg(cfg)
        dry, force = parse_args(sys.argv[1:])
        # If force is provided, bypass idempotency by removing previous hash
        manifests_dir = Path(cfg.get("MANIFESTS_DIR", DEFAULTS["MANIFESTS_DIR"]))
        state_dir = manifests_dir / cfg.get("STATE_DIR", DEFAULTS["STATE_DIR"])
        if force and state_dir.exists():
            try:
                (state_dir / "rendered.sha256").unlink(missing_ok=True)
            except Exception:
                pass
        apply(cfg, dry_run=dry)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise
