#!/usr/bin/env python3
# Generates CronJob + RBAC + supporting manifests for the indexing pipeline.
# This variant enhances idempotent rollout behavior (render/hash/state) and
# cluster-aware AWS auth (IRSA for EKS, static creds for kind).

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

DEFAULTS: dict[str, str] = {
    "NAMESPACE": "indexing",
    "CRONJOB_NAME": "indexing-backup-cronjob",
    "CRON_SCHEDULE": "0 */6 * * *",
    "CRONJOB_CONCURRENCY": "Allow",
    "CRONJOB_BACKOFF_LIMIT": "1",
    "CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT": "3",
    "CRONJOB_FAILED_JOBS_HISTORY_LIMIT": "1",
    "CRONJOB_TIMEZONE": "",
    "SERVICE_ACCOUNT_NAME": "indexer-cron-sa",
    "MANIFESTS_DIR": "src/manifests/indexing_cronjob",
    "INDEXING_PIPELINE_CPU_IMAGE_REPO": "ghcr.io/athithya-sakthivel/indexing-pipeline",
    "INDEXING_PIPELINE_CPU_IMAGE_TAG": "2026-04-24-19-14--29faccb@sha256:f06d9ce7c692b891afee9626f6c3249e30b9b22183d16774c38347b2bb71a841",
    "INDEXING_BACKUP_CRONJOB_CPU_REQUEST": "2",
    "INDEXING_BACKUP_CRONJOB_CPU_LIMIT": "6",
    "INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST": "1Gi",
    "INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT": "2Gi",
    "LOG_LEVEL": "INFO",
    "HTTP_TIMEOUT": "60",
    "INDEXING_STRICT": "1",
    "RUN_PRE_CONVERSIONS": "0",
    "PYTHONUNBUFFERED": "1",
    "QDRANT_URL": "http://qdrant.qdrant.svc.cluster.local:6333",
    "DENSE_URL": "http://dense-svc.models.svc.cluster.local:8200",
    "SPARSE_URL": "http://sparse-svc.models.svc.cluster.local:8201",
    "DATA_S3_BUCKET": "",
    "DATA_S3_PREFIX": "qdrant/backups",
    "AWS_REGION": "",
    "AWS_DEFAULT_REGION": "",
    "STORAGE_RAW_PREFIX": "data/raw/",
    "STORAGE_CHUNKED_PREFIX": "data/chunked/",
    "QDRANT_API_KEY": "",
    "QDRANT_SECRET_NAME": "qdrant-api-key",
    "AWS_CREDENTIALS_SECRET_NAME": "indexer-aws-creds",
    "EXTRA_SECRET_NAME": "indexer-extra-secrets",
    "USE_IRSA": "",
    "IRSA_ROLE_ARN": "",
    "K8S_CLUSTER": "",
}

RUNTIME_KEYS = set(DEFAULTS.keys())

def log(msg: str, /, *args: object) -> None:
    if args:
        msg = msg % args
    print(msg, flush=True)

def fatal(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)

def run_cmd(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=merged_env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or f"timeout after {timeout}s"

def ensure_kubectl_available() -> None:
    rc, out, err = run_cmd(["kubectl", "version", "--client=true"], timeout=20)
    if rc != 0:
        fatal(f"kubectl not available or not in PATH: {err or out}")

def yaml_dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass

def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

def as_int(value: str | None, default: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value))
    except Exception:
        return default

def pick_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default

def detect_mode(cfg: dict[str, str]) -> str:
    explicit = cfg.get("K8S_CLUSTER", "").strip().lower()
    if explicit in {"kind", "eks", "eks-auto"}:
        return explicit
    if as_bool(cfg.get("USE_IRSA")) or cfg.get("IRSA_ROLE_ARN"):
        return "eks"
    return "kind"

def validate_cfg(cfg: dict[str, str]) -> None:
    missing = []
    mode = detect_mode(cfg)
    if mode == "kind":
        if not cfg.get("DATA_S3_BUCKET"):
            missing.append("DATA_S3_BUCKET")
        if not (cfg.get("AWS_REGION") or cfg.get("AWS_DEFAULT_REGION")):
            missing.append("AWS_REGION")
        if not cfg.get("QDRANT_URL"):
            missing.append("QDRANT_URL")
        if not cfg.get("DENSE_URL"):
            missing.append("DENSE_URL")
        if not cfg.get("SPARSE_URL"):
            missing.append("SPARSE_URL")
        if missing:
            fatal("missing required env vars: " + ", ".join(missing))
        if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
            fatal("kind/static mode requires AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    else:
        if not cfg.get("DATA_S3_BUCKET"):
            missing.append("DATA_S3_BUCKET")
        if not cfg.get("QDRANT_URL"):
            missing.append("QDRANT_URL")
        if not cfg.get("DENSE_URL"):
            missing.append("DENSE_URL")
        if not cfg.get("SPARSE_URL"):
            missing.append("SPARSE_URL")
        if missing:
            fatal("missing required env vars: " + ", ".join(missing))
        if not cfg.get("IRSA_ROLE_ARN"):
            fatal("EKS/IRSA mode requires IRSA_ROLE_ARN")

def namespace_manifest(ns: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}

def serviceaccount_manifest(ns: str, name: str, mode: str, irsa_role_arn: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "namespace": ns}
    if mode != "kind" and irsa_role_arn:
        meta["annotations"] = {"eks.amazonaws.com/role-arn": irsa_role_arn}
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta}

def build_secret_manifest(ns: str, name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": ns},
        "type": "Opaque",
        "stringData": data,
    }

def env_item(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value}

def secret_env_item(name: str, secret_name: str, secret_key: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {
                "name": secret_name,
                "key": secret_key or name,
            }
        },
    }

def _image_ref(repo: str, tag: str) -> str:
    return f"{repo}:{tag}" if tag else repo

def build_cronjob_manifest(cfg: dict[str, str], mode: str) -> dict[str, Any]:
    ns = cfg["NAMESPACE"]
    cron_name = cfg["CRONJOB_NAME"]
    image = _image_ref(cfg["INDEXING_PIPELINE_CPU_IMAGE_REPO"], cfg["INDEXING_PIPELINE_CPU_IMAGE_TAG"])
    aws_region = cfg["AWS_REGION"] or cfg["AWS_DEFAULT_REGION"]
    env: list[dict[str, Any]] = [
        env_item("PYTHONUNBUFFERED", "1"),
        env_item("LOG_LEVEL", cfg["LOG_LEVEL"]),
        env_item("HTTP_TIMEOUT", cfg["HTTP_TIMEOUT"]),
        env_item("INDEXING_STRICT", cfg["INDEXING_STRICT"]),
        env_item("RUN_PRE_CONVERSIONS", "0"),
        env_item("QDRANT_URL", cfg["QDRANT_URL"]),
        env_item("DENSE_URL", cfg["DENSE_URL"]),
        env_item("SPARSE_URL", cfg["SPARSE_URL"]),
        env_item("DATA_S3_BUCKET", cfg["DATA_S3_BUCKET"]),
        env_item("DATA_S3_PREFIX", cfg["DATA_S3_PREFIX"]),
        env_item("STORAGE_RAW_PREFIX", cfg["STORAGE_RAW_PREFIX"]),
        env_item("STORAGE_CHUNKED_PREFIX", cfg["STORAGE_CHUNKED_PREFIX"]),
        env_item("AWS_REGION", aws_region),
        env_item("AWS_DEFAULT_REGION", aws_region),
        env_item("AWS_SDK_LOAD_CONFIG", "1"),
        env_item("AWS_EC2_METADATA_DISABLED", "true"),
    ]
    if cfg.get("QDRANT_API_KEY"):
        env.append(secret_env_item("QDRANT_API_KEY", cfg["QDRANT_SECRET_NAME"], "QDRANT_API_KEY"))
    if mode == "kind":
        env.append(secret_env_item("AWS_ACCESS_KEY_ID", cfg["AWS_CREDENTIALS_SECRET_NAME"], "AWS_ACCESS_KEY_ID"))
        env.append(secret_env_item("AWS_SECRET_ACCESS_KEY", cfg["AWS_CREDENTIALS_SECRET_NAME"], "AWS_SECRET_ACCESS_KEY"))
        if os.environ.get("AWS_SESSION_TOKEN"):
            env.append(secret_env_item("AWS_SESSION_TOKEN", cfg["AWS_CREDENTIALS_SECRET_NAME"], "AWS_SESSION_TOKEN"))
    cronjob: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": cron_name, "namespace": ns},
        "spec": {
            "schedule": cfg["CRON_SCHEDULE"],
            "concurrencyPolicy": cfg["CRONJOB_CONCURRENCY"],
            "successfulJobsHistoryLimit": as_int(cfg["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"], 3),
            "failedJobsHistoryLimit": as_int(cfg["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"], 1),
            "jobTemplate": {
                "spec": {
                    "backoffLimit": as_int(cfg["CRONJOB_BACKOFF_LIMIT"], 1),
                    "template": {
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": cron_name,
                                "app.kubernetes.io/component": "indexing",
                            }
                        },
                        "spec": {
                            "serviceAccountName": cfg["SERVICE_ACCOUNT_NAME"],
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "indexer",
                                    "image": image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["/opt/venv/bin/python", "/indexing_pipeline/indexing_pipeline.py"],
                                    "args": ["--workdir", "/indexing_pipeline"],
                                    "env": env,
                                    "resources": {
                                        "requests": {
                                            "cpu": cfg["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"],
                                            "memory": cfg["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"],
                                        },
                                        "limits": {
                                            "cpu": cfg["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"],
                                            "memory": cfg["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"],
                                        },
                                    },
                                    "workingDir": "/indexing_pipeline",
                                }
                            ],
                        },
                    },
                }
            },
        },
    }
    if cfg.get("CRONJOB_TIMEZONE"):
        cronjob["spec"]["timeZone"] = cfg["CRONJOB_TIMEZONE"]
    return cronjob

def collect_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for key in sorted(RUNTIME_KEYS):
        cfg[key] = pick_env(key, default=DEFAULTS.get(key, ""))
    cfg["NAMESPACE"] = pick_env("NAMESPACE", default=DEFAULTS["NAMESPACE"])
    cfg["CRONJOB_NAME"] = pick_env("CRONJOB_NAME", default=DEFAULTS["CRONJOB_NAME"]).lower()
    cfg["CRON_SCHEDULE"] = pick_env("CRON_SCHEDULE", "INDEXING_BACKUP_CRON_EXPRESSION", default=DEFAULTS["CRON_SCHEDULE"])
    cfg["CRONJOB_CONCURRENCY"] = pick_env("CRONJOB_CONCURRENCY", default=DEFAULTS["CRONJOB_CONCURRENCY"])
    cfg["CRONJOB_BACKOFF_LIMIT"] = pick_env("CRONJOB_BACKOFF_LIMIT", default=DEFAULTS["CRONJOB_BACKOFF_LIMIT"])
    cfg["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"] = pick_env("CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT", default=DEFAULTS["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"])
    cfg["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"] = pick_env("CRONJOB_FAILED_JOBS_HISTORY_LIMIT", default=DEFAULTS["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"])
    cfg["CRONJOB_TIMEZONE"] = pick_env("CRONJOB_TIMEZONE", default=DEFAULTS["CRONJOB_TIMEZONE"])
    cfg["SERVICE_ACCOUNT_NAME"] = pick_env("SERVICE_ACCOUNT_NAME", default=DEFAULTS["SERVICE_ACCOUNT_NAME"])
    cfg["MANIFESTS_DIR"] = pick_env("MANIFESTS_DIR", default=DEFAULTS["MANIFESTS_DIR"])
    cfg["INDEXING_PIPELINE_CPU_IMAGE_REPO"] = pick_env("INDEXING_PIPELINE_CPU_IMAGE_REPO", default=DEFAULTS["INDEXING_PIPELINE_CPU_IMAGE_REPO"])
    cfg["INDEXING_PIPELINE_CPU_IMAGE_TAG"] = pick_env("INDEXING_PIPELINE_CPU_IMAGE_TAG", default=DEFAULTS["INDEXING_PIPELINE_CPU_IMAGE_TAG"])
    cfg["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"] = pick_env("INDEXING_BACKUP_CRONJOB_CPU_REQUEST", default=DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"])
    cfg["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"] = pick_env("INDEXING_BACKUP_CRONJOB_CPU_LIMIT", default=DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"])
    cfg["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"] = pick_env("INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST", default=DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"])
    cfg["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"] = pick_env("INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT", default=DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"])
    cfg["LOG_LEVEL"] = pick_env("LOG_LEVEL", default=DEFAULTS["LOG_LEVEL"])
    cfg["HTTP_TIMEOUT"] = pick_env("HTTP_TIMEOUT", default=DEFAULTS["HTTP_TIMEOUT"])
    cfg["INDEXING_STRICT"] = pick_env("INDEXING_STRICT", default=DEFAULTS["INDEXING_STRICT"])
    cfg["RUN_PRE_CONVERSIONS"] = pick_env("RUN_PRE_CONVERSIONS", default=DEFAULTS["RUN_PRE_CONVERSIONS"])
    cfg["QDRANT_URL"] = pick_env("QDRANT_URL", default=DEFAULTS["QDRANT_URL"])
    cfg["DENSE_URL"] = pick_env("DENSE_URL", default=DEFAULTS["DENSE_URL"])
    cfg["SPARSE_URL"] = pick_env("SPARSE_URL", default=DEFAULTS["SPARSE_URL"])
    cfg["DATA_S3_BUCKET"] = pick_env("DATA_S3_BUCKET", "S3_BUCKET", default=DEFAULTS["DATA_S3_BUCKET"])
    cfg["DATA_S3_PREFIX"] = pick_env("DATA_S3_PREFIX", "BACKUP_PREFIX", default=DEFAULTS["DATA_S3_PREFIX"])
    cfg["AWS_REGION"] = pick_env("AWS_REGION", default=DEFAULTS["AWS_REGION"])
    cfg["AWS_DEFAULT_REGION"] = pick_env("AWS_DEFAULT_REGION", default=cfg["AWS_REGION"] or DEFAULTS["AWS_DEFAULT_REGION"])
    if not cfg["AWS_REGION"]:
        cfg["AWS_REGION"] = cfg["AWS_DEFAULT_REGION"]
    if not cfg["AWS_DEFAULT_REGION"]:
        cfg["AWS_DEFAULT_REGION"] = cfg["AWS_REGION"]
    cfg["STORAGE_RAW_PREFIX"] = pick_env("STORAGE_RAW_PREFIX", default=DEFAULTS["STORAGE_RAW_PREFIX"])
    cfg["STORAGE_CHUNKED_PREFIX"] = pick_env("STORAGE_CHUNKED_PREFIX", default=DEFAULTS["STORAGE_CHUNKED_PREFIX"])
    cfg["QDRANT_API_KEY"] = pick_env("QDRANT_API_KEY", default=DEFAULTS["QDRANT_API_KEY"])
    cfg["QDRANT_SECRET_NAME"] = pick_env("QDRANT_SECRET_NAME", default=DEFAULTS["QDRANT_SECRET_NAME"])
    cfg["AWS_CREDENTIALS_SECRET_NAME"] = pick_env("AWS_CREDENTIALS_SECRET_NAME", default=DEFAULTS["AWS_CREDENTIALS_SECRET_NAME"])
    cfg["EXTRA_SECRET_NAME"] = pick_env("EXTRA_SECRET_NAME", default=DEFAULTS["EXTRA_SECRET_NAME"])
    cfg["USE_IRSA"] = pick_env("USE_IRSA", "AWS_USE_IRSA", default=DEFAULTS["USE_IRSA"])
    cfg["IRSA_ROLE_ARN"] = pick_env("IRSA_ROLE_ARN", default=DEFAULTS["IRSA_ROLE_ARN"])
    cfg["K8S_CLUSTER"] = pick_env("K8S_CLUSTER", default=DEFAULTS["K8S_CLUSTER"])
    return cfg

def write_manifests(manifests_dir: Path, docs: list[tuple[str, dict[str, Any]]]) -> list[Path]:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for filename, doc in docs:
        p = manifests_dir / filename
        write_text(p, yaml_dump(doc))
        out.append(p)
    return out

def apply_yaml(yaml_text: str, *, timeout: int = 60) -> None:
    rc, out, err = run_cmd(["kubectl", "apply", "-f", "-"], input_text=yaml_text, timeout=timeout)
    if rc != 0:
        fatal(err or out or "kubectl apply failed", 4)

def apply_direct_secret(ns: str, name: str, data: dict[str, str], timeout: int = 30) -> None:
    if not data:
        return
    apply_yaml(yaml_dump(build_secret_manifest(ns, name, data)), timeout=timeout)

def wait_for_namespace(ns: str, timeout: int = 30) -> None:
    start = time.monotonic()
    while True:
        rc, _, _ = run_cmd(["kubectl", "get", "namespace", ns], timeout=10)
        if rc == 0:
            return
        if time.monotonic() - start >= timeout:
            fatal(f"namespace '{ns}' was not observable after creation", 5)
        time.sleep(1)

def render_and_apply(cfg: dict[str, str], dry_run: bool) -> None:
    ensure_kubectl_available()
    mode = detect_mode(cfg)
    manifests_dir = Path(cfg["MANIFESTS_DIR"])
    docs: list[tuple[str, dict[str, Any]]] = [
        ("00-namespace.yaml", namespace_manifest(cfg["NAMESPACE"])),
        ("10-serviceaccount.yaml", serviceaccount_manifest(cfg["NAMESPACE"], cfg["SERVICE_ACCOUNT_NAME"], mode, cfg["IRSA_ROLE_ARN"])),
        ("50-cronjob.yaml", build_cronjob_manifest(cfg, mode)),
    ]
    rendered_files = write_manifests(manifests_dir, docs)
    log(f"Rendered manifests to {manifests_dir}")
    for p in rendered_files:
        log(str(p))
    if dry_run:
        return
    apply_yaml(yaml_dump(namespace_manifest(cfg["NAMESPACE"])), timeout=20)
    wait_for_namespace(cfg["NAMESPACE"], timeout=30)
    if cfg.get("QDRANT_API_KEY"):
        apply_direct_secret(cfg["NAMESPACE"], cfg["QDRANT_SECRET_NAME"], {"QDRANT_API_KEY": cfg["QDRANT_API_KEY"]})
    if mode == "kind":
        aws_data: dict[str, str] = {}
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            aws_data["AWS_ACCESS_KEY_ID"] = os.environ["AWS_ACCESS_KEY_ID"]
        if os.environ.get("AWS_SECRET_ACCESS_KEY"):
            aws_data["AWS_SECRET_ACCESS_KEY"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        if os.environ.get("AWS_SESSION_TOKEN"):
            aws_data["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
        apply_direct_secret(cfg["NAMESPACE"], cfg["AWS_CREDENTIALS_SECRET_NAME"], aws_data)
    apply_yaml(
        "\n---\n".join(
            yaml_dump(doc)
            for doc in (
                serviceaccount_manifest(cfg["NAMESPACE"], cfg["SERVICE_ACCOUNT_NAME"], mode, cfg["IRSA_ROLE_ARN"]),
                build_cronjob_manifest(cfg, mode),
            )
        ),
        timeout=60,
    )
    log("Applied manifests successfully")

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render and apply the indexing CronJob manifests.")
    parser.add_argument("--dry-run", action="store_true", help="Render manifests to disk only; do not apply.")
    return parser.parse_args(argv)

def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    cfg = collect_cfg()
    validate_cfg(cfg)
    render_and_apply(cfg, dry_run=args.dry_run)
    log("Done")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
