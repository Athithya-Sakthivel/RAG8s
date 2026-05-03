#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, NoReturn

import yaml

DOMAIN = (os.environ.get("DOMAIN", "athithya.site") or "athithya.site").strip().rstrip(".") or "athithya.site"
AUTH_HOST = (os.environ.get("AUTH_HOST", f"auth.{DOMAIN}") or f"auth.{DOMAIN}").strip().rstrip(".") or f"auth.{DOMAIN}"
NAMESPACE = (os.environ.get("ZITADEL_NAMESPACE", "inference") or "inference").strip() or "inference"

APP_NAME = "zitadel"
APP_NAMESPACE = "argocd"
DEST_SERVER = "https://kubernetes.default.svc"

VALUES_OUTPUT = Path("src/argocd/zitadel-application.yaml")

CHART_REPO = os.environ.get("ZITADEL_CHART_REPO", "https://charts.zitadel.com").strip() or "https://charts.zitadel.com"
CHART_NAME = os.environ.get("ZITADEL_CHART_NAME", "zitadel").strip() or "zitadel"
CHART_VERSION = os.environ.get("ZITADEL_CHART_VERSION", "9.34.0").strip() or "9.34.0"
IMAGE_REPOSITORY = os.environ.get("ZITADEL_IMAGE_REPOSITORY", "ghcr.io/zitadel/zitadel").strip() or "ghcr.io/zitadel/zitadel"
IMAGE_TAG = os.environ.get("ZITADEL_IMAGE_TAG", "v4.13.0").strip() or "v4.13.0"

MASTERKEY_SECRET_NAME = os.environ.get("ZITADEL_MASTERKEY_SECRET_NAME", "zitadel-masterkey").strip() or "zitadel-masterkey"
CONFIG_SECRET_NAME = os.environ.get("ZITADEL_CONFIG_SECRET_NAME", "zitadel-config-secret").strip() or "zitadel-config-secret"

LOGIN_CLIENT_SECRET_PREFIX = (os.environ.get("ZITADEL_LOGIN_CLIENT_SECRET_PREFIX", "") or "").strip()

VERBOSE = (os.environ.get("VERBOSE", "0") or "0").strip().lower() in {"1", "true", "yes", "y", "on"}


class Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str_representer)


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


def log(*parts: object) -> None:
    print(*parts, flush=True)


def dbg(*parts: object) -> None:
    if VERBOSE:
        log(*parts)


def fatal(msg: str, code: int = 2) -> NoReturn:
    raise SystemExit(f"ERROR: {msg}")


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        fatal(f"{name} must be an integer")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_file_text(name: str) -> str:
    raw = os.environ.get(name, "")
    raw = raw.strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_file():
        fatal(f"{name} points to a missing file: {path}")
    return path.read_text(encoding="utf-8").strip()


def require_cmd(name: str) -> None:
    if which(name) is None:
        fatal(f"Required command not found: {name}")


def run_cmd(args: list[str], *, stdin: str | None = None) -> None:
    dbg("RUN:", " ".join(args))
    subprocess.run(args, input=stdin, text=True, check=True)


def ensure_namespace(namespace: str) -> None:
    require_cmd("kubectl")
    result = subprocess.run(
        ["kubectl", "get", "namespace", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        run_cmd(["kubectl", "create", "namespace", namespace])


def secret_exists(namespace: str, name: str) -> bool:
    require_cmd("kubectl")
    result = subprocess.run(
        ["kubectl", "get", "secret", name, "-n", namespace],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0


@dataclass(frozen=True)
class Config:
    masterkey: str
    admin_password: str
    database_dsn: str
    instance_name: str
    admin_email: str
    replicas: int
    cpu_requests: str
    cpu_limits: str
    memory_requests: str
    memory_limits: str
    login_enabled: bool
    ingress_enabled: bool
    login_ingress_enabled: bool
    login_client_pat: str
    login_client_secret_name: str


def load_config() -> Config:
    login_enabled = env_bool("ZITADEL_LOGIN_ENABLED", False)
    login_client_secret_name = (
        f"{LOGIN_CLIENT_SECRET_PREFIX}login-client" if LOGIN_CLIENT_SECRET_PREFIX else "login-client"
    )

    return Config(
        masterkey=env("ZITADEL_MASTERKEY", ""),
        admin_password=env("ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD", ""),
        database_dsn=env("ZITADEL_DATABASE_POSTGRES_DSN", ""),
        instance_name=env("ZITADEL_INSTANCE_NAME", "athithya"),
        admin_email=env("ZITADEL_ADMIN_EMAIL", f"admin@{DOMAIN}"),
        replicas=env_int("ZITADEL_REPLICAS", 1),
        cpu_requests=env("ZITADEL_CPU_REQUESTS", "200m"),
        cpu_limits=env("ZITADEL_CPU_LIMITS", "1000m"),
        memory_requests=env("ZITADEL_MEMORY_REQUESTS", "256Mi"),
        memory_limits=env("ZITADEL_MEMORY_LIMITS", "1Gi"),
        login_enabled=login_enabled,
        ingress_enabled=env_bool("ZITADEL_INGRESS_ENABLED", False),
        login_ingress_enabled=env_bool("ZITADEL_LOGIN_INGRESS_ENABLED", False),
        login_client_pat=env("ZITADEL_LOGIN_CLIENT_PAT", "") or env_file_text("ZITADEL_LOGIN_CLIENT_PAT_FILE"),
        login_client_secret_name=login_client_secret_name,
    )


def validate(cfg: Config) -> None:
    if len(cfg.masterkey.encode("utf-8")) != 32:
        fatal("ZITADEL_MASTERKEY must be exactly 32 bytes")

    if not re.match(r"^postgres(ql)?://", cfg.database_dsn):
        fatal("ZITADEL_DATABASE_POSTGRES_DSN must start with postgresql:// or postgres://")

    if cfg.replicas < 1:
        fatal("ZITADEL_REPLICAS must be >= 1")

    for name, value in {
        "ZITADEL_CPU_REQUESTS": cfg.cpu_requests,
        "ZITADEL_CPU_LIMITS": cfg.cpu_limits,
        "ZITADEL_MEMORY_REQUESTS": cfg.memory_requests,
        "ZITADEL_MEMORY_LIMITS": cfg.memory_limits,
    }.items():
        if not value:
            fatal(f"{name} cannot be empty")

    if not cfg.admin_password:
        fatal("ZITADEL_FIRSTINSTANCE_ORG_HUMAN_PASSWORD cannot be empty")

    if not cfg.admin_email:
        fatal("ZITADEL_ADMIN_EMAIL cannot be empty")

    if cfg.login_enabled and not cfg.login_client_secret_name:
        fatal("login-client secret name cannot be empty")


def render_runtime_config() -> dict[str, Any]:
    return {
        "ExternalDomain": AUTH_HOST,
        "ExternalPort": 443,
        "ExternalSecure": True,
        "TLS": {
            "Enabled": False,
        },
    }


def render_secret_config(cfg: Config) -> dict[str, Any]:
    return {
        "Database": {
            "Postgres": {
                "DSN": cfg.database_dsn,
            }
        },
        "FirstInstance": {
            "InstanceName": cfg.instance_name,
            "DefaultLanguage": "en",
            "Org": {
                "Name": cfg.instance_name,
                "Human": {
                    "UserName": "admin",
                    "FirstName": "admin",
                    "LastName": "admin",
                    "Email": {
                        "Address": cfg.admin_email,
                        "Verified": True,
                    },
                    "Password": cfg.admin_password,
                    "PasswordChangeRequired": True,
                },
            },
        },
    }


def render_values(cfg: Config) -> dict[str, Any]:
    values: dict[str, Any] = {
        "replicaCount": cfg.replicas,
        "image": {
            "repository": IMAGE_REPOSITORY,
            "tag": IMAGE_TAG,
            "pullPolicy": "IfNotPresent",
        },
        "zitadel": {
            "masterkeySecretName": MASTERKEY_SECRET_NAME,
            "configSecretName": CONFIG_SECRET_NAME,
            "configSecretKey": "config-yaml",
            "configmapConfig": render_runtime_config(),
        },
        "login": {
            "enabled": False,
        },
        "ingress": {
            "enabled": cfg.ingress_enabled,
        },
        "podSecurityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "fsGroup": 1000,
            "seccompProfile": {
                "type": "RuntimeDefault",
            },
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "readOnlyRootFilesystem": True,
            "privileged": False,
            "allowPrivilegeEscalation": False,
            "capabilities": {
                "drop": ["ALL"],
            },
        },
        "resources": {
            "requests": {
                "cpu": cfg.cpu_requests,
                "memory": cfg.memory_requests,
            },
            "limits": {
                "cpu": cfg.cpu_limits,
                "memory": cfg.memory_limits,
            },
        },
        "autoscaling": {
            "enabled": False,
        },
        "podDisruptionBudget": {
            "enabled": True,
            "minAvailable": 1,
        },
        "affinity": {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "labelSelector": {
                                "matchExpressions": [
                                    {
                                        "key": "app.kubernetes.io/name",
                                        "operator": "In",
                                        "values": ["zitadel"],
                                    }
                                ]
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        },
                    }
                ]
            }
        },
    }

    if cfg.login_enabled:
        values["login"] = {
            "enabled": True,
            "ingress": {
                "enabled": cfg.login_ingress_enabled,
            },
        }

    return values


def render_login_client_secret(cfg: Config) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": cfg.login_client_secret_name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": APP_NAME,
                "app.kubernetes.io/component": "login",
            },
        },
        "type": "Opaque",
        "stringData": {
            "pat": cfg.login_client_pat,
        },
    }


def render_secrets(cfg: Config) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": MASTERKEY_SECRET_NAME,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": APP_NAME,
                    "app.kubernetes.io/component": "secrets",
                },
            },
            "type": "Opaque",
            "stringData": {
                "masterkey": cfg.masterkey,
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": CONFIG_SECRET_NAME,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/name": APP_NAME,
                    "app.kubernetes.io/component": "secrets",
                },
            },
            "type": "Opaque",
            "stringData": {
                "config-yaml": yaml_dump(render_secret_config(cfg)).rstrip() + "\n",
            },
        },
    ]

    if cfg.login_enabled:
        if secret_exists(NAMESPACE, cfg.login_client_secret_name):
            log(f"login-client secret already exists in namespace {NAMESPACE}")
        else:
            if not cfg.login_client_pat:
                fatal(
                    "ZITADEL_LOGIN_ENABLED=true but the login-client secret is missing. "
                    "Provide ZITADEL_LOGIN_CLIENT_PAT or ZITADEL_LOGIN_CLIENT_PAT_FILE with a real PAT."
                )
            if len(cfg.login_client_pat.strip()) < 20:
                fatal("ZITADEL_LOGIN_CLIENT_PAT looks invalid; provide a real PAT, not a placeholder.")
            docs.append(render_login_client_secret(cfg))

    return docs


def build_application(cfg: Config) -> dict[str, Any]:
    values_yaml = yaml_dump(render_values(cfg)).rstrip() + "\n"
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": APP_NAME,
            "namespace": APP_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": APP_NAME,
                "app.kubernetes.io/managed-by": "argocd",
            },
            "annotations": {
                "description": "ZITADEL Helm deployment for Cloudflare Tunnel and backend OIDC auth",
            },
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": CHART_REPO,
                "chart": CHART_NAME,
                "targetRevision": CHART_VERSION,
                "helm": {
                    "values": values_yaml,
                },
            },
            "destination": {
                "server": DEST_SERVER,
                "namespace": NAMESPACE,
            },
            "syncPolicy": {
                "automated": {
                    "prune": True,
                    "selfHeal": True,
                },
                "syncOptions": [
                    "CreateNamespace=true",
                    "PrunePropagationPolicy=foreground",
                    "PruneLast=true",
                ],
                "retry": {
                    "limit": 3,
                    "backoff": {
                        "duration": "10s",
                        "factor": 2,
                        "maxDuration": "3m",
                    },
                },
            },
        },
    }


def render_application(cfg: Config) -> str:
    return yaml_dump(build_application(cfg)).rstrip() + "\n"


def render_cluster_secrets_payload(cfg: Config) -> str:
    return "\n---\n".join(yaml_dump(doc).rstrip() for doc in render_secrets(cfg)) + "\n"


def write_output(cfg: Config) -> None:
    atomic_write_text(VALUES_OUTPUT, render_application(cfg))
    log(f"Wrote {VALUES_OUTPUT}")


def apply_docs(docs_yaml: str) -> None:
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=docs_yaml)


def apply_secrets(cfg: Config) -> None:
    ensure_namespace(NAMESPACE)
    apply_docs(render_cluster_secrets_payload(cfg))


def apply_application(cfg: Config) -> None:
    ensure_namespace(APP_NAMESPACE)
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=render_application(cfg))


def delete_application() -> None:
    require_cmd("kubectl")
    run_cmd(
        [
            "kubectl",
            "delete",
            "application",
            APP_NAME,
            "-n",
            APP_NAMESPACE,
            "--ignore-not-found=true",
        ]
    )


def delete_secrets() -> None:
    require_cmd("kubectl")
    run_cmd(
        [
            "kubectl",
            "delete",
            "secret",
            MASTERKEY_SECRET_NAME,
            CONFIG_SECRET_NAME,
            "-n",
            NAMESPACE,
            "--ignore-not-found=true",
        ]
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render and deploy ZITADEL Argo CD Application YAML.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--rollout", action="store_true")
    mode.add_argument("--destroy", action="store_true")
    parser.add_argument("--apply-secrets", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config()
    validate(cfg)

    if args.destroy:
        delete_application()
        delete_secrets()
        return

    if not args.apply_secrets:
        fatal("--write and --rollout both require --apply-secrets")

    write_output(cfg)
    apply_secrets(cfg)

    if args.rollout:
        apply_application(cfg)


if __name__ == "__main__":
    main()