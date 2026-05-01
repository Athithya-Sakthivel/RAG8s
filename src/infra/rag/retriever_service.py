from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

MANIFEST_DIR = Path("src/manifests/retriever")
STATE_DIR = MANIFEST_DIR / ".state"
RENDERED_PATH = STATE_DIR / "rendered.yaml"
HASH_PATH = STATE_DIR / "rendered.sha256"

SUPPORTED_CLUSTERS = {"kind", "eks"}

DEPLOYMENT_DEFAULTS: dict[str, str] = {
    "NAMESPACE": "inference",
    "DEPLOYMENT_NAME": "retriever",
    "SERVICE_NAME": "retriever",
    "SERVICE_ACCOUNT_NAME": "retriever-sa",
    "SECRET_NAME": "retriever-secrets",
    "IMAGE": "ghcr.io/athithya-sakthivel/retriever:2026-05-01-07-15--813f3ab@sha256:2fff10a209f9f622c66d549b4c7e496444935299f170a896ce15980f8f4019cf",
    "IMAGE_PULL_POLICY": "IfNotPresent",
    "REPLICAS": "1",
    "CONTAINER_PORT": "8001",
    "CPU_REQUEST": "250m",
    "CPU_LIMIT": "1",
    "MEMORY_REQUEST": "512Mi",
    "MEMORY_LIMIT": "1Gi",
    "RUN_AS_USER": "1000",
    "RUN_AS_GROUP": "1000",
    "FS_GROUP": "1000",
    "SERVICE_TYPE": "ClusterIP",
    "IRSA_ROLE_ARN": "",
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
    "AWS_BEDROCK_MODEL_ID": "meta.llama3-8b-instruct-v1:0",
    "LLM_MAX_TOKENS": "400",
    "LLM_TEMPERATURE": "0.0",
    "BEDROCK_GUARDRAIL_IDENTIFIER": "",
    "BEDROCK_GUARDRAIL_VERSION": "",
    "CORPUS_VERSION": "v1",
    "PROMPT_VERSION": "v1",
    "RETRIEVAL_VERSION": "retrieval-v1",
    "TENANT_ID": "",
    "DENSE_DIM": "384",
    "MAX_CHUNKS_TO_LLM": "5",
    "QUERY_TOPK_DENSE": "50",
    "QUERY_TOPK_SPARSE": "50",
    "FETCH_K": "20",
    "RERANKER_TOP_K": "10",
    "RERANKER_MODE": "AUTO",
    "RERANK_AUTO_THRESHOLD": "0.75",
    "RERANK_MARGIN": "0.08",
    "RERANK_ALPHA": "0.6",
    "RRF_K": "60",
    "CACHE_SCORE_THRESHOLD": "0.72",
    "CACHE_TTL_SECONDS": "86400",
    "CACHE_CLEANUP_INTERVAL_SECONDS": "900",
    "PROMPT_MAX_CONTENT_CHARS": "2500",
    "CHUNK_OUTPUT_MAX_CHARS": "1600",
    "MAX_PROMPT_CHARS": "40000",
    "MAX_CONCURRENT_REQUESTS": "64",
    "HTTP_TIMEOUT": "10.0",
    "HTTP_MAX_CONNECTIONS": "100",
    "HTTP_MAX_KEEPALIVE": "20",
    "RETRY_MAX_ATTEMPTS": "3",
    "RETRY_BASE_DELAY": "0.08",
    "RETRY_MAX_DELAY": "0.8",
    "BREAKER_FAILURE_THRESHOLD": "3",
    "BREAKER_RESET_TIMEOUT": "20.0",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://signoz-otel-collector.signoz.svc.cluster.local:4317",
    "OTEL_TIMEOUT_SECONDS": "5.0",
    "OTEL_METRIC_EXPORT_INTERVAL_MS": "15000",
    "OTEL_METRIC_EXPORT_TIMEOUT_MS": "10000",
    "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
    "OTEL_TRACES_SAMPLER_ARG": "0.10",
    "LOG_LEVEL": "WARNING",
    "ENABLE_OTEL_TRACES": "true",
    "ENABLE_OTEL_METRICS": "true",
    "ENABLE_OTEL_LOGS": "true",
    "AUTH_VALIDATE_URL": "http://auth-svc.inference.svc.cluster.local:8000/me",
    "AUTH_TIMEOUT_SECONDS": "2.0",
    "PORT": "8001",
    "UVICORN_LOOP": "uvloop",
    "UVICORN_HTTP": "httptools",
    "FORWARDED_ALLOW_IPS": "*",
    "USE_IAM": "false",
    "SEMANTIC_CACHE_SCORE_THRESHOLD": "",
    "SEMANTIC_CACHE_RELAXED_SCORE_THRESHOLD": "",
    "RERANKER_MODEL": "cross-encoder",
}

APP_ENV_ORDER = list(APP_ENV_DEFAULTS.keys())


def log(level: str, event: str, message: str, **fields: Any) -> None:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    payload = [f"ts={ts}", f"level={level.upper()}", f"event={event}", f"message={message}"]
    for key, value in fields.items():
        payload.append(f"{key}={value}")
    print(" ".join(payload), file=sys.stderr)


@dataclass(frozen=True, slots=True)
class DeploymentSettings:
    cluster: str
    namespace: str
    deployment_name: str
    service_name: str
    service_account_name: str
    secret_name: str
    image: str
    image_pull_policy: str
    replicas: int
    container_port: int
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    run_as_user: int
    run_as_group: int
    fs_group: int
    service_type: str
    use_iam: bool
    irsa_role_arn: str | None

    def __post_init__(self) -> None:
        if self.cluster not in SUPPORTED_CLUSTERS:
            raise RuntimeError(f"K8S_CLUSTER must be one of {sorted(SUPPORTED_CLUSTERS)}, got {self.cluster!r}")
        if not self.namespace.strip():
            raise RuntimeError("NAMESPACE must not be empty")
        if not self.deployment_name.strip():
            raise RuntimeError("DEPLOYMENT_NAME must not be empty")
        if not self.service_name.strip():
            raise RuntimeError("SERVICE_NAME must not be empty")
        if not self.service_account_name.strip():
            raise RuntimeError("SERVICE_ACCOUNT_NAME must not be empty")
        if not self.secret_name.strip():
            raise RuntimeError("SECRET_NAME must not be empty")
        if not self.image.strip():
            raise RuntimeError("IMAGE must not be empty")
        if self.replicas < 1:
            raise RuntimeError("REPLICAS must be >= 1")
        if self.container_port < 1:
            raise RuntimeError("CONTAINER_PORT must be >= 1")
        if not self.cpu_request.strip():
            raise RuntimeError("CPU_REQUEST must not be empty")
        if not self.cpu_limit.strip():
            raise RuntimeError("CPU_LIMIT must not be empty")
        if not self.memory_request.strip():
            raise RuntimeError("MEMORY_REQUEST must not be empty")
        if not self.memory_limit.strip():
            raise RuntimeError("MEMORY_LIMIT must not be empty")
        if self.run_as_user < 1:
            raise RuntimeError("RUN_AS_USER must be >= 1")
        if self.run_as_group < 1:
            raise RuntimeError("RUN_AS_GROUP must be >= 1")
        if self.fs_group < 1:
            raise RuntimeError("FS_GROUP must be >= 1")
        if self.cluster == "kind" and self.use_iam:
            raise RuntimeError("USE_IAM=true is not supported for K8S_CLUSTER=kind")
        if self.cluster == "eks" and self.use_iam and not self.irsa_role_arn:
            raise RuntimeError("IRSA_ROLE_ARN is required when K8S_CLUSTER=eks and USE_IAM=true")


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
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _require_nonempty(value: str | None, message: str) -> str:
    if value is None or not value.strip():
        raise RuntimeError(message)
    return value.strip()


def _cluster() -> str:
    raw = os.getenv("K8S_CLUSTER", "kind").strip().lower()
    if raw not in SUPPORTED_CLUSTERS:
        raise RuntimeError(f"K8S_CLUSTER must be one of {sorted(SUPPORTED_CLUSTERS)}, got {raw!r}")
    return raw


def _default_use_iam(cluster: str) -> bool:
    return cluster == "eks"


def load_settings() -> tuple[DeploymentSettings, dict[str, str], dict[str, str]]:
    cluster = _cluster()
    use_iam = _env_bool("USE_IAM", _default_use_iam(cluster))

    app_env: dict[str, str] = {}
    for name in APP_ENV_ORDER:
        value = _env_str(name, APP_ENV_DEFAULTS[name])
        if name == "LOG_LEVEL":
            value = value.upper()
            if value == "WARN":
                value = "WARNING"
        if name == "CACHE_COLLECTION_NAME" and not value:
            value = f"{_env_str('COLLECTION_NAME', APP_ENV_DEFAULTS['COLLECTION_NAME'])}__semantic_cache"
        if name in {"ENABLE_OTEL_TRACES", "ENABLE_OTEL_METRICS", "ENABLE_OTEL_LOGS"}:
            value = value.lower()
        app_env[name] = value

    app_env["USE_IAM"] = "true" if use_iam else "false"

    secret_env: dict[str, str] = {}
    qdrant_key = os.getenv("QDRANT_API_KEY", "").strip()
    if qdrant_key:
        secret_env["QDRANT_API_KEY"] = qdrant_key

    if not use_iam:
        secret_env["AWS_ACCESS_KEY_ID"] = _require_nonempty(
            os.getenv("AWS_ACCESS_KEY_ID"),
            "AWS_ACCESS_KEY_ID is required when USE_IAM=false",
        )
        secret_env["AWS_SECRET_ACCESS_KEY"] = _require_nonempty(
            os.getenv("AWS_SECRET_ACCESS_KEY"),
            "AWS_SECRET_ACCESS_KEY is required when USE_IAM=false",
        )

    settings = DeploymentSettings(
        cluster=cluster,
        namespace=_env_str("NAMESPACE", DEPLOYMENT_DEFAULTS["NAMESPACE"]),
        deployment_name=_env_str("DEPLOYMENT_NAME", DEPLOYMENT_DEFAULTS["DEPLOYMENT_NAME"]),
        service_name=_env_str("SERVICE_NAME", DEPLOYMENT_DEFAULTS["SERVICE_NAME"]),
        service_account_name=_env_str("SERVICE_ACCOUNT_NAME", DEPLOYMENT_DEFAULTS["SERVICE_ACCOUNT_NAME"]),
        secret_name=_env_str("SECRET_NAME", DEPLOYMENT_DEFAULTS["SECRET_NAME"]),
        image=_env_str("IMAGE", DEPLOYMENT_DEFAULTS["IMAGE"]),
        image_pull_policy=_env_str("IMAGE_PULL_POLICY", DEPLOYMENT_DEFAULTS["IMAGE_PULL_POLICY"]),
        replicas=_env_int("REPLICAS", int(DEPLOYMENT_DEFAULTS["REPLICAS"])),
        container_port=_env_int("CONTAINER_PORT", int(DEPLOYMENT_DEFAULTS["CONTAINER_PORT"])),
        cpu_request=_env_str("CPU_REQUEST", DEPLOYMENT_DEFAULTS["CPU_REQUEST"]),
        cpu_limit=_env_str("CPU_LIMIT", DEPLOYMENT_DEFAULTS["CPU_LIMIT"]),
        memory_request=_env_str("MEMORY_REQUEST", DEPLOYMENT_DEFAULTS["MEMORY_REQUEST"]),
        memory_limit=_env_str("MEMORY_LIMIT", DEPLOYMENT_DEFAULTS["MEMORY_LIMIT"]),
        run_as_user=_env_int("RUN_AS_USER", int(DEPLOYMENT_DEFAULTS["RUN_AS_USER"])),
        run_as_group=_env_int("RUN_AS_GROUP", int(DEPLOYMENT_DEFAULTS["RUN_AS_GROUP"])),
        fs_group=_env_int("FS_GROUP", int(DEPLOYMENT_DEFAULTS["FS_GROUP"])),
        service_type=_env_str("SERVICE_TYPE", DEPLOYMENT_DEFAULTS["SERVICE_TYPE"]),
        use_iam=use_iam,
        irsa_role_arn=os.getenv("IRSA_ROLE_ARN", DEPLOYMENT_DEFAULTS["IRSA_ROLE_ARN"]).strip() or None,
    )

    return settings, app_env, secret_env


def _base_labels(settings: DeploymentSettings) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": settings.service_name,
        "app.kubernetes.io/instance": settings.deployment_name,
        "app.kubernetes.io/managed-by": "retriever-manifest-generator",
        "app.kubernetes.io/component": "retriever",
    }


def _pod_labels(settings: DeploymentSettings) -> dict[str, str]:
    labels = _base_labels(settings).copy()
    labels["app.kubernetes.io/part-of"] = settings.service_name
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

    data["SERVICE_NAME"] = app_env["SERVICE_NAME"]
    data["OTEL_SERVICE_NAME"] = app_env["OTEL_SERVICE_NAME"]
    data["SERVICE_VERSION"] = app_env["SERVICE_VERSION"]
    data["OTEL_SERVICE_VERSION"] = app_env["OTEL_SERVICE_VERSION"]
    data["ENV"] = app_env["ENV"]
    data["DEPLOYMENT_ENVIRONMENT"] = app_env["DEPLOYMENT_ENVIRONMENT"]
    data["CLUSTER_NAME"] = app_env["CLUSTER_NAME"]
    data["K8S_CLUSTER_NAME"] = app_env["K8S_CLUSTER_NAME"]
    data["AWS_REGION"] = app_env["AWS_REGION"]
    data["AWS_DEFAULT_REGION"] = app_env["AWS_DEFAULT_REGION"]
    data["CACHE_COLLECTION_NAME"] = app_env["CACHE_COLLECTION_NAME"]
    return data


def build_service_account_doc(settings: DeploymentSettings) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": settings.service_account_name,
        "namespace": settings.namespace,
        "labels": _base_labels(settings),
    }
    if settings.use_iam:
        metadata["annotations"] = {
            "eks.amazonaws.com/role-arn": _require_nonempty(
                settings.irsa_role_arn,
                "IRSA_ROLE_ARN is required when USE_IAM=true on EKS",
            )
        }
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": metadata,
    }


def build_configmap_doc(settings: DeploymentSettings, app_env: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": settings.deployment_name,
            "namespace": settings.namespace,
            "labels": _base_labels(settings),
        },
        "data": _configmap_data(app_env),
    }


def build_secret_doc(settings: DeploymentSettings, secret_env: dict[str, str]) -> None:
    if not secret_env:
        log("info", "secret.skip", "no secret data to apply", secret_name=settings.secret_name)
        return

    log("info", "secret.apply.start", "applying secret", secret_name=settings.secret_name, secret_keys=sorted(secret_env.keys()))
    cmd = ["kubectl", "create", "secret", "generic", settings.secret_name, "-n", settings.namespace]
    for key, value in sorted(secret_env.items()):
        cmd.extend(["--from-literal", f"{key}={value}"])
    cmd.extend(["--dry-run=client", "-o", "yaml"])
    rendered = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    subprocess.run(["kubectl", "apply", "-f", "-"], input=rendered, text=True, check=True)
    log("info", "secret.apply.done", "secret applied", secret_name=settings.secret_name)


def _shared_mounts() -> list[dict[str, Any]]:
    return [{"name": "tmp", "mountPath": "/tmp"}]


def _shared_volumes() -> list[dict[str, Any]]:
    return [{"name": "tmp", "emptyDir": {}}]


def _probe_http(path: str, port: int) -> dict[str, Any]:
    return {
        "httpGet": {"path": path, "port": port, "scheme": "HTTP"},
        "initialDelaySeconds": 0,
        "timeoutSeconds": 5,
        "periodSeconds": 5,
        "failureThreshold": 12,
        "successThreshold": 1,
    }


def _secret_key_ref(name: str, secret_name: str, key: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {
                "name": secret_name,
                "key": key or name,
            }
        },
    }


def _container_env(settings: DeploymentSettings, app_env: dict[str, str], secret_env: dict[str, str]) -> list[dict[str, Any]]:
    env: list[dict[str, Any]] = [
        {"name": "INSTANCE_ID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "USE_IAM", "value": app_env["USE_IAM"]},
    ]

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "QDRANT_API_KEY"):
        if name in secret_env:
            env.append(_secret_key_ref(name, settings.secret_name))

    for name in APP_ENV_ORDER:
        if name in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "QDRANT_API_KEY", "USE_IAM", "INSTANCE_ID"}:
            continue
        env.append({"name": name, "value": app_env[name]})

    return env


def build_deployment_doc(
    settings: DeploymentSettings,
    app_env: dict[str, str],
    secret_env: dict[str, str],
    config_hash: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": settings.deployment_name,
            "namespace": settings.namespace,
            "labels": _base_labels(settings),
            "annotations": {"retriever.io/config-sha256": config_hash},
        },
        "spec": {
            "replicas": settings.replicas,
            "revisionHistoryLimit": 3,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": settings.service_name,
                    "app.kubernetes.io/instance": settings.deployment_name,
                    "app.kubernetes.io/component": "retriever",
                }
            },
            "template": {
                "metadata": {
                    "labels": _pod_labels(settings),
                    "annotations": {"retriever.io/config-sha256": config_hash},
                },
                "spec": {
                    "serviceAccountName": settings.service_account_name,
                    "automountServiceAccountToken": True,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": settings.run_as_user,
                        "runAsGroup": settings.run_as_group,
                        "fsGroup": settings.fs_group,
                    },
                    "volumes": _shared_volumes(),
                    "containers": [
                        {
                            "name": "retriever",
                            "image": settings.image,
                            "imagePullPolicy": settings.image_pull_policy,
                            "ports": [{"name": "http", "containerPort": settings.container_port, "protocol": "TCP"}],
                            "env": _container_env(settings, app_env, secret_env),
                            "volumeMounts": _shared_mounts(),
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "resources": {
                                "requests": {"cpu": settings.cpu_request, "memory": settings.memory_request},
                                "limits": {"cpu": settings.cpu_limit, "memory": settings.memory_limit},
                            },
                            "readinessProbe": _probe_http("/readyz", settings.container_port),
                            "livenessProbe": _probe_http("/healthz", settings.container_port),
                            "startupProbe": _probe_http("/healthz", settings.container_port),
                        }
                    ],
                },
            },
        },
    }


def build_service_doc(settings: DeploymentSettings) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": settings.service_name,
            "namespace": settings.namespace,
            "labels": _base_labels(settings),
        },
        "spec": {
            "type": settings.service_type,
            "selector": {
                "app.kubernetes.io/name": settings.service_name,
                "app.kubernetes.io/instance": settings.deployment_name,
                "app.kubernetes.io/component": "retriever",
            },
            "ports": [{"name": "http", "port": settings.container_port, "targetPort": settings.container_port, "protocol": "TCP"}],
        },
    }


def build_pdb_doc(settings: DeploymentSettings) -> dict[str, Any] | None:
    if settings.replicas < 2:
        return None
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {
            "name": f"{settings.deployment_name}-pdb",
            "namespace": settings.namespace,
            "labels": _base_labels(settings),
        },
        "spec": {
            "minAvailable": 1,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": settings.service_name,
                    "app.kubernetes.io/instance": settings.deployment_name,
                    "app.kubernetes.io/component": "retriever",
                }
            },
        },
    }


def build_documents(
    settings: DeploymentSettings,
    app_env: dict[str, str],
    secret_env: dict[str, str],
    config_hash: str,
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = [
        build_service_account_doc(settings),
        build_configmap_doc(settings, app_env),
        build_deployment_doc(settings, app_env, secret_env, config_hash),
        build_service_doc(settings),
    ]
    pdb_doc = build_pdb_doc(settings)
    if pdb_doc is not None:
        docs.append(pdb_doc)
    return docs


def render_documents(docs: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False, explicit_start=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_secret(secret_env: dict[str, str]) -> str:
    payload = json.dumps(sorted(secret_env.items()), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def write_rendered_files(docs: list[dict[str, Any]], rendered_yaml: str, digest: str) -> None:
    ensure_state_dir()
    for child in MANIFEST_DIR.glob("*.yaml"):
        try:
            child.unlink()
        except Exception:
            pass

    for idx, doc in enumerate(docs, start=1):
        kind = str(doc.get("kind", "doc")).lower()
        name = str(doc.get("metadata", {}).get("name", f"{idx:02d}")).lower().replace("/", "-")
        path = MANIFEST_DIR / f"{idx:02d}-{kind}-{name}.yaml"
        path.write_text(render_documents([doc]), encoding="utf-8")

    RENDERED_PATH.write_text(rendered_yaml, encoding="utf-8")
    HASH_PATH.write_text(digest + "\n", encoding="utf-8")


def write_temp_yaml(text: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    try:
        tmp.write(text)
        tmp.flush()
        return Path(tmp.name)
    finally:
        tmp.close()


def kubectl_apply(path: Path) -> None:
    subprocess.run(["kubectl", "apply", "-f", str(path)], check=True)


def kubectl_delete(path: Path) -> None:
    subprocess.run(["kubectl", "delete", "-f", str(path), "--ignore-not-found"], check=True)


def namespace_phase(namespace: str) -> str | None:
    try:
        out = subprocess.check_output(["kubectl", "get", "ns", namespace, "-o", "json"], text=True)
    except subprocess.CalledProcessError:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    return str(data.get("status", {}).get("phase") or "").strip() or None


def ensure_namespace_ready(settings: DeploymentSettings) -> None:
    phase = namespace_phase(settings.namespace)
    if phase is None:
        raise RuntimeError(
            f"namespace {settings.namespace!r} does not exist; create it outside this subsystem before rollout"
        )
    if phase == "Terminating":
        raise RuntimeError(
            f"namespace {settings.namespace!r} is terminating; this subsystem will not touch namespaces"
        )


def apply_secret(settings: DeploymentSettings, secret_env: dict[str, str]) -> None:
    if not secret_env:
        log("info", "secret.skip", "no secret data to apply", secret_name=settings.secret_name)
        return

    log("info", "secret.apply.start", "applying secret", secret_name=settings.secret_name, secret_keys=sorted(secret_env.keys()))
    cmd = ["kubectl", "create", "secret", "generic", settings.secret_name, "-n", settings.namespace]
    for key, value in sorted(secret_env.items()):
        cmd.extend(["--from-literal", f"{key}={value}"])
    cmd.extend(["--dry-run=client", "-o", "yaml"])
    rendered = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    subprocess.run(["kubectl", "apply", "-f", "-"], input=rendered, text=True, check=True)
    log("info", "secret.apply.done", "secret applied", secret_name=settings.secret_name)


def delete_secret(settings: DeploymentSettings) -> None:
    log("info", "secret.delete.start", "deleting secret", secret_name=settings.secret_name)
    subprocess.run(
        ["kubectl", "delete", "secret", settings.secret_name, "-n", settings.namespace, "--ignore-not-found"],
        check=True,
    )
    log("info", "secret.delete.done", "secret deleted", secret_name=settings.secret_name)


def rollout() -> int:
    settings, app_env, secret_env = load_settings()

    ensure_namespace_ready(settings)

    config_material = {
        "app_env": app_env,
        "secret_hash": sha256_secret(secret_env),
        "image": settings.image,
        "replicas": settings.replicas,
        "service_account_name": settings.service_account_name,
        "namespace": settings.namespace,
        "use_iam": settings.use_iam,
        "irsa_role_arn": settings.irsa_role_arn,
    }
    config_hash = sha256_text(json.dumps(config_material, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    previous_hash = HASH_PATH.read_text(encoding="utf-8").strip() if HASH_PATH.exists() else ""
    if previous_hash == config_hash:
        log("info", "rollout.noop", "configuration unchanged; skipping apply", config_hash=config_hash)
        return 0

    docs = build_documents(settings, app_env, secret_env, config_hash)
    rendered = render_documents(docs)
    write_rendered_files(docs, rendered, config_hash)

    log("info", "rollout.start", "applying retriever manifests", namespace=settings.namespace, deployment=settings.deployment_name, service=settings.service_name)
    kubectl_apply(write_temp_yaml(render_documents([build_service_account_doc(settings)])))
    apply_secret(settings, secret_env)
    kubectl_apply(write_temp_yaml(render_documents([build_configmap_doc(settings, app_env)])))
    kubectl_apply(write_temp_yaml(render_documents([build_deployment_doc(settings, app_env, secret_env, config_hash)])))
    kubectl_apply(write_temp_yaml(render_documents([build_service_doc(settings)])))

    pdb_doc = build_pdb_doc(settings)
    if pdb_doc is not None:
        kubectl_apply(write_temp_yaml(render_documents([pdb_doc])))

    log("info", "rollout.done", "rollout complete", config_hash=config_hash, rendered=str(MANIFEST_DIR))
    return 0


def delete() -> int:
    settings, app_env, _ = load_settings()

    log("info", "delete.start", "deleting retriever resources", namespace=settings.namespace, deployment=settings.deployment_name)

    docs = [
        build_service_doc(settings),
        build_configmap_doc(settings, app_env),
        build_deployment_doc(settings, app_env, {}, "delete"),
        build_service_account_doc(settings),
    ]
    pdb_doc = build_pdb_doc(settings)
    if pdb_doc is not None:
        docs.append(pdb_doc)

    kubectl_delete(write_temp_yaml(render_documents(docs)))
    delete_secret(settings)

    for path in (RENDERED_PATH, HASH_PATH):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for child in MANIFEST_DIR.glob("*.yaml"):
        try:
            child.unlink()
        except Exception:
            pass

    log("info", "delete.done", "delete complete", namespace=settings.namespace)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="retriever_manifests.py",
        description="Render and apply Kubernetes manifests for the retriever service.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--rollout", action="store_true", help="Render and apply manifests idempotently")
    group.add_argument("--delete", action="store_true", help="Delete retriever resources only")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.rollout:
            return rollout()
        if args.delete:
            return delete()
        return 1
    except subprocess.CalledProcessError as exc:
        log("error", "kubectl.failed", "kubectl command failed", returncode=exc.returncode)
        return exc.returncode or 1
    except Exception as exc:
        log("error", "fatal", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
