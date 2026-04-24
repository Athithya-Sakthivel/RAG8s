#!/usr/bin/env python3
"""
run_qdrant_backup_service.py

AWS-native Qdrant backup (data-plane / app-layer).

Behavior:
 - Creates snapshots via Qdrant HTTP API for each collection.
 - Downloads snapshot archive(s) via Qdrant service endpoints.
 - Uploads snapshot files + manifest.json + latest.manifest.json to Amazon S3.
 - Uses AWS SDK credential resolution (environment variables, profile, IAM role, etc.).
 - Retries transient network/storage failures with exponential backoff + jitter.
 - Exits 0 on success, non-zero on any fatal failure.

Primary configuration:
 - DATA_S3_BUCKET   (required)
 - DATA_S3_PREFIX    (optional, default: qdrant/backups)
 - QDRANT_URL       (optional, default: http://127.0.0.1:6333)
 - QDRANT_API_KEY   (optional, sent as Qdrant api-key header)

Compatibility fallbacks:
 - BACKUP_S3_BUCKET / BACKUP_BUCKET for bucket
 - BACKUP_PREFIX for prefix
 - BACKUP_ENV / ENV for manifest env tag

Exit codes:
 - 0: success
 - 2: user error / missing required args/env
 - 3: operation failure (snapshot/download/upload/permission/etc.)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:  # pragma: no cover
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception

# ----------------------------
# Environment helpers
# ----------------------------
def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# ----------------------------
# Configuration
# ----------------------------
DEFAULT_QDRANT_URL = _env_str("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
DEFAULT_S3_PREFIX = _env_str("DATA_S3_PREFIX", _env_str("BACKUP_PREFIX", "qdrant/backups")).strip("/")
DEFAULT_LOCAL_DIR = _env_str("BACKUP_LOCAL_DIR", "tmp")
DEFAULT_TIMEOUT = _env_int("BACKUP_TIMEOUT", 300)
DEFAULT_ENV_TAG = _env_str("BACKUP_ENV", _env_str("ENV", "STAGING")).upper()

RETRY_ATTEMPTS = _env_int("BACKUP_RETRY_ATTEMPTS", 4)
RETRY_BASE_SECONDS = _env_float("BACKUP_RETRY_BASE", 1.5)
RETRY_CAP_SECONDS = _env_float("BACKUP_RETRY_CAP", 60.0)
CHUNK_SIZE = 1024 * 1024

S3_BUCKET = (
    _env_str("DATA_S3_BUCKET", "")
    or _env_str("BACKUP_S3_BUCKET", "")
    or _env_str("BACKUP_BUCKET", "")
).strip()

QDRANT_API_KEY = _env_str("QDRANT_API_KEY", "").strip()
KEEP_LOCAL = _env_bool("BACKUP_KEEP_LOCAL", False)

# ----------------------------
# Logging
# ----------------------------
def log(msg: str, /, *args) -> None:
    ts = dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    if args:
        msg = msg % args
    print(f"{ts} {msg}", flush=True)


# ----------------------------
# Retry
# ----------------------------
def _sleep_with_backoff(attempt: int) -> None:
    backoff = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    jitter = backoff * (0.5 + random.random() * 0.5)
    time.sleep(jitter)


def retry_call(func, attempts: int = RETRY_ATTEMPTS, retriable: tuple[type, ...] = (Exception,)):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retriable as e:
            last_exc = e
            if attempt >= attempts:
                raise
            log("Transient error (attempt %d/%d): %s", attempt, attempts, str(e))
            _sleep_with_backoff(attempt)
    raise last_exc  # pragma: no cover


# ----------------------------
# Qdrant client helpers
# ----------------------------
def qdrant_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"accept": "application/json"})
    if QDRANT_API_KEY:
        session.headers.update({"api-key": QDRANT_API_KEY})
    return session


def _qdrant_json(resp: requests.Response) -> Any:
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Expected JSON response from Qdrant, got: {resp.text[:500]}") from e


def list_collections(qdrant_url: str, timeout: int = 10) -> list[str]:
    url = f"{qdrant_url.rstrip('/')}/collections"
    session = qdrant_session()

    def _call() -> list[str]:
        with session.get(url, timeout=timeout) as r:
            j = _qdrant_json(r)
        result = j.get("result", j)

        cols: list[str] = []
        if isinstance(result, dict) and "collections" in result:
            for c in result["collections"]:
                if isinstance(c, dict) and "name" in c:
                    cols.append(str(c["name"]))
                elif isinstance(c, str):
                    cols.append(c)
        elif isinstance(result, list):
            for c in result:
                if isinstance(c, dict) and "name" in c:
                    cols.append(str(c["name"]))
                elif isinstance(c, str):
                    cols.append(c)
        return cols

    return retry_call(_call, attempts=RETRY_ATTEMPTS, retriable=(requests.RequestException,))


def request_snapshot_name(qdrant_url: str, collection: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots"
    session = qdrant_session()

    def _call() -> str:
        with session.post(url, params={"wait": "true"}, timeout=timeout) as r:
            j = _qdrant_json(r)

        cand = j.get("result", j)
        if isinstance(cand, dict):
            for key in ("name", "snapshot", "snapshot_name"):
                if cand.get(key):
                    return str(cand[key])
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
        for key in ("name", "snapshot", "snapshot_name"):
            if j.get(key):
                return str(j[key])

        raise RuntimeError(f"Unable to determine snapshot name from Qdrant response: {j}")

    return retry_call(_call, attempts=RETRY_ATTEMPTS, retriable=(requests.RequestException,))


def download_snapshot(
    qdrant_url: str,
    collection: str,
    snapshot_name: str,
    dest: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """
    Download via the current collection snapshot endpoint:
      GET /collections/:collection_name/snapshots/:snapshot_name
    """
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots/{snapshot_name}"
    session = qdrant_session()

    def _call() -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(dest.suffix + ".part")

        if tmp_dest.exists():
            try:
                tmp_dest.unlink()
            except Exception:
                pass

        try:
            with session.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                with tmp_dest.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
            tmp_dest.replace(dest)
        except Exception:
            try:
                if tmp_dest.exists():
                    tmp_dest.unlink()
            except Exception:
                pass
            raise

    retry_call(_call, attempts=RETRY_ATTEMPTS, retriable=(requests.RequestException,))


# ----------------------------
# S3 helpers
# ----------------------------
def s3_client():
    if boto3 is None:
        raise RuntimeError(
            "boto3 and botocore are required. Install them in the runtime before running backups."
        )
    return boto3.client("s3")


def _join_s3_key(*parts: str) -> str:
    cleaned: list[str] = []
    for part in parts:
        if part is None:
            continue
        s = str(part).strip("/")
        if s:
            cleaned.append(s)
    return "/".join(cleaned)


def _safe_fs_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def upload_file_with_retries(
    client,
    bucket: str,
    key: str,
    filename: str,
    attempts: int = RETRY_ATTEMPTS,
    content_type: str | None = None,
) -> None:
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            extra_args = {}
            if content_type:
                extra_args["ContentType"] = content_type

            if extra_args:
                client.upload_file(filename, bucket, key, ExtraArgs=extra_args)
            else:
                client.upload_file(filename, bucket, key)
            return
        except (ClientError, BotoCoreError, OSError, Exception) as e:
            last_exc = e
            if attempt >= attempts:
                raise RuntimeError(f"Upload failed for s3://{bucket}/{key}: {e}") from e
            log("S3 upload transient error (attempt %d/%d): %s", attempt, attempts, str(e))
            _sleep_with_backoff(attempt)

    raise RuntimeError(f"Upload failed for s3://{bucket}/{key}: {last_exc}")


# ----------------------------
# Backup workflow
# ----------------------------
def run_service_backup(
    qdrant_url: str,
    s3_bucket: str,
    s3_prefix: str,
    local_dir: str | None,
    timeout: int,
    env_tag: str,
) -> tuple[str, str]:
    """
    Main orchestration:
     - enumerate collections
     - request snapshot for each collection
     - download snapshot files
     - upload snapshot files to S3 under <s3_prefix>/<backup_id>/
     - write manifest.json and latest.manifest.json
    Returns (backup_id, local_tmp_dir)
    """
    timestamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
    local_tmp = Path(local_dir or DEFAULT_LOCAL_DIR).resolve() / backup_id
    local_tmp.mkdir(parents=True, exist_ok=True)

    log("Starting AWS-native backup: id=%s qdrant=%s bucket=%s prefix=%s", backup_id, qdrant_url, s3_bucket, s3_prefix)

    s3 = s3_client()

    collections = list_collections(qdrant_url, timeout=min(10, timeout))
    if not collections:
        raise RuntimeError("No collections found to backup from Qdrant")

    manifest: dict[str, Any] = {
        "backup_id": backup_id,
        "created_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "env": env_tag,
        "mode": "service",
        "qdrant_url": qdrant_url,
        "storage": {
            "provider": "aws_s3",
            "bucket": s3_bucket,
            "prefix": s3_prefix,
        },
        "collections": {},
    }

    for collection in collections:
        safe_collection = _safe_fs_name(collection)
        log("[%s] requesting snapshot...", collection)
        snapshot_name = request_snapshot_name(qdrant_url, collection, timeout=timeout)
        log("[%s] snapshot name: %s", collection, snapshot_name)

        collection_dir = local_tmp / safe_collection
        local_snapshot_path = collection_dir / snapshot_name

        log("[%s] downloading snapshot to: %s", collection, local_snapshot_path)
        download_snapshot(qdrant_url, collection, snapshot_name, local_snapshot_path, timeout=timeout)

        sha = sha256_of_file(local_snapshot_path)
        size = local_snapshot_path.stat().st_size

        s3_key = _join_s3_key(s3_prefix, backup_id, collection, snapshot_name)
        log("[%s] uploading to s3 bucket=%s key=%s", collection, s3_bucket, s3_key)
        upload_file_with_retries(
            s3,
            s3_bucket,
            s3_key,
            str(local_snapshot_path),
            content_type="application/octet-stream",
        )

        manifest["collections"][collection] = {
            "snapshot_name": snapshot_name,
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
            "s3_uri": f"s3://{s3_bucket}/{s3_key}",
            "sha256": sha,
            "size_bytes": size,
            "local_path": str(local_snapshot_path),
        }
        log("[%s] uploaded (size=%d sha256=%s)", collection, size, sha)

    manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
    manifest_local = local_tmp / "manifest.json"
    latest_local = local_tmp / "latest.manifest.json"
    manifest_local.write_text(manifest_json, encoding="utf-8")
    latest_local.write_text(manifest_json, encoding="utf-8")

    manifest_key = _join_s3_key(s3_prefix, backup_id, "manifest.json")
    latest_key = _join_s3_key(s3_prefix, "latest.manifest.json")

    log("Uploading manifest -> s3://%s/%s", s3_bucket, manifest_key)
    upload_file_with_retries(
        s3,
        s3_bucket,
        manifest_key,
        str(manifest_local),
        content_type="application/json",
    )

    log("Uploading latest manifest -> s3://%s/%s", s3_bucket, latest_key)
    upload_file_with_retries(
        s3,
        s3_bucket,
        latest_key,
        str(latest_local),
        content_type="application/json",
    )

    log("Backup manifest: %s", manifest_json)
    log("Backup finished. backup_id=%s local=%s", backup_id, str(local_tmp))
    return backup_id, str(local_tmp)


# ----------------------------
# CLI / main
# ----------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Qdrant service-mode backup (AWS S3).")
    p.add_argument("--data-s3-bucket", default=S3_BUCKET, help="S3 bucket for backups. Can also be set via DATA_S3_BUCKET env.")
    p.add_argument("--data-s3-prefix", default=DEFAULT_S3_PREFIX, help="S3 prefix for backups (default qdrant/backups).")
    p.add_argument("--local-dir", default=DEFAULT_LOCAL_DIR, help="Local directory to store temporary backup files.")
    p.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL, help="Qdrant service URL.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout seconds for HTTP/storage operations.")
    p.add_argument("--env", default=DEFAULT_ENV_TAG, help="ENV tag for manifest (STAGING/PROD).")
    return p.parse_args()


def main():
    args = parse_args()

    qdrant_url = str(args.qdrant_url).rstrip("/")
    s3_bucket = str(args.data_s3_bucket or "").strip()
    s3_prefix = str(args.data_s3_prefix or DEFAULT_S3_PREFIX).strip("/")
    local_dir = str(args.local_dir or DEFAULT_LOCAL_DIR)
    timeout = int(args.timeout)
    env_tag = str(args.env or DEFAULT_ENV_TAG).upper()

    if not s3_bucket:
        print(
            "ERROR: DATA_S3_BUCKET is required (set --data-s3-bucket or DATA_S3_BUCKET env).",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        backup_id, local_path = run_service_backup(
            qdrant_url=qdrant_url,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            local_dir=local_dir,
            timeout=timeout,
            env_tag=env_tag,
        )

        print("SUCCESS:", backup_id, local_path)
        sys.exit(0)
    except Exception as e:
        print("ERROR:", str(e), file=sys.stderr)
        sys.exit(3)
    finally:
        if not KEEP_LOCAL:
            try:
                backup_root = Path(local_dir).resolve()
                if backup_root.exists():
                    shutil.rmtree(backup_root, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
