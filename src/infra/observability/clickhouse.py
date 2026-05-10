#!/usr/bin/env python3
"""
clickhouse.py – production-ready ClickHouse manifest generator for RAG stack.
Native metrics at :8001/metrics, non‑root, default‑storage‑class, network policies.
Usage:
  --generate   -> write YAML files to src/manifests/clickhouse
  --rollout    -> generate + apply to cluster (kubectl)
  --delete     -> delete all resources (needs --confirm)
"""
from __future__ import annotations

import os
import sys
import json
import time
import shutil
import subprocess
import hashlib
from pathlib import Path
from typing import Dict, Any, List
import yaml

# ── Configuration (environment overrides) ─────────────────
NAMESPACE         = os.getenv("CH_NAMESPACE", "logging")
SERVICE_NAME      = os.getenv("CLICKHOUSE_SERVICE_NAME", "clickhouse")
STS_NAME          = os.getenv("CLICKHOUSE_STS_NAME", "clickhouse")
APP_LABEL         = os.getenv("CLICKHOUSE_APP_LABEL", "clickhouse")
IMAGE             = os.getenv("CLICKHOUSE_IMAGE", "docker.io/clickhouse/clickhouse-server:26.4.1@sha256:1688b8976802967d4754307b408d00ee57fe0396b40c6ffc195be228699e4fc2")
PVC_SIZE          = os.getenv("CLICKHOUSE_PVC_SIZE", "10Gi")
STORAGE_CLASS     = os.getenv("CLICKHOUSE_STORAGE_CLASS", "default-storage-class")
DB_NAME           = os.getenv("CLICKHOUSE_DB", "logs")
TABLE_NAME        = os.getenv("CLICKHOUSE_TABLE", "inference_logs")
VECTOR_USER       = os.getenv("CLICKHOUSE_USER", "vector")
VECTOR_PASS       = os.getenv("CLICKHOUSE_PASSWORD", "vectorpass")
SECRET_NAME       = os.getenv("CLICKHOUSE_SECRET_NAME", "clickhouse-credentials")
TTL_DAYS          = int(os.getenv("LOGS_TTL_DAYS", "30"))
MAX_MEM           = os.getenv("CLICKHOUSE_MAX_MEM", "12Gi")
MAX_MEM_USER      = os.getenv("CLICKHOUSE_MAX_MEM_USER", "8Gi")
MAX_THREADS       = os.getenv("CLICKHOUSE_MAX_THREADS", "2")
BG_POOL_SIZE      = os.getenv("CLICKHOUSE_BG_POOL_SIZE", "2")
REQ_CPU           = os.getenv("CLICKHOUSE_REQ_CPU", "1")
REQ_MEM           = os.getenv("CLICKHOUSE_REQ_MEM", "0.5Gi")
LIMIT_CPU         = os.getenv("CLICKHOUSE_LIMIT_CPU", "4")
LIMIT_MEM         = os.getenv("CLICKHOUSE_LIMIT_MEM", "2Gi")
INIT_TIMEOUT      = int(os.getenv("CLICKHOUSE_INIT_TIMEOUT", "300"))
MANIFESTS_DIR     = os.getenv("CH_MANIFESTS_DIR", "src/manifests/clickhouse")

RENDER_DIR       = Path(MANIFESTS_DIR).resolve()
INIT_SQL_PATH    = RENDER_DIR / "init.sql"

# ── helpers ────────────────────────────────────────────
def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def run(cmd: List[str], timeout: int = 60, check: bool = True) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            print("[error]", " ".join(cmd))
            if proc.stdout: print(proc.stdout)
            if proc.stderr: print(proc.stderr, file=sys.stderr)
            raise SystemExit(proc.returncode)
        return {"rc": proc.returncode, "out": proc.stdout.strip(), "err": proc.stderr.strip()}
    except subprocess.TimeoutExpired as e:
        return {"rc": 124, "out": "", "err": f"timeout after {timeout}s"}

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def prometheus_xml() -> str:
    return """<?xml version="1.0"?>
<clickhouse>
  <listen_host>::</listen_host>
  <prometheus>
    <port>8001</port>
    <endpoint>/metrics</endpoint>
    <metrics>true</metrics>
    <asynchronous_metrics>true</asynchronous_metrics>
    <events>true</events>
    <errors>true</errors>
    <histograms>true</histograms>
  </prometheus>
</clickhouse>"""

def users_xml() -> str:
    return f"""<?xml version="1.0"?>
<clickhouse>
  <profiles>
    <default>
      <max_memory_usage>{MAX_MEM}</max_memory_usage>
      <max_memory_usage_for_user>{MAX_MEM_USER}</max_memory_usage_for_user>
      <max_threads>{MAX_THREADS}</max_threads>
      <background_pool_size>{BG_POOL_SIZE}</background_pool_size>
    </default>
  </profiles>
</clickhouse>"""

def init_sql() -> str:
    ttl = f" TTL toDateTime(ts) + INTERVAL {TTL_DAYS} DAY" if TTL_DAYS > 0 else ""
    return f"""
CREATE DATABASE IF NOT EXISTS {DB_NAME};
CREATE TABLE IF NOT EXISTS {DB_NAME}.{TABLE_NAME} (
    ts        DateTime64(3) DEFAULT now(),
    service   String,
    pod       String,
    namespace String,
    message   String,
    fields    String,
    level     String,
    container String,
    trace_id  String,
    span_id   String
) ENGINE = MergeTree()
ORDER BY ts{ttl};
CREATE USER IF NOT EXISTS {VECTOR_USER} IDENTIFIED WITH plaintext_password BY '{VECTOR_PASS}';
GRANT INSERT ON {DB_NAME}.* TO {VECTOR_USER};
GRANT SELECT ON {DB_NAME}.* TO {VECTOR_USER};
"""

# ── manifest builders ──────────────────────────────────
def build_namespace() -> Dict[str, Any]:
    return {"apiVersion":"v1","kind":"Namespace","metadata":{"name":NAMESPACE}}

def build_service() -> Dict[str, Any]:
    return {
        "apiVersion":"v1","kind":"Service",
        "metadata":{"name":SERVICE_NAME,"namespace":NAMESPACE},
        "spec":{
            "selector":{"app":APP_LABEL},
            "ports":[
                {"name":"http","port":8123,"targetPort":8123},
                {"name":"tcp","port":9000,"targetPort":9000},
                {"name":"metrics","port":8001,"targetPort":8001},
            ],
            "type":"ClusterIP"
        }
    }

def build_networkpolicy() -> Dict[str, Any]:
    return {
        "apiVersion":"networking.k8s.io/v1",
        "kind":"NetworkPolicy",
        "metadata":{"name":"clickhouse","namespace":NAMESPACE},
        "spec":{
            "podSelector":{"matchLabels":{"app":APP_LABEL}},
            "policyTypes":["Ingress","Egress"],
            "ingress":[
                {
                    "from":[
                        {"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":NAMESPACE}},
                         "podSelector":{"matchLabels":{"app":"vector"}}}
                    ],
                    "ports":[{"port":8123,"protocol":"TCP"}]
                },
                {
                    "from":[
                        {"namespaceSelector":{"matchLabels":{"kubernetes.io/metadata.name":"monitoring"}},
                         "podSelector":{"matchLabels":{"app.kubernetes.io/name":"prometheus"}}}
                    ],
                    "ports":[{"port":8001,"protocol":"TCP"}]
                }
            ],
            "egress":[
                {
                    "to":[
                        {"namespaceSelector":{},"podSelector":{"matchLabels":{"k8s-app":"kube-dns"}}}
                    ],
                    "ports":[{"port":53,"protocol":"UDP"},{"port":53,"protocol":"TCP"}]
                },
                {
                    "to":[{"podSelector":{"matchLabels":{"app":APP_LABEL}}}]
                }
            ]
        }
    }

def build_statefulset() -> Dict[str, Any]:
    labels = {"app": APP_LABEL}
    chests = {
        "prometheus-config": sha256_str(prometheus_xml()),
        "users-config": sha256_str(users_xml()),
        "init-sql": sha256_str(init_sql()),
        "image": sha256_str(IMAGE),
    }
    annotations = {f"checksum/{k}": v for k,v in chests.items()}

    container = {
        "name":"clickhouse",
        "image":IMAGE,
        "ports":[
            {"containerPort":8123,"name":"http"},
            {"containerPort":9000,"name":"tcp"},
            {"containerPort":8001,"name":"metrics"},
        ],
        "volumeMounts":[
            {"name":"data","mountPath":"/var/lib/clickhouse"},
            {"name":"clickhouse-logs","mountPath":"/var/log/clickhouse-server"},
            {"name":"prometheus-config","mountPath":"/etc/clickhouse-server/config.d","readOnly":True},
            {"name":"users-d","mountPath":"/etc/clickhouse-server/users.d"},
            {"name":"users-config","mountPath":"/etc/clickhouse-server/users.d/10-settings.xml","subPath":"10-settings.xml","readOnly":True},
            {"name":"tmp","mountPath":"/tmp"},
        ],
        "resources":{
            "requests":{"cpu":REQ_CPU,"memory":REQ_MEM},
            "limits":{"cpu":LIMIT_CPU,"memory":LIMIT_MEM},
        },
        "securityContext":{
            "allowPrivilegeEscalation":False,
            "readOnlyRootFilesystem":True,
            "runAsNonRoot":True,
            "runAsUser":101,
            "runAsGroup":101,
            "capabilities":{"drop":["ALL"]},
        },
        "livenessProbe":{
            "exec":{"command":["bash","-c","clickhouse-client --query 'SELECT 1' || exit 1"]},
            "initialDelaySeconds":15,"periodSeconds":20,"timeoutSeconds":5,
        },
        "readinessProbe":{
            "exec":{"command":["bash","-c","clickhouse-client --query 'SELECT 1' || exit 1"]},
            "initialDelaySeconds":10,"periodSeconds":10,"timeoutSeconds":3,
        },
    }

    return {
        "apiVersion":"apps/v1",
        "kind":"StatefulSet",
        "metadata":{"name":STS_NAME,"namespace":NAMESPACE},
        "spec":{
            "serviceName":SERVICE_NAME,
            "replicas":1,
            "selector":{"matchLabels":labels},
            "template":{
                "metadata":{"labels":labels,"annotations":annotations},
                "spec":{
                    "securityContext":{"fsGroup":101},
                    "containers":[container],
                    "volumes":[
                        {"name":"clickhouse-logs","emptyDir":{}},
                        {"name":"prometheus-config","configMap":{"name":f"{STS_NAME}-prometheus-settings"}},
                        {"name":"users-d","emptyDir":{}},
                        {"name":"users-config","configMap":{"name":f"{STS_NAME}-users-settings","items":[{"key":"10-settings.xml","path":"10-settings.xml"}]}},
                        {"name":"tmp","emptyDir":{}},
                    ],
                },
            },
            "volumeClaimTemplates":[
                {
                    "metadata":{"name":"data"},
                    "spec":{
                        "accessModes":["ReadWriteOnce"],
                        "storageClassName":STORAGE_CLASS,
                        "resources":{"requests":{"storage":PVC_SIZE}},
                    },
                }
            ],
        },
    }

def build_prometheus_configmap() -> Dict[str, Any]:
    return {
        "apiVersion":"v1",
        "kind":"ConfigMap",
        "metadata":{"name":f"{STS_NAME}-prometheus-settings","namespace":NAMESPACE},
        "data":{"prometheus.xml":prometheus_xml()},
    }

def build_users_configmap() -> Dict[str, Any]:
    return {
        "apiVersion":"v1",
        "kind":"ConfigMap",
        "metadata":{"name":f"{STS_NAME}-users-settings","namespace":NAMESPACE},
        "data":{"10-settings.xml":users_xml()},
    }

# ── generate / apply ───────────────────────────────────
def clean_manifest_dir() -> None:
    if RENDER_DIR.exists():
        for f in RENDER_DIR.iterdir():
            if f.is_file():
                f.unlink()
                print(f"  removed stale {f}")

def generate_manifests():
    ensure_dir(RENDER_DIR)
    clean_manifest_dir()
    atomic_write(RENDER_DIR/"00-namespace.yaml", yaml.safe_dump(build_namespace(), sort_keys=False))
    atomic_write(RENDER_DIR/"10-service.yaml", yaml.safe_dump(build_service(), sort_keys=False))
    atomic_write(RENDER_DIR/"20-prometheus-configmap.yaml", yaml.safe_dump(build_prometheus_configmap(), sort_keys=False))
    atomic_write(RENDER_DIR/"21-users-configmap.yaml", yaml.safe_dump(build_users_configmap(), sort_keys=False))
    atomic_write(RENDER_DIR/"30-statefulset.yaml", yaml.safe_dump(build_statefulset(), sort_keys=False))
    atomic_write(RENDER_DIR/"31-networkpolicy.yaml", yaml.safe_dump(build_networkpolicy(), sort_keys=False))
    atomic_write(INIT_SQL_PATH, init_sql())
    print("[ok] manifests written to", RENDER_DIR)

def apply_manifests():
    if not shutil.which("kubectl"):
        sys.exit("kubectl not found")
    generate_manifests()
    run(["kubectl","apply","-f",str(RENDER_DIR/"00-namespace.yaml")])
    run(["kubectl","apply","-f",str(RENDER_DIR/"20-prometheus-configmap.yaml")])
    run(["kubectl","apply","-f",str(RENDER_DIR/"21-users-configmap.yaml")])
    secret_yaml = f"""apiVersion: v1
kind: Secret
metadata:
  name: {SECRET_NAME}
  namespace: {NAMESPACE}
type: Opaque
stringData:
  username: {VECTOR_USER}
  password: {VECTOR_PASS}"""
    rc = subprocess.run(["kubectl","apply","-f","-"], input=secret_yaml, text=True, capture_output=True)
    if rc.returncode != 0:
        print("[warn] secret apply failed:", rc.stderr)
    run(["kubectl","apply","-f",str(RENDER_DIR/"10-service.yaml")])
    run(["kubectl","apply","-f",str(RENDER_DIR/"30-statefulset.yaml")])
    run(["kubectl","apply","-f",str(RENDER_DIR/"31-networkpolicy.yaml")])
    run(["kubectl","rollout","status",f"statefulset/{STS_NAME}","-n",NAMESPACE,f"--timeout={INIT_TIMEOUT}s"])
    rc = run(["kubectl","get","pods","-n",NAMESPACE,"-l",f"app={APP_LABEL}","-o","json"], timeout=10)
    pods = json.loads(rc["out"]).get("items",[])
    if not pods:
        raise SystemExit("no clickhouse pod found")
    pod = pods[0]["metadata"]["name"]
    for _ in range(INIT_TIMEOUT//2):
        r = run(["kubectl","exec","-n",NAMESPACE,pod,"--","clickhouse-client","--query","SELECT 1"], check=False, timeout=10)
        if r["rc"]==0 and "1" in r["out"]:
            break
        time.sleep(2)
    else:
        raise SystemExit("clickhouse not ready after timeout")
    sql = init_sql().replace("'", "'\\''")
    run(["kubectl","exec","-n",NAMESPACE,pod,"--","bash","-c",f"clickhouse-client --multiquery --query '{sql}'"], timeout=60)
    print("[ok] clickhouse deployed and initialized")

def delete_manifests(confirm=False):
    if not confirm:
        print("[error] --confirm required for delete")
        sys.exit(2)
    run(["kubectl","delete","statefulset",STS_NAME,"-n",NAMESPACE,"--ignore-not-found"])
    run(["kubectl","delete","service",SERVICE_NAME,"-n",NAMESPACE,"--ignore-not-found"])
    run(["kubectl","delete","networkpolicy","clickhouse","-n",NAMESPACE,"--ignore-not-found"])
    run(["kubectl","delete","configmap",f"{STS_NAME}-users-settings","-n",NAMESPACE,"--ignore-not-found"])
    run(["kubectl","delete","configmap",f"{STS_NAME}-prometheus-settings","-n",NAMESPACE,"--ignore-not-found"])
    run(["kubectl","delete","secret",SECRET_NAME,"-n",NAMESPACE,"--ignore-not-found"])
    run(["kubectl","delete","namespace",NAMESPACE,"--ignore-not-found"])
    clean_manifest_dir()
    print("[ok] clickhouse resources deleted")

# ── CLI ────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="ClickHouse manifest generator (RAG stack)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--generate", action="store_true", help="Write YAML files")
    g.add_argument("--rollout", action="store_true", help="Generate + apply to cluster")
    g.add_argument("--delete", action="store_true")
    p.add_argument("--confirm", action="store_true")
    args = p.parse_args()

    if args.generate:
        generate_manifests()
    elif args.rollout:
        apply_manifests()
    elif args.delete:
        delete_manifests(args.confirm)
