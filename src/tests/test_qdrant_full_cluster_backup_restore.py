#!/usr/bin/env python3
"""
test_qdrant_full_cluster_backup_restore.py

Fully-automated, robust cluster-level backup & restore for Qdrant (per-node, per-collection snapshots).
- Discovers qdrant pods in a namespace
- Discovers collections (robust to response shapes)
- For each pod -> for each collection:
    - port-forward pod -> POST /collections/{collection}/snapshots?wait=true
    - download snapshot -> upload to S3 under <s3_prefix>/<backup_id>/
- Writes manifest.json and latest.manifest.json to S3
- Restore reads manifest and uploads snapshots into the matching pod via
  POST /collections/{collection}/snapshots/upload (priority=snapshot)
- Deterministic ordering: pods and collections are sorted
- Writes local artifacts to ./tmp/<backup_id>/

Dependencies:
    pip install boto3 requests

Usage (examples):
    Backup:
      python3 infra/tests/test_qdrant_full_cluster_backup_restore.py --backup --s3-bucket e2e-rag-system-42

    Restore:
      python3 infra/tests/test_qdrant_full_cluster_backup_restore.py --restore <backup_id> --s3-bucket e2e-rag-system-42
"""
import argparse
import datetime
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

# ---------------- config ----------------
DEFAULT_NAMESPACE = "qdrant"
DEFAULT_S3_PREFIX = "qdrant/backups"
KUBECTL = shutil.which("kubectl") or "kubectl"
PORT_FORWARD_TIMEOUT = 15
REQUEST_TIMEOUT = 600

# ---------------- utils ----------------
def sha256sum(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest(), path.stat().st_size

def get_free_port():
    s = socket.socket()
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def run_cmd(cmd, timeout=20, capture=True):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None,
                              timeout=timeout, check=True, text=True)
        return proc.stdout if capture else ""
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"cmd failed: {' '.join(cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"cmd timeout: {' '.join(cmd)}")

# ---------------- kubectl helpers ----------------
def list_qdrant_pods(namespace):
    labels = ["app.kubernetes.io/name=qdrant", "app=qdrant"]
    for label in labels:
        try:
            out = run_cmd([KUBECTL, "-n", namespace, "get", "pods", "-l", label, "-o", "jsonpath={.items[*].metadata.name}"], timeout=10)
            out = out.strip()
            if out:
                pods = out.split()
                pods.sort()
                return pods
        except Exception:
            continue
    raise RuntimeError(f"No qdrant pods found in namespace '{namespace}'. Check kubectl context and labels.")

def start_port_forward(namespace, pod, local_port):
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
                stdout, stderr = proc.communicate(timeout=1)
                raise RuntimeError(f"port-forward for pod {pod} failed early: {stderr.strip()}")
    # timed out
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    raise RuntimeError(f"Timed out waiting for port-forward to pod {pod} on local port {local_port}")

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

# ---------------- qdrant API helpers ----------------
def normalize_collections_response(resp_json):
    """
    Accept variants and return list of collection names.
    Example shapes:
      {"result": {"collections":[ {"name":"col1"}, ... ] }, "status":"ok"}
      {"result": [{"name":"col1"}, ...]}
      ["col1","col2"]
      {"collections":[ {"name":"col1"}, ... ]}
    """
    if isinstance(resp_json, dict):
        if "result" in resp_json:
            result = resp_json["result"]
            if isinstance(result, dict) and "collections" in result:
                raw = result["collections"]
            elif isinstance(result, list):
                raw = result
            else:
                # unexpected but try to extract collections key inside
                if isinstance(result, dict) and "collections" in result:
                    raw = result["collections"]
                else:
                    raise ValueError(f"Unsupported /collections 'result' shape: {resp_json}")
        elif "collections" in resp_json:
            raw = resp_json["collections"]
        else:
            raise ValueError(f"Unsupported /collections response shape: {resp_json}")
    elif isinstance(resp_json, list):
        raw = resp_json
    else:
        raise ValueError(f"Unsupported /collections response type: {type(resp_json)}")

    names = []
    for entry in raw:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and "name" in entry:
            names.append(entry["name"])
        else:
            # skip unknown entries
            continue
    # filter obvious garbage and dedupe
    names = [n for n in names if isinstance(n, str) and n.strip() != "" and n not in ("collections", "result")]
    # deterministic ordering
    names = sorted(set(names))
    return names

def create_collection_snapshot_local(port, collection, api_key, wait=True, timeout=REQUEST_TIMEOUT):
    url = f"http://127.0.0.1:{port}/collections/{collection}/snapshots"
    headers = {}
    if api_key:
        headers["api-key"] = api_key
    params = {"wait": "true" if wait else "false"}
    r = requests.post(url, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    js = r.json()
    # try to extract name
    if isinstance(js, dict):
        if "result" in js and isinstance(js["result"], dict) and "name" in js["result"]:
            return js["result"]["name"]
        if "name" in js:
            return js["name"]
    return str(js)

def download_snapshot_local(port, collection, snapshot_name, out_path, api_key, timeout=REQUEST_TIMEOUT):
    url = f"http://127.0.0.1:{port}/collections/{collection}/snapshots/{snapshot_name}"
    headers = {}
    if api_key:
        headers["api-key"] = api_key
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return Path(out_path)

def upload_snapshot_to_pod_local(port, collection, file_path: Path, api_key, checksum=None, timeout=REQUEST_TIMEOUT):
    url = f"http://127.0.0.1:{port}/collections/{collection}/snapshots/upload"
    headers = {}
    if api_key:
        headers["api-key"] = api_key
    params = {"priority": "snapshot"}
    if checksum:
        params["checksum"] = checksum
    with open(file_path, "rb") as fh:
        files = {"snapshot": (file_path.name, fh, "application/octet-stream")}
        r = requests.post(url, headers=headers, params=params, files=files, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ---------------- S3 helpers ----------------
def s3_client(region):
    return boto3.client("s3", region_name=region)

def upload_file_s3(s3, local_path: Path, bucket: str, key: str):
    s3.upload_file(str(local_path), bucket, key)
    return {"bucket": bucket, "key": key}

def download_file_s3(s3, bucket: str, key: str, local_path: Path):
    s3.download_file(bucket, key, str(local_path))
    return Path(local_path)

# ---------------- backup / restore flows ----------------
def backup_cluster(namespace, s3_bucket, s3_prefix, collections_arg, api_key, aws_region):
    pods = list_qdrant_pods(namespace)
    if not pods:
        raise RuntimeError("no qdrant pods found")

    # discover collections if not provided
    if collections_arg:
        collections = sorted(set(collections_arg))
    else:
        # query first pod for /collections
        tmp_port = get_free_port()
        pf = start_port_forward(namespace, pods[0], tmp_port)
        try:
            url = f"http://127.0.0.1:{tmp_port}/collections"
            headers = {"api-key": api_key} if api_key else {}
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            raw = r.json()
            collections = normalize_collections_response(raw)
        finally:
            stop_port_forward(pf)
        if not collections:
            raise RuntimeError("no collections discovered; provide --collections")

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{timestamp}-{os.urandom(4).hex()[:8]}"
    workdir = Path.cwd() / "tmp" / backup_id
    workdir.mkdir(parents=True, exist_ok=True)

    s3 = s3_client(aws_region)
    manifest = {
        "backup_id": backup_id,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "namespace": namespace,
        "pods": {},
        "collections": collections,
    }

    print(f"Starting backup -> local tmp: {workdir} ; s3://{s3_bucket}/{s3_prefix}/{backup_id}/")
    # deterministic order
    pods_sorted = sorted(pods)
    cols_sorted = sorted(collections)

    for pod in pods_sorted:
        pod_entry = {"collections": {}}
        pod_dir = workdir / pod
        pod_dir.mkdir(parents=True, exist_ok=True)
        local_port = get_free_port()
        pf = start_port_forward(namespace, pod, local_port)
        try:
            for col in cols_sorted:
                print(f"[{pod}] creating snapshot for collection '{col}' ...")
                try:
                    snap_name = create_collection_snapshot_local(local_port, col, api_key, wait=True, timeout=REQUEST_TIMEOUT)
                except requests.HTTPError as e:
                    # log and skip this collection on this pod
                    print(f"[{pod}] snapshot request failed for collection '{col}': {e}", file=sys.stderr)
                    continue
                local_fname = f"{pod}-{col}-{snap_name}.snapshot"
                local_path = pod_dir / local_fname
                print(f"[{pod}] downloading snapshot {snap_name} -> {local_path}")
                download_snapshot_local(local_port, col, snap_name, local_path, api_key, timeout=REQUEST_TIMEOUT)
                checksum, size = sha256sum(local_path)
                s3_key = f"{s3_prefix.rstrip('/')}/{backup_id}/{local_fname}"
                print(f"[{pod}] uploading to s3: s3://{s3_bucket}/{s3_key}")
                upload_file_s3(s3, local_path, s3_bucket, s3_key)
                pod_entry["collections"][col] = {"snapshot_name": snap_name, "s3_key": s3_key, "sha256": checksum, "size": size}
            manifest["pods"][pod] = pod_entry
        finally:
            stop_port_forward(pf)

    # write manifest locally and upload
    manifest_path = workdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    latest_path = workdir / "latest.manifest.json"
    latest_path.write_text(json.dumps(manifest, indent=2))

    manifest_key = f"{s3_prefix.rstrip('/')}/{backup_id}/manifest.json"
    latest_key = f"{s3_prefix.rstrip('/')}/latest.manifest.json"
    print("Uploading manifest to s3...")
    upload_file_s3(s3, manifest_path, s3_bucket, manifest_key)
    upload_file_s3(s3, latest_path, s3_bucket, latest_key)
    print("Backup finished. backup_id:", backup_id, "local:", str(workdir))
    return backup_id, workdir

def restore_cluster(namespace, s3_bucket, s3_prefix, backup_id, api_key, aws_region):
    s3 = s3_client(aws_region)
    manifest_key = f"{s3_prefix.rstrip('/')}/{backup_id}/manifest.json"
    tmpdir = Path.cwd() / "tmp" / f"restore-{backup_id}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    local_manifest = tmpdir / "manifest.json"
    print("Downloading manifest from s3:", manifest_key)
    download_file_s3(s3, s3_bucket, manifest_key, local_manifest)
    manifest = json.loads(local_manifest.read_text())
    pods = sorted(manifest.get("pods", {}).keys())
    if not pods:
        raise RuntimeError("manifest contains no pods to restore")

    for pod in pods:
        pod_info = manifest["pods"][pod]
        print("Restoring pod:", pod)
        local_port = get_free_port()
        pf = start_port_forward(namespace, pod, local_port)
        try:
            for col, info in sorted(pod_info.get("collections", {}).items()):
                s3_key = info["s3_key"]
                s3_local = tmpdir / Path(s3_key).name
                print(f"  downloading s3://{s3_bucket}/{s3_key} -> {s3_local}")
                download_file_s3(s3, s3_bucket, s3_key, s3_local)
                checksum = info.get("sha256")
                print(f"  uploading snapshot into pod {pod} for collection {col} (checksum={checksum})")
                upload_snapshot_to_pod_local(local_port, col, s3_local, api_key, checksum=checksum, timeout=REQUEST_TIMEOUT)
                print(f"  restored collection {col} on pod {pod}")
        finally:
            stop_port_forward(pf)
    print("Restore complete for backup_id:", backup_id)

# ---------------- CLI ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Cluster-level Qdrant backup/restore (per-node snapshots)")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--backup", action="store_true", help="Perform cluster backup and upload to S3")
    grp.add_argument("--restore", metavar="BACKUP_ID", help="Restore cluster from BACKUP_ID (reads manifest from S3)")
    p.add_argument("--s3-bucket", required=True, help="S3 bucket to store backups")
    p.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX, help="S3 prefix for backups")
    p.add_argument("--collections", nargs="+", help="Collections to snapshot; if omitted, will attempt to discover")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="Kubernetes namespace where Qdrant runs")
    p.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-east-1"), help="AWS region for S3")
    p.add_argument("--api-key", default=os.environ.get("QDRANT_API_KEY", ""), help="Qdrant API key (if required)")
    return p.parse_args()

def main():
    args = parse_args()
    namespace = args.namespace
    s3_bucket = args.s3_bucket
    s3_prefix = args.s3_prefix
    aws_region = args.aws_region
    api_key = args.api_key

    # inputs hash (deterministic check) - still allow repeated backups
    manifests_dir = Path("infra/manifests/qdrant_backups")
    manifests_dir.mkdir(parents=True, exist_ok=True)
    inputs = {"namespace": namespace, "s3_bucket": s3_bucket, "s3_prefix": s3_prefix, "collections": args.collections or []}
    ih = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    ih_path = manifests_dir / ".inputs_hash"
    if ih_path.exists() and ih_path.read_text().strip() == ih:
        print("No non-secret changes detected (inputs hash unchanged); proceeding (still performs backup).")
    else:
        ih_path.write_text(ih)

    if args.backup:
        backup_id, workdir = backup_cluster(namespace, s3_bucket, s3_prefix, args.collections, api_key, aws_region)
        print("Backup complete:", backup_id, "localdir:", workdir)
        return
    if args.restore:
        restore_cluster(namespace, s3_bucket, s3_prefix, args.restore, api_key, aws_region)
        return

if __name__ == "__main__":
    main()
