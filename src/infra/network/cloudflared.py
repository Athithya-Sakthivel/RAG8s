#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

import yaml

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "src" / "manifests" / "cloudflared"

NAMESPACE = os.getenv("NAMESPACE", "inference").strip() or "inference"
SERVICE_ACCOUNT = os.getenv("SERVICE_ACCOUNT", "cloudflared-sa").strip() or "cloudflared-sa"
CONFIGMAP_NAME = os.getenv("CONFIGMAP_NAME", "cloudflared-config").strip() or "cloudflared-config"
SECRET_NAME = os.getenv("CLOUDFLARE_SECRET_NAME", "cloudflared-token").strip() or "cloudflared-token"
SECRET_KEY = os.getenv("CLOUDFLARE_SECRET_KEY", "token").strip() or "token"

IMAGE = (
    os.getenv(
        "IMAGE",
        "cloudflare/cloudflared:2026.3.0@sha256:6b599ca3e974349ead3286d178da61d291961182ec3fe9c505e1dd02c8ac31b0",
    ).strip()
    or "cloudflare/cloudflared:2026.3.0@sha256:6b599ca3e974349ead3286d178da61d291961182ec3fe9c505e1dd02c8ac31b0"
)

REPLICAS = int(os.getenv("REPLICAS", "1"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "2000"))
TUNNEL_PROTOCOL = os.getenv("TUNNEL_PROTOCOL", "http2").strip().lower() or "http2"
TUNNEL_NAME = os.getenv("CLOUDFLARE_TUNNEL_NAME", "default-tunnel-1").strip() or "default-tunnel-1"
TOKEN = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()

DOMAIN = os.getenv("DOMAIN", "athithya.site").strip().rstrip(".") or "athithya.site"
ROOT_HOST = os.getenv("ROOT_HOST", DOMAIN).strip().rstrip(".") or DOMAIN
API_HOST = os.getenv("API_HOST", f"api.{DOMAIN}").strip().rstrip(".") or f"api.{DOMAIN}"
AUTH_HOST = os.getenv("AUTH_HOST", f"auth.{DOMAIN}").strip().rstrip(".") or f"auth.{DOMAIN}"

AUTH_UPSTREAM = (
    os.getenv("AUTH_UPSTREAM", "http://authentik-server.authentik.svc.cluster.local:80").strip()
    or "http://authentik-server.authentik.svc.cluster.local:80"
)
FRONTEND_UPSTREAM = (
    os.getenv("FRONTEND_UPSTREAM", "http://frontend-nginx.inference.svc.cluster.local:8080").strip()
    or "http://frontend-nginx.inference.svc.cluster.local:8080"
)

ALLOWED_PROTOCOLS = {"auto", "http2", "quic"}

STARTUP_FAILURE_THRESHOLD = int(os.getenv("CLOUDFLARED_STARTUP_FAILURE_THRESHOLD", "36"))
STARTUP_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_STARTUP_PERIOD_SECONDS", "5"))
READINESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_INITIAL_DELAY_SECONDS", "5"))
READINESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_PERIOD_SECONDS", "5"))
LIVENESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_INITIAL_DELAY_SECONDS", "15"))
LIVENESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_PERIOD_SECONDS", "10"))

VERBOSE = os.getenv("VERBOSE", "0").strip().lower() in {"1", "true", "yes", "y", "on"}


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


def run(cmd: list[str], stdin: str | None = None) -> None:
    dbg("RUN:", " ".join(cmd))
    subprocess.run(cmd, input=stdin, text=True, check=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        fatal(message)


def normalize_upstream(upstream: str) -> str:
    value = upstream.strip()
    if not value:
        return value
    if value == "http_status:404":
        return value
    if "://" not in value:
        return f"http://{value}"
    return value.rstrip("/")


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ingress_rules() -> list[dict[str, Any]]:
    return [
        {"hostname": AUTH_HOST, "service": normalize_upstream(AUTH_UPSTREAM)},
        {"hostname": API_HOST, "service": normalize_upstream(FRONTEND_UPSTREAM)},
        {"hostname": ROOT_HOST, "service": normalize_upstream(FRONTEND_UPSTREAM)},
        {"service": "http_status:404"},
    ]


def render_serviceaccount(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": SERVICE_ACCOUNT,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel",
            },
        },
    }


def render_secret(namespace: str) -> dict[str, Any]:
    require(bool(TOKEN), "CLOUDFLARE_TUNNEL_TOKEN is required for --rollout")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": SECRET_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel-token",
            },
        },
        "type": "Opaque",
        "stringData": {
            SECRET_KEY: TOKEN,
        },
    }


def render_configmap(namespace: str) -> dict[str, Any]:
    config = {
        "tunnel": TUNNEL_NAME,
        "protocol": TUNNEL_PROTOCOL,
        "ingress": ingress_rules(),
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CONFIGMAP_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel-config",
            },
        },
        "data": {
            "config.yaml": yaml_dump(config),
        },
    }


def render_routes_reference() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "cloudflared-routes-reference",
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel-reference",
            },
        },
        "data": {
            "routes.yaml": yaml_dump(
                {
                    "tunnel": TUNNEL_NAME,
                    "ingress": ingress_rules(),
                }
            ),
        },
    }


def render_deployment(namespace: str, checksum: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "cloudflared",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel",
            },
        },
        "spec": {
            "replicas": REPLICAS,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "cloudflared",
                    "app.kubernetes.io/component": "tunnel",
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "cloudflared",
                        "app.kubernetes.io/component": "tunnel",
                    },
                    "annotations": {
                        "cloudflared/config-checksum": checksum,
                    },
                },
                "spec": {
                    "serviceAccountName": SERVICE_ACCOUNT,
                    "terminationGracePeriodSeconds": 30,
                    "volumes": [
                        {
                            "name": "cloudflared-config",
                            "configMap": {
                                "name": CONFIGMAP_NAME,
                                "items": [{"key": "config.yaml", "path": "config.yaml"}],
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": "cloudflared",
                            "image": IMAGE,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["cloudflared"],
                            "args": [
                                "tunnel",
                                "--no-autoupdate",
                                "--loglevel",
                                "info",
                                "--protocol",
                                TUNNEL_PROTOCOL,
                                "--metrics",
                                f"0.0.0.0:{METRICS_PORT}",
                                "--config",
                                "/etc/cloudflared/config.yaml",
                                "run",
                            ],
                            "env": [
                                {
                                    "name": "TUNNEL_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": SECRET_NAME,
                                            "key": SECRET_KEY,
                                        }
                                    },
                                }
                            ],
                            "ports": [
                                {"name": "metrics", "containerPort": METRICS_PORT, "protocol": "TCP"}
                            ],
                            "volumeMounts": [
                                {
                                    "name": "cloudflared-config",
                                    "mountPath": "/etc/cloudflared",
                                    "readOnly": True,
                                }
                            ],
                            "startupProbe": {
                                "httpGet": {"path": "/ready", "port": METRICS_PORT},
                                "periodSeconds": STARTUP_PERIOD_SECONDS,
                                "failureThreshold": STARTUP_FAILURE_THRESHOLD,
                                "timeoutSeconds": 1,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/ready", "port": METRICS_PORT},
                                "initialDelaySeconds": READINESS_INITIAL_DELAY_SECONDS,
                                "periodSeconds": READINESS_PERIOD_SECONDS,
                                "failureThreshold": 3,
                                "timeoutSeconds": 1,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/ready", "port": METRICS_PORT},
                                "initialDelaySeconds": LIVENESS_INITIAL_DELAY_SECONDS,
                                "periodSeconds": LIVENESS_PERIOD_SECONDS,
                                "failureThreshold": 3,
                                "timeoutSeconds": 1,
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 65532,
                                "runAsGroup": 65532,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }


def build() -> tuple[list[dict[str, Any]], str]:
    require(bool(NAMESPACE), "NAMESPACE is required")
    require(bool(DOMAIN), "DOMAIN is required")
    require(TUNNEL_PROTOCOL in ALLOWED_PROTOCOLS, f"TUNNEL_PROTOCOL must be one of: {', '.join(sorted(ALLOWED_PROTOCOLS))}")
    require(REPLICAS > 0, "REPLICAS must be greater than 0")
    require(METRICS_PORT > 0, "METRICS_PORT must be greater than 0")
    require(STARTUP_FAILURE_THRESHOLD > 0, "CLOUDFLARED_STARTUP_FAILURE_THRESHOLD must be greater than 0")

    configmap = render_configmap(NAMESPACE)
    checksum_source = {
        "namespace": NAMESPACE,
        "image": IMAGE,
        "replicas": REPLICAS,
        "metrics_port": METRICS_PORT,
        "protocol": TUNNEL_PROTOCOL,
        "tunnel_name": TUNNEL_NAME,
        "secret_name": SECRET_NAME,
        "secret_key": SECRET_KEY,
        "token_hash": hash_text(TOKEN) if TOKEN else "",
        "domain": DOMAIN,
        "root_host": ROOT_HOST,
        "api_host": API_HOST,
        "auth_host": AUTH_HOST,
        "auth_upstream": normalize_upstream(AUTH_UPSTREAM),
        "frontend_upstream": normalize_upstream(FRONTEND_UPSTREAM),
        "config_yaml": configmap["data"]["config.yaml"],
    }
    checksum = hash_obj(checksum_source)

    docs = [
        render_serviceaccount(NAMESPACE),
        configmap,
        render_deployment(NAMESPACE, checksum),
        render_routes_reference(),
    ]
    rendered = "\n---\n".join(yaml_dump(d).rstrip() for d in docs) + "\n"
    return docs, rendered


def write_manifests(docs: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(OUT_DIR / "01-serviceaccount.yaml", yaml_dump(docs[0]).rstrip() + "\n")
    atomic_write_text(OUT_DIR / "02-configmap.yaml", yaml_dump(docs[1]).rstrip() + "\n")
    atomic_write_text(OUT_DIR / "03-deployment.yaml", yaml_dump(docs[2]).rstrip() + "\n")
    atomic_write_text(OUT_DIR / "04-routes-reference.yaml", yaml_dump(docs[3]).rstrip() + "\n")


def apply_rollout(docs: list[dict[str, Any]]) -> None:
    require(bool(TOKEN), "CLOUDFLARE_TUNNEL_TOKEN is required for --rollout")

    payload_docs = [
        docs[0],
        docs[1],
        render_secret(NAMESPACE),
        docs[2],
    ]
    payload = "\n---\n".join(yaml_dump(d).rstrip() for d in payload_docs) + "\n"
    run(["kubectl", "apply", "-f", "-"], stdin=payload)


def destroy() -> None:
    run(
        [
            "kubectl",
            "delete",
            "deployment/cloudflared",
            "serviceaccount/cloudflared-sa",
            "configmap/cloudflared-config",
            "configmap/cloudflared-routes-reference",
            "-n",
            NAMESPACE,
            "--ignore-not-found=true",
        ]
    )
    run(
        [
            "kubectl",
            "delete",
            "secret",
            SECRET_NAME,
            "-n",
            NAMESPACE,
            "--ignore-not-found=true",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rollout", action="store_true")
    group.add_argument("--destroy", action="store_true")
    args = parser.parse_args()

    if args.destroy:
        destroy()
        return

    docs, rendered = build()
    if not args.rollout:
        sys.stdout.write(rendered)
        return

    write_manifests(docs)
    apply_rollout(docs)


if __name__ == "__main__":
    main()