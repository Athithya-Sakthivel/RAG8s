#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Iterable

import tomllib
import yaml


DEFAULT_NAMESPACE = os.getenv("VECTOR_NAMESPACE", "logging")
DEFAULT_CLICKHOUSE_NAMESPACE = os.getenv("CLICKHOUSE_NAMESPACE", "logging")
DEFAULT_CLICKHOUSE_SERVICE = os.getenv("CLICKHOUSE_SERVICE_NAME", "clickhouse")
DEFAULT_CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_HTTP_PORT", "8123"))
DEFAULT_CLICKHOUSE_DB = os.getenv("CLICKHOUSE_DB", "logs")
DEFAULT_CLICKHOUSE_TABLE = os.getenv("CLICKHOUSE_TABLE", "inference_logs")
DEFAULT_CLICKHOUSE_SECRET_NAME = os.getenv("CLICKHOUSE_SECRET_NAME", "clickhouse-credentials")
DEFAULT_CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "vector")
DEFAULT_CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "vectorpass")

DEFAULT_VECTOR_IMAGE_REPO = os.getenv("VECTOR_IMAGE_REPO", "timberio/vector")
DEFAULT_VECTOR_IMAGE_TAG = os.getenv("VECTOR_IMAGE_TAG", "0.55.0-distroless-static")
DEFAULT_BUSYBOX_IMAGE = os.getenv("BUSYBOX_IMAGE", "busybox:1.37.0")

DEFAULT_VECTOR_DATA_DIR = os.getenv("VECTOR_DATA_DIR", "/var/lib/vector")
DEFAULT_VECTOR_METRICS_PORT = int(os.getenv("VECTOR_METRICS_PORT", "9598"))

DEFAULT_REQ_CPU = os.getenv("VECTOR_REQ_CPU", "50m")
DEFAULT_REQ_MEM = os.getenv("VECTOR_REQ_MEM", "128Mi")
DEFAULT_LIMIT_CPU = os.getenv("VECTOR_LIMIT_CPU", "200m")
DEFAULT_LIMIT_MEM = os.getenv("VECTOR_LIMIT_MEM", "256Mi")

DEFAULT_DROP_NAMESPACES = os.getenv(
    "VECTOR_DROP_NAMESPACES",
    "kube-system,cert-manager,monitoring",
)
DEFAULT_ENABLE_METRICS_EXPORTER = (
    os.getenv("VECTOR_ENABLE_METRICS_EXPORTER", "true").strip().lower() in {"1", "true", "yes", "on"}
)

DEFAULT_MANIFEST_DIR = Path(os.getenv("VECTOR_MANIFEST_DIR", "src/manifests/vector"))
DEFAULT_MANIFEST_FILE = Path(os.getenv("VECTOR_MANIFEST_FILE", str(DEFAULT_MANIFEST_DIR / "vector.yaml")))

VECTOR_LABELS = {
    "app.kubernetes.io/name": "vector",
    "app.kubernetes.io/component": "logging",
    "app.kubernetes.io/managed-by": "vector.py",
}


def _parse_csv(value: str) -> list[str]:
    return [item for item in (part.strip() for part in value.split(",")) if item]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(_toml_string(v) for v in values) + "]"


def _dedent_block(text: str) -> str:
    return textwrap.dedent(text).strip("\n")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _labels() -> dict[str, str]:
    return dict(VECTOR_LABELS)


def _namespace_bootstrap_yaml(namespace: str) -> str:
    return textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Namespace
        metadata:
          name: {namespace}
        """
    )


def _secret_yaml(namespace: str, secret_name: str, username: str, password: str) -> str:
    return textwrap.dedent(
        f"""\
        apiVersion: v1
        kind: Secret
        metadata:
          name: {secret_name}
          namespace: {namespace}
        type: Opaque
        stringData:
          username: {json.dumps(username, ensure_ascii=False)}
          password: {json.dumps(password, ensure_ascii=False)}
        """
    )


def run_checked(cmd: list[str], *, input_text: str | None = None) -> None:
    proc = subprocess.run(cmd, input=input_text, text=True, capture_output=True)
    if proc.returncode == 0:
        return

    message = [f"command failed ({proc.returncode}): {' '.join(cmd)}"]
    if proc.stdout.strip():
        message.append(f"stdout:\n{proc.stdout.strip()}")
    if proc.stderr.strip():
        message.append(f"stderr:\n{proc.stderr.strip()}")
    raise SystemExit("\n".join(message))


def render_vrl() -> str:
    return _dedent_block(
        """
        if is_string(.message) {
          parsed, err = parse_json(.message)
          if err == null && is_object(parsed) {
            . = merge!(., parsed, true)
          }
        }
        """
    )


def render_namespace_filter_condition(drop_namespaces: list[str]) -> str:
    if not drop_namespaces:
        return ""
    return _dedent_block(
        f"""
        !is_string(.kubernetes.pod_namespace) || !includes({_toml_array(drop_namespaces)}, .kubernetes.pod_namespace)
        """
    )


def render_vector_toml(
    *,
    clickhouse_namespace: str = DEFAULT_CLICKHOUSE_NAMESPACE,
    clickhouse_service: str = DEFAULT_CLICKHOUSE_SERVICE,
    clickhouse_port: int = DEFAULT_CLICKHOUSE_PORT,
    clickhouse_db: str = DEFAULT_CLICKHOUSE_DB,
    clickhouse_table: str = DEFAULT_CLICKHOUSE_TABLE,
    vector_data_dir: str = DEFAULT_VECTOR_DATA_DIR,
    metrics_port: int = DEFAULT_VECTOR_METRICS_PORT,
    drop_namespaces: list[str] | None = None,
    enable_metrics_exporter: bool = DEFAULT_ENABLE_METRICS_EXPORTER,
) -> str:
    drop_namespaces = drop_namespaces or []
    clickhouse_endpoint = f"http://{clickhouse_service}.{clickhouse_namespace}.svc.cluster.local:{clickhouse_port}"
    vrl = render_vrl()

    lines: list[str] = []
    lines.append(f'data_dir = {_toml_string(vector_data_dir)}')
    lines.append("")
    lines.append("[api]")
    lines.append("enabled = true")
    lines.append('address = "0.0.0.0:8686"')
    lines.append("")

    lines.append("[sources.kube_logs]")
    lines.append('type = "kubernetes_logs"')
    lines.append("auto_partial_merge = true")
    lines.append('self_node_name = "${VECTOR_SELF_NODE_NAME}"')
    # Prevent checkpoints from expiring; ensure old files are always eligible
    lines.append("ignore_older_secs = 1_000_000_000")
    # Re‑read existing log files from the beginning after a fresh deployment
    lines.append('read_from = "beginning"')
    lines.append("")

    lines.append("[sources.internal_metrics]")
    lines.append('type = "internal_metrics"')
    lines.append("")

    lines.append("[transforms.decode_service_schema]")
    lines.append('type = "remap"')
    lines.append('inputs = ["kube_logs"]')
    lines.append('source = """')
    lines.append(vrl)
    lines.append('"""')
    lines.append("")

    previous_input = "decode_service_schema"
    if drop_namespaces:
        condition = render_namespace_filter_condition(drop_namespaces)
        lines.append("[transforms.drop_excluded_namespaces]")
        lines.append('type = "filter"')
        lines.append('inputs = ["decode_service_schema"]')
        lines.append('condition = """')
        lines.append(condition)
        lines.append('"""')
        lines.append("")
        previous_input = "drop_excluded_namespaces"

    lines.append("[sinks.clickhouse]")
    lines.append('type = "clickhouse"')
    lines.append(f'inputs = [{_toml_string(previous_input)}]')
    lines.append(f'endpoint = {_toml_string(clickhouse_endpoint)}')
    lines.append(f'database = {_toml_string(clickhouse_db)}')
    lines.append(f'table = {_toml_string(clickhouse_table)}')
    lines.append('format = "json_each_row"')
    lines.append('compression = "gzip"')
    lines.append("skip_unknown_fields = true")
    lines.append("")
    lines.append("[sinks.clickhouse.auth]")
    lines.append('strategy = "basic"')
    lines.append('user = "${CLICKHOUSE_USER}"')
    lines.append('password = "${CLICKHOUSE_PASSWORD}"')
    lines.append("")
    lines.append("[sinks.clickhouse.batch]")
    lines.append("max_events = 200")
    lines.append("timeout_secs = 2.0")
    lines.append("")
    lines.append("[sinks.clickhouse.healthcheck]")
    lines.append("enabled = true")
    lines.append("")

    if enable_metrics_exporter:
        lines.append("[sinks.prometheus_exporter]")
        lines.append('type = "prometheus_exporter"')
        lines.append('inputs = ["internal_metrics"]')
        lines.append(f'address = "0.0.0.0:{metrics_port}"')
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def validate_vector_toml(toml_text: str) -> None:
    tomllib.loads(toml_text)


def build_manifest(
    *,
    namespace: str = DEFAULT_NAMESPACE,
    clickhouse_namespace: str = DEFAULT_CLICKHOUSE_NAMESPACE,
    clickhouse_service: str = DEFAULT_CLICKHOUSE_SERVICE,
    clickhouse_port: int = DEFAULT_CLICKHOUSE_PORT,
    clickhouse_db: str = DEFAULT_CLICKHOUSE_DB,
    clickhouse_table: str = DEFAULT_CLICKHOUSE_TABLE,
    vector_image_repo: str = DEFAULT_VECTOR_IMAGE_REPO,
    vector_image_tag: str = DEFAULT_VECTOR_IMAGE_TAG,
    busybox_image: str = DEFAULT_BUSYBOX_IMAGE,
    vector_data_dir: str = DEFAULT_VECTOR_DATA_DIR,
    metrics_port: int = DEFAULT_VECTOR_METRICS_PORT,
    req_cpu: str = DEFAULT_REQ_CPU,
    req_mem: str = DEFAULT_REQ_MEM,
    limit_cpu: str = DEFAULT_LIMIT_CPU,
    limit_mem: str = DEFAULT_LIMIT_MEM,
    drop_namespaces: list[str] | None = None,
    enable_metrics_exporter: bool = DEFAULT_ENABLE_METRICS_EXPORTER,
) -> str:
    drop_namespaces = drop_namespaces or []
    vector_toml = render_vector_toml(
        clickhouse_namespace=clickhouse_namespace,
        clickhouse_service=clickhouse_service,
        clickhouse_port=clickhouse_port,
        clickhouse_db=clickhouse_db,
        clickhouse_table=clickhouse_table,
        vector_data_dir=vector_data_dir,
        metrics_port=metrics_port,
        drop_namespaces=drop_namespaces,
        enable_metrics_exporter=enable_metrics_exporter,
    )
    validate_vector_toml(vector_toml)

    cfg_checksum = _sha256(vector_toml)
    labels = _labels()

    docs: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": "vector",
                "namespace": namespace,
                "labels": labels,
            },
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {
                "name": "vector-log-reader",
                "labels": labels,
            },
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "namespaces", "nodes"],
                    "verbs": ["list", "watch"],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {
                "name": "vector-log-reader",
                "labels": labels,
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": "vector-log-reader",
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "vector",
                    "namespace": namespace,
                }
            ],
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "vector-config",
                "namespace": namespace,
                "labels": labels,
            },
            "data": {
                "vector.toml": vector_toml,
            },
        },
    ]

    if enable_metrics_exporter:
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "vector-metrics",
                    "namespace": namespace,
                    "labels": labels,
                },
                "spec": {
                    "selector": {"app": "vector"},
                    "ports": [
                        {
                            "name": "metrics",
                            "port": metrics_port,
                            "targetPort": metrics_port,
                        }
                    ],
                    "type": "ClusterIP",
                },
            }
        )

    daemonset_doc: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": "vector",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "selector": {
                "matchLabels": {
                    "app": "vector",
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": "vector",
                        **labels,
                    },
                    "annotations": {
                        "vector/config-checksum": cfg_checksum,
                    },
                },
                "spec": {
                    "serviceAccountName": "vector",
                    "tolerations": [{"operator": "Exists"}],
                    "initContainers": [
                        {
                            "name": "fix-data-dir-permissions",
                            "image": busybox_image,
                            "command": [
                                "sh",
                                "-c",
                                f"mkdir -p {vector_data_dir} && chown -R 65534:65534 {vector_data_dir}",
                            ],
                            "volumeMounts": [
                                {
                                    "name": "data-dir",
                                    "mountPath": vector_data_dir,
                                }
                            ],
                            "securityContext": {
                                "runAsUser": 0,
                                "runAsNonRoot": False,
                                "allowPrivilegeEscalation": False,
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "vector-config",
                            "configMap": {
                                "name": "vector-config",
                                "items": [{"key": "vector.toml", "path": "vector.toml"}],
                            },
                        },
                        {
                            "name": "data-dir",
                            "hostPath": {
                                "path": vector_data_dir,
                                "type": "DirectoryOrCreate",
                            },
                        },
                        {
                            "name": "pod-logs",
                            "hostPath": {
                                "path": "/var/log/pods",
                                "type": "Directory",
                            },
                        },
                    ],
                    "containers": [
                        {
                            "name": "vector",
                            "image": f"{vector_image_repo}:{vector_image_tag}",
                            "args": ["-c", "/etc/vector/vector.toml"],
                            **(
                                {
                                    "ports": [
                                        {
                                            "name": "metrics",
                                            "containerPort": metrics_port,
                                        }
                                    ]
                                }
                                if enable_metrics_exporter
                                else {}
                            ),
                            "env": [
                                {
                                    "name": "CLICKHOUSE_USER",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": DEFAULT_CLICKHOUSE_SECRET_NAME,
                                            "key": "username",
                                        }
                                    },
                                },
                                {
                                    "name": "CLICKHOUSE_PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": DEFAULT_CLICKHOUSE_SECRET_NAME,
                                            "key": "password",
                                        }
                                    },
                                },
                                {
                                    "name": "VECTOR_SELF_NODE_NAME",
                                    "valueFrom": {
                                        "fieldRef": {
                                            "fieldPath": "spec.nodeName",
                                        }
                                    },
                                },
                            ],
                            "volumeMounts": [
                                {
                                    "name": "vector-config",
                                    "mountPath": "/etc/vector/vector.toml",
                                    "subPath": "vector.toml",
                                    "readOnly": True,
                                },
                                {
                                    "name": "data-dir",
                                    "mountPath": vector_data_dir,
                                },
                                {
                                    "name": "pod-logs",
                                    "mountPath": "/var/log/pods",
                                    "readOnly": True,
                                },
                            ],
                            "resources": {
                                "requests": {"cpu": req_cpu, "memory": req_mem},
                                "limits": {"cpu": limit_cpu, "memory": limit_mem},
                            },
                            # Production fix: group 0 allows reading host log files (owned by root:root)
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": 65534,
                                "runAsGroup": 0,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }
    docs.append(daemonset_doc)

    return "\n---\n".join(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False).rstrip() for doc in docs) + "\n"


def validate_manifest(manifest_text: str) -> None:
    list(yaml.safe_load_all(manifest_text))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def clean_legacy_artifacts(output_path: Path) -> None:
    for name in {"vector.toml", "vector.yml", "vector.json"}:
        legacy_path = output_path.parent / name
        if legacy_path.exists() and legacy_path != output_path:
            legacy_path.unlink()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and manage Vector manifests.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--generate", action="store_true", help="Render manifests and write them to disk.")
    action.add_argument("--rollout", action="store_true", help="Render, write, and apply the manifests.")
    action.add_argument("--delete", action="store_true", help="Delete only the resources created by this generator.")
    parser.add_argument("--confirm", action="store_true", help="Confirm deletion for --delete.")
    parser.add_argument("--stdout", action="store_true", help="Print the rendered Vector TOML and manifest to stdout.")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MANIFEST_FILE,
        help=f"Manifest output path (default: {DEFAULT_MANIFEST_FILE})",
    )
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--clickhouse-namespace", default=DEFAULT_CLICKHOUSE_NAMESPACE)
    parser.add_argument("--clickhouse-service", default=DEFAULT_CLICKHOUSE_SERVICE)
    parser.add_argument("--clickhouse-port", type=int, default=DEFAULT_CLICKHOUSE_PORT)
    parser.add_argument("--clickhouse-db", default=DEFAULT_CLICKHOUSE_DB)
    parser.add_argument("--clickhouse-table", default=DEFAULT_CLICKHOUSE_TABLE)
    parser.add_argument("--clickhouse-user", default=DEFAULT_CLICKHOUSE_USER)
    parser.add_argument("--clickhouse-password", default=DEFAULT_CLICKHOUSE_PASSWORD)
    parser.add_argument("--vector-image-repo", default=DEFAULT_VECTOR_IMAGE_REPO)
    parser.add_argument("--vector-image-tag", default=DEFAULT_VECTOR_IMAGE_TAG)
    parser.add_argument("--busybox-image", default=DEFAULT_BUSYBOX_IMAGE)
    parser.add_argument("--vector-data-dir", default=DEFAULT_VECTOR_DATA_DIR)
    parser.add_argument("--vector-metrics-port", type=int, default=DEFAULT_VECTOR_METRICS_PORT)
    parser.add_argument("--drop-namespaces", default=DEFAULT_DROP_NAMESPACES)
    parser.add_argument("--no-metrics-exporter", action="store_true", help="Omit the internal_metrics -> prometheus_exporter pipeline.")
    parser.add_argument("--req-cpu", default=DEFAULT_REQ_CPU)
    parser.add_argument("--req-mem", default=DEFAULT_REQ_MEM)
    parser.add_argument("--limit-cpu", default=DEFAULT_LIMIT_CPU)
    parser.add_argument("--limit-mem", default=DEFAULT_LIMIT_MEM)
    return parser


def _render_all(args: argparse.Namespace) -> tuple[str, str]:
    drop_namespaces = _parse_csv(args.drop_namespaces)

    vector_toml = render_vector_toml(
        clickhouse_namespace=args.clickhouse_namespace,
        clickhouse_service=args.clickhouse_service,
        clickhouse_port=args.clickhouse_port,
        clickhouse_db=args.clickhouse_db,
        clickhouse_table=args.clickhouse_table,
        vector_data_dir=args.vector_data_dir,
        metrics_port=args.vector_metrics_port,
        drop_namespaces=drop_namespaces,
        enable_metrics_exporter=not args.no_metrics_exporter,
    )
    validate_vector_toml(vector_toml)

    manifest_text = build_manifest(
        namespace=args.namespace,
        clickhouse_namespace=args.clickhouse_namespace,
        clickhouse_service=args.clickhouse_service,
        clickhouse_port=args.clickhouse_port,
        clickhouse_db=args.clickhouse_db,
        clickhouse_table=args.clickhouse_table,
        vector_image_repo=args.vector_image_repo,
        vector_image_tag=args.vector_image_tag,
        busybox_image=args.busybox_image,
        vector_data_dir=args.vector_data_dir,
        metrics_port=args.vector_metrics_port,
        req_cpu=args.req_cpu,
        req_mem=args.req_mem,
        limit_cpu=args.limit_cpu,
        limit_mem=args.limit_mem,
        drop_namespaces=drop_namespaces,
        enable_metrics_exporter=not args.no_metrics_exporter,
    )
    validate_manifest(manifest_text)
    return vector_toml, manifest_text


def generate(args: argparse.Namespace) -> int:
    vector_toml, manifest_text = _render_all(args)
    clean_legacy_artifacts(args.output)
    atomic_write(args.output, manifest_text)

    if args.stdout:
        sys.stdout.write("=== Vector TOML ===\n")
        sys.stdout.write(vector_toml)
        sys.stdout.write("=== End Vector TOML ===\n")
        sys.stdout.write("=== Manifest ===\n")
        sys.stdout.write(manifest_text)
        sys.stdout.write("=== End Manifest ===\n")
    else:
        print(f"[ok] wrote {args.output}")

    return 0


def rollout(args: argparse.Namespace) -> int:
    vector_toml, manifest_text = _render_all(args)
    clean_legacy_artifacts(args.output)
    atomic_write(args.output, manifest_text)

    if args.stdout:
        sys.stdout.write("=== Vector TOML ===\n")
        sys.stdout.write(vector_toml)
        sys.stdout.write("=== End Vector TOML ===\n")
        sys.stdout.write("=== Manifest ===\n")
        sys.stdout.write(manifest_text)
        sys.stdout.write("=== End Manifest ===\n")

    run_checked(["kubectl", "apply", "-f", "-"], input_text=_namespace_bootstrap_yaml(args.namespace))
    run_checked(
        ["kubectl", "apply", "-f", "-"],
        input_text=_secret_yaml(
            args.namespace,
            DEFAULT_CLICKHOUSE_SECRET_NAME,
            args.clickhouse_user,
            args.clickhouse_password,
        ),
    )
    run_checked(["kubectl", "apply", "-f", str(args.output)])

    print("[ok] rollout complete")
    return 0


def delete(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise SystemExit("--confirm is required for --delete")

    if args.output.exists():
        run_checked(["kubectl", "delete", "-f", str(args.output), "--ignore-not-found=true"])
        args.output.unlink(missing_ok=True)

    run_checked(
        [
            "kubectl",
            "-n",
            args.namespace,
            "delete",
            "secret",
            DEFAULT_CLICKHOUSE_SECRET_NAME,
            "--ignore-not-found=true",
        ]
    )

    print("[ok] deleted vector resources")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.generate:
        return generate(args)
    if args.rollout:
        return rollout(args)
    if args.delete:
        return delete(args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())