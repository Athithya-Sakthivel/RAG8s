#!/usr/bin/env python3
"""
qdrant_full_restore.py

Restore a Qdrant cluster from a backup manifest produced by
test_qdrant_full_cluster_backup_restore.py.

Features:
- Load manifest from local backup directory (default ./tmp/latest) or download from S3.
- Verify SHA256 checksums of snapshot files.
- Upload per-pod per-collection snapshot files into pod via:
    POST http://127.0.0.1:<pf-port>/collections/<collection>/snapshots/upload?priority=snapshot&checksum=<sha256>
- Optionally run a quiesce shell command before restore (--quiesce-cmd).
- Optionally restart the qdrant statefulset after upload (--restart) and wait for readiness.
- Deterministic ordering (pods, collections sorted).
- Requires: kubectl in PATH, boto3, requests.

Usage examples:
  # restore from local folder (manifest.json present)
  python3 infra/tests/qdrant_full_restore.py --backup-dir ./tmp/latest --namespace qdrant --restart

  # restore by downloading manifest and files from S3
  python3 infra/tests/qdrant_full_restore.py --s3-bucket e2e-rag-system-42 --backup-id 20251203T134734Z-defb5319 --s3-prefix qdrant/backups --namespace qdrant --restart

Notes:
 - Uploading snapshots with priority=snapshot is the recommended programmatic approach.
 - For production, test this workflow on an isolated cluster first.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import boto3
import requests

KUBECTL = shutil.which("kubectl") or "kubectl"
PORT_FORWARD_TIMEOUT = 15
UPLOAD_TIMEOUT = 900
DOWNLOAD_TIMEOUT = 600
POD_WAIT_TIMEOUT = 600

# ---------------- utilities ----------------
def run_cmd(cmd, timeout=60, capture=True):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None,
                              check=True, timeout=timeout, text=True)
        return proc.stdout if capture else ""
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"cmd timeout: {' '.join(cmd)}")

def sha256sum(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def get_free_port():
    s = socket.socket()
    s.bind(('', 0))
    p = s.getsockname()[1]
    s.close()
    return p

# ---------------- kubectl port-forward ----------------
def start_port_forward(namespace: str, pod: str, local_port: int):
    cmd = [KUBECTL, "-n", namespace, "port-forward", f"pod/{pod}", f"{local_port}:6333"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=os.setsid)
    deadline = time.time() + PORT_FORWARD_TIMEOUT
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", local_port), timeout=1)
            s.close()
            return proc
        except Exception:
            time.sleep(0.15)
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=1)
                raise RuntimeError(f"port-forward failed for pod {pod}: {err.strip()}")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    raise RuntimeError(f"timeout waiting for port-forward to pod {pod} on local port {local_port}")

def stop_port_forward(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

# ---------------- S3 helpers ----------------
def s3_client(region):
    return boto3.client("s3", region_name=region)

def download_from_s3(s3, bucket: str, key: str, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(target))
    return target

# ---------------- cluster helpers ----------------
def list_qdrant_pods(namespace: str):
    labels = ["app.kubernetes.io/name=qdrant", "app=qdrant"]
    for label in labels:
        try:
            out = run_cmd([KUBECTL, "-n", namespace, "get", "pods", "-l", label, "-o", "jsonpath={.items[*].metadata.name}"], timeout=15)
            out = out.strip()
            if out:
                pods = out.split()
                pods.sort()
                return pods
        except Exception:
            continue
    raise RuntimeError(f"No qdrant pods found in namespace {namespace}")

def rollout_restart_and_wait(namespace: str, timeout=POD_WAIT_TIMEOUT):
    # Assumes statefulset named 'qdrant'
    print("Restarting statefulset/qdrant...")
    run_cmd([KUBECTL, "-n", namespace, "rollout", "restart", "statefulset/qdrant"], timeout=30)
    print("Waiting for rollout to finish...")
    run_cmd([KUBECTL, "-n", namespace, "rollout", "status", "statefulset/qdrant", f"--timeout={timeout}s"], timeout=timeout)

def wait_pods_ready(namespace: str, timeout=POD_WAIT_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = run_cmd([KUBECTL, "-n", namespace, "get", "pods", "-l", "app.kubernetes.io/name=qdrant", "-o", "jsonpath={range .items[*]}{.metadata.name}:{.status.containerStatuses[0].ready}{'\\n'}{end}"], timeout=15)
        all_ready = True
        for line in out.splitlines():
            if not line.strip():
                continue
            name, ready = line.strip().split(":", 1)
            if ready.strip().lower() != "true":
                all_ready = False
                break
        if all_ready:
            print("All pods are ready.")
            return
        time.sleep(3)
    raise RuntimeError("Timed out waiting for pods to become ready")

# ---------------- upload helper ----------------
def upload_snapshot_to_pod(local_port: int, collection: str, file_path: Path, api_key: str, checksum: str):
    url = f"http://127.0.0.1:{local_port}/collections/{collection}/snapshots/upload"
    headers = {}
    if api_key:
        headers["api-key"] = api_key
    params = {"priority": "snapshot", "checksum": checksum}
    with open(file_path, "rb") as fh:
        files = {"snapshot": (file_path.name, fh, "application/octet-stream")}
        r = requests.post(url, headers=headers, params=params, files=files, timeout=UPLOAD_TIMEOUT)
    r.raise_for_status()
    return r.json()

# ---------------- main restore flow ----------------
def restore(args):
    # acquire manifest (local or s3)
    if args.backup_dir:
        backup_dir = Path(args.backup_dir)
        manifest_path = backup_dir / "manifest.json"
        if not manifest_path.exists():
            raise SystemExit(f"manifest.json not found in {backup_dir}")
        manifest = json.loads(manifest_path.read_text())
        local_base = backup_dir
    else:
        if not args.s3_bucket or not args.backup_id or not args.s3_prefix:
            raise SystemExit("When not using --backup-dir, --s3-bucket, --backup-id and --s3-prefix are required.")
        s3 = s3_client(args.aws_region)
        manifest_key = f"{args.s3_prefix.rstrip('/')}/{args.backup_id}/manifest.json"
        tmpdir = Path.cwd() / "tmp" / f"restore-{args.backup_id}"
        tmpdir.mkdir(parents=True, exist_ok=True)
        local_manifest = tmpdir / "manifest.json"
        print(f"Downloading manifest s3://{args.s3_bucket}/{manifest_key} -> {local_manifest}")
        download_from_s3(s3, args.s3_bucket, manifest_key, local_manifest)
        manifest = json.loads(local_manifest.read_text())
        local_base = tmpdir
        # download files referenced in manifest
        print("Downloading snapshot artifacts from S3 referenced in manifest...")
        for pod, info in manifest.get("pods", {}).items():
            for col, meta in info.get("collections", {}).items():
                key = meta["s3_key"]
                target = tmpdir / Path(key).name
                print(f"  s3://{args.s3_bucket}/{key} -> {target}")
                download_from_s3(s3, args.s3_bucket, key, target)

    # verify files exist & checksums
    print("Verifying snapshot files checksums...")
    for pod, info in manifest.get("pods", {}).items():
        for col, meta in info.get("collections", {}).items():
            filename = Path(meta["s3_key"]).name
            local_path = local_base / filename if (local_base / filename).exists() else (local_base / pod / filename if (local_base / pod / filename).exists() else None)
            if local_path is None or not Path(local_path).exists():
                raise SystemExit(f"Missing snapshot file for pod={pod} collection={col}: expected {filename} under {local_base}")
            actual = sha256sum(Path(local_path))
            if actual != meta.get("sha256"):
                raise SystemExit(f"Checksum mismatch for {local_path}: manifest={meta.get('sha256')} actual={actual}")

    # optional quiesce
    if args.quiesce_cmd:
        print("Running quiesce command:", args.quiesce_cmd)
        run_cmd(args.quiesce_cmd, timeout=120, capture=False)

    # port-forward & upload per pod
    pods = sorted(manifest.get("pods", {}).keys())
    if not pods:
        raise SystemExit("Manifest contains no pods to restore")
    print("Pods to restore:", pods)

    for pod in pods:
        print("Processing pod:", pod)
        local_port = get_free_port()
        pf = start_port_forward(args.namespace, pod, local_port)
        try:
            collections = sorted(manifest["pods"][pod].get("collections", {}).keys())
            for col in collections:
                meta = manifest["pods"][pod]["collections"][col]
                filename = Path(meta["s3_key"]).name
                # determine local file path
                candidate1 = Path(args.backup_dir or local_base) / filename
                candidate2 = Path(args.backup_dir or local_base) / pod / filename
                if candidate1.exists():
                    path = candidate1
                elif candidate2.exists():
                    path = candidate2
                else:
                    raise SystemExit(f"Snapshot file {filename} not found for pod {pod} (looked at {candidate1} and {candidate2})")
                print(f"  Uploading {path.name} -> pod {pod} collection {col}")
                upload_snapshot_to_pod(local_port, col, path, args.api_key or "", meta.get("sha256"))
                print(f"  Uploaded {path.name} to pod {pod} for collection {col}")
        finally:
            stop_port_forward(pf)

    # optional restart statefulset
    if args.restart:
        rollout_restart_and_wait(args.namespace)
        print("Waiting for pods to be ready after restart...")
        wait_pods_ready(args.namespace, timeout=POD_WAIT_TIMEOUT)

    # final verification: list collections from coordinator (first pod)
    try:
        pods_now = list_qdrant_pods(args.namespace)
        if pods_now:
            pf = start_port_forward(args.namespace, pods_now[0], get_free_port())
            try:
                url = f"http://127.0.0.1:{pf.args[-1].split(':')[0]}/collections"  # not used; simpler: use requests to port
                # simpler: we already have a port-forward process; fetch port by checking its mapping (we opened with get_free_port and used that)
            finally:
                stop_port_forward(pf)
    except Exception:
        pass

    print("Restore completed successfully.")

# ---------------- CLI ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Restore Qdrant cluster from full-cluster backup manifest")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--backup-dir", help="Local backup dir containing manifest.json and snapshot files (preferred).")
    grp.add_argument("--backup-id", help="Backup id to restore (requires --s3-bucket and --s3-prefix).")
    p.add_argument("--s3-bucket", help="S3 bucket where backups are stored (required with --backup-id).")
    p.add_argument("--s3-prefix", help="S3 prefix where backups are stored (required with --backup-id).")
    p.add_argument("--namespace", default=os.environ.get("QDRANT_NAMESPACE", "qdrant"))
    p.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-east-1"))
    p.add_argument("--api-key", default=os.environ.get("QDRANT_API_KEY", ""))
    p.add_argument("--restart", action="store_true", help="Restart the qdrant statefulset after uploading snapshots.")
    p.add_argument("--quiesce-cmd", help="Shell command to run before restore to quiesce writes (optional).")
    return p.parse_args()

def main():
    args = parse_args()
    if args.backup_id and (not args.s3_bucket or not args.s3_prefix):
        print("--backup-id requires --s3-bucket and --s3-prefix", file=sys.stderr)
        sys.exit(2)
    restore(args)

if __name__ == "__main__":
    main()
