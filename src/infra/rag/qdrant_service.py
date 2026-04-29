#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
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


DEFAULT_OUTPUT = Path("src/argocd/qdrant-application.yaml")

DEFAULT_APP_NAME = "qdrant"
DEFAULT_APP_NAMESPACE = "argocd"
DEFAULT_PROJECT = "e2e-rag-system"
DEFAULT_DEST_NAMESPACE = "qdrant"
DEFAULT_DEST_SERVER = "https://kubernetes.default.svc"

DEFAULT_REPO_URL = "https://qdrant.github.io/qdrant-helm"
DEFAULT_CHART_NAME = "qdrant"
DEFAULT_CHART_VERSION = "v1.17.1"

DEFAULT_IMAGE_REPO = "docker.io/qdrant/qdrant"
DEFAULT_IMAGE_TAG = "v1.17.1"
DEFAULT_IMAGE_PULL_POLICY = "IfNotPresent"

DEFAULT_REPLICA_COUNT = 1
DEFAULT_PERSISTENCE_ENABLED = True
DEFAULT_PERSISTENCE_SIZE = "20Gi"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_ON_DISK_PAYLOAD = False

DEFAULT_CPU_REQUEST = "1"
DEFAULT_CPU_LIMIT = "1"
DEFAULT_MEMORY_REQUEST = "2Gi"
DEFAULT_MEMORY_LIMIT = "2Gi"

DEFAULT_CLUSTER_P2P_PORT = 6335
DEFAULT_CLUSTER_CONSENSUS_TICK_MS = 100
DEFAULT_PRESTOP_SLEEP_SECONDS = 3

DEFAULT_SYNC_OPTIONS = [
    "CreateNamespace=true",
    "PrunePropagationPolicy=foreground",
    "PruneLast=true",
]

VERBOSE = os.environ.get("VERBOSE", "0").strip().lower() in {"1", "true", "yes", "y", "on"}


class Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str_representer)


def log(*parts: object) -> None:
    print(*parts, flush=True)


def dbg(*parts: object) -> None:
    if VERBOSE:
        log(*parts)


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


@dataclass(frozen=True)
class Config:
    output: Path

    app_name: str
    app_namespace: str
    project: str
    dest_server: str
    dest_namespace: str

    repo_url: str
    chart_name: str
    chart_version: str

    image_repo: str
    image_tag: str
    image_pull_policy: str

    replica_count: int
    persistence_enabled: bool
    persistence_size: str
    log_level: str
    on_disk_payload: bool

    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str

    cluster_enabled: bool
    cluster_p2p_port: int
    cluster_consensus_tick_ms: int
    service_enable_tls: bool

    update_volume_fs_ownership: bool
    pod_management_policy: str
    pre_stop_sleep_seconds: int

    automated_prune: bool
    automated_self_heal: bool
    sync_options: list[str]
    retry_limit: int
    retry_backoff_duration: str
    retry_backoff_factor: int
    retry_backoff_max_duration: str

    secret_name: str
    secret_key: str
    api_key: str


def load_config() -> Config:
    return Config(
        output=Path(env("QDRANT_APPLICATION_OUTPUT", str(DEFAULT_OUTPUT))),
        app_name=env("QDRANT_APP_NAME", DEFAULT_APP_NAME),
        app_namespace=env("QDRANT_APP_NAMESPACE", DEFAULT_APP_NAMESPACE),
        project=env("QDRANT_PROJECT", DEFAULT_PROJECT),
        dest_server=env("QDRANT_DEST_SERVER", DEFAULT_DEST_SERVER),
        dest_namespace=env("QDRANT_DEST_NAMESPACE", DEFAULT_DEST_NAMESPACE),
        repo_url=env("QDRANT_CHART_REPO_URL", DEFAULT_REPO_URL),
        chart_name=env("QDRANT_CHART_NAME", DEFAULT_CHART_NAME),
        chart_version=env("QDRANT_CHART_VERSION", DEFAULT_CHART_VERSION),
        image_repo=env("QDRANT_IMAGE_REPO", DEFAULT_IMAGE_REPO),
        image_tag=env("QDRANT_IMAGE_TAG", DEFAULT_IMAGE_TAG),
        image_pull_policy=env("QDRANT_IMAGE_PULL_POLICY", DEFAULT_IMAGE_PULL_POLICY),
        replica_count=env_int("QDRANT_REPLICA_COUNT", DEFAULT_REPLICA_COUNT),
        persistence_enabled=env_bool("QDRANT_PERSISTENCE_ENABLED", DEFAULT_PERSISTENCE_ENABLED),
        persistence_size=env("QDRANT_PERSISTENCE_SIZE", DEFAULT_PERSISTENCE_SIZE),
        log_level=env("QDRANT_LOG_LEVEL", DEFAULT_LOG_LEVEL),
        on_disk_payload=env_bool("QDRANT_ON_DISK_PAYLOAD", DEFAULT_ON_DISK_PAYLOAD),
        cpu_request=env("QDRANT_CPU_REQUEST", DEFAULT_CPU_REQUEST),
        cpu_limit=env("QDRANT_CPU_LIMIT", DEFAULT_CPU_LIMIT),
        memory_request=env("QDRANT_MEMORY_REQUEST", DEFAULT_MEMORY_REQUEST),
        memory_limit=env("QDRANT_MEMORY_LIMIT", DEFAULT_MEMORY_LIMIT),
        cluster_enabled=env_bool("QDRANT_CLUSTER_ENABLED", True),
        cluster_p2p_port=env_int("QDRANT_CLUSTER_P2P_PORT", DEFAULT_CLUSTER_P2P_PORT),
        cluster_consensus_tick_ms=env_int("QDRANT_CLUSTER_CONSENSUS_TICK_MS", DEFAULT_CLUSTER_CONSENSUS_TICK_MS),
        service_enable_tls=env_bool("QDRANT_SERVICE_ENABLE_TLS", False),
        update_volume_fs_ownership=env_bool("QDRANT_UPDATE_VOLUME_FS_OWNERSHIP", True),
        pod_management_policy=env("QDRANT_POD_MANAGEMENT_POLICY", "Parallel"),
        pre_stop_sleep_seconds=env_int("QDRANT_PRESTOP_SLEEP_SECONDS", DEFAULT_PRESTOP_SLEEP_SECONDS),
        automated_prune=env_bool("QDRANT_AUTOMATED_PRUNE", True),
        automated_self_heal=env_bool("QDRANT_AUTOMATED_SELF_HEAL", True),
        sync_options=[
            item.strip()
            for item in env("QDRANT_SYNC_OPTIONS", ",".join(DEFAULT_SYNC_OPTIONS)).split(",")
            if item.strip()
        ],
        retry_limit=env_int("QDRANT_RETRY_LIMIT", 3),
        retry_backoff_duration=env("QDRANT_RETRY_BACKOFF_DURATION", "10s"),
        retry_backoff_factor=env_int("QDRANT_RETRY_BACKOFF_FACTOR", 2),
        retry_backoff_max_duration=env("QDRANT_RETRY_BACKOFF_MAX_DURATION", "3m"),
        secret_name=env("QDRANT_SECRET_NAME", "qdrant-service-creds"),
        secret_key=env("QDRANT_SECRET_KEY", "QDRANT__SERVICE__API_KEY"),
        api_key=env("QDRANT_API_KEY", ""),
    )


def build_values(cfg: Config) -> dict[str, Any]:
    values: dict[str, Any] = {
        "replicaCount": cfg.replica_count,
        "image": {
            "repository": cfg.image_repo,
            "tag": cfg.image_tag,
            "pullPolicy": cfg.image_pull_policy,
        },
        "service": {
            "type": "ClusterIP",
        },
        "persistence": {
            "enabled": cfg.persistence_enabled,
            "size": cfg.persistence_size,
            "accessModes": ["ReadWriteOnce"],
        },
        "resources": {
            "requests": {"cpu": cfg.cpu_request, "memory": cfg.memory_request},
            "limits": {"cpu": cfg.cpu_limit, "memory": cfg.memory_limit},
        },
        "config": {
            "cluster": {
                "enabled": cfg.cluster_enabled,
                "p2p": {
                    "port": cfg.cluster_p2p_port,
                    "enable_tls": False,
                },
                "consensus": {
                    "tick_period_ms": cfg.cluster_consensus_tick_ms,
                },
            },
            "service": {
                "enable_tls": cfg.service_enable_tls,
            },
            "log_level": cfg.log_level,
            "on_disk_payload": cfg.on_disk_payload,
        },
        "updateVolumeFsOwnership": cfg.update_volume_fs_ownership,
        "podManagementPolicy": cfg.pod_management_policy,
        "lifecycle": {
            "preStop": {
                "exec": {
                    "command": ["sleep", str(cfg.pre_stop_sleep_seconds)],
                }
            }
        },
    }

    if cfg.api_key:
        values["env"] = [
            {
                "name": cfg.secret_key,
                "valueFrom": {
                    "secretKeyRef": {
                        "name": cfg.secret_name,
                        "key": cfg.secret_key,
                    }
                },
            }
        ]

    checksum = hashlib.sha256(yaml_dump(values).encode("utf-8")).hexdigest()
    values["podAnnotations"] = {
        "app.kubernetes.io/managed-by": "argocd",
        "qdrant.argoproj.io/config-checksum": checksum,
    }
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
                "description": "Qdrant Helm deployment",
            },
        },
        "spec": {
            "project": cfg.project,
            "source": {
                "repoURL": cfg.repo_url,
                "chart": cfg.chart_name,
                "targetRevision": cfg.chart_version,
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
                "retry": {
                    "limit": cfg.retry_limit,
                    "backoff": {
                        "duration": cfg.retry_backoff_duration,
                        "factor": cfg.retry_backoff_factor,
                        "maxDuration": cfg.retry_backoff_max_duration,
                    },
                },
            },
            "ignoreDifferences": [
                {
                    "group": "apiextensions.k8s.io",
                    "kind": "CustomResourceDefinition",
                    "jsonPointers": ["/spec/versions"],
                },
                {
                    "group": "",
                    "kind": "ConfigMap",
                    "jsonPointers": ["/data"],
                },
            ],
        },
    }


def render(cfg: Config) -> str:
    return yaml_dump(build_application(cfg))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render the Qdrant Argo CD Application manifest.")
    parser.add_argument("--stdout", action="store_true", help="Print the rendered YAML to stdout.")
    parser.add_argument("--write", action="store_true", help="Write the rendered YAML to disk.")
    parser.add_argument("--check", action="store_true", help="Fail if the on-disk file differs from the rendered output.")
    args = parser.parse_args(argv)

    cfg = load_config()

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