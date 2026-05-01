from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml

LOG_LEVEL = os.environ.get("DENSE_GEN_LOGLEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gen_dense")

DEFAULT_MANIFESTS_DIR = Path("src/manifests/dense-service")
DEFAULT_STATE_DIRNAME = ".state"
DEFAULTS: dict[str, Any] = {
    "DEPLOY_ENV": "NONPROD",
    "IMAGE": "ghcr.io/athithya-sakthivel/dense:2026-04-26-08-57--a0cce10@sha256:5b804638527906701a4065b27071a237c3783ca3c9ef4bf5e94dc233e5dc7f7f",
    "NAMESPACE": "inference",
    "SERVICE_NAME": "dense",
    "CONTAINER_PORT": 8200,
    "HOST": "0.0.0.0",
    "LOGLEVEL": "WARN",
    "DENSE_MODEL_NAME": "BAAI/bge-small-en-v1.5",
    "DENSE_DIM": 384,
    "DENSE_BATCH_SIZE": 16,
    "DENSE_NORMALIZE": True,
    "DENSE_CUDA": False,
    "PROD": {
        "REPLICAS": 2,
        "CPU_REQUEST": "1000m",
        "CPU_LIMIT": "4000m",
        "MEMORY_REQUEST": "1Gi",
        "MEMORY_LIMIT": "2Gi",
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


def run_cmd(
    cmd: list[str],
    capture: bool = True,
    check: bool = False,
    timeout: int | None = None,
    input_text: str | None = None,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=capture,
            text=True,
            check=check,
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


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}

    # DEPLOY_ENV precedence: DEPLOY_ENV -> DENSE_ENV -> ENV -> default
    deploy_env = os.environ.get("DEPLOY_ENV") or os.environ.get("DENSE_ENV") or os.environ.get("ENV") or DEFAULTS["DEPLOY_ENV"]
    env = str(deploy_env).upper()
    cfg["DEPLOY_ENV"] = env

    cfg["MANIFESTS_DIR"] = Path(os.environ.get("MANIFESTS_DIR", str(DEFAULTS["MANIFESTS_DIR"])))
    cfg["STATE_DIRNAME"] = os.environ.get("STATE_DIRNAME", DEFAULTS["STATE_DIRNAME"])
    cfg["INPUTS_HASH_PATH"] = cfg["MANIFESTS_DIR"] / cfg["STATE_DIRNAME"] / "inputs.sha256"

    cfg["IMAGE"] = os.environ.get("DENSE_IMAGE", DEFAULTS["IMAGE"])
    cfg["NAMESPACE"] = os.environ.get("DENSE_NAMESPACE", DEFAULTS["NAMESPACE"])
    cfg["SERVICE_NAME"] = os.environ.get("DENSE_SERVICE_NAME", DEFAULTS["SERVICE_NAME"])
    cfg["CONTAINER_PORT"] = int(os.environ.get("DENSE_PORT", str(DEFAULTS["CONTAINER_PORT"])))
    cfg["HOST"] = os.environ.get("DENSE_HOST", DEFAULTS["HOST"])
    cfg["LOGLEVEL"] = os.environ.get("DENSE_LOGLEVEL", DEFAULTS["LOGLEVEL"])

    cfg["SA_NAME"] = os.environ.get("DENSE_SA_NAME", f"{cfg['SERVICE_NAME']}-sa")
    cfg["ROLE_NAME"] = os.environ.get("DENSE_ROLE_NAME", f"{cfg['SERVICE_NAME']}-role")
    cfg["ROLEBIND_NAME"] = os.environ.get("DENSE_ROLEBIND_NAME", f"{cfg['SERVICE_NAME']}-rb")

    if env in ("PROD", "PRODUCTION"):
        prod = DEFAULTS["PROD"]
        cfg["REPLICAS"] = int(os.environ.get("DENSE_REPLICAS", str(prod["REPLICAS"])))
        cfg["CPU_REQUEST"] = os.environ.get("DENSE_CPU_REQUEST", prod["CPU_REQUEST"])
        cfg["CPU_LIMIT"] = os.environ.get("DENSE_CPU_LIMIT", prod["CPU_LIMIT"])
        cfg["MEMORY_REQUEST"] = os.environ.get("DENSE_MEMORY_REQUEST", prod["MEMORY_REQUEST"])
        cfg["MEMORY_LIMIT"] = os.environ.get("DENSE_MEMORY_LIMIT", prod["MEMORY_LIMIT"])
        cfg["STARTUP_FAILURE_THRESHOLD"] = int(os.environ.get("DENSE_STARTUP_FAILURE_THRESHOLD", str(prod["STARTUP_FAILURE_THRESHOLD"])))
    else:
        nonprod = DEFAULTS["NONPROD"]
        cfg["REPLICAS"] = int(os.environ.get("DENSE_REPLICAS", str(nonprod["REPLICAS"])))
        cfg["CPU_REQUEST"] = os.environ.get("DENSE_CPU_REQUEST", nonprod["CPU_REQUEST"])
        cfg["CPU_LIMIT"] = os.environ.get("DENSE_CPU_LIMIT", nonprod["CPU_LIMIT"])
        cfg["MEMORY_REQUEST"] = os.environ.get("DENSE_MEMORY_REQUEST", nonprod["MEMORY_REQUEST"])
        cfg["MEMORY_LIMIT"] = os.environ.get("DENSE_MEMORY_LIMIT", nonprod["MEMORY_LIMIT"])
        cfg["STARTUP_FAILURE_THRESHOLD"] = int(os.environ.get("DENSE_STARTUP_FAILURE_THRESHOLD", str(nonprod["STARTUP_FAILURE_THRESHOLD"])))

    cfg["PROBE_PERIOD_SECONDS"] = int(os.environ.get("DENSE_PROBE_PERIOD_SECONDS", str(DEFAULTS["PROBE_PERIOD_SECONDS"])))
    cfg["READINESS_INITIAL_DELAY"] = int(os.environ.get("DENSE_READINESS_INITIAL_DELAY", str(DEFAULTS["READINESS_INITIAL_DELAY"])))
    cfg["LIVENESS_INITIAL_DELAY"] = int(os.environ.get("DENSE_LIVENESS_INITIAL_DELAY", str(DEFAULTS["LIVENESS_INITIAL_DELAY"])))
    cfg["PROBE_TIMEOUT_SECONDS"] = int(os.environ.get("DENSE_PROBE_TIMEOUT_SECONDS", str(DEFAULTS["PROBE_TIMEOUT_SECONDS"])))

    # GPU enablement: respect explicit ENABLE flag OR DENSE_CUDA for backward compatibility
    cfg["ENABLE_GPU"] = _env_bool("DENSE_ENABLE_GPU", str(DEFAULTS["ENABLE_GPU"]).lower()) or _env_bool("DENSE_CUDA", str(DEFAULTS["DENSE_CUDA"]).lower())
    cfg["GPU_RESOURCE_NAME"] = os.environ.get("DENSE_GPU_RESOURCE", DEFAULTS["GPU_RESOURCE_NAME"])
    cfg["GPU_COUNT"] = int(os.environ.get("DENSE_GPU_COUNT", str(DEFAULTS["GPU_COUNT"])))
    cfg["GPU_NODE_SELECTOR"] = os.environ.get("DENSE_GPU_NODE_SELECTOR", DEFAULTS["GPU_NODE_SELECTOR"])

    cfg["HPA_ENABLED"] = _env_bool("DENSE_HPA_ENABLED", str(DEFAULTS["HPA_ENABLED"]).lower())
    cfg["HPA_MIN"] = int(os.environ.get("DENSE_HPA_MIN_REPLICAS", str(DEFAULTS["HPA_MIN"])))
    cfg["HPA_MAX"] = int(os.environ.get("DENSE_HPA_MAX_REPLICAS", str(DEFAULTS["HPA_MAX"])))
    cfg["HPA_TARGET_CPU"] = int(os.environ.get("DENSE_HPA_TARGET_CPU", str(DEFAULTS["HPA_TARGET_CPU"])))

    cfg["DENSE_MODEL_NAME"] = os.environ.get("DENSE_MODEL_NAME", DEFAULTS["DENSE_MODEL_NAME"])
    try:
        cfg["DENSE_DIM"] = int(os.environ.get("DENSE_DIM", str(DEFAULTS["DENSE_DIM"])))
    except Exception:
        cfg["DENSE_DIM"] = DEFAULTS["DENSE_DIM"]
    try:
        cfg["DENSE_BATCH_SIZE"] = int(os.environ.get("DENSE_BATCH_SIZE", str(DEFAULTS["DENSE_BATCH_SIZE"])))
    except Exception:
        cfg["DENSE_BATCH_SIZE"] = DEFAULTS["DENSE_BATCH_SIZE"]
    cfg["DENSE_NORMALIZE"] = _env_bool("DENSE_NORMALIZE", str(DEFAULTS["DENSE_NORMALIZE"]).lower())
    cfg["DENSE_CUDA"] = _env_bool("DENSE_CUDA", str(DEFAULTS["DENSE_CUDA"]).lower())
    cfg["PRELOAD_MODEL"] = _env_bool("DENSE_PRELOAD_MODEL", "0")

    cfg["RUN_AS_NONROOT"] = _env_bool("DENSE_RUN_AS_NONROOT", str(DEFAULTS["RUN_AS_NONROOT"]).lower())
    try:
        cfg["RUN_AS_USER"] = int(os.environ.get("DENSE_RUN_AS_USER", str(DEFAULTS["RUN_AS_USER"])))
    except Exception:
        cfg["RUN_AS_USER"] = DEFAULTS["RUN_AS_USER"]
    cfg["ALLOW_PRIV_ESC"] = _env_bool("DENSE_ALLOW_PRIV_ESC", str(DEFAULTS["ALLOW_PRIV_ESC"]).lower())
    cfg["READONLY_ROOTFS"] = _env_bool("DENSE_READONLY_ROOTFS", str(DEFAULTS["READONLY_ROOTFS"]).lower())
    try:
        fs_group_env = os.environ.get("DENSE_FS_GROUP", "")
        cfg["FS_GROUP"] = int(fs_group_env) if fs_group_env != "" else DEFAULTS["FS_GROUP"]
    except Exception:
        cfg["FS_GROUP"] = DEFAULTS["FS_GROUP"]

    cfg["LABELS"] = {
        "app.kubernetes.io/name": cfg["SERVICE_NAME"],
        "app.kubernetes.io/component": "embedder",
        "app.kubernetes.io/managed-by": "gen_dense",
        "app.kubernetes.io/instance": cfg["SERVICE_NAME"],
        "env": cfg["DEPLOY_ENV"].lower(),
    }
    cfg["MAX_UNAVAILABLE"] = os.environ.get("DENSE_MAX_UNAVAILABLE", DEFAULTS["MAX_UNAVAILABLE"])
    cfg["MAX_SURGE"] = os.environ.get("DENSE_MAX_SURGE", DEFAULTS["MAX_SURGE"])
    cfg["ROLLOUT_TIMEOUT"] = int(os.environ.get("DENSE_ROLLOUT_TIMEOUT", str(DEFAULTS["ROLLOUT_TIMEOUT"])))

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
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": cfg["NAMESPACE"], "labels": {"app.kubernetes.io/managed-by": "gen_dense"}},
    }
    return yaml.safe_dump(ns, sort_keys=False)


def render_serviceaccount(cfg: dict[str, Any]) -> str:
    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": cfg["SA_NAME"], "namespace": cfg["NAMESPACE"]},
    }
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
            # Provide both ENV (sparse-compatible) and DEPLOY_ENV (backwards-compatible)
            {"name": "ENV", "value": cfg["DEPLOY_ENV"]},
            {"name": "DEPLOY_ENV", "value": cfg["DEPLOY_ENV"]},
            {"name": "DENSE_HOST", "value": str(cfg["HOST"])},
            {"name": "DENSE_PORT", "value": str(cfg["CONTAINER_PORT"])},
            {"name": "DENSE_LOGLEVEL", "value": str(cfg["LOGLEVEL"])},
            {"name": "DENSE_MODEL_NAME", "value": str(cfg["DENSE_MODEL_NAME"])},
            {"name": "DENSE_DIM", "value": str(cfg["DENSE_DIM"])},
            {"name": "DENSE_BATCH_SIZE", "value": str(cfg["DENSE_BATCH_SIZE"])},
            {"name": "DENSE_NORMALIZE", "value": "1" if cfg.get("DENSE_NORMALIZE", False) else "0"},
            {"name": "DENSE_CUDA", "value": "1" if cfg.get("DENSE_CUDA", False) else "0"},
            {"name": "PRELOAD_MODEL", "value": "1" if cfg.get("PRELOAD_MODEL", False) else "0"},
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
        "resources": {
            "requests": {"cpu": cfg["CPU_REQUEST"], "memory": cfg["MEMORY_REQUEST"]},
            "limits": {"cpu": cfg["CPU_LIMIT"], "memory": cfg["MEMORY_LIMIT"]},
        },
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
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": cfg["MAX_UNAVAILABLE"], "maxSurge": cfg["MAX_SURGE"]},
            },
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": cfg["SA_NAME"],
                    "containers": [container],
                },
            },
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

    # GPU node selector handling (same behavior as sparse)
    if cfg["ENABLE_GPU"] and cfg["GPU_NODE_SELECTOR"]:
        if "=" in cfg["GPU_NODE_SELECTOR"]:
            k, v = cfg["GPU_NODE_SELECTOR"].split("=", 1)
            deployment["spec"]["template"]["spec"]["nodeSelector"] = {k: v}
        else:
            deployment["spec"]["template"]["spec"]["nodeSelector"] = {cfg["GPU_NODE_SELECTOR"]: "true"}

    # Ensure volume mounts and volumes remain intact when READONLY_ROOTFS is enabled
    if cfg.get("READONLY_ROOTFS", True):
        tmp_mounts = [
            {"name": "tmp-writable", "mountPath": "/tmp"},
            {"name": "tmp-writable", "mountPath": "/var/tmp"},
            {"name": "tmp-writable", "mountPath": "/usr/tmp"},
        ]
        existing_mounts = container.get("volumeMounts", []) or []
        # Add tmp mounts if missing
        for m in tmp_mounts:
            if not any(vm.get("mountPath") == m["mountPath"] for vm in existing_mounts):
                existing_mounts.append(m)
        # Add models-cache only if not present
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
                {
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": cfg["HPA_TARGET_CPU"]}},
                }
            ],
        },
    }
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


def apply_to_cluster(cfg: dict[str, Any], dry_run: bool = False, verbose: bool = False, mode_label: str = "rollout") -> None:
    kubectl = shutil.which("kubectl")
    if not kubectl:
        log.error("kubectl not found; aborting apply.")
        raise SystemExit(2) from None

    inputs_hash = generate_manifests(cfg, dry_run=dry_run, verbose=verbose)
    if dry_run:
        log.info("Dry-run requested; skipping kubectl apply.")
        return

    if inputs_hash is None:
        log.info("No manifest changes; skipping kubectl apply.")
        return

    files = [
        cfg["FILES"]["namespace"],
        cfg["FILES"]["serviceaccount"],
        cfg["FILES"]["role"],
        cfg["FILES"]["rolebinding"],
        cfg["FILES"]["deployment"],
        cfg["FILES"]["service"],
    ]
    if cfg["HPA_ENABLED"]:
        files.append(cfg["FILES"]["hpa"])

    combined = ""
    for p in files:
        if not p.exists():
            log.warning("Manifest missing, skipping: %s", str(p))
            continue
        combined += f"---\n# source: {p.name}\n" + p.read_text(encoding="utf-8") + "\n"

    res = kubectl_apply_yaml(combined, dry_run=False)
    if not res.get("applied", False):
        log.error("%s apply failed: %s", mode_label, res.get("stderr") or res.get("error"))
        raise SystemExit(2) from None

    deployment_name = f"{cfg['SERVICE_NAME']}-deployment"
    log.info("Applied manifests; waiting for rollout of %s/%s", cfg["NAMESPACE"], deployment_name)
    rc, out, err = run_cmd(
        [shutil.which("kubectl") or "kubectl", "rollout", "status", f"deployment/{deployment_name}", "-n", cfg["NAMESPACE"], f"--timeout={cfg.get('ROLLOUT_TIMEOUT', 300)}s"],
        timeout=cfg.get("ROLLOUT_TIMEOUT", 300) + 10,
    )
    if rc != 0:
        log.error("Rollout failed or timed out (rc=%d). Gathering diagnostics.", rc)
        cmds: list[tuple[list[str], str]] = [
            ([shutil.which("kubectl") or "kubectl", "get", "pods", "-n", cfg["NAMESPACE"]], "get pods"),
            ([shutil.which("kubectl") or "kubectl", "describe", "pod", "-l", f"app.kubernetes.io/name={cfg['SERVICE_NAME']}", "-n", cfg["NAMESPACE"]], "describe pods"),
            ([shutil.which("kubectl") or "kubectl", "logs", "-l", f"app.kubernetes.io/name={cfg['SERVICE_NAME']}", "-n", cfg["NAMESPACE"], "--tail=200"], "logs"),
        ]
        for cmd, tag in cmds:
            rcout, out, err = run_cmd(cmd, timeout=30)
            log.error("=== %s (rc=%d) ===\n%s\n%s", tag, rcout, (out or "").strip(), (err or "").strip())
        raise SystemExit(2) from None
    log.info("Rollout successful for %s/%s", cfg["NAMESPACE"], deployment_name)


def delete_manifests(cfg: dict[str, Any]) -> None:
    def _delete(dir_path: Path, state_dirname: str) -> None:
        if not dir_path or str(dir_path).strip() in ("", "/", "."):
            return
        if not dir_path.exists():
            log.info("No manifests found at %s", str(dir_path))
            return
        try:
            subprocess.run(["kubectl", "delete", "-f", str(dir_path), "--ignore-not-found=true"], check=False, capture_output=True, text=True)
        except Exception:
            log.debug("kubectl delete failed for %s", dir_path, exc_info=True)
        for p in sorted(dir_path.glob("*")):
            try:
                if p.is_file():
                    p.unlink()
            except Exception:
                log.debug("Failed to remove %s", p, exc_info=True)
        # remove state dir if present
        state_dir = dir_path / state_dirname
        try:
            if state_dir.exists():
                for p in sorted(state_dir.glob("*")):
                    try:
                        if p.is_file():
                            p.unlink()
                    except Exception:
                        log.debug("Failed to remove %s", p, exc_info=True)
                state_dir.rmdir()
        except Exception:
            log.debug("Failed to cleanup state dir %s", state_dir, exc_info=True)

    manifests_dir = Path(os.environ.get("MANIFESTS_DIR", str(DEFAULTS["MANIFESTS_DIR"])))
    _delete(manifests_dir, os.environ.get("STATE_DIRNAME", DEFAULT_STATE_DIRNAME))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate and optionally apply Dense service Kubernetes manifests.")
    parser.add_argument("--apply", action="store_true", help="DEPRECATED: use --rollout instead (kept for compatibility).")
    parser.add_argument("--rollout", action="store_true", help="Apply manifests to cluster and wait for rollout.")
    parser.add_argument("--dry-run", action="store_true", help="Generate manifests but do not apply.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug output.")
    parser.add_argument("--delete", action="store_true", help="Delete generated manifests from cluster and disk.")
    args = parser.parse_args()

    cfg = load_config()
    if args.verbose:
        log.setLevel(logging.DEBUG)

    if args.delete:
        delete_manifests(cfg)
        raise SystemExit(0)

    if args.rollout or args.apply:
        apply_to_cluster(cfg, dry_run=args.dry_run, verbose=args.verbose, mode_label="rollout")
    else:
        generate_manifests(cfg, dry_run=args.dry_run, verbose=args.verbose)
