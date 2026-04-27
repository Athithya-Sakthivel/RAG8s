#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


class LiteralStr(str):
    pass


def _literal_str_representer(dumper: yaml.Dumper, data: LiteralStr):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


yaml.SafeDumper.add_representer(LiteralStr, _literal_str_representer)

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "src" / "manifests" / "cloudflared"

NAMESPACE = os.getenv("NAMESPACE", "inference").strip() or "inference"
IMAGE = os.getenv("IMAGE", "docker.io/cloudflare/cloudflared:2026.3.0@sha256:6b599ca3e974349ead3286d178da61d291961182ec3fe9c505e1dd02c8ac31b0").strip() or "cloudflare/cloudflared:2026.3.0"
REPLICAS = int(os.getenv("REPLICAS", "1"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "2000"))
TUNNEL_PROTOCOL = os.getenv("TUNNEL_PROTOCOL", "http2").strip().lower() or "http2"
DOMAIN = os.getenv("DOMAIN", "").strip().rstrip(".")
TUNNEL_NAME = os.getenv("CLOUDFLARE_TUNNEL_NAME", "tabular-api-tunnel").strip() or "tabular-api-tunnel"
SECRET_NAME = os.getenv("CLOUDFLARE_SECRET_NAME", "cloudflared-token").strip() or "cloudflared-token"
SECRET_KEY = os.getenv("CLOUDFLARE_SECRET_KEY", "token").strip() or "token"
TOKEN = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()

AUTH_UPSTREAM = os.getenv(
    "AUTH_UPSTREAM",
    "http://auth-svc.inference.svc.cluster.local:80",
).strip()
PREDICT_UPSTREAM = os.getenv(
    "PREDICT_UPSTREAM",
    "http://tabular-inference-serve-svc.inference.svc.cluster.local:8000",
).strip()

ALLOWED_PROTOCOLS = {"auto", "http2", "quic"}
STARTUP_FAILURE_THRESHOLD = int(os.getenv("CLOUDFLARED_STARTUP_FAILURE_THRESHOLD", "36"))
STARTUP_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_STARTUP_PERIOD_SECONDS", "5"))
READINESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_INITIAL_DELAY_SECONDS", "5"))
READINESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_PERIOD_SECONDS", "5"))
LIVENESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_INITIAL_DELAY_SECONDS", "15"))
LIVENESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_PERIOD_SECONDS", "10"))


def require(condition: bool, message: str) -> None:
    if not condition:
        print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(2)


def dump_yaml(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, width=120)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run(cmd: list[str], stdin: str | None = None) -> None:
    subprocess.run(cmd, input=stdin, text=True, check=True)


def normalize_upstream(upstream: str) -> str:
    value = upstream.strip()
    require(bool(value), "upstream values must not be empty")
    if "://" not in value:
        value = f"http://{value}"
    return value.rstrip("/")


def auth_host() -> str:
    require(bool(DOMAIN), "DOMAIN is required")
    return f"auth.{DOMAIN}"


def predict_host() -> str:
    require(bool(DOMAIN), "DOMAIN is required")
    return f"predict.{DOMAIN}"


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


def render_config_yaml() -> str:
    config = {
        "tunnel": TUNNEL_NAME,
        "ingress": [
            {"hostname": auth_host(), "service": normalize_upstream(AUTH_UPSTREAM)},
            {"hostname": predict_host(), "service": normalize_upstream(PREDICT_UPSTREAM)},
            {"service": "http_status:404"},
        ],
    }
    return dump_yaml(config).rstrip() + "\n"


def render_configmap(namespace: str) -> dict[str, Any]:
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
            "config.yaml": LiteralStr(render_config_yaml()),
        },
    }


def render_routes_reference() -> dict[str, Any]:
    return {
        "tunnel": TUNNEL_NAME,
        "ingress": [
            {"hostname": auth_host(), "service": normalize_upstream(AUTH_UPSTREAM)},
            {"hostname": predict_host(), "service": normalize_upstream(PREDICT_UPSTREAM)},
            {"service": "http_status:404"},
        ],
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
    require(bool(DOMAIN), "DOMAIN is required")
    require(
        TUNNEL_PROTOCOL in ALLOWED_PROTOCOLS,
        f"TUNNEL_PROTOCOL must be one of: {', '.join(sorted(ALLOWED_PROTOCOLS))}",
    )
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
        "auth_upstream": normalize_upstream(AUTH_UPSTREAM),
        "predict_upstream": normalize_upstream(PREDICT_UPSTREAM),
        "config_yaml": configmap["data"]["config.yaml"],
    }
    checksum = sha256_obj(checksum_source)

    docs = [
        render_namespace(NAMESPACE),
        render_serviceaccount(NAMESPACE),
        configmap,
        render_deployment(NAMESPACE, IMAGE, REPLICAS, METRICS_PORT, SECRET_NAME, SECRET_KEY, checksum),
        render_routes_reference(),
    ]
    rendered = "\n---\n".join(dump_yaml(d).rstrip() for d in docs) + "\n"
    return docs, rendered


def reset_output_dir() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_manifests(docs: list[dict[str, Any]]) -> None:
    reset_output_dir()
    atomic_files = [
        ("00-namespace.yaml", docs[0]),
        ("01-serviceaccount.yaml", docs[1]),
        ("02-configmap.yaml", docs[2]),
        ("03-deployment.yaml", docs[3]),
        ("04-routes-reference.yaml", docs[4]),
    ]
    for filename, doc in atomic_files:
        (OUT_DIR / filename).write_text(dump_yaml(doc), encoding="utf-8")


def apply_rollout(docs: list[dict[str, Any]]) -> None:
    require(bool(TOKEN), "CLOUDFLARE_TUNNEL_TOKEN is required for --rollout")
    payload_docs = [
        docs[0],
        docs[1],
        docs[2],
        render_secret(NAMESPACE, SECRET_NAME, SECRET_KEY, TOKEN),
        docs[3],
    ]
    payload = "\n---\n".join(dump_yaml(d).rstrip() for d in payload_docs) + "\n"
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
    shutil.rmtree(OUT_DIR, ignore_errors=True)


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
