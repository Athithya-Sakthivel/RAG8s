#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None


# =============================================================================
# Defaults / env
# =============================================================================
ROOT = Path(__file__).resolve().parent.parent.parent
MANIFESTS_DIR = Path(os.environ.get("MANIFESTS_DIR", "src/infra/manifests/qdrant"))
VALUES_FILE = MANIFESTS_DIR / "values.yaml"
NAMESPACE_FILE = MANIFESTS_DIR / "namespace.yaml"

QDRANT_RELEASE = os.environ.get("QDRANT_RELEASE", "qdrant")
QDRANT_NAMESPACE = os.environ.get("QDRANT_NAMESPACE", "qdrant")

QDRANT_IMAGE_REPO = os.environ.get("QDRANT_IMAGE_REPO", "docker.io/qdrant/qdrant").strip()
QDRANT_IMAGE_TAG = os.environ.get("QDRANT_IMAGE_TAG", "").strip()
QDRANT_IMAGE_PULL_POLICY = os.environ.get("QDRANT_IMAGE_PULL_POLICY", "IfNotPresent").strip()

QDRANT_REPLICAS = int(os.environ.get("QDRANT_REPLICAS", "1"))
QDRANT_PERSISTENCE_ENABLED = os.environ.get("QDRANT_PERSISTENCE_ENABLED", "true").lower() in ("1", "true", "yes", "y", "on")
QDRANT_PERSISTENCE_SIZE = os.environ.get("QDRANT_PERSISTENCE_SIZE", "20Gi").strip()
QDRANT_PERSISTENCE_STORAGE_CLASS = os.environ.get("QDRANT_PERSISTENCE_STORAGE_CLASS", "").strip()

QDRANT_METRICS_SERVICE_MONITOR = os.environ.get("QDRANT_METRICS_SERVICE_MONITOR", "false").lower() in ("1", "true", "yes", "y", "on")
QDRANT_METRICS_TARGET_PORT = os.environ.get("QDRANT_METRICS_TARGET_PORT", "http").strip()

QDRANT_ONDISK = os.environ.get("QDRANT_ONDISK", "false").lower() in ("1", "true", "yes", "y", "on")
QDRANT_LOG_LEVEL = os.environ.get("QDRANT_LOG_LEVEL", "INFO").strip()

QDRANT_STORAGE_PATH = os.environ.get("QDRANT__STORAGE__STORAGE_PATH", "").strip()
QDRANT_SNAPSHOTS_PATH = os.environ.get("QDRANT__STORAGE__SNAPSHOTS_PATH", "").strip()
QDRANT_SERVICE_ENABLE_TLS = os.environ.get("QDRANT__SERVICE__ENABLE_TLS", "").strip().lower() in ("1", "true", "yes", "y", "on")
QDRANT_API_KEY = os.environ.get("QDRANT__SERVICE__API_KEY", os.environ.get("QDRANT_API_KEY", "")).strip()

SECRET_SERVICE_NAME = os.environ.get("SECRET_SERVICE_NAME", "qdrant-service-creds").strip()
SECRET_API_KEY_KEY = os.environ.get("SECRET_API_KEY_KEY", "QDRANT__SERVICE__API_KEY").strip()

SERVICE_VALIDATION_WAIT = int(os.environ.get("SERVICE_VALIDATION_WAIT", "180"))
HELM_TIMEOUT = os.environ.get("HELM_TIMEOUT", "10m").strip()

VENDOR_CHART_DIR = os.environ.get("VENDOR_CHART_DIR", "infra/archive/qdrant-helm-chart/qdrant").strip()
CHART_REPO_NAME = os.environ.get("QDRANT_HELM_REPO_NAME", "qdrant").strip()
CHART_REPO_URL = os.environ.get("QDRANT_HELM_REPO_URL", "https://qdrant.github.io/qdrant-helm").strip()
CHART_NAME = os.environ.get("QDRANT_HELM_CHART", "qdrant").strip()
CHART_VERSION = os.environ.get("QDRANT_CHART_VERSION", "").strip()

MANAGE_DEFAULT_STORAGECLASS = os.environ.get("MANAGE_DEFAULT_STORAGECLASS", "true").lower() in ("1", "true", "yes", "y", "on")
TARGET_STORAGECLASS = os.environ.get("TARGET_STORAGECLASS", "default-storage-class").strip()

LOCAL_PATH_PROVISIONER_TAG = os.environ.get("LOCAL_PATH_PROVISIONER_TAG", "v0.0.35").strip()

VERBOSE = os.environ.get("VERBOSE", "0") != "0"
STRICT = os.environ.get("STRICT", "1").lower() in ("1", "true", "yes", "y", "on")

_TMP_FILES: list[str] = []


# =============================================================================
# Logging
# =============================================================================
def LOG(*parts: object) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(ts, *parts, flush=True)


def DBG(*parts: object) -> None:
    if VERBOSE:
        LOG(*parts)


def fatal(msg: str, code: int = 2) -> None:
    LOG("ERROR:", msg)
    raise SystemExit(code)


# =============================================================================
# File helpers
# =============================================================================
def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    _TMP_FILES.append(tmp)
    try:
        with open(tmp, "wb") as fh:
            fh.write(content)
        os.replace(tmp, str(path))
    finally:
        try:
            _TMP_FILES.remove(tmp)
        except Exception:
            pass


def atomic_write_text(path: Path, content: str) -> None:
    atomic_write_bytes(path, content.encode("utf-8"))


def safe_yaml_dump(data: Any) -> str:
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


# =============================================================================
# Subprocess helpers
# =============================================================================
def require_bin(name: str) -> None:
    if shutil.which(name) is None:
        fatal(f"{name} not found in PATH", 2)


def run_cmd(
    cmd: list[str],
    *,
    check: bool = False,
    capture: bool = False,
    timeout: int | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    DBG("run_cmd:", " ".join(cmd))
    env_used = os.environ.copy()
    if env:
        env_used.update(env)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=capture,
            text=True,
            check=False,
            timeout=timeout,
            env=env_used,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        rc = proc.returncode
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, output=out, stderr=err)
        return rc, out, err
    except subprocess.TimeoutExpired as exc:
        return 124, getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or f"timeout after {timeout}s"
    except subprocess.CalledProcessError as exc:
        return exc.returncode, getattr(exc, "output", "") or "", getattr(exc, "stderr", "") or ""


def run_cmd_capture(cmd: list[str], timeout: int | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    rc, out, _ = run_cmd(cmd, check=False, capture=True, timeout=timeout, env=env)
    return rc, out


def kubectl_json(args: list[str], timeout: int | None = None) -> Any:
    rc, out, err = run_cmd(["kubectl", *args], capture=True, timeout=timeout)
    if rc != 0:
        raise RuntimeError(err or out or f"kubectl {' '.join(args)} failed rc={rc}")
    try:
        return json.loads(out)
    except Exception as exc:
        raise RuntimeError(f"failed to parse kubectl json for {' '.join(args)}: {exc}") from exc


def kubectl_apply_text(yaml_text: str) -> None:
    rc, out, err = run_cmd(
        ["kubectl", "apply", "-f", "-"],
        capture=True,
        input_text=yaml_text,
    )
    if rc != 0:
        raise RuntimeError(err or out or "kubectl apply failed")


def kubectl_wait_rollout(namespace: str, deployment: str, timeout: str = "180s") -> bool:
    rc, _, _ = run_cmd(
        ["kubectl", "-n", namespace, "rollout", "status", f"deployment/{deployment}", f"--timeout={timeout}"],
        capture=True,
    )
    return rc == 0


# =============================================================================
# Cluster detection
# =============================================================================
def detect_cluster_mode() -> str:
    """
    Returns: kind | eks | eks-auto | unknown
    Explicit K8S_CLUSTER overrides detection.
    """
    explicit = os.environ.get("K8S_CLUSTER", "").strip().lower()
    if explicit in {"kind", "eks", "eks-auto"}:
        return explicit

    rc, _, _ = run_cmd(["kubectl", "version", "--request-timeout=5s"], capture=True)
    if rc != 0:
        fatal("kubectl cannot reach a cluster; ensure kubeconfig is configured", 2)

    node_name = ""
    provider_id = ""
    try:
        node_name = run_cmd_capture(
            ["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"],
            timeout=10,
        )[1].strip()
    except Exception:
        node_name = ""

    if node_name:
        try:
            provider_id = run_cmd_capture(
                ["kubectl", "get", "node", node_name, "-o", "jsonpath={.spec.providerID}"],
                timeout=10,
            )[1].strip()
        except Exception:
            provider_id = ""

    csidrivers = run_cmd_capture(["kubectl", "get", "csidrivers", "-o", "name"], timeout=10)[1].splitlines()
    csidrivers = [x.strip() for x in csidrivers if x.strip()]

    if any("ebs.csi.eks.amazonaws.com" in x for x in csidrivers):
        return "eks-auto"
    if provider_id.startswith("aws://") or provider_id.startswith("aws:"):
        return "eks"
    if any("ebs.csi.aws.com" in x for x in csidrivers):
        return "eks"

    if node_name.startswith("kind-") or run_cmd(["kubectl", "get", "ns", "local-path-storage"], capture=True)[0] == 0:
        return "kind"

    return "unknown"


# =============================================================================
# StorageClass validation
# =============================================================================
def list_storageclasses() -> list[dict[str, Any]]:
    data = kubectl_json(["get", "storageclass", "-o", "json"])
    return list(data.get("items", []))


def get_default_storageclasses() -> list[dict[str, Any]]:
    defaults: list[dict[str, Any]] = []
    for sc in list_storageclasses():
        md = sc.get("metadata", {}) or {}
        ann = md.get("annotations", {}) or {}
        if str(ann.get("storageclass.kubernetes.io/is-default-class", "")).lower() == "true":
            defaults.append(sc)
    return defaults


def storageclass_exists(name: str) -> bool:
    rc, _, _ = run_cmd(["kubectl", "get", "storageclass", name], capture=True)
    return rc == 0


def get_storageclass_provisioner(name: str) -> str:
    return run_cmd_capture(["kubectl", "get", "storageclass", name, "-o", "jsonpath={.provisioner}"], timeout=10)[1].strip()


def validate_default_storageclass(cluster_mode: str) -> None:
    defaults = get_default_storageclasses()
    if not defaults:
        fatal("no default StorageClass found; Qdrant persistence will not bind without a default class", 3)

    if len(defaults) > 1:
        names = [
            str((sc.get("metadata", {}) or {}).get("name", ""))
            for sc in defaults
        ]
        fatal(f"multiple default StorageClasses found: {', '.join(n for n in names if n)}", 3)

    sc = defaults[0]
    md = sc.get("metadata", {}) or {}
    name = str(md.get("name", ""))
    prov = str(sc.get("provisioner", "")).strip()

    LOG(f"default storageclass detected: {name} (provisioner={prov})")

    if cluster_mode in {"eks", "eks-auto"}:
        if prov not in {"ebs.csi.aws.com", "ebs.csi.eks.amazonaws.com"}:
            fatal(
                f"default StorageClass '{name}' uses provisioner '{prov}', expected AWS EBS CSI on EKS",
                3,
            )


def ensure_local_path_provisioner(tag: str) -> None:
    if run_cmd(["kubectl", "-n", "local-path-storage", "get", "deploy", "local-path-provisioner"], capture=True)[0] == 0:
        LOG("local-path-provisioner already installed")
        return

    LOG(f"installing local-path-provisioner {tag}")
    url = f"https://raw.githubusercontent.com/rancher/local-path-provisioner/{tag}/deploy/local-path-storage.yaml"
    rc, out, err = run_cmd(["kubectl", "apply", "-f", url], capture=True)
    if rc != 0:
        raise RuntimeError(err or out or "failed to install local-path-provisioner")

    if not kubectl_wait_rollout("local-path-storage", "local-path-provisioner", "180s"):
        LOG("warning: local-path-provisioner rollout not ready yet; continuing")


# =============================================================================
# Namespace / secret
# =============================================================================
def ensure_namespace() -> None:
    ns_doc = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": QDRANT_NAMESPACE},
    }
    atomic_write_text(NAMESPACE_FILE, safe_yaml_dump(ns_doc))
    LOG("Rendered", str(NAMESPACE_FILE))
    kubectl_apply_text(safe_yaml_dump(ns_doc))


def create_or_update_secret() -> bool:
    if not QDRANT_API_KEY:
        DBG("no QDRANT_API_KEY provided; skipping secret creation")
        return False

    secret_yaml = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": SECRET_SERVICE_NAME,
            "namespace": QDRANT_NAMESPACE,
        },
        "type": "Opaque",
        "stringData": {
            SECRET_API_KEY_KEY: QDRANT_API_KEY,
        },
    }
    rc, out, err = run_cmd(
        ["kubectl", "-n", QDRANT_NAMESPACE, "apply", "-f", "-"],
        capture=True,
        input_text=safe_yaml_dump(secret_yaml),
    )
    if rc != 0:
        raise RuntimeError(err or out or "failed to apply qdrant api key secret")
    LOG("created/updated secret", SECRET_SERVICE_NAME)
    return True


# =============================================================================
# Qdrant values
# =============================================================================
def _resources_from_env() -> dict[str, Any]:
    cpu_req = os.environ.get("QDRANT_CPU_REQUEST") or os.environ.get("QDRANT_CPU") or "1"
    cpu_lim = os.environ.get("QDRANT_CPU_LIMIT") or os.environ.get("QDRANT_CPU") or cpu_req
    mem_req = os.environ.get("QDRANT_MEMORY_REQUEST") or os.environ.get("QDRANT_MEMORY") or "2Gi"
    mem_lim = os.environ.get("QDRANT_MEMORY_LIMIT") or os.environ.get("QDRANT_MEMORY") or mem_req

    return {
        "requests": {
            "cpu": cpu_req,
            "memory": mem_req,
        },
        "limits": {
            "cpu": cpu_lim,
            "memory": mem_lim,
        },
    }


def build_qdrant_values(cluster_mode: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "replicaCount": QDRANT_REPLICAS,
        "image": {
            "repository": QDRANT_IMAGE_REPO,
            "pullPolicy": QDRANT_IMAGE_PULL_POLICY,
        },
        "service": {
            "type": "ClusterIP",
        },
        "persistence": {
            "enabled": bool(QDRANT_PERSISTENCE_ENABLED),
            "size": QDRANT_PERSISTENCE_SIZE,
            "accessModes": ["ReadWriteOnce"],
        },
        "metrics": {
            "serviceMonitor": {
                "enabled": bool(QDRANT_METRICS_SERVICE_MONITOR),
                "targetPort": QDRANT_METRICS_TARGET_PORT,
            }
        },
        "podAnnotations": {
            "app.kubernetes.io/managed-by": "qdrant_cluster.py",
        },
        "podLabels": {
            "app.kubernetes.io/managed-by": "qdrant_cluster.py",
        },
        "resources": _resources_from_env(),
        "config": {
            "cluster": {
                "enabled": True,
                "p2p": {
                    "port": 6335,
                    "enable_tls": False,
                },
                "consensus": {
                    "tick_period_ms": 100,
                },
            },
            "service": {
                "enable_tls": bool(QDRANT_SERVICE_ENABLE_TLS),
            },
            "log_level": QDRANT_LOG_LEVEL,
            "on_disk_payload": bool(QDRANT_ONDISK),
        },
        "updateVolumeFsOwnership": True,
        "podManagementPolicy": "Parallel",
        "lifecycle": {
            "preStop": {
                "exec": {
                    "command": ["sleep", "3"],
                }
            }
        },
    }

    if QDRANT_IMAGE_TAG:
        values["image"]["tag"] = QDRANT_IMAGE_TAG

    if QDRANT_STORAGE_PATH or QDRANT_SNAPSHOTS_PATH:
        values["config"]["storage"] = {}
        if QDRANT_STORAGE_PATH:
            values["config"]["storage"]["storage_path"] = QDRANT_STORAGE_PATH
        if QDRANT_SNAPSHOTS_PATH:
            values["config"]["storage"]["snapshots_path"] = QDRANT_SNAPSHOTS_PATH

    if QDRANT_API_KEY:
        values["env"] = [
            {
                "name": "QDRANT__SERVICE__API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": SECRET_SERVICE_NAME,
                        "key": SECRET_API_KEY_KEY,
                    }
                },
            }
        ]
        values["podAnnotations"]["qdrant/api-key-present"] = "true"

    if QDRANT_REPLICAS > 1:
        values["podDisruptionBudget"] = {
            "enabled": True,
            "maxUnavailable": 1,
        }
        values["topologySpreadConstraints"] = [
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": QDRANT_RELEASE,
                    }
                },
            }
        ]

    checksum = hashlib.sha256(safe_yaml_dump(values).encode("utf-8")).hexdigest()
    values["podAnnotations"]["qdrant/config-checksum"] = checksum

    if QDRANT_PERSISTENCE_STORAGE_CLASS:
        values["persistence"]["storageClass"] = QDRANT_PERSISTENCE_STORAGE_CLASS

    # Important: when QDRANT_PERSISTENCE_STORAGE_CLASS is not set, we intentionally
    # omit storageClass so Kubernetes uses the cluster default StorageClass.
    return values


def render_values_file(cluster_mode: str) -> None:
    vals = build_qdrant_values(cluster_mode)
    atomic_write_text(VALUES_FILE, safe_yaml_dump(vals))
    LOG("Rendered", str(VALUES_FILE))


# =============================================================================
# Helm
# =============================================================================
def helm_install(cluster_mode: str) -> bool:
    ensure_namespace()
    create_or_update_secret()
    render_values_file(cluster_mode)

    vendor = Path(VENDOR_CHART_DIR)
    helm_args_base = ["helm", "upgrade", "--install", QDRANT_RELEASE]

    if vendor.is_dir() and (vendor / "Chart.yaml").exists():
        LOG("Attempting vendor chart install from", str(vendor))
        cmd = [
            *helm_args_base,
            str(vendor),
            "--namespace",
            QDRANT_NAMESPACE,
            "--create-namespace",
            "-f",
            str(VALUES_FILE),
            "--atomic",
            "--wait",
            f"--timeout={HELM_TIMEOUT}",
        ]
        rc, out = run_cmd_capture(cmd)
        if rc == 0:
            LOG("helm vendor install succeeded")
            return True
        DBG("helm vendor install failed:", out)

    LOG("Using upstream helm repo for qdrant")
    run_cmd(["helm", "repo", "add", "--force-update", CHART_REPO_NAME, CHART_REPO_URL], check=False)
    run_cmd(["helm", "repo", "update"], check=False)

    chart_ref = f"{CHART_REPO_NAME}/{CHART_NAME}"
    cmd = [
        *helm_args_base,
        chart_ref,
        "--namespace",
        QDRANT_NAMESPACE,
        "--create-namespace",
        "-f",
        str(VALUES_FILE),
        "--atomic",
        "--wait",
        f"--timeout={HELM_TIMEOUT}",
    ]
    if CHART_VERSION:
        cmd.extend(["--version", CHART_VERSION])

    rc, out = run_cmd_capture(cmd)
    if rc == 0:
        LOG("helm install/upgrade succeeded")
        return True

    DBG("helm install failed:", out)
    return False


def validate_post_install() -> bool:
    """
    Basic post-install validation:
      - wait for qdrant pod(s) to be Ready
      - ensure at least one pod exists
    """
    selector = f"app.kubernetes.io/instance={QDRANT_RELEASE}"
    run_cmd(
        [
            "kubectl",
            "-n",
            QDRANT_NAMESPACE,
            "wait",
            "--for=condition=Ready",
            "pod",
            "-l",
            selector,
            f"--timeout={SERVICE_VALIDATION_WAIT}s",
        ],
        capture=True,
    )

    end = time.time() + SERVICE_VALIDATION_WAIT
    while time.time() < end:
        rc, out, _ = run_cmd(["kubectl", "-n", QDRANT_NAMESPACE, "get", "pods", "-l", selector, "-o", "json"], capture=True)
        if rc == 0:
            try:
                pj = json.loads(out)
                items = pj.get("items", [])
                if items:
                    ready_count = 0
                    for pod in items:
                        statuses = pod.get("status", {}).get("conditions", []) or []
                        if any(c.get("type") == "Ready" and c.get("status") == "True" for c in statuses):
                            ready_count += 1
                    if ready_count > 0:
                        LOG("pods ready:", ready_count)
                        return True
            except Exception:
                pass
        time.sleep(2)

    LOG("no ready qdrant pods found after validation window")
    return False


def delete_qdrant() -> None:
    run_cmd(["kubectl", "delete", "ns", QDRANT_NAMESPACE, "--ignore-not-found"], capture=True)
    if MANIFESTS_DIR.exists():
        try:
            shutil.rmtree(MANIFESTS_DIR)
        except Exception:
            DBG("failed to remove manifests dir", MANIFESTS_DIR)
    LOG("deleted qdrant namespace and rendered manifests (best-effort)")


# =============================================================================
# Main
# =============================================================================
def usage_and_exit() -> None:
    print("usage: qdrant_cluster.py --rollout|--delete", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    require_bin("kubectl")
    require_bin("helm")

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        usage_and_exit()

    cmd: str | None = None
    for a in argv:
        if a == "--rollout":
            cmd = "rollout"
        elif a == "--delete":
            cmd = "delete"
        elif a in ("--help", "-h"):
            usage_and_exit()
        else:
            usage_and_exit()

    cluster_mode = detect_cluster_mode()
    if cluster_mode == "unknown":
        fatal("cluster type could not be detected; set K8S_CLUSTER to one of: kind, eks, eks-auto", 2)

    LOG(f"cluster mode: {cluster_mode}")
    LOG(f"starting setup for release={QDRANT_RELEASE} namespace={QDRANT_NAMESPACE}")

    if cmd == "rollout":
        validate_default_storageclass(cluster_mode)
        ok = helm_install(cluster_mode)
        if not ok:
            fatal("helm install/upgrade failed", 3)

        post_ok = validate_post_install()
        if not post_ok and STRICT:
            fatal("post-install validation failed", 3)

        LOG("rollout complete")

    elif cmd == "delete":
        delete_qdrant()
    else:
        usage_and_exit()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2) from None
