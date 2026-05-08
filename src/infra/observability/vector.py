#!/usr/bin/env python3
"""
vector_logger.py – Vector DaemonSet for inference namespace log collection.
Ships structured JSON logs from retriever + auth/frontend → ClickHouse.

Usage:
  --generate   → write manifests to src/manifests/vector
  --rollout    → generate + apply to cluster
  --delete     → delete all resources (needs --confirm)
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
import hashlib
import textwrap
from pathlib import Path
from typing import Any
import yaml

# ═══════════════════════════════════════════════════════════════
# Environment – single source of truth
# ═══════════════════════════════════════════════════════════════
NAMESPACE          = os.getenv("VECTOR_NAMESPACE", "inference")   # Same namespace as your apps
VECTOR_IMAGE       = os.getenv("VECTOR_IMAGE", "timberio/vector:0.52.0-distroless-static")
CLICKHOUSE_SVC     = os.getenv("CLICKHOUSE_SERVICE_NAME", "clickhouse")
CLICKHOUSE_NS      = os.getenv("CLICKHOUSE_NAMESPACE", "inference")
CLICKHOUSE_PORT    = int(os.getenv("CLICKHOUSE_HTTP_PORT", "8123"))
CLICKHOUSE_SECRET  = os.getenv("CLICKHOUSE_SECRET_NAME", "clickhouse-credentials")
CH_DB              = os.getenv("CLICKHOUSE_DB", "logs")
CH_TABLE           = os.getenv("CLICKHOUSE_TABLE", "inference_logs")
CH_USER            = os.getenv("CLICKHOUSE_USER", "vector")
CH_PASS            = os.getenv("CLICKHOUSE_PASSWORD", "vectorpass")
BATCH_MAX_EVENTS   = int(os.getenv("VECTOR_BATCH_MAX_EVENTS", "200"))
BATCH_TIMEOUT_SEC  = float(os.getenv("VECTOR_BATCH_TIMEOUT_SEC", "2.0"))
REQ_CPU            = os.getenv("VECTOR_REQ_CPU", "50m")
REQ_MEM            = os.getenv("VECTOR_REQ_MEM", "128Mi")
LIMIT_CPU          = os.getenv("VECTOR_LIMIT_CPU", "200m")
LIMIT_MEM          = os.getenv("VECTOR_LIMIT_MEM", "256Mi")
METRICS_PORT       = int(os.getenv("VECTOR_METRICS_PORT", "9598"))
DATA_DIR           = os.getenv("VECTOR_DATA_DIR", "/var/lib/vector")
DROP_NAMESPACES    = os.getenv("VECTOR_DROP_NAMESPACES", "kube-system,cert-manager,monitoring")
MANIFESTS_DIR      = os.getenv("VECTOR_MANIFESTS_DIR", "src/manifests/vector")

CH_ENDPOINT = f"http://{CLICKHOUSE_SVC}.{CLICKHOUSE_NS}.svc.cluster.local:{CLICKHOUSE_PORT}"

# –– helpers ––––––––––––––––––––––––––––––––––––––––––––––––––
def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def run(cmd: list[str], timeout: int = 60, check: bool = True) -> dict:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        print("[error]", " ".join(cmd))
        if p.stdout: print(p.stdout)
        if p.stderr: print(p.stderr, file=sys.stderr)
        raise SystemExit(p.returncode)
    return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}

def ensure_dir(p: Path) -> None: p.mkdir(parents=True, exist_ok=True)

def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    (tmp := path.with_suffix(path.suffix + ".tmp")).write_text(content, encoding="utf-8")
    tmp.replace(path)

# –– VRL – minimal field mapping (apps already emit structured JSON) ––––
def build_vrl(drop_ns: list[str]) -> str:
    drop_json = json.dumps(drop_ns)
    return textwrap.dedent(f"""\
    # Parse the JSON log line emitted by the application
    parsed = parse_json(.message) ?? {{}}

    # --- Timestamp ---
    if exists(parsed.timestamp) {{
      .ts = parse_timestamp(parsed.timestamp, format: "%+") ?? now()
    }} else {{
      .ts = now()
    }}

    # --- Level ---
    .level = "INFO"
    if exists(parsed.level) && is_string(parsed.level) {{
      lvl = downcase(parsed.level) ?? "info"
      if lvl == "debug" {{ .level = "DEBUG" }}
      else if lvl == "info"  {{ .level = "INFO" }}
      else if lvl == "warn" || lvl == "warning" {{ .level = "WARN" }}
      else if lvl == "error" {{ .level = "ERROR" }}
    }}

    # --- Message ---
    if exists(parsed.message) && is_string(parsed.message) {{
      .message = parsed.message
    }} else {{
      .message = .message ?? ""
    }}

    # --- Service name ---
    if exists(parsed.service) && is_string(parsed.service) {{
      .service = parsed.service
    }} else if exists(.kubernetes.labels.app) {{
      .service = .kubernetes.labels.app
    }} else {{
      .service = .kubernetes.container_name ?? ""
    }}

    # --- Pod & Container ---
    .pod       = .kubernetes.pod_name       ?? ""
    .container = .kubernetes.container_name ?? ""

    # --- Namespace ---
    .namespace = .kubernetes.pod_namespace ?? ""

    # --- Trace context (pass through) ---
    .trace_id = parsed.trace_id ?? ""
    .span_id  = parsed.span_id  ?? ""

    # --- Extra fields as JSON string ---
    .fields = encode_json(parsed)

    # --- Drop excluded namespaces ---
    drop_namespaces = {drop_json}
    if .namespace != "" {{
      for_each(drop_namespaces) -> |_, v| {{
        if v == .namespace {{ abort }}
      }}
    }}
    """)

# –– Vector TOML config ––––––––––––––––––––––––––––––––––––––––
def build_vector_toml(drop_ns: list[str]) -> str:
    """Return Vector config as TOML (preferred format since Vector 0.38+)"""
    vrl_src = build_vrl(drop_ns)
    # Indent VRL for TOML multiline string
    vrl_indented = textwrap.indent(vrl_src, "    ")
    return textwrap.dedent(f"""\
    data_dir = "{DATA_DIR}"

    [api]
    enabled = true
    address = "0.0.0.0:8686"
    playground = false

    [sources.kube_logs]
    type = "kubernetes_logs"
    auto_partial_merge = true

    [sources.internal_metrics]
    type = "internal_metrics"

    [transforms.normalize]
    type = "remap"
    inputs = ["kube_logs"]
    source = \"\"\"
{vrl_indented}\"\"\"

    [sinks.clickhouse]
    type = "clickhouse"
    inputs = ["normalize"]
    endpoint = "{CH_ENDPOINT}"
    database = "{CH_DB}"
    table = "{CH_TABLE}"
    format = "json_each_row"
    compression = "gzip"
    skip_unknown_fields = true

    [sinks.clickhouse.auth]
    strategy = "basic"
    user = "{CH_USER}"
    password = "{CH_PASS}"

    [sinks.clickhouse.batch]
    max_events = {BATCH_MAX_EVENTS}
    timeout_secs = {BATCH_TIMEOUT_SEC}

    [sinks.clickhouse.healthcheck]
    enabled = true

    [sinks.prometheus_exporter]
    type = "prometheus_exporter"
    inputs = ["internal_metrics"]
    address = "0.0.0.0:{METRICS_PORT}"
    """)

# –– Manifest builders ––––––––––––––––––––––––––––––––––––––––––
def build_manifests(ns: str, clickhouse_ns: str, drop_ns: list[str]) -> str:
    vector_toml = build_vector_toml(drop_ns)
    cfg_checksum = sha256_str(vector_toml)

    docs = [
        # Namespace
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}},

        # ServiceAccount
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "vector", "namespace": ns}},

        # ClusterRole
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": "vector-log-reader"},
         "rules": [{"apiGroups": [""], "resources": ["pods", "pods/log", "namespaces", "nodes"],
                    "verbs": ["get", "list", "watch"]}]},

        # ClusterRoleBinding
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": "vector-log-reader"},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "vector-log-reader"},
         "subjects": [{"kind": "ServiceAccount", "name": "vector", "namespace": ns}]},

        # ConfigMap – TOML as literal block scalar (readable)
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "vector-config", "namespace": ns},
         "data": {"vector.toml": vector_toml}},   # Vector TOML is plain text, no YAML escaping needed

        # Metrics Service
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "vector-metrics", "namespace": ns, "labels": {"app": "vector"}},
         "spec": {"selector": {"app": "vector"},
                  "ports": [{"name": "metrics", "port": METRICS_PORT, "targetPort": METRICS_PORT}],
                  "type": "ClusterIP"}},
    ]

    # DaemonSet
    ds: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": "vector", "namespace": ns},
        "spec": {
            "selector": {"matchLabels": {"app": "vector"}},
            "template": {
                "metadata": {
                    "labels": {"app": "vector"},
                    "annotations": {"vector/config-checksum": cfg_checksum},
                },
                "spec": {
                    "serviceAccountName": "vector",
                    "volumes": [
                        {"name": "config", "configMap": {"name": "vector-config"}},
                        {"name": "data", "hostPath": {"path": DATA_DIR, "type": "DirectoryOrCreate"}},
                        {"name": "pod-logs", "hostPath": {"path": "/var/log/pods", "type": "DirectoryOrCreate"}},
                    ],
                    "containers": [{
                        "name": "vector",
                        "image": VECTOR_IMAGE,
                        "args": ["--config", "/etc/vector/vector.toml"],   # TOML format
                        "ports": [{"name": "metrics", "containerPort": METRICS_PORT}],
                        "volumeMounts": [
                            {"name": "config", "mountPath": "/etc/vector", "readOnly": True},
                            {"name": "data", "mountPath": DATA_DIR},
                            {"name": "pod-logs", "mountPath": "/var/log/pods", "readOnly": True},
                        ],
                        "env": [
                            {"name": "VECTOR_SELF_NODE_NAME",
                             "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                        ],
                        "resources": {
                            "requests": {"cpu": REQ_CPU, "memory": REQ_MEM},
                            "limits": {"cpu": LIMIT_CPU, "memory": LIMIT_MEM},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "readOnlyRootFilesystem": True,
                            "runAsNonRoot": True,
                            "runAsUser": 65534,
                            "runAsGroup": 65534,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }],
                },
            },
        },
    }
    docs.append(ds)

    # Render as YAML with literal block scalar for the TOML file
    return "\n---\n".join(yaml.safe_dump(d, sort_keys=False, default_flow_style=False) for d in docs)

# –– CLI actions ––––––––––––––––––––––––––––––––––––––––––––––––
MANIFEST_FILE = Path(MANIFESTS_DIR) / "vector.yaml"

def generate() -> None:
    drop_ns = [p.strip() for p in DROP_NAMESPACES.split(",") if p.strip()]
    ensure_dir(Path(MANIFESTS_DIR))
    manifest = build_manifests(NAMESPACE, CLICKHOUSE_NS, drop_ns)

    # Print the Vector TOML config to stdout for review
    print("=== Vector TOML config ===")
    print(build_vector_toml(drop_ns))
    print("=== End Vector TOML config ===")

    atomic_write(MANIFEST_FILE, manifest)
    print(f"[ok] manifests written to {MANIFEST_FILE}")

def rollout() -> None:
    drop_ns = [p.strip() for p in DROP_NAMESPACES.split(",") if p.strip()]
    ensure_dir(Path(MANIFESTS_DIR))
    manifest = build_manifests(NAMESPACE, CLICKHOUSE_NS, drop_ns)
    atomic_write(MANIFEST_FILE, manifest)

    # Print TOML for review
    print("=== Vector TOML config ===")
    print(build_vector_toml(drop_ns))
    print("=== End Vector TOML config ===")

    # Create namespace (idempotent)
    subprocess.run(
        f"kubectl create namespace {NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -",
        shell=True, capture_output=True)

    # Create/update ClickHouse credentials secret
    secret_yaml = f"""apiVersion: v1
kind: Secret
metadata:
  name: {CLICKHOUSE_SECRET}
  namespace: {NAMESPACE}
type: Opaque
stringData:
  username: "{CH_USER}"
  password: "{CH_PASS}"
"""
    subprocess.run("kubectl apply -f -", input=secret_yaml, text=True, shell=True, capture_output=True)

    # Apply manifests
    run(["kubectl", "apply", "-f", str(MANIFEST_FILE)])
    print("[ok] rollout complete")

def delete_resources(confirm: bool = False) -> None:
    if not confirm:
        print("[error] --confirm required for delete")
        sys.exit(2)
    run(["kubectl", "delete", "daemonset", "vector", "-n", NAMESPACE, "--ignore-not-found"])
    run(["kubectl", "delete", "service", "vector-metrics", "-n", NAMESPACE, "--ignore-not-found"])
    run(["kubectl", "delete", "configmap", "vector-config", "-n", NAMESPACE, "--ignore-not-found"])
    run(["kubectl", "delete", "secret", CLICKHOUSE_SECRET, "-n", NAMESPACE, "--ignore-not-found"])
    run(["kubectl", "delete", "serviceaccount", "vector", "-n", NAMESPACE, "--ignore-not-found"])
    run(["kubectl", "delete", "clusterrole", "vector-log-reader", "--ignore-not-found"])
    run(["kubectl", "delete", "clusterrolebinding", "vector-log-reader", "--ignore-not-found"])
    if MANIFEST_FILE.exists(): MANIFEST_FILE.unlink()
    print("[ok] delete complete")

# –– CLI ––––––––––––––––––––––––––––––––––––––––––––––––––––––––
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Vector logger manifest generator")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--generate", action="store_true", help="Write YAML manifests + print TOML config")
    g.add_argument("--rollout", action="store_true", help="Generate + apply to cluster")
    g.add_argument("--delete", action="store_true", help="Delete all resources")
    p.add_argument("--confirm", action="store_true", help="Confirm deletion")
    args = p.parse_args()

    if args.generate:     generate()
    elif args.rollout:    rollout()
    elif args.delete:     delete_resources(args.confirm)