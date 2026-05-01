#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
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


ROOT = Path(__file__).resolve().parents[3]

APP_OUTPUT = ROOT / "src/argocd/authentik-application.yaml"
VALUES_OUTPUT = ROOT / "src/scripts/archive/authentik-values.yaml"
BLUEPRINT_OUTPUT = ROOT / "src/scripts/archive/authentik-blueprints.yaml"

APP_NAME = "authentik"
APP_NAMESPACE = "argocd"
PROJECT = "default"
DEST_NAMESPACE = "authentik"
DEST_SERVER = "https://kubernetes.default.svc"

HELM_REPO_URL = "https://charts.goauthentik.io"
HELM_CHART_NAME = "authentik"
HELM_CHART_VERSION = "2026.2.2"
HELM_RELEASE_NAME = "authentik"
HELM_TIMEOUT = "20m"

NAMESPACE = "authentik"
DOMAIN = "athithya.site"
AUTH_HOST = f"auth.{DOMAIN}"
API_HOST = f"api.{DOMAIN}"
COOKIE_DOMAIN = f".{DOMAIN}"

AUTHENTIK_SECRET_NAME = "authentik-env"
POSTGRES_SECRET_NAME = "authentik-postgresql-auth"
BLUEPRINT_SECRET_NAME = "authentik-blueprints"

POSTGRES_USERNAME = "authentik"
POSTGRES_DATABASE = "authentik"
POSTGRES_HOST = "authentik-postgresql"
POSTGRES_PORT = 5432

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


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


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


def run_cmd(args: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> None:
    dbg("RUN:", " ".join(args))
    subprocess.run(args, cwd=str(cwd) if cwd else None, input=stdin, text=True, check=True)


def require_cmd(name: str) -> None:
    from shutil import which

    if which(name) is None:
        fatal(f"Required command not found: {name}")


@dataclass
class Config:
    app_output: Path = APP_OUTPUT
    values_output: Path = VALUES_OUTPUT
    blueprint_output: Path = BLUEPRINT_OUTPUT

    app_name: str = APP_NAME
    app_namespace: str = APP_NAMESPACE
    project: str = PROJECT
    dest_server: str = DEST_SERVER
    dest_namespace: str = DEST_NAMESPACE

    repo_url: str = HELM_REPO_URL
    chart_name: str = HELM_CHART_NAME
    chart_version: str = HELM_CHART_VERSION
    helm_release_name: str = HELM_RELEASE_NAME
    helm_timeout: str = HELM_TIMEOUT

    namespace: str = NAMESPACE
    domain: str = DOMAIN
    auth_host: str = AUTH_HOST
    api_host: str = API_HOST
    cookie_domain: str = COOKIE_DOMAIN

    sync_options: list[str] = None  # type: ignore[assignment]
    retry_limit: int = 3
    retry_backoff_duration: str = "10s"
    retry_backoff_factor: int = 2
    retry_backoff_max_duration: str = "3m"

    authentik_secret_key: str = ""
    postgres_password: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    create_namespace: bool = True
    rollout_application: bool = False


def load_config() -> Config:
    cfg = Config(
        authentik_secret_key=env("AUTHENTIK_SECRET_KEY"),
        postgres_password=env("AUTHENTIK_POSTGRESQL_PASSWORD"),
        google_client_id=env("GOOGLE_OAUTH_CLIENT_ID"),
        google_client_secret=env("GOOGLE_OAUTH_CLIENT_SECRET"),
    )
    cfg.sync_options = [item.strip() for item in env("AUTHENTIK_SYNC_OPTIONS", ",".join(DEFAULT_SYNC_OPTIONS)).split(",") if item.strip()]
    cfg.retry_limit = int(env("AUTHENTIK_RETRY_LIMIT", "3"))
    cfg.retry_backoff_duration = env("AUTHENTIK_RETRY_BACKOFF_DURATION", "10s")
    cfg.retry_backoff_factor = int(env("AUTHENTIK_RETRY_BACKOFF_FACTOR", "2"))
    cfg.retry_backoff_max_duration = env("AUTHENTIK_RETRY_BACKOFF_MAX_DURATION", "3m")
    cfg.create_namespace = env("AUTHENTIK_CREATE_NAMESPACE", "true").lower() in {"1", "true", "yes", "y", "on"}
    return cfg


def require_secrets(cfg: Config) -> None:
    missing = []
    if not cfg.authentik_secret_key:
        missing.append("AUTHENTIK_SECRET_KEY")
    if not cfg.postgres_password:
        missing.append("AUTHENTIK_POSTGRESQL_PASSWORD")
    if not cfg.google_client_id:
        missing.append("GOOGLE_OAUTH_CLIENT_ID")
    if not cfg.google_client_secret:
        missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
    if missing:
        fatal(f"Missing required environment variable(s): {', '.join(missing)}")


def namespace_exists(namespace: str) -> bool:
    result = subprocess.run(
        ["kubectl", "get", "namespace", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0


def ensure_namespace(namespace: str) -> None:
    if namespace_exists(namespace):
        return
    run_cmd(["kubectl", "create", "namespace", namespace])


def render_authentik_env_secret(cfg: Config) -> dict[str, Any]:
    require_secrets(cfg)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": AUTHENTIK_SECRET_NAME,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/name": cfg.app_name,
                "app.kubernetes.io/component": "secrets",
            },
        },
        "type": "Opaque",
        "stringData": {
            "AUTHENTIK_SECRET_KEY": cfg.authentik_secret_key,
            "AUTHENTIK_POSTGRESQL__PASSWORD": cfg.postgres_password,
        },
    }


def render_postgres_secret(cfg: Config) -> dict[str, Any]:
    require_secrets(cfg)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": POSTGRES_SECRET_NAME,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/name": cfg.app_name,
                "app.kubernetes.io/component": "postgresql-credentials",
            },
        },
        "type": "Opaque",
        "stringData": {
            "postgres-password": cfg.postgres_password,
            "password": cfg.postgres_password,
            "replication-password": cfg.postgres_password,
            "metrics-password": cfg.postgres_password,
        },
    }


def render_blueprints_yaml(cfg: Config) -> str:
    require_secrets(cfg)
    return (
        "# yaml-language-server: $schema=https://goauthentik.io/blueprints/schema.json\n"
        "version: 1\n"
        "metadata:\n"
        "  name: athithya-authentik-bootstrap\n"
        "entries:\n"
        "  - model: authentik_sources_oauth.oauthsource\n"
        "    state: present\n"
        "    identifiers:\n"
        "      slug: google\n"
        "    attrs:\n"
        "      name: Google\n"
        "      slug: google\n"
        "      provider_type: google\n"
        "      consumer_key: !Env GOOGLE_OAUTH_CLIENT_ID\n"
        "      consumer_secret: !Env GOOGLE_OAUTH_CLIENT_SECRET\n"
        "      scope: openid email profile\n"
        "      promoted: true\n"
    )


def render_blueprints_secret(cfg: Config) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": BLUEPRINT_SECRET_NAME,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/name": cfg.app_name,
                "app.kubernetes.io/component": "blueprints",
            },
        },
        "type": "Opaque",
        "stringData": {
            "google-oauth.yaml": render_blueprints_yaml(cfg),
        },
    }


def build_values(cfg: Config) -> dict[str, Any]:
    values: dict[str, Any] = {
        "global": {
            "envFrom": [
                {"secretRef": {"name": AUTHENTIK_SECRET_NAME}},
            ],
            "env": [
                {"name": "AUTHENTIK_HOST", "value": f"https://{cfg.auth_host}"},
                {"name": "AUTHENTIK_HOST_BROWSER", "value": f"https://{cfg.auth_host}"},
                {"name": "AUTHENTIK_COOKIE_DOMAIN", "value": cfg.cookie_domain},
                {"name": "AUTHENTIK_POSTGRESQL__HOST", "value": POSTGRES_HOST},
                {"name": "AUTHENTIK_POSTGRESQL__PORT", "value": str(POSTGRES_PORT)},
                {"name": "AUTHENTIK_POSTGRESQL__USER", "value": POSTGRES_USERNAME},
                {"name": "AUTHENTIK_POSTGRESQL__NAME", "value": POSTGRES_DATABASE},
            ],
        },
        "authentik": {
            "enabled": True,
            "existingSecret": {
                "secretName": AUTHENTIK_SECRET_NAME,
            },
            "log_level": "info",
            "error_reporting": {
                "enabled": False,
            },
            "web": {
                "path": "/",
            },
            "postgresql": {
                "host": POSTGRES_HOST,
                "name": POSTGRES_DATABASE,
                "user": POSTGRES_USERNAME,
                "port": POSTGRES_PORT,
                "password": "env://AUTHENTIK_POSTGRESQL__PASSWORD",
            },
        },
        "server": {
            "enabled": True,
            "ingress": {
                "enabled": False,
            },
        },
        "worker": {
            "enabled": True,
        },
        "blueprints": {
            "secrets": [
                {"name": BLUEPRINT_SECRET_NAME},
            ]
        },
        "postgresql": {
            "enabled": True,
            "auth": {
                "username": POSTGRES_USERNAME,
                "database": POSTGRES_DATABASE,
                "existingSecret": POSTGRES_SECRET_NAME,
                "secretKeys": {
                    "adminPasswordKey": "postgres-password",
                    "userPasswordKey": "password",
                    "replicationPasswordKey": "replication-password",
                    "metricsPasswordKey": "metrics-password",
                },
            },
        },
    }

    return values


def render_values(cfg: Config) -> str:
    return yaml_dump(build_values(cfg)).rstrip() + "\n"


def build_application(cfg: Config) -> dict[str, Any]:
    values_yaml = render_values(cfg)
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
                "description": "Authentik Helm deployment for Google federation and forward auth",
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
                    "prune": True,
                    "selfHeal": True,
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
        },
    }


def render_application(cfg: Config) -> str:
    return yaml_dump(build_application(cfg))


def render_secrets_payload(cfg: Config) -> str:
    docs = [
        render_authentik_env_secret(cfg),
        render_postgres_secret(cfg),
        render_blueprints_secret(cfg),
    ]
    return "\n---\n".join(yaml_dump(doc).rstrip() for doc in docs) + "\n"


def write_outputs(cfg: Config) -> None:
    atomic_write_text(cfg.values_output, render_values(cfg))
    atomic_write_text(cfg.blueprint_output, yaml_dump(render_blueprints_secret(cfg)).rstrip() + "\n")
    atomic_write_text(cfg.app_output, render_application(cfg))
    log(f"Wrote {cfg.values_output}")
    log(f"Wrote {cfg.blueprint_output}")
    log(f"Wrote {cfg.app_output}")


def rollout(cfg: Config) -> None:
    require_cmd("kubectl")
    if cfg.create_namespace:
        ensure_namespace(cfg.namespace)
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=render_secrets_payload(cfg))
    write_outputs(cfg)
    if cfg.rollout_application:
        run_cmd(["kubectl", "apply", "-f", str(cfg.app_output)])


def delete_cluster_secrets(cfg: Config) -> None:
    require_cmd("kubectl")
    run_cmd(
        [
            "kubectl",
            "delete",
            "secret",
            AUTHENTIK_SECRET_NAME,
            POSTGRES_SECRET_NAME,
            BLUEPRINT_SECRET_NAME,
            "-n",
            cfg.namespace,
            "--ignore-not-found=true",
        ]
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render or roll out the Authentik application and secrets.")
    parser.add_argument("--stdout", action="store_true", help="Print the rendered Application YAML to stdout.")
    parser.add_argument("--write", action="store_true", help="Write the rendered manifests to disk.")
    parser.add_argument("--check", action="store_true", help="Fail if the on-disk Application YAML differs.")
    parser.add_argument("--rollout", action="store_true", help="Apply namespace + secrets to the cluster and write manifest files.")
    parser.add_argument("--delete", action="store_true", help="Delete the cluster secrets.")
    parser.add_argument("--apply-app", action="store_true", help="Apply the rendered Argo CD Application too.")

    args = parser.parse_args(argv)
    cfg = load_config()
    if args.apply_app:
        cfg.rollout_application = True

    if not any([args.stdout, args.write, args.check, args.rollout, args.delete]):
        args.write = True

    if args.rollout and (args.stdout or args.check or args.delete):
        fatal("--rollout cannot be combined with --stdout, --check, or --delete")

    rendered = render_application(cfg)

    if args.stdout:
        sys.stdout.write(rendered)

    if args.write:
        write_outputs(cfg)

    if args.check:
        existing = cfg.app_output.read_text(encoding="utf-8") if cfg.app_output.exists() else ""
        if existing != rendered:
            fatal(f"{cfg.app_output} is out of date", 3)
        log(f"OK {cfg.app_output} is up to date")

    if args.rollout:
        rollout(cfg)

    if args.delete:
        delete_cluster_secrets(cfg)


if __name__ == "__main__":
    main()