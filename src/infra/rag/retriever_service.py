from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

# From original file:
# LOG_LEVEL = os.environ.get("RETRIEVER_GEN_LOGLEVEL", "INFO").upper()
# SECRET_KEYS = ("QDRANT_API_KEY", "ZITADEL_CLIENT_ID", "ZITADEL_CLIENT_SECRET", "SESSION_SECRET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",)
#
# The rewritten file below avoids writing AWS_* secrets into YAML files while still allowing them
# to be applied into the cluster and made available to pods when USE_IAM=false.

LOG_LEVEL = os.environ.get("RETRIEVER_GEN_LOGLEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gen_retriever")

MANIFESTS_DIR = Path("src/manifests/retriever")
STATE_DIRNAME = ".state"

DEFAULTS: dict[str, Any] = {
    "NAMESPACE": "inference",
    "DEPLOYMENT_NAME": "retriever",
    "SERVICE_NAME": "retriever",
    "SERVICE_ACCOUNT_NAME": "retriever-sa",
    "SECRET_NAME": "retriever-secrets",
    "IMAGE": "ghcr.io/athithya-sakthivel/retriever:2026-05-03-13-29--d13b057@sha256:2b4bb7f34c57db45e4241576951d43195f286bcdddf4f321fcd28b629ba72a3f",
    "IMAGE_PULL_POLICY": "IfNotPresent",
    "REPLICAS": 1,
    "CONTAINER_PORT": 8001,
    "CPU_REQUEST": "250m",
    "CPU_LIMIT": "1",
    "MEMORY_REQUEST": "512Mi",
    "MEMORY_LIMIT": "1Gi",
    "RUN_AS_USER": 1000,
    "RUN_AS_GROUP": 1000,
    "FS_GROUP": 1000,
    "SERVICE_TYPE": "ClusterIP",
    "USE_IAM": False,
    "IRSA_ROLE_ARN": "",
    "READINESS_INITIAL_DELAY": 10,
    "LIVENESS_INITIAL_DELAY": 30,
    "PROBE_PERIOD_SECONDS": 5,
    "PROBE_TIMEOUT_SECONDS": 5,
    "STARTUP_FAILURE_THRESHOLD": 60,
    "ROLLOUT_TIMEOUT": 300,
    "RUN_AS_NONROOT": True,
    "ALLOW_PRIV_ESC": False,
    "READONLY_ROOTFS": True,
}

# Keys that are considered sensitive AWS credentials
AWS_SECRET_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

# All secret keys we consider for collection (keeps parity with original)
SECRET_KEYS = (
    "QDRANT_API_KEY",
    "ZITADEL_CLIENT_ID",
    "ZITADEL_CLIENT_SECRET",
    "SESSION_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip()
    return text if text else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def canonical_inputs_hash(payload: dict[str, Any]) -> str:
    serial: dict[str, Any] = {}
    for k in sorted(payload.keys()):
        if k in ("INPUTS_HASH_PATH", "MANIFESTS_DIR", "STATE_DIRNAME", "FILES"):
            continue
        v = payload.get(k)
        try:
            json.dumps(v)
            serial[k] = v
        except Exception:
            serial[k] = str(v)
    j = json.dumps(serial, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(j.encode("utf-8")).hexdigest()


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    cfg["MANIFESTS_DIR"] = Path(os.getenv("MANIFESTS_DIR", str(MANIFESTS_DIR)))
    cfg["STATE_DIRNAME"] = os.getenv("STATE_DIRNAME", STATE_DIRNAME)
    cfg["INPUTS_HASH_PATH"] = cfg["MANIFESTS_DIR"] / cfg["STATE_DIRNAME"] / "inputs.sha256"

    cfg["NAMESPACE"] = _env_str("NAMESPACE", DEFAULTS["NAMESPACE"])
    cfg["DEPLOYMENT_NAME"] = _env_str("DEPLOYMENT_NAME", DEFAULTS["DEPLOYMENT_NAME"])
    cfg["SERVICE_NAME"] = _env_str("SERVICE_NAME", DEFAULTS["SERVICE_NAME"])
    cfg["SERVICE_ACCOUNT_NAME"] = _env_str("SERVICE_ACCOUNT_NAME", DEFAULTS["SERVICE_ACCOUNT_NAME"])
    cfg["SECRET_NAME"] = _env_str("SECRET_NAME", DEFAULTS["SECRET_NAME"])
    cfg["IMAGE"] = _env_str("IMAGE", DEFAULTS["IMAGE"])
    cfg["IMAGE_PULL_POLICY"] = _env_str("IMAGE_PULL_POLICY", DEFAULTS["IMAGE_PULL_POLICY"])
    cfg["REPLICAS"] = _env_int("REPLICAS", DEFAULTS["REPLICAS"])
    cfg["CONTAINER_PORT"] = _env_int("CONTAINER_PORT", DEFAULTS["CONTAINER_PORT"])
    cfg["CPU_REQUEST"] = _env_str("CPU_REQUEST", DEFAULTS["CPU_REQUEST"])
    cfg["CPU_LIMIT"] = _env_str("CPU_LIMIT", DEFAULTS["CPU_LIMIT"])
    cfg["MEMORY_REQUEST"] = _env_str("MEMORY_REQUEST", DEFAULTS["MEMORY_REQUEST"])
    cfg["MEMORY_LIMIT"] = _env_str("MEMORY_LIMIT", DEFAULTS["MEMORY_LIMIT"])
    cfg["RUN_AS_USER"] = _env_int("RUN_AS_USER", DEFAULTS["RUN_AS_USER"])
    cfg["RUN_AS_GROUP"] = _env_int("RUN_AS_GROUP", DEFAULTS["RUN_AS_GROUP"])
    cfg["FS_GROUP"] = _env_int("FS_GROUP", DEFAULTS["FS_GROUP"])
    cfg["SERVICE_TYPE"] = _env_str("SERVICE_TYPE", DEFAULTS["SERVICE_TYPE"])
    cfg["USE_IAM"] = _env_bool("USE_IAM", DEFAULTS["USE_IAM"])
    cfg["IRSA_ROLE_ARN"] = _env_str("IRSA_ROLE_ARN", DEFAULTS["IRSA_ROLE_ARN"])

    cfg["READINESS_INITIAL_DELAY"] = _env_int("READINESS_INITIAL_DELAY", DEFAULTS["READINESS_INITIAL_DELAY"])
    cfg["LIVENESS_INITIAL_DELAY"] = _env_int("LIVENESS_INITIAL_DELAY", DEFAULTS["LIVENESS_INITIAL_DELAY"])
    cfg["PROBE_PERIOD_SECONDS"] = _env_int("PROBE_PERIOD_SECONDS", DEFAULTS["PROBE_PERIOD_SECONDS"])
    cfg["PROBE_TIMEOUT_SECONDS"] = _env_int("PROBE_TIMEOUT_SECONDS", DEFAULTS["PROBE_TIMEOUT_SECONDS"])
    cfg["STARTUP_FAILURE_THRESHOLD"] = _env_int("STARTUP_FAILURE_THRESHOLD", DEFAULTS["STARTUP_FAILURE_THRESHOLD"])
    cfg["ROLLOUT_TIMEOUT"] = _env_int("ROLLOUT_TIMEOUT", DEFAULTS["ROLLOUT_TIMEOUT"])

    cfg["RUN_AS_NONROOT"] = _env_bool("RUN_AS_NONROOT", DEFAULTS["RUN_AS_NONROOT"])
    cfg["ALLOW_PRIV_ESC"] = _env_bool("ALLOW_PRIV_ESC", DEFAULTS["ALLOW_PRIV_ESC"])
    cfg["READONLY_ROOTFS"] = _env_bool("READONLY_ROOTFS", DEFAULTS["READONLY_ROOTFS"])

    cfg["FILES"] = {
        "serviceaccount": cfg["MANIFESTS_DIR"] / "01-serviceaccount.yaml",
        "secret": cfg["MANIFESTS_DIR"] / "02-secret.yaml",
        "deployment": cfg["MANIFESTS_DIR"] / "03-deployment.yaml",
        "service": cfg["MANIFESTS_DIR"] / "04-service.yaml",
        "pdb": cfg["MANIFESTS_DIR"] / "05-pdb.yaml",
    }

    cfg["UUID_SHORT"] = str(uuid.uuid4())[:8]
    log.info(
        "Loaded config: namespace=%s deployment=%s replicas=%d image=%s",
        cfg["NAMESPACE"],
        cfg["DEPLOYMENT_NAME"],
        cfg["REPLICAS"],
        cfg["IMAGE"],
    )
    return cfg


def _base_labels(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": cfg["SERVICE_NAME"],
        "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"],
        "app.kubernetes.io/managed-by": "retriever-manifest-generator",
        "app.kubernetes.io/component": "retriever",
    }


def _pod_labels(cfg: dict[str, Any]) -> dict[str, str]:
    labels = _base_labels(cfg).copy()
    labels["app.kubernetes.io/part-of"] = cfg["SERVICE_NAME"]
    return labels


def _probe_http(path: str, port: int, initial_delay: int, period: int, timeout: int, failure_threshold: int) -> dict[str, Any]:
    return {
        "httpGet": {"path": path, "port": port, "scheme": "HTTP"},
        "initialDelaySeconds": initial_delay,
        "timeoutSeconds": timeout,
        "periodSeconds": period,
        "failureThreshold": failure_threshold,
        "successThreshold": 1,
    }


def collect_secret_env() -> dict[str, str]:
    """
    Collect secret values from environment. This returns all non-empty values for keys in SECRET_KEYS.
    Note: AWS keys are included here when present; later logic decides whether to write them to YAML
    or apply them directly to the cluster (to avoid hardcoding them into manifest files).
    """
    secret_env: dict[str, str] = {}
    for key in SECRET_KEYS:
        value = os.getenv(key, "")
        if value is None:
            continue
        v = value.strip()
        if not v:
            continue
        # If USE_IAM is true, we should not include AWS keys at all (they are ignored).
        # The caller will check cfg["USE_IAM"] and act accordingly.
        secret_env[key] = v
    return secret_env


def build_service_account_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": cfg["SERVICE_ACCOUNT_NAME"],
        "namespace": cfg["NAMESPACE"],
        "labels": _base_labels(cfg),
    }
    if cfg["USE_IAM"] and cfg["IRSA_ROLE_ARN"]:
        metadata.setdefault("annotations", {})["eks.amazonaws.com/role-arn"] = cfg["IRSA_ROLE_ARN"]
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata}


def build_secret_doc(cfg: dict[str, Any], secret_env_for_yaml: dict[str, str]) -> dict[str, Any] | None:
    """
    Build a Secret manifest only for the keys that are safe to write into YAML.
    AWS_* keys will be excluded from secret_env_for_yaml when we want to avoid writing them.
    """
    if not secret_env_for_yaml:
        return None
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": cfg["SECRET_NAME"],
            "namespace": cfg["NAMESPACE"],
            "labels": _base_labels(cfg),
        },
        "type": "Opaque",
        "stringData": secret_env_for_yaml,
    }


def build_deployment_doc(cfg: dict[str, Any], has_secret: bool) -> dict[str, Any]:
    env_from: list[dict[str, Any]] = []
    if has_secret:
        env_from.append({"secretRef": {"name": cfg["SECRET_NAME"]}})

    container = {
        "name": cfg["DEPLOYMENT_NAME"],
        "image": cfg["IMAGE"],
        "imagePullPolicy": cfg["IMAGE_PULL_POLICY"],
        "ports": [{"name": "http", "containerPort": cfg["CONTAINER_PORT"], "protocol": "TCP"}],
        "env": [
            {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        ],
        "envFrom": env_from,
        "volumeMounts": [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "tmp", "mountPath": "/var/tmp"},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": bool(cfg["ALLOW_PRIV_ESC"]),
            "readOnlyRootFilesystem": bool(cfg["READONLY_ROOTFS"]),
            "runAsNonRoot": bool(cfg["RUN_AS_NONROOT"]),
            "runAsUser": int(cfg["RUN_AS_USER"]),
            "runAsGroup": int(cfg["RUN_AS_GROUP"]),
        },
        "resources": {
            "requests": {"cpu": cfg["CPU_REQUEST"], "memory": cfg["MEMORY_REQUEST"]},
            "limits": {"cpu": cfg["CPU_LIMIT"], "memory": cfg["MEMORY_LIMIT"]},
        },
        "readinessProbe": _probe_http(
            "/readyz",
            cfg["CONTAINER_PORT"],
            cfg["READINESS_INITIAL_DELAY"],
            cfg["PROBE_PERIOD_SECONDS"],
            cfg["PROBE_TIMEOUT_SECONDS"],
            3,
        ),
        "livenessProbe": _probe_http(
            "/healthz",
            cfg["CONTAINER_PORT"],
            cfg["LIVENESS_INITIAL_DELAY"],
            cfg["PROBE_PERIOD_SECONDS"],
            cfg["PROBE_TIMEOUT_SECONDS"],
            6,
        ),
        "startupProbe": _probe_http(
            "/healthz",
            cfg["CONTAINER_PORT"],
            cfg["LIVENESS_INITIAL_DELAY"],
            cfg["PROBE_PERIOD_SECONDS"],
            cfg["PROBE_TIMEOUT_SECONDS"],
            cfg["STARTUP_FAILURE_THRESHOLD"],
        ),
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": cfg["DEPLOYMENT_NAME"],
            "namespace": cfg["NAMESPACE"],
            "labels": _base_labels(cfg),
        },
        "spec": {
            "replicas": cfg["REPLICAS"],
            "revisionHistoryLimit": 3,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": cfg["SERVICE_NAME"],
                    "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"],
                    "app.kubernetes.io/component": "retriever",
                }
            },
            "template": {
                "metadata": {"labels": _pod_labels(cfg)},
                "spec": {
                    "serviceAccountName": cfg["SERVICE_ACCOUNT_NAME"],
                    "automountServiceAccountToken": True,
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {
                        "fsGroup": cfg["FS_GROUP"],
                    },
                    "volumes": [{"name": "tmp", "emptyDir": {}}],
                    "containers": [container],
                },
            },
        },
    }


def build_service_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": cfg["SERVICE_NAME"],
            "namespace": cfg["NAMESPACE"],
            "labels": _base_labels(cfg),
        },
        "spec": {
            "type": cfg["SERVICE_TYPE"],
            "selector": {
                "app.kubernetes.io/name": cfg["SERVICE_NAME"],
                "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"],
                "app.kubernetes.io/component": "retriever",
            },
            "ports": [
                {
                    "name": "http",
                    "port": cfg["CONTAINER_PORT"],
                    "targetPort": cfg["CONTAINER_PORT"],
                    "protocol": "TCP",
                }
            ],
        },
    }


def build_pdb_doc(cfg: dict[str, Any]) -> dict[str, Any] | None:
    if cfg["REPLICAS"] < 2:
        return None
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {
            "name": f"{cfg['DEPLOYMENT_NAME']}-pdb",
            "namespace": cfg["NAMESPACE"],
            "labels": _base_labels(cfg),
        },
        "spec": {
            "minAvailable": 1,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": cfg["SERVICE_NAME"],
                    "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"],
                    "app.kubernetes.io/component": "retriever",
                }
            },
        },
    }


def sha256_secret_keys(secret_env: dict[str, str]) -> str:
    payload = json.dumps(sorted(secret_env.keys()), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_config_hash(docs: list[dict[str, Any]], secret_keys_hash: str) -> str:
    payload = {"docs": docs, "secret_keys_hash": secret_keys_hash}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_yaml_atomic(path: Path, doc: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    content = yaml.safe_dump(doc, sort_keys=False)
    atomic_write(path, content)
    log.debug("Wrote manifest %s", str(path))


def apply_yaml(path: Path) -> None:
    log.info("Applying %s", str(path))
    subprocess.run(["kubectl", "apply", "-f", str(path)], check=True, capture_output=True)


def delete_yaml(path: Path) -> None:
    log.info("Deleting %s", str(path))
    subprocess.run(["kubectl", "delete", "-f", str(path), "--ignore-not-found"], check=True, capture_output=True)


def create_namespace_if_missing(namespace: str) -> None:
    rc = subprocess.run(["kubectl", "get", "ns", namespace], capture_output=True)
    if rc.returncode != 0:
        log.info("Namespace %s not found; creating", namespace)
        subprocess.run(["kubectl", "create", "ns", namespace], check=True, capture_output=True)


def apply_secret_direct(cfg: dict[str, Any], secret_env: dict[str, str]) -> None:
    """
    Apply secrets directly into the cluster using kubectl create secret generic --from-literal ...
    This avoids writing sensitive values into YAML files on disk.
    """
    if not secret_env:
        log.info("No secret values provided for direct apply; skipping secret apply.")
        return
    cmd = ["kubectl", "create", "secret", "generic", cfg["SECRET_NAME"], "-n", cfg["NAMESPACE"]]
    for key, value in sorted(secret_env.items()):
        cmd.extend(["--from-literal", f"{key}={value}"])
    cmd.extend(["--dry-run=client", "-o", "yaml"])
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=proc.stdout, text=True, check=True, capture_output=True)
    log.info("Applied secrets directly to cluster (not written to YAML).")


def delete_secret_direct(cfg: dict[str, Any]) -> None:
    subprocess.run(
        ["kubectl", "delete", "secret", cfg["SECRET_NAME"], "-n", cfg["NAMESPACE"], "--ignore-not-found"],
        check=False,
        capture_output=True,
    )
    log.info("Deleted secret if it existed")


def generate_manifests(cfg: dict[str, Any], secret_env: dict[str, str], dry_run: bool = False, verbose: bool = False) -> str | None:
    """
    Generate manifests on disk. To avoid hardcoding AWS credentials into YAML files,
    this function splits secret_env into two buckets:
      - secret_env_for_yaml: keys that will be written into 02-secret.yaml
      - secret_env_for_direct_apply: keys that will be applied directly to the cluster (not written)
    Behavior:
      - If USE_IAM is True, AWS keys are ignored entirely (they won't be written nor applied).
      - If USE_IAM is False, AWS keys (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) will be applied directly
        to the cluster and will NOT be written into 02-secret.yaml.
      - The Deployment will reference the secret name via envFrom secretRef if any secrets are present,
        so pods will receive the values from the in-cluster Secret regardless of whether it was written to disk.
    """
    manifests_dir: Path = cfg["MANIFESTS_DIR"]
    ensure_dir(manifests_dir)

    # Decide which secrets to write to YAML and which to apply directly
    secret_env_for_yaml: dict[str, str] = {}
    secret_env_for_direct_apply: dict[str, str] = {}

    # If USE_IAM is true, we should not include AWS keys at all.
    use_iam = bool(cfg.get("USE_IAM", False))

    for k, v in secret_env.items():
        if k in AWS_SECRET_KEYS:
            if use_iam:
                # When using IAM, do not include AWS keys anywhere.
                log.debug("USE_IAM=true; ignoring AWS key %s", k)
                continue
            # USE_IAM is False: apply AWS keys directly to cluster, but do not write them to YAML.
            secret_env_for_direct_apply[k] = v
        else:
            # Non-AWS keys: safe to write to YAML (these are still sensitive; writing them is optional)
            # We will write non-AWS keys to YAML so they can be managed as manifests, but AWS keys are kept out.
            secret_env_for_yaml[k] = v

    # Determine whether the deployment should reference the secret
    has_secret = bool(secret_env_for_yaml) or bool(secret_env_for_direct_apply)

    secret_keys_hash = sha256_secret_keys({**secret_env_for_yaml, **secret_env_for_direct_apply})
    docs_for_hash = [
        {"serviceaccount": cfg["SERVICE_ACCOUNT_NAME"], "namespace": cfg["NAMESPACE"]},
        {"deployment": cfg["DEPLOYMENT_NAME"], "image": cfg["IMAGE"], "replicas": cfg["REPLICAS"], "secret_keys_hash": secret_keys_hash},
        {"service": cfg["SERVICE_NAME"], "port": cfg["CONTAINER_PORT"]},
    ]
    inputs_hash = canonical_inputs_hash({"docs": docs_for_hash, "secret_keys_hash": secret_keys_hash, "cfg": cfg})
    state_dir = manifests_dir / cfg["STATE_DIRNAME"]
    ensure_dir(state_dir)

    inputs_path = state_dir / "inputs.sha256"
    existing = ""
    if inputs_path.exists():
        try:
            existing = inputs_path.read_text(encoding="utf-8").strip()
        except Exception:
            existing = ""

    if existing == inputs_hash and not dry_run:
        log.info("No changes detected; skipping manifest generation.")
        return None

    sa_doc = build_service_account_doc(cfg)
    sec_doc = build_secret_doc(cfg, secret_env_for_yaml)
    dep_doc = build_deployment_doc(cfg, has_secret=has_secret)
    svc_doc = build_service_doc(cfg)
    pdb_doc = build_pdb_doc(cfg)

    write_yaml_atomic(cfg["FILES"]["serviceaccount"], sa_doc)

    # Only write a secret manifest if there are non-AWS keys to write.
    if sec_doc is not None:
        write_yaml_atomic(cfg["FILES"]["secret"], sec_doc)
    else:
        # If a secret manifest exists on disk but we intentionally don't want to write AWS keys to it,
        # remove the stale file to avoid committing secrets to the repo.
        if cfg["FILES"]["secret"].exists():
            try:
                cfg["FILES"]["secret"].unlink()
            except Exception:
                log.debug("Could not remove stale secret manifest", exc_info=True)

    write_yaml_atomic(cfg["FILES"]["deployment"], dep_doc)
    write_yaml_atomic(cfg["FILES"]["service"], svc_doc)

    if pdb_doc:
        write_yaml_atomic(cfg["FILES"]["pdb"], pdb_doc)
    elif cfg["FILES"]["pdb"].exists():
        try:
            cfg["FILES"]["pdb"].unlink()
        except Exception:
            log.debug("Could not remove stale pdb manifest", exc_info=True)

    inputs_path.write_text(inputs_hash + "\n", encoding="utf-8")
    log.info("Manifests written to %s (inputs_hash=%s)", str(manifests_dir), inputs_hash)

    if verbose:
        log.debug("Secret keys written to YAML: %s", sorted(secret_env_for_yaml.keys()))
        log.debug("Secret keys applied directly: %s", sorted(secret_env_for_direct_apply.keys()))
        log.debug("Deployment image: %s", cfg["IMAGE"])

    # Return a tuple-like string to indicate inputs_hash and whether there are direct secrets to apply.
    # The caller (apply_to_cluster) will inspect secret_env and apply direct secrets as needed.
    # For backward compatibility we return inputs_hash (string).
    return inputs_hash


def apply_to_cluster(cfg: dict[str, Any], secret_env: dict[str, str], dry_run: bool = False, verbose: bool = False) -> None:
    if not shutil.which("kubectl"):
        log.error("kubectl not found; aborting apply.")
        raise SystemExit(2)

    # Recompute the split so we know which secrets were written to YAML and which must be applied directly.
    secret_env_for_yaml: dict[str, str] = {}
    secret_env_for_direct_apply: dict[str, str] = {}
    use_iam = bool(cfg.get("USE_IAM", False))

    for k, v in secret_env.items():
        if k in AWS_SECRET_KEYS:
            if use_iam:
                continue
            secret_env_for_direct_apply[k] = v
        else:
            secret_env_for_yaml[k] = v

    generate_manifests(cfg, secret_env, dry_run=dry_run, verbose=verbose)
    if dry_run:
        log.info("Dry-run requested; skipping apply actions.")
        return

    # If there are direct secrets to apply (e.g., AWS keys), apply them directly to the cluster now.
    if secret_env_for_direct_apply:
        try:
            create_namespace_if_missing(cfg["NAMESPACE"])
            apply_secret_direct(cfg, secret_env_for_direct_apply)
        except subprocess.CalledProcessError as exc:
            log.error("Failed to apply direct secrets: %s", exc)
            raise SystemExit(2) from None

    # If there is a secret manifest on disk (non-AWS keys), apply it.
    try:
        create_namespace_if_missing(cfg["NAMESPACE"])

        apply_yaml(cfg["FILES"]["serviceaccount"])
        if cfg["FILES"]["secret"].exists():
            apply_yaml(cfg["FILES"]["secret"])
        apply_yaml(cfg["FILES"]["deployment"])
        apply_yaml(cfg["FILES"]["service"])
        if cfg["FILES"]["pdb"].exists():
            apply_yaml(cfg["FILES"]["pdb"])
    except subprocess.CalledProcessError as exc:
        log.error("kubectl apply failed: %s", exc)
        raise SystemExit(2) from None

    log.info("Manifests applied.")


def delete_manifests(cfg: dict[str, Any]) -> None:
    for path in (
        cfg["FILES"]["serviceaccount"],
        cfg["FILES"]["secret"],
        cfg["FILES"]["deployment"],
        cfg["FILES"]["service"],
        cfg["FILES"]["pdb"],
    ):
        if path.exists():
            try:
                delete_yaml(path)
            except Exception:
                log.debug("Failed to delete %s", path, exc_info=True)

    try:
        if cfg["MANIFESTS_DIR"].exists():
            for p in sorted(cfg["MANIFESTS_DIR"].glob("*")):
                if p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        log.debug("Failed to remove %s", p, exc_info=True)
            state_dir = cfg["MANIFESTS_DIR"] / cfg["STATE_DIRNAME"]
            if state_dir.exists():
                for p in sorted(state_dir.glob("*")):
                    if p.is_file():
                        try:
                            p.unlink()
                        except Exception:
                            log.debug("Failed to remove %s", p, exc_info=True)
                try:
                    state_dir.rmdir()
                except Exception:
                    log.debug("Failed to remove state dir %s", state_dir, exc_info=True)
    except Exception:
        log.debug("Manifest cleanup had errors", exc_info=True)


def parse_args(argv: list[str] | None = None) -> Any:
    p = argparse.ArgumentParser(description="Generate and manage Retriever manifests and secrets.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply-secrets", action="store_true", help="Create/update secrets in-cluster (no secret files written).")
    group.add_argument("--write", action="store_true", help="Write manifests to disk (no cluster apply).")
    group.add_argument("--apply", action="store_true", help="Write manifests and apply them to cluster.")
    group.add_argument("--delete", action="store_true", help="Delete manifests from disk and cluster files.")
    p.add_argument("--delete-secret", action="store_true", help="When used with --delete, also delete the in-cluster secret.")
    p.add_argument("--dry-run", action="store_true", help="Do not apply anything to cluster; only generate files when used with --write or --apply.")
    p.add_argument("--verbose", action="store_true", help="Enable verbose debug output.")
    return p.parse_args(argv or sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    if args.verbose:
        log.setLevel(logging.DEBUG)

    secret_env = collect_secret_env()

    try:
        if args.write:
            generate_manifests(cfg, secret_env, dry_run=args.dry_run, verbose=args.verbose)
            return 0

        if args.apply:
            apply_to_cluster(cfg, secret_env, dry_run=args.dry_run, verbose=args.verbose)
            return 0

        if args.apply_secrets:
            if cfg["USE_IAM"]:
                log.info("USE_IAM=true; skipping secret apply.")
                return 0
            if not secret_env:
                log.warning("No secret values found in environment; nothing to apply.")
                return 0
            if args.dry_run:
                log.info("Dry-run requested; not applying secrets.")
                return 0
            create_namespace_if_missing(cfg["NAMESPACE"])
            # Apply all secrets directly (this will include AWS keys when USE_IAM=false).
            apply_secret_direct(cfg, secret_env)
            return 0

        if args.delete:
            delete_manifests(cfg)
            if args.delete_secret:
                delete_secret_direct(cfg)
            return 0

        return 1

    except subprocess.CalledProcessError as exc:
        log.error("kubectl command failed: %s", exc)
        return exc.returncode or 1
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
