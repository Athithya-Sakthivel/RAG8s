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

try:
    import yaml
except Exception as exc:  # pragma: no cover
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from exc

ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF_PATH = ROOT / "src" / "infra" / "network" / "nginx.conf"
OUT_DIR = ROOT / "src" / "manifests" / "nginx"

NAMESPACE = os.getenv("NAMESPACE", "inference").strip() or "inference"
FRONTEND_IMAGE = os.getenv(
    "FRONTEND_IMAGE",
    "ghcr.io/athithya-sakthivel/frontend:2026-05-03-20-44--a39efe0@sha256:f220b3701f2d64f6a7a261db3c0dcfac6059d067b726d87b3a9c1e599464dc12",
).strip()
SERVICE_NAME = os.getenv("SERVICE_NAME", "frontend-nginx").strip() or "frontend-nginx"
SERVICE_ACCOUNT = os.getenv("SERVICE_ACCOUNT", "frontend-nginx").strip() or "frontend-nginx"
CONFIGMAP_NAME = os.getenv("CONFIGMAP_NAME", "frontend-nginx-config").strip() or "frontend-nginx-config"

REPLICAS = int(os.getenv("REPLICAS", "2"))
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8080"))
CONTAINER_PORT = int(os.getenv("CONTAINER_PORT", "8080"))

DOMAIN = os.getenv("DOMAIN", "athithya.site").strip().rstrip(".") or "athithya.site"
APP_HOST = os.getenv("APP_HOST", DOMAIN).strip().rstrip(".") or DOMAIN
API_HOST = os.getenv("API_HOST", "api.athithya.site").strip().rstrip(".") or "api.athithya.site"
AUTH_HOST = os.getenv("AUTH_HOST", "").strip().rstrip(".") or ""

ZITADEL_NAMESPACE = os.getenv("ZITADEL_NAMESPACE", "zitadel").strip() or "zitadel"
ZITADEL_SERVICE_NAME = os.getenv("ZITADEL_SERVICE_NAME", "zitadel").strip() or "zitadel"
ZITADEL_SERVICE_PORT = int(os.getenv("ZITADEL_SERVICE_PORT", "8080"))

STARTUP_FAILURE_THRESHOLD = int(os.getenv("STARTUP_FAILURE_THRESHOLD", "36"))
STARTUP_PERIOD_SECONDS = int(os.getenv("STARTUP_PERIOD_SECONDS", "5"))
READINESS_INITIAL_DELAY_SECONDS = int(os.getenv("READINESS_INITIAL_DELAY_SECONDS", "5"))
READINESS_PERIOD_SECONDS = int(os.getenv("READINESS_PERIOD_SECONDS", "5"))
LIVENESS_INITIAL_DELAY_SECONDS = int(os.getenv("LIVENESS_INITIAL_DELAY_SECONDS", "15"))
LIVENESS_PERIOD_SECONDS = int(os.getenv("LIVENESS_PERIOD_SECONDS", "10"))

# Minimal forbidden tokens for safety
FORBIDDEN_TOKENS = (
    "$escaped_request_uri",
    "$escaped_uri",
    "escaped_request_uri",
    "escaped_uri",
    "auth_request",
    "outpost.goauthentik.io",
)

class Dumper(yaml.SafeDumper):
    pass

def _str_representer(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

Dumper.add_representer(str, _str_representer)

def fatal(msg: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)

def require(condition: bool, message: str) -> None:
    if not condition:
        fatal(message)

def to_yaml(obj: Any) -> str:
    return yaml.dump(
        obj,
        Dumper=Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
        indent=2,
    )

def sha256_obj(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def safe_write(path: Path, content: str) -> None:
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

def run(cmd: list[str], stdin: str | None = None) -> None:
    subprocess.run(cmd, input=stdin, text=True, check=True)

def load_nginx_conf() -> str:
    require(NGINX_CONF_PATH.exists(), f"nginx.conf not found at {NGINX_CONF_PATH}")
    return NGINX_CONF_PATH.read_text(encoding="utf-8")

def validate_nginx_conf(nginx_conf: str) -> None:
    """
    Pragmatic validation:
      - ensure file exists and does not contain clearly forbidden tokens
      - ensure minimal structural markers exist so generated manifests are sensible
    This avoids brittle checks that would reject valid, environment-driven configs.
    """
    bad = [token for token in FORBIDDEN_TOKENS if token in nginx_conf]
    require(not bad, f"nginx.conf contains forbidden token(s): {', '.join(bad)}")

    require("listen 8080;" in nginx_conf, "nginx.conf must listen on 8080")
    require("server_name" in nginx_conf, "nginx.conf must declare server_name")
    require("try_files $uri $uri/ /index.html;" in nginx_conf, "nginx.conf must serve the SPA index fallback")

def render_serviceaccount(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": SERVICE_ACCOUNT,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/component": "frontend",
            },
        },
    }

def render_configmap(namespace: str, nginx_conf: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CONFIGMAP_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/component": "frontend",
            },
        },
        "data": {
            "nginx.conf": nginx_conf,
        },
    }

def render_service(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": SERVICE_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/component": "frontend",
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/component": "frontend",
            },
            "ports": [
                {
                    "name": "http",
                    "port": SERVICE_PORT,
                    "protocol": "TCP",
                    "targetPort": CONTAINER_PORT,
                }
            ],
        },
    }

def render_deployment(namespace: str, image: str, replicas: int, checksum: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": SERVICE_NAME,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/component": "frontend",
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {
                "matchLabels": {
                    "app.kubernetes.io/name": SERVICE_NAME,
                    "app.kubernetes.io/component": "frontend",
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": SERVICE_NAME,
                        "app.kubernetes.io/component": "frontend",
                    },
                    "annotations": {
                        "nginx/config-checksum": checksum,
                    },
                },
                "spec": {
                    "serviceAccountName": SERVICE_ACCOUNT,
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {
                        "fsGroup": 101,
                    },
                    "volumes": [
                        {
                            "name": "nginx-config",
                            "configMap": {
                                "name": CONFIGMAP_NAME,
                                "items": [{"key": "nginx.conf", "path": "nginx.conf"}],
                            },
                        },
                        {
                            "name": "tmp",
                            "emptyDir": {},
                        },
                    ],
                    "containers": [
                        {
                            "name": "nginx",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [
                                {
                                    "name": "http",
                                    "containerPort": CONTAINER_PORT,
                                    "protocol": "TCP",
                                }
                            ],
                            "volumeMounts": [
                                {
                                    "name": "nginx-config",
                                    "mountPath": "/etc/nginx/nginx.conf",
                                    "subPath": "nginx.conf",
                                    "readOnly": True,
                                },
                                {
                                    "name": "tmp",
                                    "mountPath": "/tmp",
                                },
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": CONTAINER_PORT},
                                "initialDelaySeconds": READINESS_INITIAL_DELAY_SECONDS,
                                "periodSeconds": READINESS_PERIOD_SECONDS,
                                "failureThreshold": 3,
                                "timeoutSeconds": 1,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": CONTAINER_PORT},
                                "initialDelaySeconds": LIVENESS_INITIAL_DELAY_SECONDS,
                                "periodSeconds": LIVENESS_PERIOD_SECONDS,
                                "failureThreshold": 3,
                                "timeoutSeconds": 1,
                            },
                            "startupProbe": {
                                "httpGet": {"path": "/healthz", "port": CONTAINER_PORT},
                                "periodSeconds": STARTUP_PERIOD_SECONDS,
                                "failureThreshold": STARTUP_FAILURE_THRESHOLD,
                                "timeoutSeconds": 1,
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 101,
                                "runAsGroup": 101,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }

def render_routes_reference() -> dict[str, Any]:
    frontend_origin = f"http://{SERVICE_NAME}.{NAMESPACE}.svc.cluster.local:{SERVICE_PORT}"
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{SERVICE_NAME}-routes-reference",
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": SERVICE_NAME,
                "app.kubernetes.io/component": "frontend",
            },
        },
        "data": {
            "routes.yaml": "\n".join(
                [
                    "tunnel: default-tunnel-1",
                    "ingress:",
                    f"  - hostname: {API_HOST}",
                    f"    service: {frontend_origin}",
                    f"  - hostname: {APP_HOST}",
                    f"    service: {frontend_origin}",
                    "  - service: http_status:404",
                ]
            )
            + "\n",
        },
    }

def build() -> tuple[list[dict[str, Any]], str]:
    require(bool(FRONTEND_IMAGE), "FRONTEND_IMAGE is required")
    require(bool(DOMAIN), "DOMAIN is required")
    require(REPLICAS > 0, "REPLICAS must be greater than 0")
    require(SERVICE_PORT > 0, "SERVICE_PORT must be greater than 0")
    require(CONTAINER_PORT > 0, "CONTAINER_PORT must be greater than 0")
    require(SERVICE_PORT == CONTAINER_PORT, "SERVICE_PORT and CONTAINER_PORT must match")

    nginx_conf = load_nginx_conf()
    validate_nginx_conf(nginx_conf)

    configmap = render_configmap(NAMESPACE, nginx_conf)
    checksum = sha256_obj(
        {
            "namespace": NAMESPACE,
            "image": FRONTEND_IMAGE,
            "replicas": REPLICAS,
            "service_port": SERVICE_PORT,
            "container_port": CONTAINER_PORT,
            "service_name": SERVICE_NAME,
            "service_account": SERVICE_ACCOUNT,
            "configmap_name": CONFIGMAP_NAME,
            "domain": DOMAIN,
            "app_host": APP_HOST,
            "api_host": API_HOST,
            "auth_host": AUTH_HOST,
            "nginx_conf": nginx_conf,
        }
    )

    docs = [
        render_serviceaccount(NAMESPACE),
        configmap,
        render_service(NAMESPACE),
        render_deployment(NAMESPACE, FRONTEND_IMAGE, REPLICAS, checksum),
        render_routes_reference(),
    ]
    rendered = "\n---\n".join(to_yaml(d).rstrip() for d in docs) + "\n"
    return docs, rendered

def write_manifests(docs: list[dict[str, Any]]) -> None:
    # Overwrite files atomically to avoid stale YAMLs
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_write(OUT_DIR / "02-serviceaccount.yaml", to_yaml(docs[0]).rstrip() + "\n")
    safe_write(OUT_DIR / "03-configmap.yaml", to_yaml(docs[1]).rstrip() + "\n")
    safe_write(OUT_DIR / "04-service.yaml", to_yaml(docs[2]).rstrip() + "\n")
    safe_write(OUT_DIR / "05-deployment.yaml", to_yaml(docs[3]).rstrip() + "\n")
    safe_write(OUT_DIR / "06-routes-reference.yaml", to_yaml(docs[4]).rstrip() + "\n")

def apply_rollout(docs: list[dict[str, Any]]) -> None:
    payload = "\n---\n".join(to_yaml(d).rstrip() for d in docs) + "\n"
    run(["kubectl", "apply", "-f", "-"], stdin=payload)

def destroy() -> None:
    run(
        [
            "kubectl",
            "delete",
            "deployment/" + SERVICE_NAME,
            "service/" + SERVICE_NAME,
            "serviceaccount/" + SERVICE_ACCOUNT,
            "configmap/" + CONFIGMAP_NAME,
            f"configmap/{SERVICE_NAME}-routes-reference",
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
