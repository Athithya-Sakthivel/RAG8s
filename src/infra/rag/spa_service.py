"""
Frontend + Auth Service manifest generator.

Generates idempotent Kubernetes manifests for the frontend service.
Sensitive values are never written to YAML; they are applied directly
to the cluster as a Secret.

SESSION_SECRET and JWT_PRIVATE_KEY_PEM are auto-generated (once) by
the generator itself and stored in the cluster Secret.

All configuration is read from environment variables with strong defaults.
Google and Microsoft OAuth can be enabled simultaneously.

Usage:
  export FRONTEND_HOSTNAME=rag.athithya.site
  export GOOGLE_CLIENT_ID=...
  export GOOGLE_CLIENT_SECRET=...
  export MS_CLIENT_ID=...
  export MS_CLIENT_SECRET=...
  (optional) export MS_TENANT_ID=...
  (optional) export MICROSOFT_ALLOWED_DOMAINS=...
  (optional) export MICROSOFT_ALLOWED_TENANT_IDS=...

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
#  Helper functions
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


# ---------------------------------------------------------------------------
#  All configuration – read once at module level with strong defaults
# ---------------------------------------------------------------------------
NAMESPACE = _env_str("NAMESPACE", "inference")
DEPLOYMENT_NAME = _env_str("DEPLOYMENT_NAME", "frontend")
SERVICE_NAME = _env_str("SERVICE_NAME", "frontend")
SERVICE_ACCOUNT_NAME = _env_str("SERVICE_ACCOUNT_NAME", "frontend-sa")
SECRET_NAME = _env_str("SECRET_NAME", "frontend-secrets")
IMAGE = _env_str("IMAGE", "ghcr.io/athithya-sakthivel/frontend:staging")
IMAGE_PULL_POLICY = _env_str("IMAGE_PULL_POLICY", "Always")
REPLICAS = _env_int("REPLICAS", 1)
CONTAINER_PORT = _env_int("CONTAINER_PORT", 8000)

CPU_REQUEST = _env_str("CPU_REQUEST", "100m")
CPU_LIMIT = _env_str("CPU_LIMIT", "500m")
MEMORY_REQUEST = _env_str("MEMORY_REQUEST", "128Mi")
MEMORY_LIMIT = _env_str("MEMORY_LIMIT", "256Mi")

RUN_AS_USER = _env_int("RUN_AS_USER", 1000)
RUN_AS_GROUP = _env_int("RUN_AS_GROUP", 1000)
FS_GROUP = _env_int("FS_GROUP", 1000)
RUN_AS_NONROOT = _env_bool("RUN_AS_NONROOT", True)
ALLOW_PRIV_ESC = _env_bool("ALLOW_PRIV_ESC", False)
READONLY_ROOTFS = _env_bool("READONLY_ROOTFS", True)

SERVICE_TYPE = _env_str("SERVICE_TYPE", "ClusterIP")

# Probes
PROBE_TIMEOUT_SECONDS = _env_int("PROBE_TIMEOUT_SECONDS", 5)
STARTUP_FAILURE_THRESHOLD = _env_int("STARTUP_FAILURE_THRESHOLD", 12)   # 60s grace
LIVENESS_PERIOD_SECONDS = _env_int("LIVENESS_PERIOD_SECONDS", 10)
LIVENESS_FAILURE_THRESHOLD = _env_int("LIVENESS_FAILURE_THRESHOLD", 3)
READINESS_PERIOD_SECONDS = _env_int("READINESS_PERIOD_SECONDS", 5)
READINESS_FAILURE_THRESHOLD = _env_int("READINESS_FAILURE_THRESHOLD", 2)
ROLLOUT_TIMEOUT = _env_int("ROLLOUT_TIMEOUT", 300)

ENABLE_PROMETHEUS = _env_bool("ENABLE_PROMETHEUS", True)
PROMETHEUS_PATH = _env_str("PROMETHEUS_PATH", "/metrics")

USE_IAM = _env_bool("USE_IAM", False)
IRSA_ROLE_ARN = _env_str("IRSA_ROLE_ARN", "")

# Auth toggles
REQUIRE_AUTH = _env_bool("REQUIRE_AUTH", True)
ENABLE_GOOGLE_AUTH = _env_bool("ENABLE_GOOGLE_AUTH", True)
ENABLE_MICROSOFT_AUTH = _env_bool("ENABLE_MICROSOFT_AUTH", True)
ENABLE_GITHUB_AUTH = _env_bool("ENABLE_GITHUB_AUTH", False)

# Domain / tenant allow‑lists (empty = no restriction)
GOOGLE_ALLOWED_DOMAINS = _env_str("GOOGLE_ALLOWED_DOMAINS", "gmail.com")
MICROSOFT_ALLOWED_DOMAINS = _env_str("MICROSOFT_ALLOWED_DOMAINS", "outlook.com")
MICROSOFT_ALLOWED_TENANT_IDS = _env_str("MICROSOFT_ALLOWED_TENANT_IDS", "")
GITHUB_ALLOWED_ORGS = _env_str("GITHUB_ALLOWED_ORGS", "")

# Public hostname – critical for OAuth redirects
FRONTEND_HOSTNAME = _env_str("FRONTEND_HOSTNAME", "rag.athithya.site")

# Upstream retriever
RETRIEVER_URL = _env_str("RETRIEVER_URL", "http://retriever.inference.svc.cluster.local:8001")
GENERATE_STREAM_PATH = _env_str("GENERATE_STREAM_PATH", "/generate/stream")
UPSTREAM_TIMEOUT_SECONDS = _env_str("UPSTREAM_TIMEOUT_SECONDS", "60")

# Valkey (URL hardcoded in config.py, just pass host/port for completeness)
VALKEY_SERVICE_HOST = _env_str("VALKEY_SERVICE_HOST", "valkey.valkey.svc.cluster.local")
VALKEY_SERVICE_PORT = _env_str("VALKEY_SERVICE_PORT", "6379")

# Rate limits
RATE_LIMIT_GENERATE_STREAM = _env_str("RATE_LIMIT_GENERATE_STREAM", "10/minute")
RATE_LIMIT_AUTH_ME = _env_str("RATE_LIMIT_AUTH_ME", "30/minute")
RATE_LIMIT_STREAM_CONCURRENCY = _env_str("RATE_LIMIT_STREAM_CONCURRENCY", "10")
RATE_LIMIT_AUTH_LOGIN = _env_str("RATE_LIMIT_AUTH_LOGIN", "10/minute")
RATE_LIMIT_AUTH_START = _env_str("RATE_LIMIT_AUTH_START", "5/minute")
RATE_LIMIT_AUTH_CALLBACK = _env_str("RATE_LIMIT_AUTH_CALLBACK", "20/minute")
RATE_LIMIT_AUTH_LOGOUT = _env_str("RATE_LIMIT_AUTH_LOGOUT", "20/minute")

# JWT
JWT_ISS = _env_str("JWT_ISS", "stateless-openid-auth")
JWT_AUD = _env_str("JWT_AUD", "rag-ui")
JWT_TTL_SECONDS = _env_str("JWT_TTL_SECONDS", "900")
JWT_CLOCK_SKEW_SECONDS = _env_str("JWT_CLOCK_SKEW_SECONDS", "90")
JWT_KID = _env_str("JWT_KID", "production-key-1")

# Microsoft tenant ID (used by stateless_openid_auth)
MS_TENANT_ID = _env_str("MS_TENANT_ID", "common")

# Presigned URL toggles
ENABLE_PRESIGNED_URLS = _env_bool("ENABLE_PRESIGNED_URLS", True)
PRESIGNED_URL_TTL_SECONDS = _env_str("PRESIGNED_URL_TTL_SECONDS", "3600")

# UI toggles
DISPLAY_SOURCES_IN_UI = _env_bool("DISPLAY_SOURCES_IN_UI", True)
DISPLAY_TOPK_IN_UI = _env_bool("DISPLAY_TOPK_IN_UI", False)

# Sensitive keys – stored in Kubernetes Secret, never in YAML
SECRET_KEYS = (
    "SESSION_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "JWT_PRIVATE_KEY_PEM",
)

AUTO_GENERATED_KEYS = ("SESSION_SECRET", "JWT_PRIVATE_KEY_PEM")
AWS_CRED_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

# ---------------------------------------------------------------------------
#  Helper utilities (unchanged)
# ---------------------------------------------------------------------------
MANIFESTS_DIR = Path("src/manifests/frontend")
STATE_DIRNAME = ".state"
FILES = {
    "serviceaccount": MANIFESTS_DIR / "01-serviceaccount.yaml",
    "deployment": MANIFESTS_DIR / "03-deployment.yaml",
    "service": MANIFESTS_DIR / "04-service.yaml",
    "pdb": MANIFESTS_DIR / "05-pdb.yaml",
    "networkpolicy": MANIFESTS_DIR / "06-networkpolicy.yaml",
}


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _get_existing_secret_data(namespace: str, name: str) -> dict[str, str] | None:
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
            pem = pem_bytes.decode("utf-8")
            secret_env["JWT_PRIVATE_KEY_PEM"] = pem
            log.info("Generated new JWT_PRIVATE_KEY_PEM")


def collect_user_secrets() -> dict[str, str]:
    env = {}
    for key in SECRET_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            env[key] = value
    return env


def _aws_cred_env() -> dict[str, str]:
    creds = {}
    for key in AWS_CRED_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            creds[key] = value
    return creds


# ---------------------------------------------------------------------------
#  Manifest builders
# ---------------------------------------------------------------------------
def _base_labels() -> dict[str, str]:
    return {
        "app.kubernetes.io/name": SERVICE_NAME,
        "app.kubernetes.io/instance": DEPLOYMENT_NAME,
        "app.kubernetes.io/managed-by": "frontend-manifest-generator",
        "app.kubernetes.io/component": "frontend",
    }


def _pod_labels() -> dict[str, str]:
    labels = _base_labels().copy()
    labels["app.kubernetes.io/part-of"] = SERVICE_NAME
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


def build_service_account_doc() -> dict[str, Any]:
    metadata = {
        "name": SERVICE_ACCOUNT_NAME,
        "namespace": NAMESPACE,
        "labels": _base_labels(),
    }
    if USE_IAM and IRSA_ROLE_ARN:
        metadata.setdefault("annotations", {})[
            "eks.amazonaws.com/role-arn"
        ] = IRSA_ROLE_ARN
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": metadata,
    }


def build_deployment_doc(has_secret: bool) -> dict[str, Any]:
    env_vars = [
        {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        {"name": "SERVICE_NAME", "value": SERVICE_NAME},
        {"name": "ENV", "value": "PROD"},
        {"name": "LOG_LEVEL", "value": "INFO"},
        {"name": "ENABLE_PROMETHEUS", "value": str(ENABLE_PROMETHEUS).lower()},
        {"name": "PROMETHEUS_PATH", "value": PROMETHEUS_PATH},
        # Auth toggles
        {"name": "REQUIRE_AUTH", "value": str(REQUIRE_AUTH).lower()},
        {"name": "ENABLE_GOOGLE_AUTH", "value": str(ENABLE_GOOGLE_AUTH).lower()},
        {"name": "ENABLE_MICROSOFT_AUTH", "value": str(ENABLE_MICROSOFT_AUTH).lower()},
        {"name": "ENABLE_GITHUB_AUTH", "value": str(ENABLE_GITHUB_AUTH).lower()},
        # UI settings
        {"name": "DISPLAY_SOURCES_IN_UI", "value": str(DISPLAY_SOURCES_IN_UI).lower()},
        {"name": "DISPLAY_TOPK_IN_UI", "value": str(DISPLAY_TOPK_IN_UI).lower()},
        {"name": "USE_IAM", "value": str(USE_IAM).lower()},
        # Rate limits
        {"name": "RATE_LIMIT_GENERATE_STREAM", "value": RATE_LIMIT_GENERATE_STREAM},
        {"name": "RATE_LIMIT_AUTH_ME", "value": RATE_LIMIT_AUTH_ME},
        {"name": "RATE_LIMIT_STREAM_CONCURRENCY", "value": RATE_LIMIT_STREAM_CONCURRENCY},
        {"name": "RATE_LIMIT_AUTH_LOGIN", "value": RATE_LIMIT_AUTH_LOGIN},
        {"name": "RATE_LIMIT_AUTH_START", "value": RATE_LIMIT_AUTH_START},
        {"name": "RATE_LIMIT_AUTH_CALLBACK", "value": RATE_LIMIT_AUTH_CALLBACK},
        {"name": "RATE_LIMIT_AUTH_LOGOUT", "value": RATE_LIMIT_AUTH_LOGOUT},
        # JWT
        {"name": "JWT_ISS", "value": JWT_ISS},
        {"name": "JWT_AUD", "value": JWT_AUD},
        {"name": "JWT_TTL_SECONDS", "value": JWT_TTL_SECONDS},
        {"name": "JWT_CLOCK_SKEW_SECONDS", "value": JWT_CLOCK_SKEW_SECONDS},
        {"name": "JWT_KID", "value": JWT_KID},
        # OAuth domain/tenant restrictions
        {"name": "GOOGLE_ALLOWED_DOMAINS", "value": GOOGLE_ALLOWED_DOMAINS},
        {"name": "MICROSOFT_ALLOWED_DOMAINS", "value": MICROSOFT_ALLOWED_DOMAINS},
        {"name": "MICROSOFT_ALLOWED_TENANT_IDS", "value": MICROSOFT_ALLOWED_TENANT_IDS},
        {"name": "GITHUB_ALLOWED_ORGS", "value": GITHUB_ALLOWED_ORGS},
        # Public hostname
        {"name": "FRONTEND_HOSTNAME", "value": FRONTEND_HOSTNAME},
        # Microsoft tenant ID
        {"name": "MS_TENANT_ID", "value": MS_TENANT_ID},
        # Upstream retriever
        {"name": "RETRIEVER_URL", "value": RETRIEVER_URL},
        {"name": "GENERATE_STREAM_PATH", "value": GENERATE_STREAM_PATH},
        {"name": "UPSTREAM_TIMEOUT_SECONDS", "value": UPSTREAM_TIMEOUT_SECONDS},
        # Valkey (optional env vars, real URL is hardcoded in config.py)
        {"name": "VALKEY_SERVICE_HOST", "value": VALKEY_SERVICE_HOST},
        {"name": "VALKEY_SERVICE_PORT", "value": VALKEY_SERVICE_PORT},
        # Presigned URL toggles
        {"name": "ENABLE_PRESIGNED_URLS", "value": str(ENABLE_PRESIGNED_URLS).lower()},
        {"name": "PRESIGNED_URL_TTL_SECONDS", "value": PRESIGNED_URL_TTL_SECONDS},
    ]

    env_from = [{"secretRef": {"name": SECRET_NAME}}] if has_secret else []

    container = {
        "name": DEPLOYMENT_NAME,
        "image": IMAGE,
        "imagePullPolicy": IMAGE_PULL_POLICY,
        "ports": [{"name": "http", "containerPort": CONTAINER_PORT, "protocol": "TCP"}],
        "env": env_vars,
        "envFrom": env_from,
        "volumeMounts": [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "tmp", "mountPath": "/var/tmp"},
            {"name": "static-files", "mountPath": "/app/static"},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": ALLOW_PRIV_ESC,
            "readOnlyRootFilesystem": READONLY_ROOTFS,
            "runAsNonRoot": RUN_AS_NONROOT,
            "runAsUser": RUN_AS_USER,
            "runAsGroup": RUN_AS_GROUP,
        },
        "resources": {
            "requests": {"cpu": CPU_REQUEST, "memory": MEMORY_REQUEST},
            "limits": {"cpu": CPU_LIMIT, "memory": MEMORY_LIMIT},
        },
        "startupProbe": _probe_http(
            "/health", CONTAINER_PORT, initial_delay=5, period=5,
            timeout=PROBE_TIMEOUT_SECONDS, failure_threshold=STARTUP_FAILURE_THRESHOLD,
        ),
        "livenessProbe": _probe_http(
            "/health", CONTAINER_PORT, initial_delay=0,
            period=LIVENESS_PERIOD_SECONDS, timeout=PROBE_TIMEOUT_SECONDS,
            failure_threshold=LIVENESS_FAILURE_THRESHOLD,
        ),
        "readinessProbe": _probe_http(
            "/orchestrator/health", CONTAINER_PORT, initial_delay=0,
            period=READINESS_PERIOD_SECONDS, timeout=PROBE_TIMEOUT_SECONDS,
            failure_threshold=READINESS_FAILURE_THRESHOLD,
        ),
    }

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": DEPLOYMENT_NAME,
            "namespace": NAMESPACE,
            "labels": _base_labels(),
        },
        "spec": {
            "replicas": REPLICAS,
            "revisionHistoryLimit": 3,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": SERVICE_NAME,
                    "app.kubernetes.io/instance": DEPLOYMENT_NAME,
                    "app.kubernetes.io/component": "frontend",
                }
            },
            "template": {
                "metadata": {"labels": _pod_labels()},
                "spec": {
                    "serviceAccountName": SERVICE_ACCOUNT_NAME,
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {"fsGroup": FS_GROUP},
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                        {"name": "static-files", "emptyDir": {}},
                    ],
                    "containers": [container],
                },
            },
        },
    }


def build_service_doc() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": SERVICE_NAME,
            "namespace": NAMESPACE,
            "labels": _base_labels(),
        },
        "spec": {
            "type": SERVICE_TYPE,
            "selector": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/instance": DEPLOYMENT_NAME,
                "app.kubernetes.io/component": "frontend",
            },
            "ports": [
                {
                    "name": "http",
                    "port": CONTAINER_PORT,
                    "targetPort": CONTAINER_PORT,
                    "protocol": "TCP",
                }
            ],
        },
    }


def build_pdb_doc() -> dict[str, Any] | None:
    if REPLICAS < 2:
        return None
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {
            "name": f"{DEPLOYMENT_NAME}-pdb",
            "namespace": NAMESPACE,
            "labels": _base_labels(),
        },
        "spec": {
            "minAvailable": 1,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": SERVICE_NAME,
                    "app.kubernetes.io/instance": DEPLOYMENT_NAME,
                    "app.kubernetes.io/component": "frontend",
                }
            },
        },
    }


def build_networkpolicy_doc() -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "frontend",
            "namespace": NAMESPACE,
            "labels": _base_labels(),
        },
        "spec": {
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/name": SERVICE_NAME}
            },
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [
                        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "cloudflared"}}}
                    ],
                    "ports": [{"protocol": "TCP", "port": CONTAINER_PORT}],
                },
                {
                    "from": [
                        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}
                    ],
                    "ports": [{"protocol": "TCP", "port": CONTAINER_PORT}],
                },
            ],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "valkey"}},
                            "podSelector": {"matchLabels": {"app": "valkey"}},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 6379}],
                },
                {
                    "to": [
                        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "retriever"}}}
                    ],
                    "ports": [{"protocol": "TCP", "port": 8001}],
                },
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 443}],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
#  File & kubectl helpers
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


def apply_secret_direct(secret_data: dict[str, str]) -> None:
    if not secret_data:
        log.info("No secret values; skipping.")
        return

    cmd = [
        "kubectl", "create", "secret", "generic",
        SECRET_NAME, "-n", NAMESPACE,
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


def delete_secret_direct() -> None:
    subprocess.run(
        ["kubectl", "delete", "secret", SECRET_NAME, "-n", NAMESPACE, "--ignore-not-found"],
        check=False,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
#  Core operations
# ---------------------------------------------------------------------------
def generate_manifests(
    secret_env: dict[str, str],
    dry_run: bool = False,
    verbose: bool = False,
) -> str | None:
    manifests_dir = MANIFESTS_DIR
    ensure_dir(manifests_dir)

    # Determine if we have a secret to mount
    secret_for_yaml = {
        k: v for k, v in secret_env.items()
        if k not in AUTO_GENERATED_KEYS or k == "JWT_PRIVATE_KEY_PEM"
    }
    has_secret = bool(secret_for_yaml)

    sa_doc = build_service_account_doc()
    dep_doc = build_deployment_doc(has_secret=has_secret)
    svc_doc = build_service_doc()
    pdb_doc = build_pdb_doc()
    netpol_doc = build_networkpolicy_doc()

    # Compute inputs hash (unchanged logic, but using module vars)
    docs_for_hash = [
        {"serviceaccount": SERVICE_ACCOUNT_NAME, "namespace": NAMESPACE},
        {"deployment": DEPLOYMENT_NAME, "image": IMAGE, "replicas": REPLICAS},
        {"service": SERVICE_NAME, "port": CONTAINER_PORT},
        {"has_secret": has_secret},
    ]
    # canonical_inputs_hash now uses global vars, but we keep the function unmodified for simplicity
    # However, since we removed the cfg dict, we adapt canonical_inputs_hash to just use the provided payload.
    # We'll create a small wrapper inline.
    def _inputs_hash(payload: dict[str, Any]) -> str:
        # same logic as before, but without the exclusion keys (we only pass what we want)
        serial = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serial.encode("utf-8")).hexdigest()

    inputs_hash = _inputs_hash({"docs": docs_for_hash, "has_secret": has_secret,
                                 "image": IMAGE, "replicas": REPLICAS,
                                 "service_name": SERVICE_NAME, "port": CONTAINER_PORT})

    state_dir = manifests_dir / STATE_DIRNAME
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

    write_yaml_atomic(FILES["serviceaccount"], sa_doc)
    write_yaml_atomic(FILES["deployment"], dep_doc)
    write_yaml_atomic(FILES["service"], svc_doc)
    if pdb_doc:
        write_yaml_atomic(FILES["pdb"], pdb_doc)
    elif FILES["pdb"].exists():
        try:
            FILES["pdb"].unlink()
        except Exception:
            pass
    write_yaml_atomic(FILES["networkpolicy"], netpol_doc)

    inputs_path.write_text(inputs_hash + "\n", encoding="utf-8")
    log.info("Manifests written to %s (inputs_hash=%s)", str(manifests_dir), inputs_hash)
    if verbose:
        log.debug("SA: %s\nDeployment: %s\nService: %s\nNetworkPolicy: %s",
                  sa_doc, dep_doc, svc_doc, netpol_doc)
    return inputs_hash


def apply_to_cluster(dry_run: bool = False) -> None:
    if not shutil.which("kubectl"):
        log.error("kubectl not found; aborting.")
        raise SystemExit(2)

    secret_env = collect_user_secrets()
    _auto_generate_missing_keys(secret_env, NAMESPACE, SECRET_NAME)

    if not USE_IAM:
        aws = _aws_cred_env()
        if aws:
            secret_env.update(aws)

    if ENABLE_GOOGLE_AUTH and (not secret_env.get("GOOGLE_CLIENT_ID") or not secret_env.get("GOOGLE_CLIENT_SECRET")):
        log.warning("Google auth is enabled but GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are missing.")

    generate_manifests(secret_env, dry_run=dry_run)

    if dry_run:
        log.info("Dry-run requested; skipping apply.")
        return

    if secret_env:
        try:
            create_namespace_if_missing(NAMESPACE)
            apply_secret_direct(secret_env)
        except subprocess.CalledProcessError:
            log.error("Failed to apply secrets")
            raise SystemExit(2) from None

    for key in ["serviceaccount", "deployment", "service", "networkpolicy"]:
        apply_yaml(FILES[key])
    pdb_file = FILES["pdb"]
    if pdb_file.exists():
        apply_yaml(pdb_file)

    log.info("Manifests and secrets applied successfully.")
    if FRONTEND_HOSTNAME:
        log.info("Google OAuth redirect URI: https://%s/auth/callback/google", FRONTEND_HOSTNAME)
        log.info("Microsoft OAuth redirect URI: https://%s/auth/callback/microsoft", FRONTEND_HOSTNAME)


def apply_secrets_only(dry_run: bool = False) -> int:
    if not shutil.which("kubectl"):
        log.error("kubectl not found.")
        return 2

    secret_env = collect_user_secrets()
    _auto_generate_missing_keys(secret_env, NAMESPACE, SECRET_NAME)
    if not USE_IAM:
        aws = _aws_cred_env()
        if aws:
            secret_env.update(aws)

    if not secret_env:
        log.warning("No secret values found; nothing to apply.")
        return 0

    if dry_run:
        log.info("Dry-run; not applying secrets.")
        return 0

    try:
        create_namespace_if_missing(NAMESPACE)
        apply_secret_direct(secret_env)
    except subprocess.CalledProcessError:
        log.error("Failed to apply secrets")
        return 2
    return 0


def delete_manifests(delete_secret: bool = False) -> None:
    for key in ["serviceaccount", "deployment", "service", "pdb", "networkpolicy"]:
        path = FILES[key]
        if path.exists():
            try:
                delete_yaml(path)
            except Exception:
                log.debug("Failed to delete %s", path, exc_info=True)

    try:
        if MANIFESTS_DIR.exists():
            for p in sorted(MANIFESTS_DIR.glob("*")):
                if p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        log.debug("Failed to remove %s", p, exc_info=True)
            state_dir = MANIFESTS_DIR / STATE_DIRNAME
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
        delete_secret_direct()


# ---------------------------------------------------------------------------
#  CLI
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
    if args.verbose:
        log.setLevel(logging.DEBUG)

    log.info("Loaded config: namespace=%s deployment=%s replicas=%d image=%s google_auth=%s require_auth=%s frontend_hostname=%s",
             NAMESPACE, DEPLOYMENT_NAME, REPLICAS, IMAGE, ENABLE_GOOGLE_AUTH, REQUIRE_AUTH, FRONTEND_HOSTNAME)

    try:
        if args.write:
            secret_env = collect_user_secrets()
            _auto_generate_missing_keys(secret_env, NAMESPACE, SECRET_NAME)
            generate_manifests(secret_env, dry_run=args.dry_run, verbose=args.verbose)
            return 0
        elif args.apply:
            apply_to_cluster(dry_run=args.dry_run)
            return 0
        elif args.apply_secrets:
            return apply_secrets_only(dry_run=args.dry_run)
        elif args.delete:
            delete_manifests(delete_secret=args.delete_secret)
            return 0
        else:
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