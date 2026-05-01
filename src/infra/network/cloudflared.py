from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path.cwd()
OUT_DIR = ROOT / "src" / "manifests" / "cloudflared"

NAMESPACE = os.getenv("NAMESPACE", "inference").strip() or "inference"
IMAGE = os.getenv("IMAGE", "cloudflare/cloudflared:2026.3.0@sha256:6b599ca3e974349ead3286d178da61d291961182ec3fe9c505e1dd02c8ac31b0").strip()
REPLICAS = int(os.getenv("REPLICAS", "1"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "2000"))
TUNNEL_PROTOCOL = os.getenv("TUNNEL_PROTOCOL", "http2").strip().lower() or "http2"

DOMAIN = os.getenv("DOMAIN", "").strip().rstrip(".")
AUTH_HOST = os.getenv("AUTH_HOST", f"auth.{DOMAIN}" if DOMAIN else "auth.athithya.site").strip().rstrip(".")
API_HOST = os.getenv("API_HOST", f"api.{DOMAIN}" if DOMAIN else "api.athithya.site").strip().rstrip(".")
ROOT_HOST = os.getenv("ROOT_HOST", DOMAIN or "athithya.site").strip().rstrip(".")

TUNNEL_NAME = os.getenv("CLOUDFLARE_TUNNEL_NAME", "default-api-tunnel").strip() or "default-api-tunnel"
SECRET_NAME = os.getenv("CLOUDFLARE_SECRET_NAME", "cloudflared-token").strip() or "cloudflared-token"
SECRET_KEY = os.getenv("CLOUDFLARE_SECRET_KEY", "token").strip() or "token"
TOKEN = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()

AUTH_UPSTREAM = os.getenv(
    "AUTH_UPSTREAM",
    "http://authentik-server.inference.svc.cluster.local:9000",
).strip()
FRONTEND_UPSTREAM = os.getenv(
    "FRONTEND_UPSTREAM",
    "http://frontend-nginx.inference.svc.cluster.local:8080",
).strip()

ALLOWED_PROTOCOLS = {"auto", "http2", "quic"}
STARTUP_FAILURE_THRESHOLD = int(os.getenv("CLOUDFLARED_STARTUP_FAILURE_THRESHOLD", "36"))
STARTUP_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_STARTUP_PERIOD_SECONDS", "5"))
READINESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_INITIAL_DELAY_SECONDS", "5"))
READINESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_PERIOD_SECONDS", "5"))
LIVENESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_INITIAL_DELAY_SECONDS", "15"))
LIVENESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_PERIOD_SECONDS", "10"))


def to_yaml(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, width=120)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def run(cmd: list[str], stdin: str | None = None) -> None:
    subprocess.run(cmd, input=stdin, text=True, check=True)


def normalize_upstream(upstream: str) -> str:
    value = upstream.strip()
    if not value:
        return value
    if "://" not in value:
        return f"http://{value}"
    return value.rstrip("/")


def require(condition: bool, message: str) -> None:
    if not condition:
        print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(2)


def ingress_rules() -> list[dict[str, Any]]:
    return [
        {"hostname": AUTH_HOST, "service": normalize_upstream(AUTH_UPSTREAM)},
        {"hostname": API_HOST, "service": normalize_upstream(FRONTEND_UPSTREAM)},
        {"hostname": ROOT_HOST, "service": normalize_upstream(FRONTEND_UPSTREAM)},
        {"service": "http_status:404"},
    ]


def render_namespace(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/managed-by": "generator",
            },
        },
    }


def render_serviceaccount(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "cloudflared-sa",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel",
            },
        },
    }


def render_secret(namespace: str, name: str, key: str, token: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel-token",
            },
        },
        "type": "Opaque",
        "stringData": {key: token},
    }


def render_configmap(namespace: str) -> dict[str, Any]:
    config = {
        "ingress": ingress_rules(),
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "cloudflared-config",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "cloudflared",
                "app.kubernetes.io/component": "tunnel-config",
            },
        },
        "data": {
            "config.yaml": to_yaml(config),
        },
    }


def render_routes_reference(tunnel_name: str) -> dict[str, Any]:
    return {
        "tunnel": tunnel_name,
        "ingress": ingress_rules(),
    }


def render_deployment(
    namespace: str,
    image: str,
    replicas: int,
    metrics_port: int,
    secret_name: str,
    secret_key: str,
    checksum: str,
) -> dict[str, Any]:
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
            "replicas": replicas,
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
                    "serviceAccountName": "cloudflared-sa",
                    "terminationGracePeriodSeconds": 60,
                    "securityContext": {
                        "sysctls": [
                            {"name": "net.ipv4.ping_group_range", "value": "65532 65532"}
                        ]
                    },
                    "volumes": [
                        {
                            "name": "cloudflared-config",
                            "configMap": {
                                "name": "cloudflared-config",
                                "items": [{"key": "config.yaml", "path": "config.yaml"}],
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": "cloudflared",
                            "image": image,
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
                                f"0.0.0.0:{metrics_port}",
                                "--config",
                                "/etc/cloudflared/config.yaml",
                                "run",
                            ],
                            "env": [
                                {
                                    "name": "TUNNEL_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": secret_name, "key": secret_key}
                                    },
                                }
                            ],
                            "ports": [{"name": "metrics", "containerPort": metrics_port}],
                            "volumeMounts": [
                                {
                                    "name": "cloudflared-config",
                                    "mountPath": "/etc/cloudflared",
                                    "readOnly": True,
                                }
                            ],
                            "startupProbe": {
                                "httpGet": {"path": "/ready", "port": metrics_port},
                                "periodSeconds": STARTUP_PERIOD_SECONDS,
                                "failureThreshold": STARTUP_FAILURE_THRESHOLD,
                                "timeoutSeconds": 1,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/ready", "port": metrics_port},
                                "initialDelaySeconds": READINESS_INITIAL_DELAY_SECONDS,
                                "periodSeconds": READINESS_PERIOD_SECONDS,
                                "failureThreshold": 3,
                                "timeoutSeconds": 1,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/ready", "port": metrics_port},
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
    require(bool(ROOT_HOST), "DOMAIN or ROOT_HOST is required")
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
        "token_hash": sha256_text(TOKEN),
        "domain": DOMAIN,
        "auth_host": AUTH_HOST,
        "api_host": API_HOST,
        "root_host": ROOT_HOST,
        "auth_upstream": normalize_upstream(AUTH_UPSTREAM),
        "frontend_upstream": normalize_upstream(FRONTEND_UPSTREAM),
        "config_yaml": configmap["data"]["config.yaml"],
    }
    checksum = sha256_obj(checksum_source)

    docs = [
        render_namespace(NAMESPACE),
        render_serviceaccount(NAMESPACE),
        configmap,
        render_deployment(NAMESPACE, IMAGE, REPLICAS, METRICS_PORT, SECRET_NAME, SECRET_KEY, checksum),
        render_routes_reference(TUNNEL_NAME),
    ]
    rendered = "\n---\n".join(to_yaml(d).rstrip() for d in docs) + "\n"
    return docs, rendered


def write_manifests(docs: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_write(OUT_DIR / "01-serviceaccount.yaml", to_yaml(docs[1]))
    safe_write(OUT_DIR / "02-configmap.yaml", to_yaml(docs[2]))
    safe_write(OUT_DIR / "03-deployment.yaml", to_yaml(docs[3]))
    safe_write(OUT_DIR / "04-routes-reference.yaml", to_yaml(docs[4]))


def apply_rollout(docs: list[dict[str, Any]]) -> None:
    require(bool(TOKEN), "CLOUDFLARE_TUNNEL_TOKEN is required for --rollout")

    payload_docs = [docs[0], docs[1], docs[2], render_secret(NAMESPACE, SECRET_NAME, SECRET_KEY, TOKEN), docs[3]]
    payload = "\n---\n".join(to_yaml(d).rstrip() for d in payload_docs) + "\n"
    run(["kubectl", "apply", "-f", "-"], stdin=payload)


def destroy() -> None:
    run(
        [
            "kubectl",
            "delete",
            "deployment/cloudflared",
            "serviceaccount/cloudflared-sa",
            "configmap/cloudflared-config",
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