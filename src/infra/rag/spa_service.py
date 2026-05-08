"""
Frontend + Auth Service manifest generator.

Generates idempotent Kubernetes manifests for the frontend service.
Sensitive values are never written to YAML; they are applied directly
to the cluster as a Secret.

SESSION_SECRET and JWT_PRIVATE_KEY_PEM are auto-generated (once) by
the generator itself and stored in the cluster Secret.

AWS credentials (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) are only
included when USE_IAM=false.  When USE_IAM=true the service account
receives an IRSA annotation and the Pod gets an IRSA_ROLE_ARN env var.

Google OAuth is enabled by default; set the following before running
--apply-secrets:

  export FRONTEND_HOSTNAME=your-domain.tld
  export GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
  export GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
  export VALKEY_URL=redis://:<password>@valkey.valkey.svc.cluster.local:6379

Then:

  kubectl delete -f src/manifests/frontend || true
  python3 src/infra/rag/spa_service.py --apply-secrets
  python3 src/infra/rag/spa_service.py --write
  python3 src/infra/rag/spa_service.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

LOG_LEVEL = os.environ.get("FRONTEND_GEN_LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gen_frontend")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "NAMESPACE": "inference",
    "DEPLOYMENT_NAME": "frontend",
    "SERVICE_NAME": "frontend",
    "SERVICE_ACCOUNT_NAME": "frontend-sa",
    "SECRET_NAME": "frontend-secrets",
    "IMAGE": "ghcr.io/athithya-sakthivel/frontend:staging",
    "IMAGE_PULL_POLICY": "Always",
    "REPLICAS": 1,
    "CONTAINER_PORT": 8000,
    "CPU_REQUEST": "100m",
    "CPU_LIMIT": "500m",
    "MEMORY_REQUEST": "128Mi",
    "MEMORY_LIMIT": "256Mi",
    "RUN_AS_USER": 1000,
    "RUN_AS_GROUP": 1000,
    "FS_GROUP": 1000,
    "RUN_AS_NONROOT": True,
    "ALLOW_PRIV_ESC": False,
    "READONLY_ROOTFS": True,
    "SERVICE_TYPE": "ClusterIP",
    # Probes – robust for single-node clusters
    "PROBE_TIMEOUT_SECONDS": 5,
    "STARTUP_FAILURE_THRESHOLD": 12,     # 60s grace
    "LIVENESS_PERIOD_SECONDS": 10,
    "LIVENESS_FAILURE_THRESHOLD": 3,
    "READINESS_PERIOD_SECONDS": 5,
    "READINESS_FAILURE_THRESHOLD": 2,
    "ROLLOUT_TIMEOUT": 300,
    "ENABLE_PROMETHEUS": True,
    "PROMETHEUS_PATH": "/metrics",
    "USE_IAM": False,
    "IRSA_ROLE_ARN": "",
    # Auth & domain
    "ENABLE_GOOGLE_AUTH": True,
    "REQUIRE_AUTH": True,
    "FRONTEND_HOSTNAME": "athithya.site",
    "ENABLE_PRESIGNED_URLS": True,
    "PRESIGNED_URL_TTL_SECONDS": 3600,
}

# Sensitive keys that will be stored in the cluster Secret (never on disk)
SECRET_KEYS = (
    "SESSION_SECRET",
    "VALKEY_URL",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "JWT_PRIVATE_KEY_PEM",
)

# Keys that are auto‑generated if not already present in the cluster
AUTO_GENERATED_KEYS = ("SESSION_SECRET", "JWT_PRIVATE_KEY_PEM")

# AWS credential keys (only included when USE_IAM=false)
AWS_CRED_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() or default

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
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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

def _get_existing_secret_data(namespace: str, name: str) -> dict[str, str] | None:
    """Fetch existing base64-encoded secret data from the cluster."""
    try:
        proc = subprocess.run(
            ["kubectl", "get", "secret", name, "-n", namespace, "-o", "json"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return {k: v for k, v in data.get("data", {}).items()}
    except Exception:
        return None

def _decode_secret_value(b64_value: str) -> str:
    import base64
    return base64.b64decode(b64_value).decode("utf-8")

def _auto_generate_missing_keys(
    secret_env: dict[str, str],
    namespace: str,
    secret_name: str,
) -> None:
    """
    Ensure SESSION_SECRET and JWT_PRIVATE_KEY_PEM exist.
    Look up existing cluster secret first; if missing, generate and add.
    """
    existing = _get_existing_secret_data(namespace, secret_name) or {}

    if "SESSION_SECRET" not in secret_env or not secret_env["SESSION_SECRET"]:
        if "SESSION_SECRET" in existing:
            secret_env["SESSION_SECRET"] = _decode_secret_value(existing["SESSION_SECRET"])
        else:
            secret_env["SESSION_SECRET"] = secrets.token_hex(32)
            log.info("Generated new SESSION_SECRET")

    if "JWT_PRIVATE_KEY_PEM" not in secret_env or not secret_env["JWT_PRIVATE_KEY_PEM"]:
        if "JWT_PRIVATE_KEY_PEM" in existing:
            secret_env["JWT_PRIVATE_KEY_PEM"] = _decode_secret_value(existing["JWT_PRIVATE_KEY_PEM"])
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            pem_bytes = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            pem = pem_bytes.decode("utf-8")  # real newlines
            secret_env["JWT_PRIVATE_KEY_PEM"] = pem
            log.info("Generated new JWT_PRIVATE_KEY_PEM")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MANIFESTS_DIR = Path("src/manifests/frontend")
STATE_DIRNAME = ".state"

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
    cfg["RUN_AS_NONROOT"] = _env_bool("RUN_AS_NONROOT", DEFAULTS["RUN_AS_NONROOT"])
    cfg["ALLOW_PRIV_ESC"] = _env_bool("ALLOW_PRIV_ESC", DEFAULTS["ALLOW_PRIV_ESC"])
    cfg["READONLY_ROOTFS"] = _env_bool("READONLY_ROOTFS", DEFAULTS["READONLY_ROOTFS"])
    cfg["SERVICE_TYPE"] = _env_str("SERVICE_TYPE", DEFAULTS["SERVICE_TYPE"])
    cfg["PROBE_TIMEOUT_SECONDS"] = _env_int("PROBE_TIMEOUT_SECONDS", DEFAULTS["PROBE_TIMEOUT_SECONDS"])
    cfg["STARTUP_FAILURE_THRESHOLD"] = _env_int("STARTUP_FAILURE_THRESHOLD", DEFAULTS["STARTUP_FAILURE_THRESHOLD"])
    cfg["LIVENESS_PERIOD_SECONDS"] = _env_int("LIVENESS_PERIOD_SECONDS", DEFAULTS["LIVENESS_PERIOD_SECONDS"])
    cfg["LIVENESS_FAILURE_THRESHOLD"] = _env_int("LIVENESS_FAILURE_THRESHOLD", DEFAULTS["LIVENESS_FAILURE_THRESHOLD"])
    cfg["READINESS_PERIOD_SECONDS"] = _env_int("READINESS_PERIOD_SECONDS", DEFAULTS["READINESS_PERIOD_SECONDS"])
    cfg["READINESS_FAILURE_THRESHOLD"] = _env_int("READINESS_FAILURE_THRESHOLD", DEFAULTS["READINESS_FAILURE_THRESHOLD"])
    cfg["ROLLOUT_TIMEOUT"] = _env_int("ROLLOUT_TIMEOUT", DEFAULTS["ROLLOUT_TIMEOUT"])
    cfg["ENABLE_PROMETHEUS"] = _env_bool("ENABLE_PROMETHEUS", DEFAULTS["ENABLE_PROMETHEUS"])
    cfg["PROMETHEUS_PATH"] = _env_str("PROMETHEUS_PATH", DEFAULTS["PROMETHEUS_PATH"])
    cfg["USE_IAM"] = _env_bool("USE_IAM", DEFAULTS["USE_IAM"])
    cfg["IRSA_ROLE_ARN"] = _env_str("IRSA_ROLE_ARN", DEFAULTS["IRSA_ROLE_ARN"])

    # Auth & domain settings
    cfg["ENABLE_GOOGLE_AUTH"] = _env_bool("ENABLE_GOOGLE_AUTH", DEFAULTS["ENABLE_GOOGLE_AUTH"])
    cfg["REQUIRE_AUTH"] = _env_bool("REQUIRE_AUTH", DEFAULTS["REQUIRE_AUTH"])
    cfg["FRONTEND_HOSTNAME"] = _env_str("FRONTEND_HOSTNAME", DEFAULTS["FRONTEND_HOSTNAME"])
    cfg["ENABLE_PRESIGNED_URLS"] = _env_bool("ENABLE_PRESIGNED_URLS", DEFAULTS["ENABLE_PRESIGNED_URLS"])
    cfg["PRESIGNED_URL_TTL_SECONDS"] = _env_int("PRESIGNED_URL_TTL_SECONDS", DEFAULTS["PRESIGNED_URL_TTL_SECONDS"])

    # No 02-secret.yaml anymore
    cfg["FILES"] = {
        "serviceaccount": cfg["MANIFESTS_DIR"] / "01-serviceaccount.yaml",
        "deployment": cfg["MANIFESTS_DIR"] / "03-deployment.yaml",
        "service": cfg["MANIFESTS_DIR"] / "04-service.yaml",
        "pdb": cfg["MANIFESTS_DIR"] / "05-pdb.yaml",
    }
    cfg["UUID_SHORT"] = str(uuid.uuid4())[:8]
    log.info(
        "Loaded config: namespace=%s deployment=%s replicas=%d image=%s google_auth=%s require_auth=%s frontend_hostname=%s",
        cfg["NAMESPACE"],
        cfg["DEPLOYMENT_NAME"],
        cfg["REPLICAS"],
        cfg["IMAGE"],
        cfg["ENABLE_GOOGLE_AUTH"],
        cfg["REQUIRE_AUTH"],
        cfg["FRONTEND_HOSTNAME"],
    )
    return cfg

# ---------------------------------------------------------------------------
# Labels & probes
# ---------------------------------------------------------------------------
def _base_labels(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": cfg["SERVICE_NAME"],
        "app.kubernetes.io/instance": cfg["DEPLOYMENT_NAME"],
        "app.kubernetes.io/managed-by": "frontend-manifest-generator",
        "app.kubernetes.io/component": "frontend",
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

# ---------------------------------------------------------------------------
# Secret collection
# ---------------------------------------------------------------------------
def collect_user_secrets() -> dict[str, str]:
    """Return only secrets explicitly set in the environment."""
    env = {}
    for key in SECRET_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            env[key] = value
    return env

def _aws_cred_env() -> dict[str, str]:
    """Return AWS creds if set in the environment."""
    creds = {}
    for key in AWS_CRED_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            creds[key] = value
    return creds

# ---------------------------------------------------------------------------
# Manifest builders (no secret manifest)
# ---------------------------------------------------------------------------
def build_service_account_doc(cfg: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "name": cfg["SERVICE_ACCOUNT_NAME"],
        "namespace": cfg["NAMESPACE"],
        "labels": _base_labels(cfg),
    }
    if cfg["USE_IAM"] and cfg["IRSA_ROLE_ARN"]:
        metadata.setdefault("annotations", {})[
            "eks.amazonaws.com/role-arn"
        ] = cfg["IRSA_ROLE_ARN"]
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": metadata,
    }

def build_deployment_doc(cfg: dict[str, Any], has_secret: bool) -> dict[str, Any]:
    env_vars = [
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        {"name": "SERVICE_NAME", "value": cfg["SERVICE_NAME"]},
        {"name": "ENV", "value": "PROD"},
        {"name": "LOG_LEVEL", "value": "INFO"},
        {"name": "ENABLE_PROMETHEUS", "value": str(cfg["ENABLE_PROMETHEUS"]).lower()},
        {"name": "PROMETHEUS_PATH", "value": cfg["PROMETHEUS_PATH"]},
        # Auth toggles
        {"name": "REQUIRE_AUTH", "value": str(cfg.get("REQUIRE_AUTH", True)).lower()},
        {"name": "ENABLE_GOOGLE_AUTH", "value": str(cfg.get("ENABLE_GOOGLE_AUTH", True)).lower()},
        {"name": "ENABLE_MICROSOFT_AUTH", "value": "false"},
        {"name": "ENABLE_GITHUB_AUTH", "value": "false"},
        # UI settings
        {"name": "DISPLAY_SOURCES_IN_UI", "value": "true"},
        {"name": "DISPLAY_TOPK_IN_UI", "value": "true"},
        {"name": "USE_IAM", "value": str(cfg["USE_IAM"]).lower()},
        # Rate limits
        {"name": "RATE_LIMIT_GENERATE_STREAM", "value": "10/minute"},
        {"name": "RATE_LIMIT_AUTH_ME", "value": "30/minute"},
        {"name": "RATE_LIMIT_STREAM_CONCURRENCY", "value": "10"},
        {"name": "RATE_LIMIT_AUTH_LOGIN", "value": "10/minute"},
        {"name": "RATE_LIMIT_AUTH_START", "value": "5/minute"},
        {"name": "RATE_LIMIT_AUTH_CALLBACK", "value": "20/minute"},
        {"name": "RATE_LIMIT_AUTH_LOGOUT", "value": "20/minute"},
        # JWT
        {"name": "JWT_ISS", "value": "stateless-openid-auth"},
        {"name": "JWT_AUD", "value": "rag-ui"},
        {"name": "JWT_TTL_SECONDS", "value": "900"},
        {"name": "JWT_CLOCK_SKEW_SECONDS", "value": "90"},
        {"name": "JWT_KID", "value": "production-key-1"},
        # OAuth domain restrictions (empty = allow all)
        {"name": "GOOGLE_ALLOWED_DOMAINS", "value": "gmail.com"},
        {"name": "MICROSOFT_ALLOWED_DOMAINS", "value": ""},
        {"name": "MICROSOFT_ALLOWED_TENANT_IDS", "value": ""},
        {"name": "GITHUB_ALLOWED_ORGS", "value": ""},
        # Critical: the public hostname for OAuth redirects
        {"name": "FRONTEND_HOSTNAME", "value": cfg.get("FRONTEND_HOSTNAME", "")},
        # Upstream retriever
        {"name": "RETRIEVER_URL", "value": "http://retriever.inference.svc.cluster.local:8001"},
        {"name": "GENERATE_STREAM_PATH", "value": "/generate/stream"},
        {"name": "UPSTREAM_TIMEOUT_SECONDS", "value": "60"},
        # Valkey / Redis
        {"name": "VALKEY_SERVICE_HOST", "value": "valkey.valkey.svc.cluster.local"},
        {"name": "VALKEY_SERVICE_PORT", "value": "6379"},
        # Presigned URL toggles
        {"name": "ENABLE_PRESIGNED_URLS", "value": str(cfg.get("ENABLE_PRESIGNED_URLS", True)).lower()},
        {"name": "PRESIGNED_URL_TTL_SECONDS", "value": str(cfg.get("PRESIGNED_URL_TTL_SECONDS", 3600))},
    ]

    env_from = [{"secretRef": {"name": cfg["SECRET_NAME"]}}] if has_secret else []

    container = {
        "name": cfg["DEPLOYMENT_NAME"],
        "image": cfg["IMAGE"],
        "imagePullPolicy": cfg["IMAGE_PULL_POLICY"],
        "ports": [{"name": "http", "containerPort": cfg["CONTAINER_PORT"], "protocol": "TCP"}],
        "env": env_vars,
        "envFrom": env_from,
        "volumeMounts": [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "tmp", "mountPath": "/var/tmp"},
            {"name": "static-files", "mountPath": "/app/static"},
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
        # Fixed probes – startup, then liveness + readiness
        "startupProbe": _probe_http(
            "/health",
            cfg["CONTAINER_PORT"],
            initial_delay=5,
            period=5,
            timeout=cfg["PROBE_TIMEOUT_SECONDS"],
            failure_threshold=cfg["STARTUP_FAILURE_THRESHOLD"],
        ),
        "livenessProbe": _probe_http(
            "/health",
            cfg["CONTAINER_PORT"],
            initial_delay=0,   # disabled until startup succeeds
            period=cfg["LIVENESS_PERIOD_SECONDS"],
            timeout=cfg["PROBE_TIMEOUT_SECONDS"],
            failure_threshold=cfg["LIVENESS_FAILURE_THRESHOLD"],
        ),
        "readinessProbe": _probe_http(
            "/orchestrator/health",
            cfg["CONTAINER_PORT"],
            initial_delay=0,
            period=cfg["READINESS_PERIOD_SECONDS"],
            timeout=cfg["PROBE_TIMEOUT_SECONDS"],
            failure_threshold=cfg["READINESS_FAILURE_THRESHOLD"],
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
                    "app.kubernetes.io/component": "frontend",
                }
            },
            "template": {
                "metadata": {"labels": _pod_labels(cfg)},
                "spec": {
                    "serviceAccountName": cfg["SERVICE_ACCOUNT_NAME"],
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {"fsGroup": cfg["FS_GROUP"]},
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                        {"name": "static-files", "emptyDir": {}},
                    ],
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
                "app.kubernetes.io/component": "frontend",
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
                    "app.kubernetes.io/component": "frontend",
                }
            },
        },
    }

# ---------------------------------------------------------------------------
# File & kubectl helpers
# ---------------------------------------------------------------------------
def write_yaml_atomic(path: Path, doc: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    content = yaml.safe_dump(doc, sort_keys=False)
    atomic_write(path, content)

def apply_yaml(path: Path) -> None:
    log.info("Applying %s", str(path))
    subprocess.run(["kubectl", "apply", "-f", str(path)], check=True, capture_output=True)

def delete_yaml(path: Path) -> None:
    log.info("Deleting %s", str(path))
    subprocess.run(
        ["kubectl", "delete", "-f", str(path), "--ignore-not-found"],
        check=True,
        capture_output=True,
    )

def create_namespace_if_missing(namespace: str) -> None:
    rc = subprocess.run(["kubectl", "get", "ns", namespace], capture_output=True)
    if rc.returncode != 0:
        log.info("Namespace %s not found; creating", namespace)
        subprocess.run(["kubectl", "create", "ns", namespace], check=True, capture_output=True)

def apply_secret_direct(cfg: dict[str, Any], secret_data: dict[str, str]) -> None:
    """Apply secret directly to cluster, preserving JWT_PRIVATE_KEY_PEM newlines."""
    if not secret_data:
        log.info("No secret values; skipping.")
        return

    cmd = [
        "kubectl", "create", "secret", "generic",
        cfg["SECRET_NAME"], "-n", cfg["NAMESPACE"],
    ]

    pem_path = None
    if "JWT_PRIVATE_KEY_PEM" in secret_data:
        pem_content = secret_data.pop("JWT_PRIVATE_KEY_PEM")
        tmpfd, pem_path = tempfile.mkstemp(suffix=".pem", text=True)
        with os.fdopen(tmpfd, "w") as f:
            f.write(pem_content)
        cmd.extend(["--from-file", f"JWT_PRIVATE_KEY_PEM={pem_path}"])

    for key, value in sorted(secret_data.items()):
        cmd.extend(["--from-literal", f"{key}={value}"])

    cmd.extend(["--dry-run=client", "-o", "yaml"])
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=proc.stdout,
        text=True,
        check=True,
        capture_output=True,
    )
    log.info("Applied secret to cluster (direct)")
    if pem_path:
        os.unlink(pem_path)

def delete_secret_direct(cfg: dict[str, Any]) -> None:
    subprocess.run(
        [
            "kubectl", "delete", "secret",
            cfg["SECRET_NAME"], "-n", cfg["NAMESPACE"], "--ignore-not-found",
        ],
        check=False,
        capture_output=True,
    )

# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------
def generate_manifests(
    cfg: dict[str, Any],
    secret_env: dict[str, str],
    dry_run: bool = False,
    verbose: bool = False,
) -> str | None:
    """Write non‑secret manifests to disk."""
    manifests_dir: Path = cfg["MANIFESTS_DIR"]
    ensure_dir(manifests_dir)

    # Determine if we have a secret to mount
    secret_for_yaml = {
        k: v for k, v in secret_env.items()
        if k not in AUTO_GENERATED_KEYS or k == "JWT_PRIVATE_KEY_PEM"
    }
    has_secret = bool(secret_for_yaml)

    # Build manifests
    sa_doc = build_service_account_doc(cfg)
    dep_doc = build_deployment_doc(cfg, has_secret=has_secret)
    svc_doc = build_service_doc(cfg)
    pdb_doc = build_pdb_doc(cfg)

    docs_for_hash = [
        {"serviceaccount": cfg["SERVICE_ACCOUNT_NAME"], "namespace": cfg["NAMESPACE"]},
        {"deployment": cfg["DEPLOYMENT_NAME"], "image": cfg["IMAGE"], "replicas": cfg["REPLICAS"]},
        {"service": cfg["SERVICE_NAME"], "port": cfg["CONTAINER_PORT"]},
        {"has_secret": has_secret},
    ]
    inputs_hash = canonical_inputs_hash({"docs": docs_for_hash, "cfg": cfg})

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

    write_yaml_atomic(cfg["FILES"]["serviceaccount"], sa_doc)
    write_yaml_atomic(cfg["FILES"]["deployment"], dep_doc)
    write_yaml_atomic(cfg["FILES"]["service"], svc_doc)
    if pdb_doc:
        write_yaml_atomic(cfg["FILES"]["pdb"], pdb_doc)
    elif cfg["FILES"]["pdb"].exists():
        try:
            cfg["FILES"]["pdb"].unlink()
        except Exception:
            pass

    inputs_path.write_text(inputs_hash + "\n", encoding="utf-8")
    log.info("Manifests written to %s (inputs_hash=%s)", str(manifests_dir), inputs_hash)
    if verbose:
        log.debug("Manifests:\nSA: %s\nDeployment: %s\nService: %s", sa_doc, dep_doc, svc_doc)
    return inputs_hash

def apply_to_cluster(cfg: dict[str, Any], dry_run: bool = False) -> None:
    """Write manifests and apply them, plus create the Secret."""
    if not shutil.which("kubectl"):
        log.error("kubectl not found; aborting.")
        raise SystemExit(2)

    use_iam = bool(cfg["USE_IAM"])

    # Assemble secrets
    secret_env = collect_user_secrets()
    _auto_generate_missing_keys(secret_env, cfg["NAMESPACE"], cfg["SECRET_NAME"])

    if not use_iam:
        aws = _aws_cred_env()
        if aws:
            secret_env.update(aws)

    # Warn if Google auth is enabled but no credentials
    if cfg["ENABLE_GOOGLE_AUTH"] and (not secret_env.get("GOOGLE_CLIENT_ID") or not secret_env.get("GOOGLE_CLIENT_SECRET")):
        log.warning("Google auth is enabled but GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are missing. Google login will be unavailable.")

    generate_manifests(cfg, secret_env, dry_run=dry_run)

    if dry_run:
        log.info("Dry-run requested; skipping apply.")
        return

    # Apply secret
    if secret_env:
        try:
            create_namespace_if_missing(cfg["NAMESPACE"])
            apply_secret_direct(cfg, secret_env)
        except subprocess.CalledProcessError:
            log.error("Failed to apply secrets")
            raise SystemExit(2) from None

    # Apply manifests
    try:
        create_namespace_if_missing(cfg["NAMESPACE"])
        for key in ["serviceaccount", "deployment", "service"]:
            apply_yaml(cfg["FILES"][key])
        pdb_file = cfg["FILES"]["pdb"]
        if pdb_file.exists():
            apply_yaml(pdb_file)
    except subprocess.CalledProcessError as exc:
        log.error("kubectl apply failed: %s", exc)
        raise SystemExit(2) from None

    log.info("Manifests and secrets applied successfully.")
    # Print redirect URI hint
    if cfg.get("FRONTEND_HOSTNAME"):
        log.info(
            "Google OAuth redirect URI: https://%s/auth/callback/google",
            cfg["FRONTEND_HOSTNAME"],
        )

def apply_secrets_only(cfg: dict[str, Any], dry_run: bool = False) -> int:
    """Apply only the Secret (no manifests)."""
    if not shutil.which("kubectl"):
        log.error("kubectl not found.")
        return 2

    use_iam = bool(cfg["USE_IAM"])
    secret_env = collect_user_secrets()
    _auto_generate_missing_keys(secret_env, cfg["NAMESPACE"], cfg["SECRET_NAME"])
    if not use_iam:
        aws = _aws_cred_env()
        if aws:
            secret_env.update(aws)

    if cfg["ENABLE_GOOGLE_AUTH"] and (not secret_env.get("GOOGLE_CLIENT_ID") or not secret_env.get("GOOGLE_CLIENT_SECRET")):
        log.warning("Google auth is enabled but credentials missing.")

    if not secret_env:
        log.warning("No secret values found; nothing to apply.")
        return 0

    if dry_run:
        log.info("Dry-run; not applying secrets.")
        return 0

    try:
        create_namespace_if_missing(cfg["NAMESPACE"])
        apply_secret_direct(cfg, secret_env)
    except subprocess.CalledProcessError:
        log.error("Failed to apply secrets")
        return 2
    return 0

def delete_manifests(cfg: dict[str, Any], delete_secret: bool = False) -> None:
    for key in ["serviceaccount", "deployment", "service", "pdb"]:
        path = cfg["FILES"][key]
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
                    log.debug("Failed to remove state dir", exc_info=True)
    except Exception:
        log.debug("Manifest cleanup had errors", exc_info=True)

    if delete_secret:
        delete_secret_direct(cfg)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> Any:
    p = argparse.ArgumentParser(description="Manage Frontend + Auth manifests (no secrets on disk).")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply-secrets", action="store_true", help="Create/update only the cluster secret.")
    group.add_argument("--write", action="store_true", help="Write manifests to disk (no cluster apply).")
    group.add_argument("--apply", action="store_true", help="Write manifests and apply them + secrets.")
    group.add_argument("--delete", action="store_true", help="Delete manifests and optionally the secret.")
    p.add_argument("--delete-secret", action="store_true", help="With --delete, also delete the cluster secret.")
    p.add_argument("--dry-run", action="store_true", help="Do not apply anything; only generate files.")
    p.add_argument("--verbose", action="store_true", help="Enable verbose debug output.")
    return p.parse_args(argv or sys.argv[1:])

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        if args.write:
            secret_env = collect_user_secrets()
            _auto_generate_missing_keys(secret_env, cfg["NAMESPACE"], cfg["SECRET_NAME"])
            generate_manifests(cfg, secret_env, dry_run=args.dry_run, verbose=args.verbose)
            return 0

        if args.apply:
            apply_to_cluster(cfg, dry_run=args.dry_run)
            return 0

        if args.apply_secrets:
            return apply_secrets_only(cfg, dry_run=args.dry_run)

        if args.delete:
            delete_manifests(cfg, delete_secret=args.delete_secret)
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