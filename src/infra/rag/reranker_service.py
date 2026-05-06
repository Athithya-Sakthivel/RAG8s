#!/usr/bin/env python3
# src/infra/rag/reranker_service.py
# Deterministic generator for Reranker Kubernetes manifests.
# - Idempotent manifest generation (inputs hash)
# - Atomic writes to disk
# - No rollout/apply performed (ArgoCD handles sync/rollout)
# - CLI: --write, --generate, --delete
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

LOG_LEVEL = os.environ.get("RERANKER_GEN_LOGLEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gen_reranker")

DEFAULT_MANIFESTS_DIR = Path("src/manifests/reranker-service")
DEFAULT_STATE_DIRNAME = ".state"

DEFAULTS: dict[str, Any] = {
    "DEPLOY_ENV": "NONPROD",
    "IMAGE": "ghcr.io/athithya-sakthivel/reranker:2026-05-06-06-26--13b7433@sha256:c2fe5136f124758462112bf7bec9f08f485ed2e53c918c4bdb6add5a777f344b",
    "NAMESPACE": "inference",
    "SERVICE_NAME": "reranker",
    "CONTAINER_PORT": 8202,
    "HOST": "0.0.0.0",
    "LOGLEVEL": "WARN",
    "RERANKER_MODEL_NAME": "Xenova/ms-marco-MiniLM-L-6-v2",
    "RERANKER_MAX_DOCS": 20,
    "RERANKER_CUDA": False,
    "PRELOAD_MODEL": False,
    "PROD": {"REPLICAS": 2, "CPU_REQUEST": "1000m", "CPU_LIMIT": "4000m", "MEMORY_REQUEST": "1Gi", "MEMORY_LIMIT": "3Gi", "STARTUP_FAILURE_THRESHOLD": 24},
    "NONPROD": {"REPLICAS": 1, "CPU_REQUEST": "250m", "CPU_LIMIT": "4000m", "MEMORY_REQUEST": "512Mi", "MEMORY_LIMIT": "4Gi", "STARTUP_FAILURE_THRESHOLD": 60},
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
    "RUN_AS_NONROOT": True,
    "RUN_AS_USER": 1000,
    "ALLOW_PRIV_ESC": False,
    "READONLY_ROOTFS": True,
    "FS_GROUP": 1000,
}


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def canonical_inputs_hash(cfg: dict[str, Any]) -> str:
    serial: dict[str, Any] = {}
    for k in sorted(cfg.keys()):
        if k in ("INPUTS_HASH_PATH", "MANIFESTS_DIR", "STATE_DIRNAME", "FILES"):
            continue
        v = cfg.get(k)
        try:
            json.dumps(v)
            serial[k] = v
        except Exception:
            serial[k] = str(v)
    j = json.dumps(serial, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(j.encode("utf-8")).hexdigest()


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    deploy_env = os.environ.get("DEPLOY_ENV") or os.environ.get("RERANKER_ENV") or os.environ.get("ENV") or DEFAULTS["DEPLOY_ENV"]
    env = str(deploy_env).upper()
    cfg["DEPLOY_ENV"] = env
    cfg["MANIFESTS_DIR"] = Path(os.environ.get("MANIFESTS_DIR", str(DEFAULTS["MANIFESTS_DIR"])))
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

    if env in ("PROD", "PRODUCTION"):
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
    cfg["ENABLE_GPU"] = _env_bool("RERANKER_ENABLE_GPU", str(DEFAULTS["ENABLE_GPU"]).lower())
    cfg["GPU_RESOURCE_NAME"] = os.environ.get("RERANKER_GPU_RESOURCE", DEFAULTS["GPU_RESOURCE_NAME"])
    cfg["GPU_COUNT"] = int(os.environ.get("RERANKER_GPU_COUNT", str(DEFAULTS["GPU_COUNT"])))
    cfg["GPU_NODE_SELECTOR"] = os.environ.get("RERANKER_GPU_NODE_SELECTOR", DEFAULTS["GPU_NODE_SELECTOR"])
    cfg["HPA_ENABLED"] = _env_bool("RERANKER_HPA_ENABLED", str(DEFAULTS["HPA_ENABLED"]).lower())
    cfg["HPA_MIN"] = int(os.environ.get("RERANKER_HPA_MIN_REPLICAS", str(DEFAULTS["HPA_MIN"])))
    cfg["HPA_MAX"] = int(os.environ.get("RERANKER_HPA_MAX_REPLICAS", str(DEFAULTS["HPA_MAX"])))
    cfg["HPA_TARGET_CPU"] = int(os.environ.get("RERANKER_HPA_TARGET_CPU", str(DEFAULTS["HPA_TARGET_CPU"])))
    cfg["RERANKER_MODEL_NAME"] = os.environ.get("RERANKER_MODEL_NAME", DEFAULTS["RERANKER_MODEL_NAME"])
    cfg["RERANKER_MAX_DOCS"] = int(os.environ.get("RERANKER_MAX_DOCS", str(DEFAULTS["RERANKER_MAX_DOCS"])))
    cfg["RERANKER_CUDA"] = _env_bool("RERANKER_CUDA", str(DEFAULTS["RERANKER_CUDA"]).lower())
    cfg["PRELOAD_MODEL"] = _env_bool("PRELOAD_MODEL", str(DEFAULTS["PRELOAD_MODEL"]).lower())
    cfg["RUN_AS_NONROOT"] = os.environ.get("RERANKER_RUN_AS_NONROOT", str(DEFAULTS["RUN_AS_NONROOT"])).lower() in ("1", "true", "yes")
    try:
        cfg["RUN_AS_USER"] = int(os.environ.get("RERANKER_RUN_AS_USER", str(DEFAULTS["RUN_AS_USER"])))
    except Exception:
        cfg["RUN_AS_USER"] = DEFAULTS["RUN_AS_USER"]
    cfg["ALLOW_PRIV_ESC"] = os.environ.get("RERANKER_ALLOW_PRIV_ESC", str(DEFAULTS["ALLOW_PRIV_ESC"])).lower() in ("1", "true", "yes")
    cfg["READONLY_ROOTFS"] = os.environ.get("RERANKER_READONLY_ROOTFS", str(DEFAULTS["READONLY_ROOTFS"])).lower() in ("1", "true", "yes")
    try:
        fs_group_env = os.environ.get("RERANKER_FS_GROUP", "")
        cfg["FS_GROUP"] = int(fs_group_env) if fs_group_env != "" else DEFAULTS["FS_GROUP"]
    except Exception:
        cfg["FS_GROUP"] = DEFAULTS["FS_GROUP"]

    cfg["LABELS"] = {
        "app.kubernetes.io/name": cfg["SERVICE_NAME"],
        "app.kubernetes.io/component": "reranker",
        "app.kubernetes.io/managed-by": "gen_reranker",
        "app.kubernetes.io/instance": cfg["SERVICE_NAME"],
        "env": cfg["DEPLOY_ENV"].lower(),
    }

    cfg["MAX_UNAVAILABLE"] = os.environ.get("RERANKER_MAX_UNAVAILABLE", DEFAULTS["MAX_UNAVAILABLE"])
    cfg["MAX_SURGE"] = os.environ.get("RERANKER_MAX_SURGE", DEFAULTS["MAX_SURGE"])
    cfg["ROLLOUT_TIMEOUT"] = int(os.environ.get("RERANKER_ROLLOUT_TIMEOUT", str(DEFAULTS["ROLLOUT_TIMEOUT"])))
    manifests_dir = cfg["MANIFESTS_DIR"]
    cfg["FILES"] = {
        "namespace": manifests_dir / "00-namespace.yaml",
        "serviceaccount": manifests_dir / "01-serviceaccount.yaml",
        "role": manifests_dir / "02-role.yaml",
        "rolebinding": manifests_dir / "03-rolebinding.yaml",
        "deployment": manifests_dir / "04-deployment.yaml",
        "service": manifests_dir / "05-service.yaml",
        "hpa": manifests_dir / "06-hpa.yaml",
    }
    cfg["UUID_SHORT"] = str(uuid.uuid4())[:8]
    log.info("Loaded config: DEPLOY_ENV=%s replicas=%d image=%s", cfg["DEPLOY_ENV"], cfg["REPLICAS"], cfg["IMAGE"])
    return cfg


def render_namespace(cfg: dict[str, Any]) -> str:
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": cfg["NAMESPACE"], "labels": {"app.kubernetes.io/managed-by": "gen_reranker"}}}
    return yaml.safe_dump(ns, sort_keys=False)


def render_serviceaccount(cfg: dict[str, Any]) -> str:
    sa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": cfg["SA_NAME"], "namespace": cfg["NAMESPACE"]}}
    return yaml.safe_dump(sa, sort_keys=False)


def render_role(cfg: dict[str, Any]) -> str:
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": cfg["ROLE_NAME"], "namespace": cfg["NAMESPACE"]},
        "rules": [
            {"apiGroups": [""], "resources": ["pods", "services", "endpoints", "configmaps"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]},
        ],
    }
    return yaml.safe_dump(role, sort_keys=False)


def render_rolebinding(cfg: dict[str, Any]) -> str:
    rb = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": cfg["ROLEBIND_NAME"], "namespace": cfg["NAMESPACE"]},
        "subjects": [{"kind": "ServiceAccount", "name": cfg["SA_NAME"], "namespace": cfg["NAMESPACE"]}],
        "roleRef": {"kind": "Role", "name": cfg["ROLE_NAME"], "apiGroup": "rbac.authorization.k8s.io"},
    }
    return yaml.safe_dump(rb, sort_keys=False)


def render_deployment(cfg: dict[str, Any], inputs_hash: str | None = None) -> str:
    labels = cfg["LABELS"].copy()
    container: dict[str, Any] = {
        "name": cfg["SERVICE_NAME"],
        "image": cfg["IMAGE"],
        "ports": [{"containerPort": cfg["CONTAINER_PORT"]}],
        "env": [
            {"name": "RERANKER_HOST", "value": str(cfg["HOST"])},
            {"name": "RERANKER_PORT", "value": str(cfg["CONTAINER_PORT"])},
            {"name": "RERANKER_LOGLEVEL", "value": str(cfg["LOGLEVEL"])},
            {"name": "RERANKER_MODEL_NAME", "value": str(cfg["RERANKER_MODEL_NAME"])},
            {"name": "RERANKER_MAX_DOCS", "value": str(cfg["RERANKER_MAX_DOCS"])},
            {"name": "RERANKER_CUDA", "value": "1" if cfg.get("RERANKER_CUDA", False) else "0"},
            {"name": "PRELOAD_MODEL", "value": "1" if cfg.get("PRELOAD_MODEL", False) else "0"},
            {"name": "ENV", "value": cfg["DEPLOY_ENV"]},
        ],
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": cfg["CONTAINER_PORT"]},
            "initialDelaySeconds": cfg["LIVENESS_INITIAL_DELAY"],
            "periodSeconds": cfg["PROBE_PERIOD_SECONDS"],
            "timeoutSeconds": cfg["PROBE_TIMEOUT_SECONDS"],
            "failureThreshold": 6,
        },
        "readinessProbe": {
            "httpGet": {"path": "/readyz", "port": cfg["CONTAINER_PORT"]},
            "initialDelaySeconds": cfg["READINESS_INITIAL_DELAY"],
            "periodSeconds": cfg["PROBE_PERIOD_SECONDS"],
            "timeoutSeconds": cfg["PROBE_TIMEOUT_SECONDS"],
            "failureThreshold": 3,
        },
        "startupProbe": {
            "httpGet": {"path": "/readyz", "port": cfg["CONTAINER_PORT"]},
            "periodSeconds": cfg["PROBE_PERIOD_SECONDS"],
            "timeoutSeconds": cfg["PROBE_TIMEOUT_SECONDS"],
            "failureThreshold": cfg["STARTUP_FAILURE_THRESHOLD"],
        },
        "resources": {"requests": {"cpu": cfg["CPU_REQUEST"], "memory": cfg["MEMORY_REQUEST"]}, "limits": {"cpu": cfg["CPU_LIMIT"], "memory": cfg["MEMORY_LIMIT"]}},
    }

    if cfg["ENABLE_GPU"]:
        container["resources"]["limits"][cfg["GPU_RESOURCE_NAME"]] = cfg["GPU_COUNT"]

    container_security: dict[str, Any] = {}
    if cfg.get("RUN_AS_NONROOT", False):
        container_security["runAsNonRoot"] = True
    if cfg.get("RUN_AS_USER") is not None:
        container_security["runAsUser"] = int(cfg["RUN_AS_USER"])
    container_security["allowPrivilegeEscalation"] = bool(cfg.get("ALLOW_PRIV_ESC", False))
    container_security["readOnlyRootFilesystem"] = bool(cfg.get("READONLY_ROOTFS", True))
    if container_security:
        container["securityContext"] = container_security

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"{cfg['SERVICE_NAME']}-deployment", "namespace": cfg["NAMESPACE"], "labels": labels},
        "spec": {
            "replicas": cfg["REPLICAS"],
            "selector": {"matchLabels": {"app.kubernetes.io/name": cfg["SERVICE_NAME"]}},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": cfg["MAX_UNAVAILABLE"], "maxSurge": cfg["MAX_SURGE"]}},
            "template": {"metadata": {"labels": labels}, "spec": {"serviceAccountName": cfg["SA_NAME"], "containers": [container]}},
        },
    }

    pod_sec: dict[str, Any] = {}
    if cfg.get("FS_GROUP") is not None:
        try:
            pod_sec["fsGroup"] = int(cfg["FS_GROUP"])
        except Exception:
            pod_sec["fsGroup"] = cfg["FS_GROUP"]
    if cfg.get("RUN_AS_NONROOT", False):
        pod_sec.setdefault("runAsNonRoot", True)
    if pod_sec:
        deployment["spec"]["template"]["spec"]["securityContext"] = pod_sec

    if inputs_hash:
        deployment["spec"]["template"]["metadata"].setdefault("annotations", {})
        deployment["spec"]["template"]["metadata"]["annotations"]["gen-reranker/inputs-hash"] = inputs_hash

    if cfg["ENABLE_GPU"] and cfg["GPU_NODE_SELECTOR"]:
        if "=" in cfg["GPU_NODE_SELECTOR"]:
            k, v = cfg["GPU_NODE_SELECTOR"].split("=", 1)
            deployment["spec"]["template"]["spec"]["nodeSelector"] = {k: v}
        else:
            deployment["spec"]["template"]["spec"]["nodeSelector"] = {cfg["GPU_NODE_SELECTOR"]: "true"}

    if cfg.get("READONLY_ROOTFS", True):
        tmp_mounts = [{"name": "tmp-writable", "mountPath": "/tmp"}, {"name": "tmp-writable", "mountPath": "/var/tmp"}, {"name": "tmp-writable", "mountPath": "/usr/tmp"}]
        existing_mounts = container.get("volumeMounts", []) or []
        for m in tmp_mounts:
            if not any(vm.get("mountPath") == m["mountPath"] for vm in existing_mounts):
                existing_mounts.append(m)
        if not any(vm.get("mountPath") == "/models_cache" for vm in existing_mounts):
            existing_mounts.append({"name": "models-cache", "mountPath": "/models_cache"})
        container["volumeMounts"] = existing_mounts

        vols = deployment["spec"]["template"]["spec"].get("volumes", []) or []
        if not any(v.get("name") == "tmp-writable" for v in vols):
            vols.append({"name": "tmp-writable", "emptyDir": {}})
        if not any(v.get("name") == "models-cache" for v in vols):
            vols.append({"name": "models-cache", "emptyDir": {}})
        deployment["spec"]["template"]["spec"]["volumes"] = vols

        pod_sc = deployment["spec"]["template"]["spec"].get("securityContext", {}) or {}
        if "fsGroup" not in pod_sc and cfg.get("FS_GROUP") is not None:
            try:
                pod_sc["fsGroup"] = int(cfg.get("FS_GROUP", 1000))
            except Exception:
                pod_sc["fsGroup"] = cfg.get("FS_GROUP", 1000)
        deployment["spec"]["template"]["spec"]["securityContext"] = pod_sc

    return yaml.safe_dump(deployment, sort_keys=False)


def render_service(cfg: dict[str, Any]) -> str:
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"{cfg['SERVICE_NAME']}-svc", "namespace": cfg["NAMESPACE"], "labels": cfg["LABELS"]}, "spec": {"type": "ClusterIP", "ports": [{"port": cfg["CONTAINER_PORT"], "targetPort": cfg["CONTAINER_PORT"], "protocol": "TCP", "name": "http"}], "selector": {"app.kubernetes.io/name": cfg["SERVICE_NAME"]}}}
    return yaml.safe_dump(svc, sort_keys=False)


def render_hpa(cfg: dict[str, Any]) -> str:
    hpa = {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": {"name": f"{cfg['SERVICE_NAME']}-hpa", "namespace": cfg["NAMESPACE"]}, "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": f"{cfg['SERVICE_NAME']}-deployment"}, "minReplicas": cfg["HPA_MIN"], "maxReplicas": cfg["HPA_MAX"], "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": cfg["HPA_TARGET_CPU"]}}}]}}
    return yaml.safe_dump(hpa, sort_keys=False)


def generate_manifests(cfg: dict[str, Any], dry_run: bool = False, verbose: bool = False) -> str | None:
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
        log.info("No changes detected; skipping manifest generation.")
        return None

    ns_yaml = render_namespace(cfg)
    sa_yaml = render_serviceaccount(cfg)
    role_yaml = render_role(cfg)
    rb_yaml = render_rolebinding(cfg)
    deploy_yaml = render_deployment(cfg, inputs_hash=inputs_hash)
    svc_yaml = render_service(cfg)

    atomic_write(cfg["FILES"]["namespace"], ns_yaml)
    atomic_write(cfg["FILES"]["serviceaccount"], sa_yaml)
    atomic_write(cfg["FILES"]["role"], role_yaml)
    atomic_write(cfg["FILES"]["rolebinding"], rb_yaml)
    atomic_write(cfg["FILES"]["deployment"], deploy_yaml)
    atomic_write(cfg["FILES"]["service"], svc_yaml)
    if cfg["HPA_ENABLED"]:
        hpa_yaml = render_hpa(cfg)
        atomic_write(cfg["FILES"]["hpa"], hpa_yaml)

    (state_dir / "inputs.sha256").write_text(inputs_hash + "\n", encoding="utf-8")
    log.info("Manifests written to %s (inputs_hash=%s)", str(manifests_dir), inputs_hash)
    if verbose:
        log.debug("Namespace manifest head:\n%s", "\n".join(ns_yaml.splitlines()[:20]))
        log.debug("Deployment manifest head:\n%s", "\n".join(deploy_yaml.splitlines()[:120]))
    return inputs_hash


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
        log.info("No manifests found at %s", str(manifests_dir))


def parse_args(argv: list[str] | None = None) -> Any:
    import argparse

    p = argparse.ArgumentParser(description="Generate or delete Reranker Kubernetes manifests. ArgoCD manages rollout.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--write", action="store_true", help="Write manifests to MANIFESTS_DIR (idempotent).")
    grp.add_argument("--generate", action="store_true", help="Alias for --write (keeps compatibility).")
    grp.add_argument("--delete", action="store_true", help="Delete generated manifests and state.")
    p.add_argument("--dry-run", action="store_true", help="Render and validate but do not write files.")
    p.add_argument("--verbose", action="store_true", help="Enable verbose debug output.")
    return p.parse_args(argv or sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    if args.verbose:
        log.setLevel(logging.DEBUG)

    try:
        if args.delete:
            log.info("Delete requested.")
            delete_manifests(cfg)
            return 0
        if args.write or args.generate:
            log.info("Write/generate requested.")
            generate_manifests(cfg, dry_run=args.dry_run, verbose=args.verbose)
            return 0
        return 1
    except Exception as exc:
        log.exception("fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
