#!/usr/bin/env python3
"""
signoz_setup.py

Fully automated helper to:
 - create/update required Kubernetes Secrets for SigNoz (ClickHouse auth + JWT secret)
 - render a commit-safe Argo CD Application YAML that references those secrets (no plaintext)
 - optionally restart SigNoz controllers so pods pick up newly-created secrets

Intended usage (example):
  python3 signoz_setup.py --apply-secret

Environment variables (optional):
  SIGNOZ_APPLICATION_OUTPUT        path to write rendered Application YAML (default: src/argocd/signoz-application.yaml)
  SIGNOZ_APP_NAME                  ArgoCD Application name (default: signoz)
  SIGNOZ_APP_NAMESPACE             ArgoCD Application namespace (default: argocd)
  SIGNOZ_PROJECT                   ArgoCD project (default: e2e-rag-system)
  SIGNOZ_DEST_NAMESPACE            destination namespace for SigNoz (default: signoz)
  SIGNOZ_DEST_SERVER               destination server (default: https://kubernetes.default.svc)
  SIGNOZ_CHART_REPO_URL            Helm repo URL (default: https://charts.signoz.io)
  SIGNOZ_CHART_NAME                Helm chart name (default: signoz)
  SIGNOZ_CHART_VERSION             Helm chart version (default: 0.120.0)
  SIGNOZ_CLICKHOUSE_HOST           external ClickHouse host (default: my-clickhouse.default.svc.cluster.local)
  SIGNOZ_CLICKHOUSE_USER           ClickHouse user (default: admin)
  SIGNOZ_CLICKHOUSE_PASSWORD       ClickHouse password (if not set, a random password is generated)
  SIGNOZ_CLICKHOUSE_SECRET_NAME    Secret name for ClickHouse creds (default: signoz-clickhouse-auth)
  SIGNOZ_JWT_SECRET                JWT secret value (if not set, a random secret is generated)
  SIGNOZ_JWT_SECRET_NAME           Secret name for JWT secret (default: signoz-jwt-secret)
  SIGNOZ_STORAGE_CLASS             storage class for persistence (default: default-storage-class)
  SIGNOZ_CLUSTER_NAME              cluster name (default: production-cluster)
  SIGNOZ_CLUSTER_DOMAIN            cluster domain (default: cluster.local)

Notes:
 - This script requires `kubectl` on PATH and configured to the target cluster.
 - The rendered Application YAML is safe to commit: it references secrets by name/key.
 - By default, when run with --apply-secret the script will create/update secrets,
   write the Application YAML, and restart SigNoz controllers so pods re-evaluate secrets.
"""

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


DEFAULT_OUTPUT = Path("src/argocd/signoz-application.yaml")
DEFAULT_APP_NAME = "signoz"
DEFAULT_APP_NAMESPACE = "argocd"
DEFAULT_PROJECT = "e2e-rag-system"
DEFAULT_DEST_NAMESPACE = "signoz"
DEFAULT_DEST_SERVER = "https://kubernetes.default.svc"
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
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(path.parent), prefix=f".{path.name}."
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


def random_secret(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


@dataclass(frozen=True)
class Config:
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
    storage_class: str

    clickhouse_host: str
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_secret_name: str

    jwt_secret: str
    jwt_secret_name: str

    sync_options: list[str]
    retry_limit: int
    retry_backoff_duration: str
    retry_backoff_factor: int
    retry_backoff_max_duration: str


def load_config() -> Config:
    output = Path(env("SIGNOZ_APPLICATION_OUTPUT", str(DEFAULT_OUTPUT)))
    app_name = env("SIGNOZ_APP_NAME", DEFAULT_APP_NAME)
    app_namespace = env("SIGNOZ_APP_NAMESPACE", DEFAULT_APP_NAMESPACE)
    project = env("SIGNOZ_PROJECT", DEFAULT_PROJECT)
    dest_namespace = env("SIGNOZ_DEST_NAMESPACE", DEFAULT_DEST_NAMESPACE)
    dest_server = env("SIGNOZ_DEST_SERVER", DEFAULT_DEST_SERVER)
    repo_url = env("SIGNOZ_CHART_REPO_URL", DEFAULT_REPO_URL)
    chart_name = env("SIGNOZ_CHART_NAME", DEFAULT_CHART_NAME)
    chart_version = env("SIGNOZ_CHART_VERSION", DEFAULT_CHART_VERSION)
    cluster_domain = env("SIGNOZ_CLUSTER_DOMAIN", DEFAULT_CLUSTER_DOMAIN)
    cluster_name = env("SIGNOZ_CLUSTER_NAME", "production-cluster")
    storage_class = env("SIGNOZ_STORAGE_CLASS", DEFAULT_STORAGE_CLASS)

    clickhouse_host = env("SIGNOZ_CLICKHOUSE_HOST", "my-clickhouse.default.svc.cluster.local")
    clickhouse_user = env("SIGNOZ_CLICKHOUSE_USER", "admin")
    clickhouse_password = env("SIGNOZ_CLICKHOUSE_PASSWORD", "")
    if not clickhouse_password:
        clickhouse_password = random_secret(24)
    clickhouse_secret_name = env("SIGNOZ_CLICKHOUSE_SECRET_NAME", "signoz-clickhouse-auth")

    jwt_secret = env("SIGNOZ_JWT_SECRET", "")
    if not jwt_secret:
        jwt_secret = random_secret(32)
    jwt_secret_name = env("SIGNOZ_JWT_SECRET_NAME", "signoz-jwt-secret")

    sync_options = [
        item.strip()
        for item in env("SIGNOZ_SYNC_OPTIONS", ",".join(DEFAULT_SYNC_OPTIONS)).split(",")
        if item.strip()
    ]

    return Config(
        output=output,
        app_name=app_name,
        app_namespace=app_namespace,
        project=project,
        dest_namespace=dest_namespace,
        dest_server=dest_server,
        repo_url=repo_url,
        chart_name=chart_name,
        chart_version=chart_version,
        cluster_domain=cluster_domain,
        cluster_name=cluster_name,
        storage_class=storage_class,
        clickhouse_host=clickhouse_host,
        clickhouse_user=clickhouse_user,
        clickhouse_password=clickhouse_password,
        clickhouse_secret_name=clickhouse_secret_name,
        jwt_secret=jwt_secret,
        jwt_secret_name=jwt_secret_name,
        sync_options=sync_options,
        retry_limit=int(env("SIGNOZ_RETRY_LIMIT", "5")),
        retry_backoff_duration=env("SIGNOZ_RETRY_BACKOFF_DURATION", "5s"),
        retry_backoff_factor=int(env("SIGNOZ_RETRY_BACKOFF_FACTOR", "2")),
        retry_backoff_max_duration=env("SIGNOZ_RETRY_BACKOFF_MAX_DURATION", "3m"),
    )


def build_values(cfg: Config) -> dict[str, Any]:
    values = {
        "global": {
            "clusterName": cfg.cluster_name,
            "clusterDomain": cfg.cluster_domain,
        },
        "clickhouse": {"enabled": False},
        "externalClickhouse": {
            "host": cfg.clickhouse_host,
            "user": cfg.clickhouse_user,
            "existingSecret": cfg.clickhouse_secret_name,
            "existingSecretPasswordKey": "password",
        },
        "signoz": {
            "name": "signoz",
            "replicaCount": 1,
            "env": {
                "signoz_telemetrystore_provider": "clickhouse",
                "signoz_include_only_log_namespaces": "inference",
                "SIGNOZ_TOKENIZER_JWT_SECRET": {
                    "valueFrom": {
                        "secretKeyRef": {"name": cfg.jwt_secret_name, "key": "SIGNOZ_TOKENIZER_JWT_SECRET"}
                    }
                },
            },
            "podSecurityContext": {"fsGroup": 1000},
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "runAsUser": 1000,
            },
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
            "persistence": {"enabled": True, "storageClass": cfg.storage_class, "accessModes": ["ReadWriteOnce"], "size": "1Gi"},
        },
    }
    checksum = hashlib.sha256(yaml_dump(values).encode("utf-8")).hexdigest()
    values["global"]["configChecksum"] = checksum
    return values


def build_application(cfg: Config) -> dict[str, Any]:
    values_yaml = yaml_dump(build_values(cfg)).rstrip() + "\n"
    app = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": cfg.app_name,
            "namespace": cfg.app_namespace,
            "labels": {"app.kubernetes.io/name": cfg.app_name, "app.kubernetes.io/managed-by": "argocd"},
            "annotations": {
                "description": "SigNoz Helm deployment (chart repo + external ClickHouse preserved)",
                "signoz.argoproj.io/environment": "prod",
            },
        },
        "spec": {
            "project": cfg.project,
            "source": {"repoURL": cfg.repo_url, "chart": cfg.chart_name, "targetRevision": str(cfg.chart_version), "helm": {"values": values_yaml}},
            "destination": {"server": cfg.dest_server, "namespace": cfg.dest_namespace},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": cfg.sync_options,
                "retry": {
                    "limit": cfg.retry_limit,
                    "backoff": {"duration": cfg.retry_backoff_duration, "factor": cfg.retry_backoff_factor, "maxDuration": cfg.retry_backoff_max_duration},
                },
            },
            "ignoreDifferences": [
                {"group": "", "kind": "Secret", "name": cfg.clickhouse_secret_name, "jsonPointers": ["/data"]},
                {"group": "", "kind": "Secret", "name": cfg.jwt_secret_name, "jsonPointers": ["/data"]},
            ],
        },
    }
    return app


def kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


def run_kubectl(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["kubectl", *args], input=input_bytes, capture_output=True, check=False)
    except Exception as exc:
        fatal(f"Failed to run kubectl: {exc}")
    return proc


def ensure_namespace_exists(ns: str) -> None:
    proc = run_kubectl(["get", "ns", ns])
    if proc.returncode == 0:
        return
    proc = run_kubectl(["create", "namespace", ns])
    if proc.returncode != 0:
        log(proc.stderr.decode().strip())
        fatal(f"Failed to create namespace {ns}")


def apply_secret_manifest(manifest: dict[str, Any]) -> None:
    manifest_yaml = yaml_dump(manifest)
    proc = run_kubectl(["apply", "-f", "-"], input_bytes=manifest_yaml.encode("utf-8"))
    if proc.returncode != 0:
        log(proc.stderr.decode().strip())
        fatal("kubectl apply failed for secret manifest")
    log(proc.stdout.decode().strip())


def create_or_update_secrets(cfg: Config) -> None:
    if not kubectl_available():
        fatal("kubectl is not available on PATH; cannot create Kubernetes secrets.")
    ensure_namespace_exists(cfg.dest_namespace)

    clickhouse_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": cfg.clickhouse_secret_name, "namespace": cfg.dest_namespace, "labels": {"app.kubernetes.io/managed-by": "setup-signoz-script"}},
        "stringData": {"password": cfg.clickhouse_password, "user": cfg.clickhouse_user},
        "type": "Opaque",
    }
    apply_secret_manifest(clickhouse_manifest)

    jwt_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": cfg.jwt_secret_name, "namespace": cfg.dest_namespace, "labels": {"app.kubernetes.io/managed-by": "setup-signoz-script"}},
        "stringData": {"SIGNOZ_TOKENIZER_JWT_SECRET": cfg.jwt_secret},
        "type": "Opaque",
    }
    apply_secret_manifest(jwt_manifest)


def write_application_yaml(cfg: Config) -> None:
    app = build_application(cfg)
    rendered = yaml_dump(app)
    atomic_write_text(cfg.output, rendered)
    log(f"Wrote {cfg.output}")


def restart_signoz_controllers(cfg: Config) -> None:
    # Restart StatefulSet and Deployment to force pods to re-evaluate secrets.
    # Best-effort: ignore errors but log them.
    log("Restarting SigNoz StatefulSet and Otel Collector deployment (best-effort).")
    ss = run_kubectl(["rollout", "restart", "statefulset/signoz", "-n", cfg.dest_namespace])
    if ss.returncode != 0:
        log("Warning: failed to restart statefulset/signoz:", ss.stderr.decode().strip())
    dep = run_kubectl(["rollout", "restart", "deployment/signoz-otel-collector", "-n", cfg.dest_namespace])
    if dep.returncode != 0:
        log("Warning: failed to restart deployment/signoz-otel-collector:", dep.stderr.decode().strip())
    # Delete migrator pods (jobs) so they can be recreated
    _ = run_kubectl(["delete", "pod", "-l", "job-name=signoz-telemetrystore-migrator", "-n", cfg.dest_namespace, "--ignore-not-found"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Automate SigNoz secret creation and render ArgoCD Application YAML.")
    parser.add_argument("--stdout", action="store_true", help="Print the rendered YAML to stdout.")
    parser.add_argument("--write", action="store_true", help="Write the rendered YAML to disk.")
    parser.add_argument("--check", action="store_true", help="Fail if the on-disk file differs from the rendered output.")
    parser.add_argument("--apply-secret", action="store_true", help="Create or update ClickHouse and JWT secrets, write Application YAML, and restart controllers.")
    parser.add_argument("--no-restart", action="store_true", help="When --apply-secret is used, do not restart controllers (useful if you want to control restarts manually).")
    args = parser.parse_args(argv)

    cfg = load_config()

    if args.apply_secret:
        log(f"Applying secrets to namespace '{cfg.dest_namespace}'")
        create_or_update_secrets(cfg)
        write_application_yaml(cfg)
        if not args.no_restart:
            restart_signoz_controllers(cfg)
        log("Secrets applied and Application YAML rendered.")
        return

    # Default behavior if not applying secrets: render and optionally write/check
    rendered = yaml_dump(build_application(cfg))
    if args.stdout:
        sys.stdout.write(rendered)
    if args.write or not any([args.stdout, args.check]):
        atomic_write_text(cfg.output, rendered)
        log(f"Wrote {cfg.output}")
    if args.check:
        existing = cfg.output.read_text(encoding="utf-8") if cfg.output.exists() else ""
        if existing != rendered:
            fatal(f"{cfg.output} is out of date", 3)
        log(f"OK {cfg.output} is up to date")


if __name__ == "__main__":
    main()
