#!/usr/bin/env python3
"""
infra/tests/validate_restore.py

Validate Qdrant restore using a manifest and local port-forwards.
If manifest or snapshot files are missing locally, and --s3-bucket/--s3-prefix
(or --backup-id with those) are provided, this script will download the
manifest and missing snapshot files from S3 automatically.

Usage examples:
  # Prefer local manifest (default path tmp/latest/manifest.json)
  python3 infra/tests/validate_restore.py --port-map qdrant-0:7000,qdrant-1:7001,qdrant-2:7002

  # If manifest not present locally, fetch latest from S3 prefix
  python3 infra/tests/validate_restore.py --s3-bucket e2e-rag-system-42 --s3-prefix qdrant/backups --port-base 7000

  # Restore from a specific backup id on S3
  python3 infra/tests/validate_restore.py --s3-bucket e2e-rag-system-42 --s3-prefix qdrant/backups --backup-id 20251203T134734Z-defb5319 --port-base 7000

Requirements:
  pip install requests boto3
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import boto3
import requests

DEFAULT_MANIFEST = Path("tmp/latest/manifest.json")
DEFAULT_PORT_BASE = 7000
TIMEOUT = 5  # seconds for HTTP calls


def sha256_of_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_port_map(s: str) -> dict[str, int]:
    m = {}
    for item in s.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"invalid mapping element '{item}', expected pod:port")
        pod, port = item.split(":", 1)
        m[pod] = int(port)
    return m


def http_get_json(url, headers=None, timeout=TIMEOUT):
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, str(e)


def http_post_json(url, json_body, headers=None, timeout=TIMEOUT):
    try:
        r = requests.post(url, json=json_body, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, str(e)


def download_manifest_from_s3(bucket: str, prefix: str, backup_id: str | None, region: str, target_dir: Path):
    s3 = boto3.client("s3", region_name=region)
    target_dir.mkdir(parents=True, exist_ok=True)
    if not backup_id:
        # download latest.manifest.json from prefix
        latest_key = prefix.rstrip("/") + "/latest.manifest.json"
        tgt = target_dir / "latest.manifest.json"
        print(f"Downloading latest manifest s3://{bucket}/{latest_key} -> {tgt}")
        s3.download_file(bucket, latest_key, str(tgt))
        data = json.loads(tgt.read_text())
        backup_id = data.get("backup_id")
        if not backup_id:
            raise RuntimeError("latest.manifest.json does not contain backup_id")
    manifest_key = prefix.rstrip("/") + f"/{backup_id}/manifest.json"
    manifest_target = target_dir / "manifest.json"
    print(f"Downloading manifest s3://{bucket}/{manifest_key} -> {manifest_target}")
    s3.download_file(bucket, manifest_key, str(manifest_target))
    return backup_id, manifest_target


def download_missing_snapshots_from_s3(manifest: dict, local_dir: Path, bucket: str, prefix: str, region: str):
    s3 = boto3.client("s3", region_name=region)
    missing = 0
    for pod, podinfo in manifest.get("pods", {}).items():
        for col, meta in (podinfo.get("collections") or {}).items():
            s3_key = meta.get("s3_key")
            if not s3_key:
                continue
            fname = Path(s3_key).name
            # look in local_dir and local_dir/pod
            candidate1 = local_dir / fname
            candidate2 = local_dir / pod / fname
            if candidate1.exists() or candidate2.exists():
                continue
            # download into local_dir
            target = local_dir / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            key = s3_key
            print(f"Downloading s3://{bucket}/{key} -> {target}")
            s3.download_file(bucket, key, str(target))
            missing += 1
    return missing


def load_manifest_local(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"manifest file not found: {path}")
    return json.loads(path.read_text())


def pretty_ok(s): return f"[ OK ] {s}"


def pretty_fail(s): return f"[FAIL] {s}"


def main():
    p = argparse.ArgumentParser(description="Validate Qdrant restore using manifest; auto-download from S3 if needed.")
    p.add_argument("--manifest", "-m", type=Path, default=DEFAULT_MANIFEST, help="path to manifest.json (default: tmp/latest/manifest.json)")
    p.add_argument("--local-dir", "-d", type=Path, help="local directory containing snapshot files; default = manifest parent dir")
    p.add_argument("--port-map", help="explicit mapping pod:port,pod2:port2 (overrides --port-base)")
    p.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE, help="base port for sequential mapping of pods (default 7000)")
    p.add_argument("--api-key", type=str, default="", help="optional Qdrant api-key header value (if used)")
    p.add_argument("--s3-bucket", help="S3 bucket to download manifest/snapshots if missing")
    p.add_argument("--s3-prefix", help="S3 prefix where backups live (e.g. qdrant/backups)")
    p.add_argument("--backup-id", help="Specific backup id to download from S3 (if omitted, latest.manifest.json is used)")
    p.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-east-1"), help="AWS region for S3")
    p.add_argument("--verbose", "-v", action="store_true", help="verbose output")
    args = p.parse_args()

    manifest_path = args.manifest
    # If manifest missing locally but S3 info provided, download
    if not manifest_path.exists():
        if args.s3_bucket and args.s3_prefix:
            print(f"Local manifest {manifest_path} missing; fetching from S3...")
            # default local dir is manifest parent
            local_dir = args.local_dir or manifest_path.parent
            try:
                backup_id, downloaded_manifest = download_manifest_from_s3(args.s3_bucket, args.s3_prefix, args.backup_id, args.aws_region, local_dir)
                # set manifest_path to downloaded path
                manifest_path = downloaded_manifest
                print(f"Downloaded manifest for backup_id={backup_id}")
            except Exception as e:
                print(pretty_fail(f"Failed to download manifest from S3: {e}"), file=sys.stderr)
                sys.exit(2)
        else:
            print(pretty_fail(f"Failed to load manifest: manifest file not found: {manifest_path}"), file=sys.stderr)
            sys.exit(2)

    # Load manifest
    try:
        manifest = load_manifest_local(manifest_path)
    except Exception as e:
        print(pretty_fail(f"Failed to load manifest: {e}"), file=sys.stderr)
        sys.exit(2)

    local_dir = args.local_dir or manifest_path.parent
    if not local_dir.exists():
        local_dir.mkdir(parents=True, exist_ok=True)

    # If snapshots missing and S3 info provided, download missing ones
    missing_files = []
    for pod, podinfo in manifest.get("pods", {}).items():
        for col, meta in (podinfo.get("collections") or {}).items():
            s3_key = meta.get("s3_key")
            if not s3_key:
                continue
            fname = Path(s3_key).name
            c1 = local_dir / fname
            c2 = local_dir / pod / fname
            if not c1.exists() and not c2.exists():
                missing_files.append((s3_key, fname))
    if missing_files:
        if args.s3_bucket and args.s3_prefix:
            print(f"Missing {len(missing_files)} snapshot files locally; downloading from S3...")
            try:
                count = download_missing_snapshots_from_s3(manifest, local_dir, args.s3_bucket, args.s3_prefix, args.aws_region)
                print(f"Downloaded {count} files.")
            except Exception as e:
                print(pretty_fail(f"Failed to download snapshot files from S3: {e}"), file=sys.stderr)
                sys.exit(3)
        else:
            print(pretty_fail(f"{len(missing_files)} snapshot files missing locally and no S3 info provided."), file=sys.stderr)
            sys.exit(3)

    # Build port mapping
    pods = manifest.get("pods") or {}
    if not pods:
        print(pretty_fail("Manifest contains no 'pods' section."), file=sys.stderr)
        sys.exit(2)
    pods_list = list(pods.keys())

    port_map = {}
    if args.port_map:
        try:
            port_map = parse_port_map(args.port_map)
        except Exception as e:
            print(pretty_fail(f"Failed to parse --port-map: {e}"), file=sys.stderr)
            sys.exit(2)
        missing = [p for p in pods_list if p not in port_map]
        if missing:
            print(pretty_fail(f"--port-map missing mappings for pods: {', '.join(missing)}"), file=sys.stderr)
            sys.exit(2)
        mapping = {p: port_map[p] for p in pods_list}
    else:
        mapping = {p: args.port_base + i for i, p in enumerate(pods_list)}

    header = {}
    if args.api_key:
        header["api-key"] = args.api_key

    print("Manifest:", manifest_path)
    print("Local snapshots dir:", local_dir)
    print("\nPod -> local port mapping:")
    for pod, port in mapping.items():
        print(f"  {pod} -> localhost:{port}")

    overall_ok = True
    files_missing = 0
    checksum_failed = 0

    # Validate snapshot files existence and checksums
    print("\nChecking snapshot files referenced in manifest (local)...")
    for pod, podinfo in pods.items():
        collections = podinfo.get("collections", {})
        for cname, cinfo in collections.items():
            s3_key = cinfo.get("s3_key")
            expected_sha = cinfo.get("sha256")
            fname = Path(s3_key).name if s3_key else None
            if not fname:
                print(pretty_fail(f"{pod}/{cname}: manifest entry missing s3_key"))
                overall_ok = False
                continue
            candidate1 = local_dir / fname
            candidate2 = local_dir / pod / fname
            if candidate1.exists():
                local_path = candidate1
            elif candidate2.exists():
                local_path = candidate2
            else:
                print(pretty_fail(f"{pod}/{cname}: missing local file {fname} (looked at {candidate1} and {candidate2})"))
                files_missing += 1
                overall_ok = False
                continue
            actual_sha = sha256_of_file(local_path)
            if expected_sha and actual_sha != expected_sha:
                print(pretty_fail(f"{pod}/{cname}: checksum mismatch for {local_path} (manifest: {expected_sha} actual: {actual_sha})"))
                checksum_failed += 1
                overall_ok = False
            else:
                if args.verbose:
                    print(pretty_ok(f"{pod}/{cname}: file present and checksum OK: {local_path.name}"))
                else:
                    print(pretty_ok(f"{pod}/{cname}: file present"))

    if files_missing:
        print(f"\n{files_missing} snapshot files missing locally. Provide correct --local-dir or ensure S3 has the files.", file=sys.stderr)
    if checksum_failed:
        print(f"\n{checksum_failed} files failed checksum verification.", file=sys.stderr)

    # Validate each pod's collections via its local port
    print("\nValidating per-pod Qdrant HTTP endpoints and collection presence/counts...")
    inconsistent_counts = []
    pod_counts = {}

    for pod in pods_list:
        port = mapping[pod]
        base = f"http://127.0.0.1:{port}"
        print(f"\n== Pod {pod} @ {base} ==")
        url_col = f"{base}/collections"
        data, err = http_get_json(url_col, headers=header, timeout=TIMEOUT)
        if err:
            print(pretty_fail(f"{pod}: failed to GET /collections -> {err}"))
            overall_ok = False
            continue
        found_collections = []
        if isinstance(data, dict) and "result" in data:
            r = data["result"]
            if isinstance(r, dict) and "collections" in r:
                for c in r["collections"]:
                    if isinstance(c, dict) and "name" in c:
                        found_collections.append(c["name"])
                    elif isinstance(c, str):
                        found_collections.append(c)
            elif isinstance(r, list):
                for c in r:
                    if isinstance(c, dict) and "name" in c:
                        found_collections.append(c["name"])
                    elif isinstance(c, str):
                        found_collections.append(c)
        else:
            print(pretty_fail(f"{pod}: unexpected /collections response structure: {data}"))
            overall_ok = False
            continue

        expected_cols = sorted(list(pods[pod].get("collections", {}).keys()))
        print("  collections (expected):", ", ".join(expected_cols) or "<none>")
        print("  collections (found)   :", ", ".join(sorted(found_collections)) or "<none>")

        missing_cols = [c for c in expected_cols if c not in found_collections]
        extra_cols = [c for c in found_collections if c not in expected_cols]
        if missing_cols:
            print(pretty_fail(f"  Missing collections on pod {pod}: {', '.join(missing_cols)}"))
            overall_ok = False
        if extra_cols:
            print(pretty_ok(f"  Extra collections on pod {pod} (present but not in manifest): {', '.join(extra_cols)}"))

        pod_counts[pod] = {}
        for col in expected_cols:
            url_count = f"{base}/collections/{col}/points/count"
            json_body = {"exact": True}
            data_c, err_c = http_post_json(url_count, json_body, headers=header, timeout=TIMEOUT)
            if err_c:
                print(pretty_fail(f"  {col}: failed points/count -> {err_c}"))
                overall_ok = False
                pod_counts[pod][col] = None
                continue
            cnt = None
            if isinstance(data_c, dict) and "result" in data_c and isinstance(data_c["result"], dict):
                cnt = data_c["result"].get("count")
            elif isinstance(data_c, dict) and "result" in data_c and isinstance(data_c["result"], (int, float)):
                cnt = data_c["result"]
            pod_counts[pod][col] = cnt
            print(f"  {col}: count = {cnt}")

    print("\nCross-pod collection count consistency (if counts present on multiple pods):")
    collections_all = set()
    for p in pod_counts:
        if pod_counts[p]:
            collections_all.update(list(pod_counts[p].keys()))
    for col in sorted(collections_all):
        counts = {}
        for p in pod_counts:
            v = pod_counts[p].get(col)
            if v is not None:
                counts[p] = v
        if not counts:
            print(f"  {col}: no count info")
            continue
        uniq = set(counts.values())
        if len(uniq) == 1:
            print(pretty_ok(f"  {col}: consistent counts across pods = {next(iter(uniq))}"))
        else:
            print(pretty_fail(f"  {col}: inconsistent counts across pods -> " + ", ".join(f"{pod}={cnt}" for pod, cnt in counts.items())))
            inconsistent_counts.append(col)
            overall_ok = False

    top_collections = manifest.get("collections") or []
    pods_union = sorted(set().union(*[list(pods[p]["collections"].keys()) for p in pods if "collections" in pods[p]]))
    if top_collections:
        missing_from_top = [c for c in pods_union if c not in top_collections]
        missing_from_pods = [c for c in top_collections if c not in pods_union]
        if missing_from_top:
            print(pretty_fail(f"\nManifest top-level 'collections' missing pods' collections: {missing_from_top}"))
            overall_ok = False
        if missing_from_pods:
            print(pretty_fail(f"\nTop-level 'collections' contains entries not found under pods: {missing_from_pods}"))
            overall_ok = False

    print("\nSummary:")
    if overall_ok:
        print(pretty_ok("All validation checks passed. Restore appears consistent."))
        sys.exit(0)
    else:
        print(pretty_fail("One or more validation checks failed. See above for details."))
        sys.exit(3)


if __name__ == "__main__":
    main()
