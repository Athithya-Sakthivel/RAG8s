from __future__ import annotations

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
    "IMAGE": "ghcr.io/athithya-sakthivel/retriever:2026-05-01-14-56--7edc587@sha256:ff60267c9e71bd00bdf2636d6c9e81f477023687bd4cf12bd825a05e19a4c49d",
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

APP_ENV_DEFAULTS: dict[str, str] = {
    "SERVICE_NAME": "retrieval",
    "OTEL_SERVICE_NAME": "retrieval",
    "SERVICE_VERSION": "v1",
    "OTEL_SERVICE_VERSION": "v1",
    "ENV": "prod",
    "DEPLOYMENT_ENVIRONMENT": "prod",
    "CLUSTER_NAME": "production-cluster",
    "K8S_CLUSTER_NAME": "production-cluster",
    "INSTANCE_ID": "",
    "AWS_REGION": "ap-south-1",
    "AWS_DEFAULT_REGION": "ap-south-1",
    "QDRANT_URL": "http://qdrant.qdrant.svc.cluster.local:6333",
    "QDRANT_API_KEY": "",
    "COLLECTION_NAME": "default_rag_collection1",
    "CACHE_COLLECTION_NAME": "",
    "DENSE_URL": "http://dense-svc.models.svc.cluster.local:8200",
    "SPARSE_URL": "http://sparse-svc.models.svc.cluster.local:8201",
    "RERANKER_URL": "http://reranker-svc.models.svc.cluster.local:8202",
    "PORT": "8001",
    "LOG_LEVEL": "WARNING",
    "ENABLE_OTEL_TRACES": "true",
    "ENABLE_OTEL_METRICS": "true",
    "ENABLE_OTEL_LOGS": "true",
}

APP_ENV_ORDER = list(APP_ENV_DEFAULTS.keys())


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


def run_cmd(cmd: list[str], capture: bool = True, check: bool = False, timeout: int | None = None, input_text: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, input=input_text, capture_output=capture, text=True, check=check, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout or "", e.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, getattr(e, "stdout", "") or "", getattr(e, "stderr", "") or f"timeout after {timeout}s"


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

    app_env: dict[str, str] = {}
    for name in APP_ENV_ORDER:
        app_env[name] = _env_str(name, APP_ENV_DEFAULTS[name])
    for k in ("ENABLE_OTEL_TRACES", "ENABLE_OTEL_METRICS", "ENABLE_OTEL_LOGS"):
        if k in app_env:
            app_env[k] = app_env[k].lower()
    cfg["APP_ENV"] = app_env

    manifests_dir = cfg["MANIFESTS_DIR"]
    cfg["FILES"] = {
        "serviceaccount": manifests_dir / "01-serviceaccount.yaml",
        "configmap": manifests_dir / "02-configmap.yaml",
        # intentionally no "secret" file
        "deployment": manifests_dir / "04-deployment.yaml",
        "service": manifests_dir / "05-service.yaml",
        "pdb": manifests_dir / "06-pdb.yaml",
    }

    cfg["UUID_SHORT"] = str(uuid.uuid4())[:8]
    log.info("Loaded config: namespace=%s deployment=%s replicas=%d image=%s", cfg["NAMESPACE"], cfg["DEPLOYMENT_NAME"], cfg["REPLICAS"], cfg["IMAGE"])
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


def _configmap_data(app_env: dict[str, str]) -> dict[str, str]:
    data: dict[str, str] = {}
    for key in APP_ENV_ORDER:
        if key in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "QDRANT_API_KEY"}:
            continue
        value = app_env.get(key, "")
        if value == "":
            continue
        data[key] = value
    if "ENV" not in data:
        data["ENV"] = app_env.get("ENV", "prod")
    if "DEPLOYMENT_ENVIRONMENT" not in data:
        data["DEPLOYMENT_ENVIRONMENT"] = app_env.get("DEPLOYMENT_ENVIRONMENT", data["ENV"])
    return data


def _probe_http(path: str, port: int, initial_delay: int, period: int, timeout: int, failure_threshold: int) -> dict[str, Any]:
    return {
        "httpGet": {"path": path, "port": port, "scheme": "HTTP"},
        "initialDelaySeconds": initial_delay,
        "timeoutSeconds": timeout,
        "periodSeconds": period,
        "failureThreshold": failure_threshold,
        "successThreshold": 1,
    }


def build_service_account_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": cfg["SERVICE_ACCOUNT_NAME"],
        "namespace": cfg["NAMESPACE"],
        "labels": _base_labels(cfg),
    }
    if cfg["USE_IAM"] and cfg["IRSA_ROLE_ARN"]:
        metadata.setdefault("annotations", {})["eks.amazonaws.com/role-arn"] = cfg["IRSA_ROLE_ARN"]
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata}


def build_configmap_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": cfg["DEPLOYMENT_NAME"], "namespace": cfg["NAMESPACE"], "labels": _base_labels(cfg)},
        "data": _configmap_data(cfg["APP_ENV"]),
    }


def _container_env(cfg: dict[str, Any], secret_env: dict[str, str]) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = []
    env.append({"name": "ENV", "value": cfg["APP_ENV"].get("ENV", "prod")})
    env.append({"name": "DEPLOYMENT_ENVIRONMENT", "value": cfg["APP_ENV"].get("DEPLOYMENT_ENVIRONMENT", cfg["APP_ENV"].get("ENV", "prod"))})
    env.append({"name": "INSTANCE_ID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}})
    for key in APP_ENV_ORDER:
        if key in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "QDRANT_API_KEY", "INSTANCE_ID"}:
            continue
        val = cfg["APP_ENV"].get(key)
        if val is None or val == "":
            continue
        env.append({"name": key, "value": val})
    # Add secretKeyRef entries only (no secret values written to disk)
    if secret_env:
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "QDRANT_API_KEY"):
            if name in secret_env:
                env.append({"name": name, "valueFrom": {"secretKeyRef": {"name": cfg["SECRET_NAME"], "key": name}}})
    return env


def build_deployment_doc(cfg: dict[str, Any], secret_env: dict[str, str]) -> dict[str, Any]:
    pod_labels = _pod_labels(cfg)
    base_labels = _base_labels(cfg)
    container = {
        "name": cfg["DEPLOYMENT_NAME"],
        "image": cfg["IMAGE"],
        "imagePullPolicy": cfg["IMAGE_PULL_POLICY"],
        "ports": [{"name": "http", "containerPort": cfg["CONTAINER_PORT"], "protocol": "TCP"}],
        "env": _container_env(cfg, secret_env),
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
        "securityContext": {
            "allowPrivilegeEscalation": bool(cfg["ALLOW_PRIV_ESC"]),
            "readOnlyRootFilesystem": bool(cfg["READONLY_ROOTFS"]),
        },
        "resources": {"requests": {"cpu": cfg["CPU_REQUEST"], "memory": cfg["MEMORY_REQUEST"]}, "limits": {"cpu": cfg["CPU_LIMIT"], "memory": cfg["MEMORY_LIMIT"]}},
        "readinessProbe": _probe_http("/readyz", cfg["CONTAINER_PORT"], cfg["READINESS_INITIAL_DELAY"], cfg["PROBE_PERIOD_SECONDS"], cfg["PROBE_TIMEOUT_SECONDS"], 3),
        "livenessProbe": _probe_http("/healthz", cfg["CONTAINER_PORT"], cfg["LIVENESS_INITIAL_DELAY"], cfg["PROBE_PERIOD_SECONDS"], cfg["PROBE_TIMEOUT_SECONDS"], 6),
        "startupProbe": _probe_http("/healthz", cfg["CONTAINER_PORT"], cfg["LIVENESS_INITIAL_DELAY"], cfg["PROBE_PERIOD_SECONDS"], cfg["PROBE_TIMEOUT_SECONDS"], cfg["STARTUP_FAILURE_THRESHOLD"]),
    }

    if cfg.get("READONLY_ROOTFS", True):
        existing_mounts = container.get("volumeMounts", []) or []
        tmp_mounts = [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "tmp", "mountPath": "/var/tmp"},
            {"name": "tmp", "mountPath": "/usr/tmp"},
        ]
        for m in tmp_mounts:
            if not any(vm.get("mountPath") == m["mountPath"] for vm in existing_mounts):
                existing_mounts.append(m)
        if not any(vm.get("mountPath") == "/models_cache" for vm in existing_mounts):
            existing_mounts.append({"name": "models-cache", "mountPath": "/models_cache"})
        container["volumeMounts"] = existing_mounts

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": cfg["DEPLOYMENT_NAME"], "namespace": cfg["NAMESPACE"], "labels": base_labels},
        "spec": {
            "replicas": cfg["REPLICAS"],
            "revisionHistoryLimit": 3,
            "selector": {"matchLabels": {"app.kubernetes.io/name": cfg["SERVICE_NAME"], "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"], "app.kubernetes.io/component": "retriever"}},
            "template": {
                "metadata": {"labels": pod_labels},
                "spec": {
                    "serviceAccountName": cfg["SERVICE_ACCOUNT_NAME"],
                    "automountServiceAccountToken": True,
                    "securityContext": {"runAsNonRoot": bool(cfg["RUN_AS_NONROOT"]), "runAsUser": int(cfg["RUN_AS_USER"]), "runAsGroup": int(cfg["RUN_AS_GROUP"]), "fsGroup": int(cfg["FS_GROUP"])},
                    "volumes": [{"name": "tmp", "emptyDir": {}}],
                    "containers": [container],
                },
            },
        },
    }

    vols = deployment["spec"]["template"]["spec"].get("volumes", []) or []
    if not any(v.get("name") == "models-cache" for v in vols):
        vols.append({"name": "models-cache", "emptyDir": {}})
    deployment["spec"]["template"]["spec"]["volumes"] = vols

    return deployment


def build_service_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": cfg["SERVICE_NAME"], "namespace": cfg["NAMESPACE"], "labels": _base_labels(cfg)},
        "spec": {
            "type": cfg["SERVICE_TYPE"],
            "selector": {"app.kubernetes.io/name": cfg["SERVICE_NAME"], "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"], "app.kubernetes.io/component": "retriever"},
            "ports": [{"name": "http", "port": cfg["CONTAINER_PORT"], "targetPort": cfg["CONTAINER_PORT"], "protocol": "TCP"}],
        },
    }


def build_pdb_doc(cfg: dict[str, Any]) -> dict[str, Any] | None:
    if cfg["REPLICAS"] < 2:
        return None
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": f"{cfg['DEPLOYMENT_NAME']}-pdb", "namespace": cfg["NAMESPACE"], "labels": _base_labels(cfg)},
        "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": cfg["SERVICE_NAME"], "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"], "app.kubernetes.io/component": "retriever"}}},
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
    else:
        log.debug("Namespace %s already exists", namespace)


def apply_secret_direct(cfg: dict[str, Any], secret_env: dict[str, str]) -> None:
    """
    Create or update the secret directly in-cluster without writing a manifest file.
    Uses kubectl create secret generic ... --dry-run=client -o yaml | kubectl apply -f -
    """
    if not secret_env:
        log.info("No secret values provided; skipping secret apply.")
        return
    cmd = ["kubectl", "create", "secret", "generic", cfg["SECRET_NAME"], "-n", cfg["NAMESPACE"]]
    for key, value in sorted(secret_env.items()):
        cmd.extend(["--from-literal", f"{key}={value}"])
    cmd.extend(["--dry-run=client", "-o", "yaml"])
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=proc.stdout, text=True, check=True, capture_output=True)
    log.info("Applied secrets")


def delete_secret_direct(cfg: dict[str, Any]) -> None:
    try:
        subprocess.run(["kubectl", "delete", "secret", cfg["SECRET_NAME"], "-n", cfg["NAMESPACE"], "--ignore-not-found"], check=True, capture_output=True)
        log.info("Deleted secret")
    except subprocess.CalledProcessError:
        log.warning("Failed to delete secret")


def generate_manifests(cfg: dict[str, Any], secret_env: dict[str, str], dry_run: bool = False, verbose: bool = False) -> str | None:
    manifests_dir: Path = cfg["MANIFESTS_DIR"]
    ensure_dir(manifests_dir)
    inputs_hash = canonical_inputs_hash({**cfg, "secret_keys": sorted(secret_env.keys())})
    state_dir = manifests_dir / cfg["STATE_DIRNAME"]
    ensure_dir(state_dir)
    existing: str | None = None
    try:
        inputs_path = state_dir / "inputs.sha256"
        if inputs_path.exists():
            existing = inputs_path.read_text(encoding="utf-8").strip()
    except Exception:
        existing = None
    if existing == inputs_hash and not dry_run:
        log.info("No changes detected; skipping manifest generation.")
        return None

    sa_doc = build_service_account_doc(cfg)
    cm_doc = build_configmap_doc(cfg)
    deployment_doc = build_deployment_doc(cfg, secret_env)
    service_doc = build_service_doc(cfg)
    pdb_doc = build_pdb_doc(cfg)

    write_yaml_atomic(cfg["FILES"]["serviceaccount"], sa_doc)
    write_yaml_atomic(cfg["FILES"]["configmap"], cm_doc)
    write_yaml_atomic(cfg["FILES"]["deployment"], deployment_doc)
    write_yaml_atomic(cfg["FILES"]["service"], service_doc)
    if pdb_doc:
        write_yaml_atomic(cfg["FILES"]["pdb"], pdb_doc)

    (state_dir / "inputs.sha256").write_text(inputs_hash + "\n", encoding="utf-8")
    log.info("Manifests written to %s (inputs_hash=%s)", str(manifests_dir), inputs_hash)
    if verbose:
        log.debug("Deployment manifest head:\n%s", "\n".join(yaml.safe_dump(deployment_doc, sort_keys=False).splitlines()[:120]))
    return inputs_hash


def apply_to_cluster(cfg: dict[str, Any], secret_env: dict[str, str], dry_run: bool = False, verbose: bool = False) -> None:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        log.error("kubectl not found; aborting apply.")
        raise SystemExit(2) from None

    inputs_hash = generate_manifests(cfg, secret_env, dry_run=dry_run, verbose=verbose)
    if dry_run:
        log.info("Dry-run requested; skipping apply actions.")
        return
    if inputs_hash is None:
        log.info("No manifest changes; still proceeding with secret apply if requested.")
    try:
        create_namespace_if_missing(cfg["NAMESPACE"])
    except subprocess.CalledProcessError as exc:
        log.error("Failed to ensure namespace %s: %s", cfg["NAMESPACE"], exc)
        raise SystemExit(2) from None

    # Apply non-secret manifests so ArgoCD can pick them up (we still apply them here for local convenience)
    try:
        apply_yaml(cfg["FILES"]["serviceaccount"])
        apply_yaml(cfg["FILES"]["configmap"])
        apply_yaml(cfg["FILES"]["deployment"])
        apply_yaml(cfg["FILES"]["service"])
        if (cfg["FILES"]["pdb"]).exists():
            apply_yaml(cfg["FILES"]["pdb"])
    except subprocess.CalledProcessError as exc:
        log.error("kubectl apply failed: %s", exc)
        raise SystemExit(2) from None

    log.info("Manifests applied (ArgoCD will manage rollout).")


def delete_manifests(cfg: dict[str, Any]) -> None:
    def _delete(dir_path: Path, state_dirname: str) -> None:
        if not dir_path or str(dir_path).strip() in ("", "/", "."):
            return
        if not dir_path.exists():
            log.info("No manifests found at %s", str(dir_path))
            return
        try:
            subprocess.run(["kubectl", "delete", "-f", str(dir_path), "--ignore-not-found=true"], check=False, capture_output=True)
        except Exception:
            log.debug("kubectl delete failed for %s", dir_path, exc_info=True)
        for p in sorted(dir_path.glob("*")):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                log.debug("Failed to remove %s", p, exc_info=True)
        state_dir = dir_path / state_dirname
        try:
            if state_dir.exists():
                for p in sorted(state_dir.glob("*")):
                    try:
                        if p.is_file():
                            p.unlink()
                    except Exception:
                        log.debug("Failed to remove %s", p, exc_info=True)
                state_dir.rmdir()
        except Exception:
            log.debug("Failed to cleanup state dir %s", state_dir, exc_info=True)

    manifests_dir = Path(os.getenv("MANIFESTS_DIR", str(MANIFESTS_DIR)))
    _delete(manifests_dir, os.getenv("STATE_DIRNAME", STATE_DIRNAME))


def collect_secret_env() -> dict[str, str]:
    secret_env: dict[str, str] = {}
    qdrant_key = os.getenv("QDRANT_API_KEY", "").strip()
    if qdrant_key:
        secret_env["QDRANT_API_KEY"] = qdrant_key
    if not _env_bool("USE_IAM", DEFAULTS["USE_IAM"]):
        aws_key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        if aws_key:
            secret_env["AWS_ACCESS_KEY_ID"] = aws_key
        if aws_secret:
            secret_env["AWS_SECRET_ACCESS_KEY"] = aws_secret
    return secret_env


def parse_args(argv: list[str] | None = None) -> Any:
    import argparse

    p = argparse.ArgumentParser(description="Generate and manage Retriever manifests and secrets.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply-secrets", action="store_true", help="Create/update secrets in-cluster (no secret files written).")
    group.add_argument("--write", action="store_true", help="Write manifests to disk (no secrets).")
    group.add_argument("--delete", action="store_true", help="Delete manifests from disk and optionally delete in-cluster secret.")
    p.add_argument("--delete-secret", action="store_true", help="When used with --delete, also delete the in-cluster secret.")
    p.add_argument("--dry-run", action="store_true", help="Do not apply anything to cluster; only generate files when used with --write.")
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
        if args.apply_secrets:
            if cfg["USE_IAM"]:
                log.info("USE_IAM=true; skipping secret apply. If you intended to apply secrets, set USE_IAM=false.")
                return 0
            if not secret_env:
                log.warning("No secret values found in environment; nothing to apply.")
                return 0
            if args.dry_run:
                log.info("Dry-run requested; not applying secrets.")
                return 0
            create_namespace_if_missing(cfg["NAMESPACE"])
            apply_secret_direct(cfg, secret_env)
            return 0
        if args.delete:
            # delete manifests and optionally secret
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
