# python3 src/infra/core/signoz_setup.py --apply-secret
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from exc


# Output path for the rendered Argo CD Application manifest
DEFAULT_OUTPUT = Path("src/argocd/signoz-application.yaml")

# Application defaults
DEFAULT_APP_NAME = "signoz"
DEFAULT_APP_NAMESPACE = "argocd"
DEFAULT_PROJECT = "e2e-rag-system"
DEFAULT_DEST_NAMESPACE = "signoz"
DEFAULT_DEST_SERVER = "https://kubernetes.default.svc"

# Chart defaults (updated to latest requested)
DEFAULT_REPO_URL = "https://charts.signoz.io"
DEFAULT_CHART_NAME = "signoz"
DEFAULT_CHART_VERSION = "0.120.0"

DEFAULT_STORAGE_CLASS = "default-storage-class"
DEFAULT_CLUSTER_DOMAIN = "cluster.local"

DEFAULT_SYNC_OPTIONS = [
    "CreateNamespace=true",
    "PrunePropagationPolicy=foreground",
    "PruneLast=true",
]

STAGING = "staging"
PROD = "prod"
SUPPORTED_ENVS = {STAGING, PROD}


class Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str_representer)


def log(*parts: object) -> None:
    print(*parts, flush=True)


def fatal(msg: str, code: int = 2) -> NoReturn:
    log(f"ERROR: {msg}")
    raise SystemExit(code)


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def yaml_dump(data: Any) -> str:
    return yaml.dump(
        data,
        Dumper=Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
        indent=2,
    )


def random_password() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Preset:
    cluster_name: str
    cloud: str
    signoz_replicas: int
    clickhouse_replicas: int
    clickhouse_shards: int
    zookeeper_replicas: int
    signoz_request_cpu: str
    signoz_request_memory: str
    signoz_limit_cpu: str
    signoz_limit_memory: str
    clickhouse_request_cpu: str
    clickhouse_request_memory: str
    clickhouse_limit_cpu: str
    clickhouse_limit_memory: str
    zookeeper_request_cpu: str
    zookeeper_request_memory: str
    zookeeper_limit_cpu: str
    zookeeper_limit_memory: str
    signoz_persistence_size: str
    clickhouse_persistence_size: str


PRESETS: dict[str, Preset] = {
    STAGING: Preset(
        cluster_name="staging-cluster",
        cloud="other",
        signoz_replicas=1,
        clickhouse_replicas=1,
        clickhouse_shards=1,
        zookeeper_replicas=1,
        signoz_request_cpu="50m",
        signoz_request_memory="128Mi",
        signoz_limit_cpu="250m",
        signoz_limit_memory="256Mi",
        clickhouse_request_cpu="100m",
        clickhouse_request_memory="256Mi",
        clickhouse_limit_cpu="500m",
        clickhouse_limit_memory="512Mi",
        zookeeper_request_cpu="100m",
        zookeeper_request_memory="256Mi",
        zookeeper_limit_cpu="500m",
        zookeeper_limit_memory="512Mi",
        signoz_persistence_size="1Gi",
        clickhouse_persistence_size="5Gi",
    ),
    PROD: Preset(
        cluster_name="production-cluster",
        cloud="aws",
        signoz_replicas=1,
        clickhouse_replicas=1,
        clickhouse_shards=1,
        zookeeper_replicas=1,
        signoz_request_cpu="100m",
        signoz_request_memory="256Mi",
        signoz_limit_cpu="500m",
        signoz_limit_memory="512Mi",
        clickhouse_request_cpu="200m",
        clickhouse_request_memory="512Mi",
        clickhouse_limit_cpu="750m",
        clickhouse_limit_memory="1Gi",
        zookeeper_request_cpu="250m",
        zookeeper_request_memory="512Mi",
        zookeeper_limit_cpu="1",
        zookeeper_limit_memory="1536Mi",
        signoz_persistence_size="1Gi",
        clickhouse_persistence_size="10Gi",
    ),
}


@dataclass(frozen=True)
class Config:
    env_name: str
    output: Path

    app_name: str
    app_namespace: str
    project: str

    dest_namespace: str
    dest_server: str

    repo_url: str
    chart_name: str
    chart_version: str

    cluster_domain: str
    cluster_name: str
    cloud: str
    storage_class: str
    inference_namespace: str

    signoz_replicas: int
    clickhouse_replicas: int
    clickhouse_shards: int
    zookeeper_replicas: int

    signoz_request_cpu: str
    signoz_request_memory: str
    signoz_limit_cpu: str
    signoz_limit_memory: str

    clickhouse_request_cpu: str
    clickhouse_request_memory: str
    clickhouse_limit_cpu: str
    clickhouse_limit_memory: str

    zookeeper_request_cpu: str
    zookeeper_request_memory: str
    zookeeper_limit_cpu: str
    zookeeper_limit_memory: str

    signoz_persistence_size: str
    clickhouse_persistence_size: str

    # ClickHouse connection info (we will create a k8s Secret and reference it)
    clickhouse_host: str
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_secret_name: str

    sync_options: list[str]
    retry_limit: int
    retry_backoff_duration: str
    retry_backoff_factor: int
    retry_backoff_max_duration: str

    automated_prune: bool
    automated_self_heal: bool


def detect_env_name() -> str:
    raw = os.environ.get("SIGNOZ_ENV", "prod").strip().lower()
    if raw not in SUPPORTED_ENVS:
        fatal(f"SIGNOZ_ENV must be one of {sorted(SUPPORTED_ENVS)}, got {raw!r}")
    return raw


def preset_for(env_name: str) -> Preset:
    return PRESETS[env_name]


def load_existing_password(output: Path) -> str:
    """
    If an existing rendered Application YAML contains an internal clickhouse.password,
    reuse it to avoid rotating the secret unnecessarily.
    """
    if not output.exists():
        return ""

    try:
        outer = yaml.safe_load(output.read_text(encoding="utf-8")) or {}
        values_text = outer.get("spec", {}).get("source", {}).get("helm", {}).get("values")
        if not isinstance(values_text, str) or not values_text.strip():
            return ""
        inner = yaml.safe_load(values_text) or {}
    except Exception:
        return ""

    clickhouse = inner.get("clickhouse")
    if isinstance(clickhouse, dict):
        pwd = clickhouse.get("password")
        if isinstance(pwd, str) and pwd.strip():
            return pwd.strip()
    return ""


def load_config() -> Config:
    env_name = detect_env_name()
    preset = preset_for(env_name)
    output = Path(env("SIGNOZ_APPLICATION_OUTPUT", str(DEFAULT_OUTPUT)))

    # ClickHouse connection info: host, user, password, secret name
    clickhouse_host = env("SIGNOZ_CLICKHOUSE_HOST", "my-clickhouse.default.svc.cluster.local")
    clickhouse_user = env("SIGNOZ_CLICKHOUSE_USER", "admin")

    # Password precedence: env -> existing rendered file -> random
    password = env("SIGNOZ_CLICKHOUSE_PASSWORD", "")
    if not password:
        password = load_existing_password(output)
    if not password:
        password = random_password()

    clickhouse_secret_name = env("SIGNOZ_CLICKHOUSE_SECRET_NAME", "signoz-clickhouse-auth")

    sync_options = [
        item.strip()
        for item in env("SIGNOZ_SYNC_OPTIONS", ",".join(DEFAULT_SYNC_OPTIONS)).split(",")
        if item.strip()
    ]

    return Config(
        env_name=env_name,
        output=output,
        app_name=env("SIGNOZ_APP_NAME", DEFAULT_APP_NAME),
        app_namespace=env("SIGNOZ_APP_NAMESPACE", DEFAULT_APP_NAMESPACE),
        project=env("SIGNOZ_PROJECT", DEFAULT_PROJECT),
        dest_namespace=env("SIGNOZ_DEST_NAMESPACE", DEFAULT_DEST_NAMESPACE),
        dest_server=env("SIGNOZ_DEST_SERVER", DEFAULT_DEST_SERVER),
        repo_url=env("SIGNOZ_CHART_REPO_URL", DEFAULT_REPO_URL),
        chart_name=env("SIGNOZ_CHART_NAME", DEFAULT_CHART_NAME),
        chart_version=env("SIGNOZ_CHART_VERSION", DEFAULT_CHART_VERSION),
        cluster_domain=env("SIGNOZ_CLUSTER_DOMAIN", DEFAULT_CLUSTER_DOMAIN),
        cluster_name=env("SIGNOZ_CLUSTER_NAME", preset.cluster_name),
        cloud=env("SIGNOZ_CLOUD", preset.cloud),
        storage_class=env("SIGNOZ_STORAGE_CLASS", DEFAULT_STORAGE_CLASS),
        inference_namespace=env("SIGNOZ_INFERENCE_NAMESPACE", "inference"),
        signoz_replicas=env_int("SIGNOZ_REPLICAS", preset.signoz_replicas),
        clickhouse_replicas=env_int("SIGNOZ_CLICKHOUSE_REPLICAS", preset.clickhouse_replicas),
        clickhouse_shards=env_int("SIGNOZ_CLICKHOUSE_SHARDS", preset.clickhouse_shards),
        zookeeper_replicas=env_int("SIGNOZ_ZOOKEEPER_REPLICAS", preset.zookeeper_replicas),
        signoz_request_cpu=env("SIGNOZ_SIGNOZ_REQUEST_CPU", preset.signoz_request_cpu),
        signoz_request_memory=env("SIGNOZ_SIGNOZ_REQUEST_MEMORY", preset.signoz_request_memory),
        signoz_limit_cpu=env("SIGNOZ_SIGNOZ_LIMIT_CPU", preset.signoz_limit_cpu),
        signoz_limit_memory=env("SIGNOZ_SIGNOZ_LIMIT_MEMORY", preset.signoz_limit_memory),
        clickhouse_request_cpu=env("SIGNOZ_CLICKHOUSE_REQUEST_CPU", preset.clickhouse_request_cpu),
        clickhouse_request_memory=env("SIGNOZ_CLICKHOUSE_REQUEST_MEMORY", preset.clickhouse_request_memory),
        clickhouse_limit_cpu=env("SIGNOZ_CLICKHOUSE_LIMIT_CPU", preset.clickhouse_limit_cpu),
        clickhouse_limit_memory=env("SIGNOZ_CLICKHOUSE_LIMIT_MEMORY", preset.clickhouse_limit_memory),
        zookeeper_request_cpu=env("SIGNOZ_ZOOKEEPER_REQUEST_CPU", preset.zookeeper_request_cpu),
        zookeeper_request_memory=env("SIGNOZ_ZOOKEEPER_REQUEST_MEMORY", preset.zookeeper_request_memory),
        zookeeper_limit_cpu=env("SIGNOZ_ZOOKEEPER_LIMIT_CPU", preset.zookeeper_limit_cpu),
        zookeeper_limit_memory=env("SIGNOZ_ZOOKEEPER_LIMIT_MEMORY", preset.zookeeper_limit_memory),
        signoz_persistence_size=env("SIGNOZ_SIGNOZ_PERSISTENCE_SIZE", preset.signoz_persistence_size),
        clickhouse_persistence_size=env("SIGNOZ_CLICKHOUSE_PERSISTENCE_SIZE", preset.clickhouse_persistence_size),
        clickhouse_host=clickhouse_host,
        clickhouse_user=clickhouse_user,
        clickhouse_password=password,
        clickhouse_secret_name=clickhouse_secret_name,
        sync_options=sync_options,
        retry_limit=env_int("SIGNOZ_RETRY_LIMIT", 3),
        retry_backoff_duration=env("SIGNOZ_RETRY_BACKOFF_DURATION", "10s"),
        retry_backoff_factor=env_int("SIGNOZ_RETRY_BACKOFF_FACTOR", 2),
        retry_backoff_max_duration=env("SIGNOZ_RETRY_BACKOFF_MAX_DURATION", "3m"),
        automated_prune=env_bool("SIGNOZ_AUTOMATED_PRUNE", True),
        automated_self_heal=env_bool("SIGNOZ_AUTOMATED_SELF_HEAL", True),
    )


def build_values(cfg: Config) -> dict[str, Any]:
    """
    Build Helm values that match the requested, fixed YAML:
    - minimal global block with clusterName and clusterDomain
    - clickhouse.enabled: false
    - externalClickhouse with existingSecret reference
    - signoz block with persistence using storageClass
    """
    values: dict[str, Any] = {
        "global": {
            "clusterName": cfg.cluster_name,
            "clusterDomain": cfg.cluster_domain,
        },
        "clickhouse": {
            "enabled": False,
        },
        "externalClickhouse": {
            "host": cfg.clickhouse_host,
            "user": cfg.clickhouse_user,
            "existingSecret": cfg.clickhouse_secret_name,
            "existingSecretPasswordKey": "password",
        },
        "signoz": {
            "name": "signoz",
            "replicaCount": cfg.signoz_replicas,
            "env": {
                "signoz_telemetrystore_provider": "clickhouse",
                "signoz_include_only_log_namespaces": cfg.inference_namespace,
            },
            "podSecurityContext": {
                "fsGroup": 1000,
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {
                    "drop": ["ALL"],
                },
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "runAsUser": 1000,
            },
            "resources": {
                "requests": {
                    "cpu": cfg.signoz_request_cpu,
                    "memory": cfg.signoz_request_memory,
                },
                "limits": {
                    "cpu": cfg.signoz_limit_cpu,
                    "memory": cfg.signoz_limit_memory,
                },
            },
            "persistence": {
                "enabled": True,
                "storageClass": cfg.storage_class,
                "accessModes": ["ReadWriteOnce"],
                "size": cfg.signoz_persistence_size,
            },
        },
    }

    # Keep a checksum so the chart can detect config changes if needed
    checksum = hashlib.sha256(yaml_dump(values).encode("utf-8")).hexdigest()
    values.setdefault("global", {})["configChecksum"] = checksum
    return values


def build_application(cfg: Config) -> dict[str, Any]:
    values_yaml = yaml_dump(build_values(cfg)).rstrip() + "\n"

    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": cfg.app_name,
            "namespace": cfg.app_namespace,
            "labels": {
                "app.kubernetes.io/name": cfg.app_name,
                "app.kubernetes.io/managed-by": "argocd",
            },
            "annotations": {
                "description": "SigNoz Helm deployment (chart repo + external ClickHouse preserved)",
                "signoz.argoproj.io/environment": cfg.env_name,
            },
        },
        "spec": {
            "project": cfg.project,
            "source": {
                "repoURL": cfg.repo_url,
                "chart": cfg.chart_name,
                "targetRevision": str(cfg.chart_version),
                "helm": {
                    "values": values_yaml,
                },
            },
            "destination": {
                "server": cfg.dest_server,
                "namespace": cfg.dest_namespace,
            },
            "syncPolicy": {
                "automated": {
                    "prune": cfg.automated_prune,
                    "selfHeal": cfg.automated_self_heal,
                },
                "syncOptions": cfg.sync_options,
            },
        },
    }


def render(cfg: Config) -> str:
    return yaml_dump(build_application(cfg))


def kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


def create_or_update_secret(cfg: Config) -> None:
    """
    Create or update a Kubernetes Secret in the destination namespace using
    stringData so we don't write base64 to disk. This function uses `kubectl apply -f -`.
    """
    if not kubectl_available():
        fatal("kubectl is not available on PATH; cannot create Kubernetes Secret.")

    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": cfg.clickhouse_secret_name,
            "namespace": cfg.dest_namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "setup-signoz-script",
            },
        },
        # Use stringData so kubectl will base64-encode server-side; avoids writing base64 here.
        "stringData": {
            "password": cfg.clickhouse_password,
            "user": cfg.clickhouse_user,
        },
        "type": "Opaque",
    }

    manifest_yaml = yaml_dump(secret_manifest)
    try:
        proc = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=manifest_yaml.encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        fatal(f"Failed to run kubectl: {exc}")

    if proc.returncode != 0:
        log("kubectl apply failed:")
        log(proc.stderr.decode("utf-8").strip())
        fatal("Failed to create/update Kubernetes Secret.")
    else:
        out = proc.stdout.decode("utf-8").strip()
        log(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render the SigNoz Argo CD Application manifest (chart 0.120.0) and optionally create ClickHouse secret.")
    parser.add_argument("--stdout", action="store_true", help="Print the rendered YAML to stdout.")
    parser.add_argument("--write", action="store_true", help="Write the rendered YAML to disk.")
    parser.add_argument("--check", action="store_true", help="Fail if the on-disk file differs from the rendered output.")
    parser.add_argument("--apply-secret", action="store_true", help="Create or update the ClickHouse secret in the cluster before rendering.")
    args = parser.parse_args(argv)

    cfg = load_config()

    if args.apply_secret:
        log(f"Creating/updating Kubernetes Secret '{cfg.clickhouse_secret_name}' in namespace '{cfg.dest_namespace}'")
        create_or_update_secret(cfg)
        log("Secret applied successfully.")

    if not any([args.stdout, args.write, args.check]):
        args.write = True

    rendered = render(cfg)

    if args.stdout:
        sys.stdout.write(rendered)

    if args.write:
        atomic_write_text(cfg.output, rendered)
        log(f"Wrote {cfg.output}")

    if args.check:
        existing = cfg.output.read_text(encoding="utf-8") if cfg.output.exists() else ""
        if existing != rendered:
            fatal(f"{cfg.output} is out of date", 3)
        log(f"OK {cfg.output} is up to date")


if __name__ == "__main__":
    main()
