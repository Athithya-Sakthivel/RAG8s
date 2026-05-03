#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NoReturn

import yaml

# Determine repository root and output directory.
try:
    _this_file = Path(__file__).resolve()
    if len(_this_file.parents) >= 4:
        ROOT = _this_file.parents[3]
    else:
        ROOT = _this_file.parent
except Exception:
    ROOT = Path.cwd()

OUT_DIR = ROOT / "src" / "manifests" / "cloudflared"

# frequently changed
NAMESPACE = os.getenv("NAMESPACE", "inference").strip() or "inference"
DOMAIN = os.getenv("DOMAIN", "athithya.site").strip().rstrip(".") or "athithya.site"
ROOT_HOST = os.getenv("ROOT_HOST", DOMAIN).strip().rstrip(".") or DOMAIN
API_HOST = os.getenv("API_HOST", f"api.{DOMAIN}").strip().rstrip(".") or f"api.{DOMAIN}"
AUTH_HOST = os.getenv("AUTH_HOST", f"auth.{DOMAIN}").strip().rstrip(".") or f"auth.{DOMAIN}"

ZITADEL_NAMESPACE = os.getenv("ZITADEL_NAMESPACE", "inference").strip() or "zitadel"
ZITADEL_SERVICE_NAME = os.getenv("ZITADEL_SERVICE_NAME", "zitadel").strip() or "zitadel"
ZITADEL_SERVICE_PORT = int(os.getenv("ZITADEL_SERVICE_PORT", "8080"))

FRONTEND_SERVICE_NAME = os.getenv("FRONTEND_SERVICE_NAME", "frontend-nginx").strip() or "frontend-nginx"
FRONTEND_SERVICE_PORT = int(os.getenv("FRONTEND_SERVICE_PORT", "8080"))

TUNNEL_NAME = os.getenv("CLOUDFLARE_TUNNEL_NAME", "default-tunnel-1").strip() or "default-tunnel-1"
TUNNEL_TOKEN = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()

# rarely changed
SERVICE_ACCOUNT = os.getenv("SERVICE_ACCOUNT", "cloudflared-sa").strip() or "cloudflared-sa"
CONFIGMAP_NAME = os.getenv("CONFIGMAP_NAME", "cloudflared-config").strip() or "cloudflared-config"
SECRET_NAME = os.getenv("CLOUDFLARE_SECRET_NAME", "cloudflared-token").strip() or "cloudflared-token"
SECRET_KEY = os.getenv("CLOUDFLARE_SECRET_KEY", "token").strip() or "token"
DEPLOYMENT_NAME = os.getenv("DEPLOYMENT_NAME", "cloudflared").strip() or "cloudflared"

IMAGE = os.getenv(
    "IMAGE",
    "cloudflare/cloudflared:2026.3.0@sha256:6b599ca3e974349ead3286d178da61d291961182ec3fe9c505e1dd02c8ac31b0",
).strip() or "cloudflare/cloudflared:2026.3.0@sha256:6b599ca3e974349ead3286d178da61d291961182ec3fe9c505e1dd02c8ac31b0"

REPLICAS = int(os.getenv("REPLICAS", "1"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "2000"))
TUNNEL_PROTOCOL = os.getenv("TUNNEL_PROTOCOL", "http2").strip().lower() or "http2"

STARTUP_FAILURE_THRESHOLD = int(os.getenv("CLOUDFLARED_STARTUP_FAILURE_THRESHOLD", "36"))
STARTUP_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_STARTUP_PERIOD_SECONDS", "5"))
READINESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_INITIAL_DELAY_SECONDS", "5"))
READINESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_READINESS_PERIOD_SECONDS", "5"))
LIVENESS_INITIAL_DELAY_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_INITIAL_DELAY_SECONDS", "15"))
LIVENESS_PERIOD_SECONDS = int(os.getenv("CLOUDFLARED_LIVENESS_PERIOD_SECONDS", "10"))

VERBOSE = os.getenv("VERBOSE", "0").strip().lower() in {"1", "true", "yes", "y", "on"}
ALLOWED_PROTOCOLS = {"auto", "http2", "quic"}

# Configure logging
LOG_LEVEL = logging.DEBUG if VERBOSE else logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cloudflared-manifest")

class Dumper(yaml.SafeDumper):
    pass

def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

Dumper.add_representer(str, _str_representer)

def fatal(msg: str, code: int = 2) -> NoReturn:
    logger.error(msg)
    raise SystemExit(code)

def require(condition: bool, message: str) -> None:
    if not condition:
        fatal(message)

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
    logger.debug("Wrote file: %s", path)

def run(cmd: list[str], stdin: str | None = None) -> None:
    logger.debug("Executing command: %s", " ".join(map(str, cmd)))
    try:
        subprocess.run(list(map(str, cmd)), input=stdin, text=True, check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Command failed (%s): exit=%s", " ".join(map(str, cmd)), e.returncode)
        raise

def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def hash_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def normalize_service(service: str) -> str:
    value = service.strip()
    if not value:
        return value
    if value == "http_status:404":
        return value
    if "://" not in value:
        value = f"http://{value}"
    return value.rstrip("/")

def ingress_rules() -> list[dict[str, Any]]:
    zitadel_upstream = f"{ZITADEL_SERVICE_NAME}.{ZITADEL_NAMESPACE}.svc.cluster.local:{ZITADEL_SERVICE_PORT}"
    frontend_upstream = f"{FRONTEND_SERVICE_NAME}.{NAMESPACE}.svc.cluster.local:{FRONTEND_SERVICE_PORT}"

    return [
        {
            "hostname": AUTH_HOST,
            "service": normalize_service(f"http://{zitadel_upstream}"),
            "originRequest": {
                "connectTimeout": "10s",
                "keepAliveTimeout": "30s",
                "noTLSVerify": True,
                "httpHostHeader": AUTH_HOST,
                "disableChunkedEncoding": True,
            },
        },
        {
            "hostname": API_HOST,
            "service": normalize_service(f"http://{frontend_upstream}"),
            "originRequest": {
                "connectTimeout": "10s",
                "keepAliveTimeout": "30s",
                "noTLSVerify": True,
                "disableChunkedEncoding": True,
            },
        },
        {
            "hostname": ROOT_HOST,
            "service": normalize_service(f"http://{frontend_upstream}"),
            "originRequest": {
                "connectTimeout": "10s",
                "keepAliveTimeout": "30s",
                "noTLSVerify": True,
                "disableChunkedEncoding": True,
            },
        },
        {"service": "http_status:404"},
    ]

def validate() -> None:
    require(bool(NAMESPACE), "NAMESPACE is required")
    require(bool(DOMAIN), "DOMAIN is required")
    require(bool(AUTH_HOST), "AUTH_HOST is required")
    require(bool(ROOT_HOST), "ROOT_HOST is required")
    require(bool(API_HOST), "API_HOST is required")
    require(TUNNEL_PROTOCOL in ALLOWED_PROTOCOLS, f"TUNNEL_PROTOCOL must be one of: {', '.join(sorted(ALLOWED_PROTOCOLS))}")
    require(REPLICAS > 0, "REPLICAS must be greater than 0")
    require(METRICS_PORT > 0, "METRICS_PORT must be greater than 0")
    require(STARTUP_FAILURE_THRESHOLD > 0, "CLOUDFLARED_STARTUP_FAILURE_THRESHOLD must be greater than 0")
    require(ZITADEL_SERVICE_PORT > 0, "ZITADEL_SERVICE_PORT must be greater than 0")
    require(FRONTEND_SERVICE_PORT > 0, "FRONTEND_SERVICE_PORT must be greater than 0")

def render_serviceaccount(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": SERVICE_ACCOUNT,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": DEPLOYMENT_NAME,
                "app.kubernetes.io/component": "tunnel",
            },
        },
    }

def render_secret(namespace: str) -> dict[str, Any]:
    require(bool(TUNNEL_TOKEN), "CLOUDFLARE_TUNNEL_TOKEN is required")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": SECRET_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": DEPLOYMENT_NAME,
                "app.kubernetes.io/component": "tunnel-token",
            },
        },
        "type": "Opaque",
        "stringData": {
            SECRET_KEY: TUNNEL_TOKEN,
        },
    }

def render_configmap(namespace: str) -> dict[str, Any]:
    config = {
        "tunnel": TUNNEL_NAME,
        "ingress": ingress_rules(),
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CONFIGMAP_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": DEPLOYMENT_NAME,
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
            "name": f"{DEPLOYMENT_NAME}-routes-reference",
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": DEPLOYMENT_NAME,
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
            "name": DEPLOYMENT_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": DEPLOYMENT_NAME,
                "app.kubernetes.io/component": "tunnel",
            },
        },
        "spec": {
            "replicas": REPLICAS,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": DEPLOYMENT_NAME,
                    "app.kubernetes.io/component": "tunnel",
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": DEPLOYMENT_NAME,
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

def build() -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    validate()

    configmap = render_configmap(NAMESPACE)
    secret = render_secret(NAMESPACE)
    checksum = hash_obj(
        {
            "namespace": NAMESPACE,
            "image": IMAGE,
            "replicas": REPLICAS,
            "metrics_port": METRICS_PORT,
            "protocol": TUNNEL_PROTOCOL,
            "tunnel_name": TUNNEL_NAME,
            "secret_name": SECRET_NAME,
            "secret_key": SECRET_KEY,
            "token_hash": hash_text(TUNNEL_TOKEN) if TUNNEL_TOKEN else "",
            "domain": DOMAIN,
            "root_host": ROOT_HOST,
            "api_host": API_HOST,
            "auth_host": AUTH_HOST,
            "zitadel_namespace": ZITADEL_NAMESPACE,
            "zitadel_service_name": ZITADEL_SERVICE_NAME,
            "zitadel_service_port": ZITADEL_SERVICE_PORT,
            "frontend_service_name": FRONTEND_SERVICE_NAME,
            "frontend_service_port": FRONTEND_SERVICE_PORT,
            "config_yaml": configmap["data"]["config.yaml"],
        }
    )

    docs = [
        render_serviceaccount(NAMESPACE),
        configmap,
        render_deployment(NAMESPACE, checksum),
        render_routes_reference(),
    ]

    rendered = "\n---\n".join(yaml_dump(d).rstrip() for d in docs) + "\n"
    return docs, secret, rendered

def write_manifests(docs: list[dict[str, Any]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(OUT_DIR / "01-serviceaccount.yaml", yaml_dump(docs[0]).rstrip() + "\n")
    atomic_write_text(OUT_DIR / "02-configmap.yaml", yaml_dump(docs[1]).rstrip() + "\n")
    atomic_write_text(OUT_DIR / "03-deployment.yaml", yaml_dump(docs[2]).rstrip() + "\n")
    atomic_write_text(OUT_DIR / "04-routes-reference.yaml", yaml_dump(docs[3]).rstrip() + "\n")
    logger.info("Manifests written to: %s", OUT_DIR)

def apply_secrets(secret: dict[str, Any]) -> None:
    logger.info("Applying secret %s in namespace %s", SECRET_NAME, NAMESPACE)
    run(["kubectl", "apply", "-f", "-"], stdin=yaml_dump(secret).rstrip() + "\n")
    logger.info("Secret %s applied successfully", SECRET_NAME)

def apply_rollout(docs: list[dict[str, Any]]) -> None:
    logger.info("Applying workload manifests to namespace %s", NAMESPACE)
    payload = "\n---\n".join(yaml_dump(d).rstrip() for d in docs) + "\n"
    run(["kubectl", "apply", "-f", "-"], stdin=payload)
    logger.info("Workload manifests applied successfully")

def delete_resources() -> None:
    logger.info("Deleting resources in namespace %s (if present)", NAMESPACE)
    run(
        [
            "kubectl",
            "delete",
            f"deployment/{DEPLOYMENT_NAME}",
            f"serviceaccount/{SERVICE_ACCOUNT}",
            f"configmap/{CONFIGMAP_NAME}",
            f"configmap/{DEPLOYMENT_NAME}-routes-reference",
            f"secret/{SECRET_NAME}",
            "-n",
            NAMESPACE,
            "--ignore-not-found=true",
        ]
    )
    logger.info("Delete command executed (ignored not found resources)")

def main() -> None:
    parser = argparse.ArgumentParser(description="Render and deploy Cloudflare Tunnel Kubernetes manifests.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="Render YAML files and apply only the tunnel secret.")
    group.add_argument("--rollout", action="store_true", help="Render YAML files, apply the tunnel secret, then apply workload manifests.")
    group.add_argument("--delete", action="store_true", help="Delete the generated tunnel resources from the cluster.")
    args = parser.parse_args()

    logger.debug("Starting build process (verbose=%s)", VERBOSE)
    docs, secret, _rendered = build()

    if args.delete:
        delete_resources()
        logger.info("Finished delete operation")
        return

    if args.write:
        write_manifests(docs)
        apply_secrets(secret)
        logger.info("Write operation complete. Manifests are available at: %s", OUT_DIR)
        return

    if args.rollout:
        write_manifests(docs)
        apply_secrets(secret)
        apply_rollout(docs)
        logger.info("Rollout operation complete. Manifests are available at: %s", OUT_DIR)
        return

if __name__ == "__main__":
    main()
