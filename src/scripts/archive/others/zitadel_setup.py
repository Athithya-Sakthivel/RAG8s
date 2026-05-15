#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, NoReturn

import yaml

DOMAIN = (os.environ.get("DOMAIN", "athithya.site") or "athithya.site").strip().rstrip(".") or "athithya.site"
AUTH_HOST = (os.environ.get("AUTH_HOST", f"auth.{DOMAIN}") or f"auth.{DOMAIN}").strip().rstrip(".") or f"auth.{DOMAIN}"
NAMESPACE = (os.environ.get("ZITADEL_NAMESPACE", "zitadel") or "zitadel").strip() or "zitadel"

APP_NAME = (os.environ.get("ZITADEL_APP_NAME", "zitadel") or "zitadel").strip() or "zitadel"
APP_NAMESPACE = (os.environ.get("ZITADEL_APP_NAMESPACE", "argocd") or "argocd").strip() or "argocd"
DEST_SERVER = "https://kubernetes.default.svc"

VALUES_OUTPUT = Path(os.environ.get("ZITADEL_VALUES_OUTPUT", "src/argocd/zitadel-application.yaml"))

CHART_REPO = os.environ.get("ZITADEL_CHART_REPO", "https://charts.zitadel.com").strip() or "https://charts.zitadel.com"
CHART_NAME = os.environ.get("ZITADEL_CHART_NAME", "zitadel").strip() or "zitadel"
CHART_VERSION = os.environ.get("ZITADEL_CHART_VERSION", "9.34.0").strip() or "9.34.0"
IMAGE_REPOSITORY = os.environ.get("ZITADEL_IMAGE_REPOSITORY", "ghcr.io/zitadel/zitadel").strip() or "ghcr.io/zitadel/zitadel"
IMAGE_TAG = os.environ.get("ZITADEL_IMAGE_TAG", "v4.13.0").strip() or "v4.13.0"

MASTERKEY_SECRET_NAME = os.environ.get("ZITADEL_MASTERKEY_SECRET_NAME", "zitadel-masterkey").strip() or "zitadel-masterkey"
CONFIG_SECRET_NAME = os.environ.get("ZITADEL_CONFIG_SECRET_NAME", "zitadel-config-secret").strip() or "zitadel-config-secret"

REPLICA_COUNT = int(os.environ.get("ZITADEL_REPLICAS", "1"))
CPU_REQUESTS = os.environ.get("ZITADEL_CPU_REQUESTS", "200m").strip() or "200m"
CPU_LIMITS = os.environ.get("ZITADEL_CPU_LIMITS", "1000m").strip() or "1000m"
MEMORY_REQUESTS = os.environ.get("ZITADEL_MEMORY_REQUESTS", "256Mi").strip() or "256Mi"
MEMORY_LIMITS = os.environ.get("ZITADEL_MEMORY_LIMITS", "1Gi").strip() or "1Gi"

VERBOSE = os.environ.get("VERBOSE", "0").strip().lower() in {"1", "true", "yes", "y", "on"}


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


def fatal(msg: str) -> NoReturn:
    raise SystemExit(f"ERROR: {msg}")


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


def cleanup_legacy_login_resources() -> None:
    require_cmd("kubectl")
    for cmd in [
        ["kubectl", "-n", NAMESPACE, "delete", "deployment", "zitadel-login", "--ignore-not-found=true"],
        ["kubectl", "-n", NAMESPACE, "delete", "service", "zitadel-login", "--ignore-not-found=true"],
        ["kubectl", "-n", NAMESPACE, "delete", "ingress", "zitadel-login", "--ignore-not-found=true"],
        ["kubectl", "-n", NAMESPACE, "delete", "secret", "login-client", "--ignore-not-found=true"],
    ]:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, text=True)


@dataclass(frozen=True)
class Config:
    replicas: int
    cpu_requests: str
    cpu_limits: str
    memory_requests: str
    memory_limits: str


def load_config() -> Config:
    return Config(
        replicas=REPLICA_COUNT,
        cpu_requests=CPU_REQUESTS,
        cpu_limits=CPU_LIMITS,
        memory_requests=MEMORY_REQUESTS,
        memory_limits=MEMORY_LIMITS,
    )


def validate(cfg: Config) -> None:
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


def render_runtime_config() -> dict[str, Any]:
    return {
        "ExternalDomain": AUTH_HOST,
        "ExternalPort": 443,
        "ExternalSecure": True,
        "TLS": {
            "Enabled": False,
        },
        "FirstInstance": {
            "Skip": False,
            "Org": {
                "Skip": False,
                "Name": "athithya",
                "Human": {
                    "UserName": "admin",
                    "FirstName": "admin",
                    "LastName": "admin",
                    "Email": {
                        "Address": f"admin@{DOMAIN}",
                        "Verified": True,
                    },
                    "PasswordChangeRequired": True,
                },
                "Machine": {
                    "Machine": {
                        "Name": "Automatically Initialized IAM Admin",
                        "Username": "iam-admin",
                    },
                    "MachineKey": {
                        "ExpirationDate": "2029-01-01T00:00:00Z",
                        "Type": 1,
                    },
                    "Pat": {
                        "ExpirationDate": "2029-01-01T00:00:00Z",
                    },
                },
                "LoginClient": {
                    "Machine": {
                        "Name": "Automatically Initialized IAM Login Client",
                        "Username": "login-client",
                    },
                    "Pat": {
                        "ExpirationDate": "2029-01-01T00:00:00Z",
                    },
                },
            },
        },
    }


def render_values() -> dict[str, Any]:
    return {
        "replicaCount": REPLICA_COUNT,
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
            "enabled": False,
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
                "cpu": CPU_REQUESTS,
                "memory": MEMORY_REQUESTS,
            },
            "limits": {
                "cpu": CPU_LIMITS,
                "memory": MEMORY_LIMITS,
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


def render_application() -> dict[str, Any]:
    values_yaml = yaml_dump(render_values()).rstrip() + "\n"
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
                "description": "ZITADEL Helm deployment for hosted login and backend OIDC auth",
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


def write_output() -> None:
    atomic_write_text(VALUES_OUTPUT, yaml_dump(render_application()).rstrip() + "\n")
    log(f"Wrote {VALUES_OUTPUT}")


def apply_application() -> None:
    ensure_namespace(APP_NAMESPACE)
    run_cmd(["kubectl", "apply", "-f", "-"], stdin=yaml_dump(render_application()).rstrip() + "\n")


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render and deploy the ZITADEL Argo CD Application.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--destroy", action="store_true")
    parser.add_argument("--cleanup-legacy-login", action="store_true", default=True)
    args = parser.parse_args(argv)

    cfg = load_config()
    validate(cfg)

    if args.destroy:
        delete_application()
        cleanup_legacy_login_resources()
        return

    write_output()
    if args.cleanup_legacy_login:
        cleanup_legacy_login_resources()
    apply_application()
    log(f"Applied Argo CD application {APP_NAME} into namespace {NAMESPACE}")
    log(f"Rendered application file: {VALUES_OUTPUT}")


if __name__ == "__main__":
    main()