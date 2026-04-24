#!/usr/bin/env python3
# src/infra/rag/reranker_service.py
# Deterministic generator for Reranker Kubernetes manifests.
# Writes manifests to src/infra/manifests/reranker_service/
#
# Features:
# - Strong sensible defaults for PROD vs non-PROD
# - Idempotent generation using inputs hash stored in state dir
# - Atomic file writes
# - Safe kubectl apply wrapper and rollout wait
# - Clear validation and early failures
# - Type hints and robust subprocess handling

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

# -------------------- logging --------------------
LOG_LEVEL = os.environ.get("GEN_RERANKER_LOGLEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gen_reranker")

# -------------------- defaults & constants --------------------
DEFAULT_MANIFESTS_DIR = Path("src/infra/manifests/reranker_service")
DEFAULT_STATE_DIRNAME = ".state"
DEFAULTS: dict[str, Any] = {
    "ENV": "STAGING",
    "IMAGE": "ghcr.io/athithya-sakthivel/reranker:latest",
    "NAMESPACE": "models",
    "SERVICE_NAME": "reranker",
    "CONTAINER_PORT": 8202,
    "HOST": "0.0.0.0",
    "LOGLEVEL": "INFO",
    "PROD": {
        "REPLICAS": 3,
        "CPU_REQUEST": "1000m",
        "CPU_LIMIT": "4000m",
        "MEMORY_REQUEST": "1Gi",
        "MEMORY_LIMIT": "4Gi",
        "STARTUP_FAILURE_THRESHOLD": 24,
    },
    "NONPROD": {
        "REPLICAS": 1,
        "CPU_REQUEST": "250m",
        "CPU_LIMIT": "1000m",
        "MEMORY_REQUEST": "512Mi",
        "MEMORY_LIMIT": "1Gi",
        "STARTUP_FAILURE_THRESHOLD": 60,
    },
    "PROBE_PERIOD_SECONDS": 5,
    "READINESS_INITIAL_DELAY": 10,
    "LIVENESS_INITIAL_DELAY": 30,
    "PROBE_TIMEOUT_SECONDS": 3,
    "ENABLE_GPU": False,
    "GPU_RESOURCE_NAME": "nvidia.com/gpu",
    "GPU_COUNT": 1,
    "GPU_NODE_SELECTOR": "",
    "HPA_ENABLED": False,
    "HPA_MIN": 1,
    "HPA_MAX": 10,
    "HPA_TARGET_CPU": 60,
    "SA_NAME": None,
    "ROLE_NAME": None,
    "ROLEBIND_NAME": None,
    "MAX_UNAVAILABLE": "25%",
    "MAX_SURGE": "25%",
    "ROLLOUT_TIMEOUT": 300,
    "MANIFESTS_DIR": DEFAULT_MANIFESTS_DIR,
    "STATE_DIRNAME": DEFAULT_STATE_DIRNAME,
}

# -------------------- helpers --------------------


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, str(path))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def run_cmd(
    cmd: list[str],
    capture: bool = True,
    check: bool = False,
    timeout: int | None = None,
    input_text: str | None = None,
) -> tuple[int, str, str]:
    """
    Run a subprocess and return (rc, stdout, stderr).
    Uses text mode consistently and capture_output when requested.
    """
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=capture,
            text=True,
            check=False,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stdout or "", e.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, getattr(e, "stdout", "") or "", getattr(e, "stderr", "") or f"timeout after {timeout}s"


def canonical_inputs_hash(cfg: dict[str, Any]) -> str:
    serial: dict[str, Any] = {}
    for k in sorted(cfg.keys()):
        if k in ("INPUTS_HASH_PATH", "MANIFESTS_DIR", "STATE_DIRNAME"):
            continue
        v = cfg.get(k)
        try:
            json.dumps(v)
            serial[k] = v
        except Exception:
            serial[k] = str(v)
    j = json.dumps(serial, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(j.encode("utf-8")).hexdigest()


def kubectl_apply_yaml(yaml_str: str, dry_run: bool = False, timeout: int = 120) -> dict[str, Any]:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        return {"applied": False, "error": "kubectl-not-found"}
    cmd = [kubectl, "apply"]
    if dry_run:
        cmd += ["--dry-run=client", "-f", "-"]
    else:
        cmd += ["-f", "-"]
    try:
        proc = subprocess.run(cmd, input=yaml_str, capture_output=True, text=True, check=True, timeout=timeout)
        return {"applied": True, "stdout": proc.stdout or ""}
    except subprocess.CalledProcessError as e:
        return {"applied": False, "stderr": e.stderr or str(e)}
    except subprocess.TimeoutExpired as e:
        return {"applied": False, "stderr": f"timeout: {e}"}


# -------------------- config loader --------------------


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}

    env = os.environ.get("RERANKER_ENV", os.environ.get("ENV", DEFAULTS["ENV"])).upper()
    cfg["ENV"] = env
    cfg["MANIFESTS_DIR"] = Path(os.environ.get("MANIFESTS_DIR", DEFAULTS["MANIFESTS_DIR"]))
    cfg["STATE_DIRNAME"] = os.environ.get("STATE_DIRNAME", DEFAULTS["STATE_DIRNAME"])
    cfg["INPUTS_HASH_PATH"] = cfg["MANIFESTS_DIR"] / cfg["STATE_DIRNAME"] / "inputs.sha256"

    cfg["IMAGE"] = os.environ.get("RERANKER_IMAGE", DEFAULTS["IMAGE"])
    cfg["NAMESPACE"] = os.environ.get("RERANKER_NAMESPACE", DEFAULTS["NAMESPACE"])
    cfg["SERVICE_NAME"] = os.environ.get("RERANKER_SERVICE_NAME", DEFAULTS["SERVICE_NAME"])
    cfg["CONTAINER_PORT"] = int(os.environ.get("RERANKER_PORT", str(DEFAULTS["CONTAINER_PORT"])))
    cfg["HOST"] = os.environ.get("RERANKER_HOST", DEFAULTS["HOST"])
    cfg["LOGLEVEL"] = os.environ.get("RERANKER_LOGLEVEL", DEFAULTS["LOGLEVEL"])

    cfg["SA_NAME"] = os.environ.get("RERANKER_SA_NAME", f"{cfg['SERVICE_NAME']}-sa")
    cfg["ROLE_NAME"] = os.environ.get("RERANKER_ROLE_NAME", f"{cfg['SERVICE_NAME']}-role")
    cfg["ROLEBIND_NAME"] = os.environ.get("RERANKER_ROLEBIND_NAME", f"{cfg['SERVICE_NAME']}-rb")

    if env == "PROD":
        prod = DEFAULTS["PROD"]
        cfg["REPLICAS"] = int(os.environ.get("RERANKER_REPLICAS", str(prod["REPLICAS"])))
        cfg["CPU_REQUEST"] = os.environ.get("RERANKER_CPU_REQUEST", prod["CPU_REQUEST"])
        cfg["CPU_LIMIT"] = os.environ.get("RERANKER_CPU_LIMIT", prod["CPU_LIMIT"])
        cfg["MEMORY_REQUEST"] = os.environ.get("RERANKER_MEMORY_REQUEST", prod["MEMORY_REQUEST"])
        cfg["MEMORY_LIMIT"] = os.environ.get("RERANKER_MEMORY_LIMIT", prod["MEMORY_LIMIT"])
        cfg["STARTUP_FAILURE_THRESHOLD"] = int(os.environ.get("RERANKER_STARTUP_FAILURE_THRESHOLD", str(prod["STARTUP_FAILURE_THRESHOLD"])))
    else:
        nonprod = DEFAULTS["NONPROD"]
        cfg["REPLICAS"] = int(os.environ.get("RERANKER_REPLICAS", str(nonprod["REPLICAS"])))
        cfg["CPU_REQUEST"] = os.environ.get("RERANKER_CPU_REQUEST", nonprod["CPU_REQUEST"])
        cfg["CPU_LIMIT"] = os.environ.get("RERANKER_CPU_LIMIT", nonprod["CPU_LIMIT"])
        cfg["MEMORY_REQUEST"] = os.environ.get("RERANKER_MEMORY_REQUEST", nonprod["MEMORY_REQUEST"])
        cfg["MEMORY_LIMIT"] = os.environ.get("RERANKER_MEMORY_LIMIT", nonprod["MEMORY_LIMIT"])
        cfg["STARTUP_FAILURE_THRESHOLD"] = int(os.environ.get("RERANKER_STARTUP_FAILURE_THRESHOLD", str(nonprod["STARTUP_FAILURE_THRESHOLD"])))

    cfg["PROBE_PERIOD_SECONDS"] = int(os.environ.get("RERANKER_PROBE_PERIOD_SECONDS", str(DEFAULTS["PROBE_PERIOD_SECONDS"])))
    cfg["READINESS_INITIAL_DELAY"] = int(os.environ.get("RERANKER_READINESS_INITIAL_DELAY", str(DEFAULTS["READINESS_INITIAL_DELAY"])))
    cfg["LIVENESS_INITIAL_DELAY"] = int(os.environ.get("RERANKER_LIVENESS_INITIAL_DELAY", str(DEFAULTS["LIVENESS_INITIAL_DELAY"])))
    cfg["PROBE_TIMEOUT_SECONDS"] = int(os.environ.get("RERANKER_PROBE_TIMEOUT_SECONDS", str(DEFAULTS["PROBE_TIMEOUT_SECONDS"])))

    cfg["ENABLE_GPU"] = os.environ.get("RERANKER_ENABLE_GPU", str(DEFAULTS["ENABLE_GPU"])).lower() in ("1", "true", "yes")
    cfg["GPU_RESOURCE_NAME"] = os.environ.get("RERANKER_GPU_RESOURCE", DEFAULTS["GPU_RESOURCE_NAME"])
    cfg["GPU_COUNT"] = int(os.environ.get("RERANKER_GPU_COUNT", str(DEFAULTS["GPU_COUNT"])))
    cfg["GPU_NODE_SELECTOR"] = os.environ.get("RERANKER_GPU_NODE_SELECTOR", DEFAULTS["GPU_NODE_SELECTOR"])

    cfg["HPA_ENABLED"] = os.environ.get("RERANKER_HPA_ENABLED", str(DEFAULTS["HPA_ENABLED"])).lower() in ("1", "true", "yes")
    cfg["HPA_MIN"] = int(os.environ.get("RERANKER_HPA_MIN_REPLICAS", str(DEFAULTS["HPA_MIN"])))
    cfg["HPA_MAX"] = int(os.environ.get("RERANKER_HPA_MAX_REPLICAS", str(DEFAULTS["HPA_MAX"])))
    cfg["HPA_TARGET_CPU"] = int(os.environ.get("RERANKER_HPA_TARGET_CPU", str(DEFAULTS["HPA_TARGET_CPU"])))

    cfg["LABELS"] = {
        "app.kubernetes.io/name": cfg["SERVICE_NAME"],
        "app.kubernetes.io/component": "reranker",
        "app.kubernetes.io/managed-by": "gen_reranker",
        "app.kubernetes.io/instance": cfg["SERVICE_NAME"],
        "env": cfg["ENV"].lower(),
    }
    cfg["MAX_UNAVAILABLE"] = os.environ.get("RERANKER_MAX_UNAVAILABLE", DEFAULTS["MAX_UNAVAILABLE"])
    cfg["MAX_SURGE"] = os.environ.get("RERANKER_MAX_SURGE", DEFAULTS["MAX_SURGE"])
    cfg["ROLLOUT_TIMEOUT"] = int(os.environ.get("RERANKER_ROLLOUT_TIMEOUT", str(DEFAULTS["ROLLOUT_TIMEOUT"])))

    manifests_dir = cfg["MANIFESTS_DIR"]
    cfg["FILES"] = {
        "namespace": manifests_dir / "00-namespace.yaml",
        "sa_role": manifests_dir / "01-sa-role.yaml",
        "deployment": manifests_dir / "02-deployment.yaml",
        "service": manifests_dir / "03-service.yaml",
        "hpa": manifests_dir / "04-hpa.yaml",
    }

    cfg["UUID_SHORT"] = str(uuid.uuid4())[:8]
    return cfg


# -------------------- renderers --------------------


def render_namespace(cfg: dict[str, Any]) -> str:
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": cfg["NAMESPACE"], "labels": {"app.kubernetes.io/managed-by": "gen_reranker"}},
    }
    return yaml.safe_dump(ns, sort_keys=False)


def render_sa_role(cfg: dict[str, Any]) -> str:
    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": cfg["SA_NAME"], "namespace": cfg["NAMESPACE"]},
    }
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": cfg["ROLE_NAME"], "namespace": cfg["NAMESPACE"]},
        "rules": [
            {"apiGroups": [""], "resources": ["pods", "services", "endpoints", "configmaps"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]},
        ],
    }
    rb = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": cfg["ROLEBIND_NAME"], "namespace": cfg["NAMESPACE"]},
        "subjects": [{"kind": "ServiceAccount", "name": cfg["SA_NAME"], "namespace": cfg["NAMESPACE"]}],
        "roleRef": {"kind": "Role", "name": cfg["ROLE_NAME"], "apiGroup": "rbac.authorization.k8s.io"},
    }
    return "\n---\n".join([yaml.safe_dump(x, sort_keys=False) for x in (sa, role, rb)])


def render_deployment(cfg: dict[str, Any], inputs_hash: str = "") -> str:
    labels = cfg["LABELS"].copy()
    container: dict[str, Any] = {
        "name": cfg["SERVICE_NAME"],
        "image": cfg["IMAGE"],
        "ports": [{"containerPort": cfg["CONTAINER_PORT"]}],
        "env": [
            {"name": "RERANKER_PORT", "value": str(cfg["CONTAINER_PORT"])},
            {"name": "ENV", "value": cfg["ENV"]},
            {"name": "RERANKER_LOGLEVEL", "value": cfg["LOGLEVEL"]},
        ],
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": cfg["CONTAINER_PORT"]},
            "initialDelaySeconds": cfg["LIVENESS_INITIAL_DELAY"],
            "periodSeconds": cfg["PROBE_PERIOD_SECONDS"],
            "timeoutSeconds": cfg["PROBE_TIMEOUT_SECONDS"],
            "failureThreshold": 6,
        },
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": cfg["CONTAINER_PORT"]},
            "initialDelaySeconds": cfg["READINESS_INITIAL_DELAY"],
            "periodSeconds": cfg["PROBE_PERIOD_SECONDS"],
            "timeoutSeconds": cfg["PROBE_TIMEOUT_SECONDS"],
            "failureThreshold": 3,
        },
        "startupProbe": {
            "httpGet": {"path": "/health", "port": cfg["CONTAINER_PORT"]},
            "periodSeconds": cfg["PROBE_PERIOD_SECONDS"],
            "timeoutSeconds": cfg["PROBE_TIMEOUT_SECONDS"],
            "failureThreshold": cfg["STARTUP_FAILURE_THRESHOLD"],
        },
        "resources": {
            "requests": {"cpu": cfg["CPU_REQUEST"], "memory": cfg["MEMORY_REQUEST"]},
            "limits": {"cpu": cfg["CPU_LIMIT"], "memory": cfg["MEMORY_LIMIT"]},
        },
    }

    if cfg["ENABLE_GPU"]:
        container["resources"]["limits"][cfg["GPU_RESOURCE_NAME"]] = cfg["GPU_COUNT"]

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"{cfg['SERVICE_NAME']}-deployment", "namespace": cfg["NAMESPACE"], "labels": labels},
        "spec": {
            "replicas": cfg["REPLICAS"],
            "selector": {"matchLabels": {"app.kubernetes.io/name": cfg["SERVICE_NAME"]}},
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": cfg["MAX_UNAVAILABLE"], "maxSurge": cfg["MAX_SURGE"]},
            },
            "template": {
                "metadata": {"labels": labels, "annotations": {}},
                "spec": {
                    "serviceAccountName": cfg["SA_NAME"],
                    "containers": [container],
                },
            },
        },
    }

    if inputs_hash:
        deployment["spec"]["template"]["metadata"]["annotations"]["gen-reranker/inputs-hash"] = inputs_hash

    if cfg["ENABLE_GPU"] and cfg["GPU_NODE_SELECTOR"]:
        if "=" in cfg["GPU_NODE_SELECTOR"]:
            k, v = cfg["GPU_NODE_SELECTOR"].split("=", 1)
            deployment["spec"]["template"]["spec"]["nodeSelector"] = {k: v}
        else:
            deployment["spec"]["template"]["spec"]["nodeSelector"] = {cfg["GPU_NODE_SELECTOR"]: "true"}

    deployment["spec"]["template"]["metadata"].setdefault("annotations", {})
    deployment["spec"]["template"]["metadata"]["annotations"].update(
        {
            "prometheus.io/scrape": "true",
            "prometheus.io/port": str(cfg["CONTAINER_PORT"]),
            "prometheus.io/path": "/metrics",
        }
    )

    return yaml.safe_dump(deployment, sort_keys=False)


def render_service(cfg: dict[str, Any]) -> str:
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"{cfg['SERVICE_NAME']}-svc", "namespace": cfg["NAMESPACE"], "labels": cfg["LABELS"]},
        "spec": {
            "type": "ClusterIP",
            "ports": [{"port": cfg["CONTAINER_PORT"], "targetPort": cfg["CONTAINER_PORT"], "protocol": "TCP", "name": "http"}],
            "selector": {"app.kubernetes.io/name": cfg["SERVICE_NAME"]},
        },
    }
    return yaml.safe_dump(svc, sort_keys=False)


def render_hpa(cfg: dict[str, Any]) -> str:
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": f"{cfg['SERVICE_NAME']}-hpa", "namespace": cfg["NAMESPACE"]},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": f"{cfg['SERVICE_NAME']}-deployment"},
            "minReplicas": cfg["HPA_MIN"],
            "maxReplicas": cfg["HPA_MAX"],
            "metrics": [
                {"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": cfg["HPA_TARGET_CPU"]}}}
            ],
        },
    }
    return yaml.safe_dump(hpa, sort_keys=False)


# -------------------- generate / rollout / delete --------------------


def generate_manifests(cfg: dict[str, Any], dry_run: bool = False, verbose: bool = False) -> str | None:
    """
    Generate manifests to MANIFESTS_DIR.
    Returns inputs_hash if generation occurred (or was written), None if skipped.
    """
    manifests_dir: Path = cfg["MANIFESTS_DIR"]
    ensure_dir(manifests_dir)
    inputs_hash = canonical_inputs_hash(cfg)

    state_dir = manifests_dir / cfg.get("STATE_DIRNAME", DEFAULT_STATE_DIRNAME)
    ensure_dir(state_dir)
    existing: str | None = None
    try:
        inputs_path = state_dir / "inputs.sha256"
        if inputs_path.exists():
            existing = inputs_path.read_text(encoding="utf-8").strip()
    except Exception:
        existing = None

    if existing == inputs_hash and not dry_run:
        log.info("No non-secret changes detected; generation skipped.")
        return None

    ns_yaml = render_namespace(cfg)
    sa_role_yaml = render_sa_role(cfg)
    deploy_yaml = render_deployment(cfg, inputs_hash=inputs_hash)
    svc_yaml = render_service(cfg)

    atomic_write(cfg["FILES"]["namespace"], ns_yaml)
    atomic_write(cfg["FILES"]["sa_role"], sa_role_yaml)
    atomic_write(cfg["FILES"]["deployment"], deploy_yaml)
    atomic_write(cfg["FILES"]["service"], svc_yaml)
    if cfg["HPA_ENABLED"]:
        hpa_yaml = render_hpa(cfg)
        atomic_write(cfg["FILES"]["hpa"], hpa_yaml)

    (state_dir / "inputs.sha256").write_text(inputs_hash + "\n", encoding="utf-8")

    log.info("Wrote manifests to %s", str(manifests_dir))
    if verbose:
        log.info("Namespace (head):\n%s", "\n".join(ns_yaml.splitlines()[:20]))
        log.info("Deployment (head):\n%s", "\n".join(deploy_yaml.splitlines()[:60]))
    return inputs_hash


def wait_for_rollout(deployment_name: str, namespace: str, timeout: int = 300) -> int:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        log.error("kubectl not found in PATH; cannot wait for rollout")
        return 127
    cmd = [kubectl, "rollout", "status", f"deployment/{deployment_name}", "-n", namespace, f"--timeout={timeout}s"]
    rc, out, err = run_cmd(cmd, capture=True, timeout=timeout + 10)
    if rc != 0:
        log.error("rollout status failed (rc=%d). stdout=%s stderr=%s", rc, out.strip(), err.strip())
    else:
        log.info("rollout status: %s", out.strip())
    return rc


def apply_to_cluster(cfg: dict[str, Any], dry_run: bool = False, verbose: bool = False, mode_label: str = "rollout") -> None:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        log.error("kubectl not found in PATH; cannot apply")
        raise SystemExit(2) from None

    inputs_hash = generate_manifests(cfg, dry_run=dry_run, verbose=verbose)
    if dry_run:
        log.info("Dry-run: skipping kubectl apply")
        return

    if inputs_hash is None:
        log.info("No manifest changes detected; skipping kubectl apply.")
        return

    files = [cfg["FILES"]["namespace"], cfg["FILES"]["sa_role"], cfg["FILES"]["deployment"], cfg["FILES"]["service"]]
    if cfg["HPA_ENABLED"]:
        files.append(cfg["FILES"]["hpa"])

    combined = ""
    for p in files:
        if not p.exists():
            log.warning("Expected manifest missing: %s (skipping)", str(p))
            continue
        combined += f"---\n# source: {p.name}\n" + p.read_text(encoding="utf-8") + "\n"

    res = kubectl_apply_yaml(combined, dry_run=False)
    if not res.get("applied", False):
        log.error("%s failed: %s", mode_label, res.get("stderr") or res.get("error"))
        raise SystemExit(2) from None

    deployment_name = f"{cfg['SERVICE_NAME']}-deployment"
    rc = wait_for_rollout(deployment_name, cfg["NAMESPACE"], timeout=cfg.get("ROLLOUT_TIMEOUT", 300))
    if rc != 0:
        log.error("%s: rollout failed (rc=%d)", mode_label, rc)
        raise SystemExit(2) from None

    summary = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "image": cfg["IMAGE"],
        "namespace": cfg["NAMESPACE"],
        "replicas": cfg["REPLICAS"],
        "inputs_hash": inputs_hash,
        "files": {k: str(v) for k, v in cfg["FILES"].items()},
    }
    atomic_write(cfg["MANIFESTS_DIR"] / "last_deploy_summary.json", json.dumps(summary, indent=2))
    log.info("%s complete; applied manifests to cluster and wrote deploy summary", mode_label)


def delete_manifests(cfg: dict[str, Any]) -> None:
    manifests_dir: Path = cfg["MANIFESTS_DIR"]
    if manifests_dir.exists():
        for p in sorted(manifests_dir.glob("*")):
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except Exception:
                log.debug("Failed to remove %s", p, exc_info=True)
        state_dir = manifests_dir / cfg.get("STATE_DIRNAME", DEFAULT_STATE_DIRNAME)
        if state_dir.exists():
            try:
                shutil.rmtree(state_dir)
            except Exception:
                log.debug("Failed to remove state dir %s", state_dir, exc_info=True)
        log.info("Deleted manifests at %s", str(manifests_dir))
    else:
        log.info("Manifests dir not present: %s", str(manifests_dir))


# -------------------- CLI --------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate/rollout/delete Reranker Kubernetes manifests.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--generate", action="store_true", help="Generate manifests to MANIFESTS_DIR.")
    grp.add_argument("--rollout", action="store_true", help="Create or converge resources to desired state (preferred over --apply).")
    grp.add_argument("--apply", action="store_true", help="Legacy alias for --rollout (deprecated).")
    grp.add_argument("--delete", action="store_true", help="Delete generated manifests.")
    p.add_argument("--dry-run", action="store_true", help="Render and validate but do not write or apply.")
    p.add_argument("--verbose", action="store_true", help="Print extra debug info.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config()
    if args.delete:
        delete_manifests(cfg)
        return
    if args.generate:
        generate_manifests(cfg, dry_run=args.dry_run, verbose=args.verbose)
        return
    if args.rollout:
        apply_to_cluster(cfg, dry_run=args.dry_run, verbose=args.verbose, mode_label="rollout")
        return
    if args.apply:
        log.warning("--apply is deprecated; use --rollout")
        apply_to_cluster(cfg, dry_run=args.dry_run, verbose=args.verbose, mode_label="apply")
        return


if __name__ == "__main__":
    main()
