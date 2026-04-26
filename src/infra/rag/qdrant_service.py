#!/usr/bin/env python3
# Deterministic Qdrant deployer: env → values.yaml → Helm → validated pods
# Runtime modes: --rollout (install/upgrade) | --delete (teardown namespace + manifests)
# On rollout: ensure namespace → optional secret → render values → helm upgrade --install --atomic --wait
# Supports image pinning (tag+digest) via post-renderer to enforce exact runtime image
# Validates readiness using kubectl wait + polling; STRICT=1 fails fast on unhealthy rollout
# Cluster-aware (kind/eks/eks-auto) but no branching side-effects—purely informational
# Idempotent + atomic: repeated runs converge; manifests written safely, no partial state

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None


# =============================================================================
# Primary knobs
# Keep the top-level env surface small and predictable.
# =============================================================================
DEFAULT_RELEASE = "qdrant"
DEFAULT_NAMESPACE = "qdrant"
DEFAULT_IMAGE_REF = "docker.io/qdrant/qdrant:v1.17.1@sha256:94728574965d17c6485dd361aa3c0818b325b9016dac5ea6afec7b4b2700865f"
# Fallback defaults for repo/tag/digest (can be overridden via env)
DEFAULT_IMAGE_REPO = ""
DEFAULT_IMAGE_TAG = ""
DEFAULT_IMAGE_DIGEST = ""
DEFAULT_CHART_REPO_NAME = "qdrant"
DEFAULT_CHART_REPO_URL = "https://qdrant.github.io/qdrant-helm"
DEFAULT_CHART_NAME = "qdrant"
DEFAULT_CHART_VERSION = "1.17.1"
DEFAULT_HELM_TIMEOUT = "10m"
DEFAULT_MANIFESTS_DIR = "src/helm/qdrant"

VERBOSE = os.environ.get("VERBOSE", "0").strip().lower() in ("1", "true", "yes", "y", "on")
STRICT = os.environ.get("STRICT", "1").strip().lower() in ("1", "true", "yes", "y", "on")

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
# Env helpers
# =============================================================================
def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def parse_image_ref(ref: str) -> tuple[str, str, str]:
    """
    Supports:
      repo:tag
      repo@sha256:...
      repo:tag@sha256:...
    Returns (repo, tag, digest).
    """
    ref = ref.strip()
    if not ref:
        return "", "", ""

    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
        digest = digest.strip()

    tag = ""
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        tag = ref[last_colon + 1 :].strip()
        ref = ref[:last_colon].strip()

    return ref, tag, digest


def compose_image_ref(repo: str, tag: str, digest: str) -> str:
    repo = repo.strip()
    tag = tag.strip()
    digest = digest.strip()
    if digest:
        return f"{repo}:{tag}@{digest}" if tag else f"{repo}@{digest}"
    if tag:
        return f"{repo}:{tag}"
    return repo


@dataclasses.dataclass(frozen=True)
class Config:
    release: str
    namespace: str
    manifests_dir: Path

    image_repo: str
    image_tag: str
    image_digest: str
    image_pull_policy: str
    image_ref: str

    replicas: int
    persistence_enabled: bool
    persistence_size: str
    on_disk_payload: bool
    log_level: str

    api_key: str
    secret_name: str
    secret_key: str

    validate_wait_seconds: int
    helm_timeout: str

    chart_repo_name: str
    chart_repo_url: str
    chart_name: str
    chart_version: str
    vendor_chart_dir: str

    verbose: bool
    strict: bool


def load_config() -> Config:
    raw_image_ref = _env("QDRANT_IMAGE_REF", DEFAULT_IMAGE_REF)
    repo, tag, digest = parse_image_ref(raw_image_ref)

    # If any piece is missing, allow explicit env overrides; fall back to top-level defaults.
    if not repo:
        repo = _env("QDRANT_IMAGE_REPO", DEFAULT_IMAGE_REPO)
    if not tag:
        tag = _env("QDRANT_IMAGE_TAG", DEFAULT_IMAGE_TAG)
    if not digest:
        digest = _env("QDRANT_IMAGE_DIGEST", DEFAULT_IMAGE_DIGEST)

    return Config(
        release=_env("QDRANT_RELEASE", DEFAULT_RELEASE),
        namespace=_env("QDRANT_NAMESPACE", DEFAULT_NAMESPACE),
        manifests_dir=Path(_env("MANIFESTS_DIR", DEFAULT_MANIFESTS_DIR)),
        image_repo=repo,
        image_tag=tag,
        image_digest=digest,
        image_pull_policy=_env("QDRANT_IMAGE_PULL_POLICY", "IfNotPresent"),
        image_ref=compose_image_ref(repo, tag, digest),
        replicas=_env_int("QDRANT_REPLICAS", 1),
        persistence_enabled=_env_bool("QDRANT_PERSISTENCE_ENABLED", True),
        persistence_size=_env("QDRANT_PERSISTENCE_SIZE", "20Gi"),
        on_disk_payload=_env_bool("QDRANT_ONDISK", False),
        log_level=_env("QDRANT_LOG_LEVEL", "INFO"),
        api_key=_env("QDRANT_API_KEY", ""),
        secret_name="qdrant-service-creds",
        secret_key="QDRANT__SERVICE__API_KEY",
        validate_wait_seconds=_env_int("SERVICE_VALIDATION_WAIT", 180),
        helm_timeout=_env("HELM_TIMEOUT", DEFAULT_HELM_TIMEOUT),
        chart_repo_name=_env("QDRANT_HELM_REPO_NAME", DEFAULT_CHART_REPO_NAME),
        chart_repo_url=_env("QDRANT_HELM_REPO_URL", DEFAULT_CHART_REPO_URL),
        chart_name=_env("QDRANT_HELM_CHART", DEFAULT_CHART_NAME),
        chart_version=_env("QDRANT_CHART_VERSION", DEFAULT_CHART_VERSION),
        vendor_chart_dir=_env("VENDOR_CHART_DIR", "infra/archive/qdrant-helm-chart/qdrant"),
        verbose=VERBOSE,
        strict=STRICT,
    )


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
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=120)


# =============================================================================
# Subprocess helpers
# =============================================================================
def require_bin(name: str) -> None:
    if shutil.which(name) is None:
        fatal(f"{name} not found in PATH", 2)


def run_cmd(
    cmd: list[str],
    *,

    capture: bool = False,
    timeout: int | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
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
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or f"timeout after {timeout}s"


def run_streaming_cmd(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    stdin_text: str | None = None,
    prefix: str = "",
) -> int:
    """
    Stream stdout/stderr live so long-running Helm operations do not appear frozen.
    """
    env_used = os.environ.copy()
    if env:
        env_used.update(env)

    DBG("streaming cmd:", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env_used,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
    except Exception as exc:
        LOG("ERROR: failed to start command:", " ".join(cmd))
        LOG("ERROR:", str(exc))
        return 1

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except Exception:
            pass

    def reader(stream, is_err: bool, label: str):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n")
                if is_err:
                    LOG(f"[{label}] {text}")
                else:
                    LOG(f"[{label}] {text}")
        except Exception:
            LOG(f"ERROR: reader failed for {label}")

    base = prefix or Path(cmd[0]).name
    t_out = threading.Thread(target=reader, args=(proc.stdout, False, base), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, True, f"{base}:err"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        LOG(f"ERROR: command timed out after {timeout}s: {' '.join(cmd)}")
        try:
            proc.kill()
        except Exception:
            pass
        return 124

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    return proc.returncode


def run_capturing_cmd(cmd: list[str], timeout: int | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    rc, out, _ = run_cmd(cmd, capture=True, timeout=timeout, env=env)
    return rc, out


# =============================================================================
# Cluster detection
# =============================================================================
def detect_cluster_mode() -> str:
    explicit = os.environ.get("K8S_CLUSTER", "").strip().lower()
    if explicit in {"kind", "eks", "eks-auto"}:
        return explicit

    rc, _, _ = run_cmd(["kubectl", "version", "--request-timeout=5s"], capture=True)
    if rc != 0:
        return "unknown"

    node_name = ""
    provider_id = ""
    try:
        node_name = run_capturing_cmd(["kubectl", "get", "nodes", "-o", "jsonpath={.items[0].metadata.name}"], timeout=10)[1].strip()
    except Exception:
        node_name = ""

    if node_name:
        try:
            provider_id = run_capturing_cmd(["kubectl", "get", "node", node_name, "-o", "jsonpath={.spec.providerID}"], timeout=10)[1].strip()
        except Exception:
            provider_id = ""

    csidrivers = run_capturing_cmd(["kubectl", "get", "csidrivers", "-o", "name"], timeout=10)[1].splitlines()
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
# Namespace / secret
# =============================================================================
def ensure_namespace(cfg: Config) -> None:
    ns_doc = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": cfg.namespace}}
    atomic_write_text(cfg.manifests_dir / "namespace.yaml", safe_yaml_dump(ns_doc))
    LOG("Rendered", str(cfg.manifests_dir / "namespace.yaml"))
    rc = run_streaming_cmd(["kubectl", "apply", "-f", "-"], stdin_text=safe_yaml_dump(ns_doc), prefix="kubectl-apply")
    if rc != 0:
        fatal("failed to create namespace", 3)


def create_or_update_secret(cfg: Config) -> bool:
    if not cfg.api_key:
        DBG("no QDRANT_API_KEY provided; skipping secret creation")
        return False

    secret_yaml = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": cfg.secret_name, "namespace": cfg.namespace},
        "type": "Opaque",
        "stringData": {cfg.secret_key: cfg.api_key},
    }
    rc, out, err = run_cmd(["kubectl", "-n", cfg.namespace, "apply", "-f", "-"], capture=True, input_text=safe_yaml_dump(secret_yaml))
    if rc != 0:
        raise RuntimeError(err or out or "failed to apply qdrant api key secret")
    LOG("created/updated secret", cfg.secret_name)
    return True


# =============================================================================
# Values rendering
# =============================================================================
def _resources_from_env() -> dict[str, Any]:
    cpu_req = os.environ.get("QDRANT_CPU_REQUEST") or os.environ.get("QDRANT_CPU") or "1"
    cpu_lim = os.environ.get("QDRANT_CPU_LIMIT") or os.environ.get("QDRANT_CPU") or cpu_req
    mem_req = os.environ.get("QDRANT_MEMORY_REQUEST") or os.environ.get("QDRANT_MEMORY") or "2Gi"
    mem_lim = os.environ.get("QDRANT_MEMORY_LIMIT") or os.environ.get("QDRANT_MEMORY") or mem_req
    return {
        "requests": {"cpu": cpu_req, "memory": mem_req},
        "limits": {"cpu": cpu_lim, "memory": mem_lim},
    }


def build_qdrant_values(cfg: Config) -> dict[str, Any]:
    values: dict[str, Any] = {
        "replicaCount": cfg.replicas,
        "image": {
            "repository": cfg.image_repo,
            "tag": cfg.image_tag,
            "pullPolicy": cfg.image_pull_policy,
        },
        "service": {"type": "ClusterIP"},
        "persistence": {
            "enabled": bool(cfg.persistence_enabled),
            "size": cfg.persistence_size,
            "accessModes": ["ReadWriteOnce"],
        },
        "podAnnotations": {"app.kubernetes.io/managed-by": "qdrant_service.py"},
        "podLabels": {"app.kubernetes.io/managed-by": "qdrant_service.py"},
        "resources": _resources_from_env(),
        "config": {
            "cluster": {
                "enabled": True,
                "p2p": {"port": 6335, "enable_tls": False},
                "consensus": {"tick_period_ms": 100},
            },
            "service": {"enable_tls": False},
            "log_level": cfg.log_level,
            "on_disk_payload": bool(cfg.on_disk_payload),
        },
        "updateVolumeFsOwnership": True,
        "podManagementPolicy": "Parallel",
        "lifecycle": {"preStop": {"exec": {"command": ["sleep", "3"]}}},
    }

    if cfg.api_key:
        values["env"] = [
            {
                "name": "QDRANT__SERVICE__API_KEY",
                "valueFrom": {"secretKeyRef": {"name": cfg.secret_name, "key": cfg.secret_key}},
            }
        ]
        values["podAnnotations"]["qdrant/api-key-present"] = "true"

    if cfg.replicas > 1:
        values["podDisruptionBudget"] = {"enabled": True, "maxUnavailable": 1}
        values["topologySpreadConstraints"] = [
            {
                "maxSkew": 1,
                "topologyKey": "kubernetes.io/hostname",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": {"app.kubernetes.io/name": cfg.release}},
            }
        ]

    checksum = hashlib.sha256(safe_yaml_dump(values).encode("utf-8")).hexdigest()
    values["podAnnotations"]["qdrant/config-checksum"] = checksum
    return values


def render_values_file(cfg: Config) -> None:
    vals = build_qdrant_values(cfg)
    atomic_write_text(cfg.manifests_dir / "values.yaml", safe_yaml_dump(vals))
    LOG("Rendered", str(cfg.manifests_dir / "values.yaml"))


# =============================================================================
# Post-render digest pinning
# =============================================================================
def render_post_renderer(cfg: Config, temp_dir: Path) -> Path | None:
    """
    Chart templates are tag-oriented. If a digest is configured, a post-renderer
    rewrites the final manifest image fields to the exact image reference.
    """
    if not cfg.image_digest:
        return None

    script = temp_dir / "qdrant-post-renderer.py"
    script.write_text(
        f"""#!/usr/bin/env python3
from __future__ import annotations

import sys
import yaml

BASE_REPO = {cfg.image_repo!r}
DESIRED_REF = {cfg.image_ref!r}

def patch(node):
    if isinstance(node, dict):
        img = node.get("image")
        if isinstance(img, str) and img.startswith(BASE_REPO):
            node["image"] = DESIRED_REF
        for value in node.values():
            patch(value)
    elif isinstance(node, list):
        for item in node:
            patch(item)

docs = [doc for doc in yaml.safe_load_all(sys.stdin) if doc is not None]
for doc in docs:
    patch(doc)

yaml.safe_dump_all(docs, sys.stdout, sort_keys=False, explicit_start=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


# =============================================================================
# Helm / rollout
# =============================================================================
def helm_install(cfg: Config) -> bool:
    ensure_namespace(cfg)
    create_or_update_secret(cfg)
    render_values_file(cfg)

    vendor = Path(cfg.vendor_chart_dir)
    helm_base = ["helm", "upgrade", "--install", cfg.release]

    with tempfile.TemporaryDirectory(prefix="qdrant-postrender-") as td:
        post_renderer = render_post_renderer(cfg, Path(td))

        def with_common(args: list[str]) -> list[str]:
            out = [
                *args,
                "--namespace",
                cfg.namespace,
                "--create-namespace",
                "-f",
                str(cfg.manifests_dir / "values.yaml"),
                "--atomic",
                "--wait",
                f"--timeout={cfg.helm_timeout}",
            ]
            if post_renderer is not None:
                out.extend(["--post-renderer", str(post_renderer)])
            return out

        if vendor.is_dir() and (vendor / "Chart.yaml").exists():
            LOG("Attempting vendor chart install from", str(vendor))
            cmd = with_common([*helm_base, str(vendor)])
            rc = run_streaming_cmd(cmd, prefix="helm")
            if rc == 0:
                LOG("helm vendor install succeeded")
                return True
            LOG("helm vendor install failed rc=", rc)

        LOG("Using upstream helm repo for qdrant")
        rc = run_streaming_cmd(["helm", "repo", "add", "--force-update", cfg.chart_repo_name, cfg.chart_repo_url], prefix="helm-repo")
        if rc != 0:
            LOG("helm repo add failed rc=", rc)
            return False

        rc = run_streaming_cmd(["helm", "repo", "update"], prefix="helm-repo")
        if rc != 0:
            LOG("helm repo update failed rc=", rc)
            return False

        chart_ref = f"{cfg.chart_repo_name}/{cfg.chart_name}"
        cmd = with_common([*helm_base, chart_ref])
        if cfg.chart_version:
            cmd.extend(["--version", cfg.chart_version])

        LOG("Installing/upgrading qdrant with Helm")
        rc = run_streaming_cmd(cmd, prefix="helm")
        if rc == 0:
            LOG("helm install/upgrade succeeded")
            return True

        LOG("helm install/upgrade failed rc=", rc)
        return False


def validate_post_install(cfg: Config) -> bool:
    selector = f"app.kubernetes.io/instance={cfg.release}"
    rc, out, err = run_cmd(
        [
            "kubectl",
            "-n",
            cfg.namespace,
            "wait",
            "--for=condition=Ready",
            "pod",
            "-l",
            selector,
            f"--timeout={cfg.validate_wait_seconds}s",
        ],
        capture=True,
    )
    if rc != 0:
        LOG("kubectl wait stdout:\n", out.strip())
        LOG("kubectl wait stderr:\n", err.strip())

    end = time.time() + cfg.validate_wait_seconds
    while time.time() < end:
        rc, out, _ = run_cmd(["kubectl", "-n", cfg.namespace, "get", "pods", "-l", selector, "-o", "json"], capture=True)
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


def delete_qdrant(cfg: Config) -> None:
    run_streaming_cmd(["kubectl", "delete", "ns", cfg.namespace, "--ignore-not-found"], prefix="kubectl")
    if cfg.manifests_dir.exists():
        try:
            shutil.rmtree(cfg.manifests_dir)
        except Exception:
            DBG("failed to remove manifests dir", cfg.manifests_dir)
    LOG("deleted qdrant namespace and rendered manifests (best-effort)")


# =============================================================================
# Main
# =============================================================================
def usage_and_exit() -> None:
    print("usage: qdrant_service.py --rollout|--delete", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    cfg = load_config()

    require_bin("kubectl")
    require_bin("helm")

    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--rollout", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--help", "-h", action="store_true")
    args = parser.parse_args(argv)

    if args.help or (not args.rollout and not args.delete):
        usage_and_exit()
    if args.rollout and args.delete:
        usage_and_exit()

    cluster_mode = detect_cluster_mode()
    LOG(f"cluster mode: {cluster_mode}")
    LOG(f"starting setup for release={cfg.release} namespace={cfg.namespace}")

    if args.rollout:
        ok = helm_install(cfg)
        if not ok:
            fatal("helm install/upgrade failed", 3)

        post_ok = validate_post_install(cfg)
        if not post_ok and cfg.strict:
            fatal("post-install validation failed", 3)

        LOG("rollout complete")
        return

    if args.delete:
        delete_qdrant(cfg)
        return


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2) from None
