# Creates Qdrant collection snapshots via HTTP API and downloads them locally.
# Uploads snapshot files and manifests to S3 under qdrant/backups prefix.
# Implements retries with exponential backoff for network and S3 operations.
# Generates a deterministic manifest with checksums, sizes, and S3 locations.
# Cleans up local temp data unless explicitly configured to retain backups.

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
except Exception:
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception

CHUNK_SIZE = 1024 * 1024
DEFAULT_QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").strip().rstrip("/")
DEFAULT_S3_PREFIX = "qdrant/backups"
DEFAULT_LOCAL_DIR = os.environ.get("BACKUP_LOCAL_DIR", "tmp").strip() or "tmp"
DEFAULT_TIMEOUT = int(os.environ.get("BACKUP_TIMEOUT", "300") or "300")
DEFAULT_ENV_TAG = (os.environ.get("BACKUP_ENV") or os.environ.get("ENV") or "STAGING").strip().upper()

RETRY_ATTEMPTS = int(os.environ.get("BACKUP_RETRY_ATTEMPTS", "4") or "4")
RETRY_BASE_SECONDS = float(os.environ.get("BACKUP_RETRY_BASE", "1.5") or "1.5")
RETRY_CAP_SECONDS = float(os.environ.get("BACKUP_RETRY_CAP", "60") or "60")

S3_BUCKET = (
    (os.environ.get("BACKUP_S3_BUCKET") or "").strip()
    or (os.environ.get("BACKUP_BUCKET") or "").strip()
)

QDRANT_API_KEY = (os.environ.get("QDRANT_API_KEY") or "").strip()
KEEP_LOCAL = str(os.environ.get("BACKUP_KEEP_LOCAL", "")).strip().lower() in {"1", "true", "yes", "y", "on"}


def log(msg: str, *args: Any) -> None:
    ts = dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    if args:
        msg = msg % args
    print(f"{ts} {msg}", flush=True)



def _sleep_with_backoff(attempt: int) -> None:
    backoff = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    time.sleep(backoff * (0.5 + random.random() * 0.5))


def retry_call(func, attempts: int = RETRY_ATTEMPTS, retriable: tuple[type, ...] = (Exception,)):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retriable as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            log("Transient error (attempt %d/%d): %s", attempt, attempts, str(exc))
            _sleep_with_backoff(attempt)
    raise last_exc


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
    except Exception as exc:
        raise RuntimeError(f"Expected JSON response from Qdrant, got: {resp.text[:500]}") from exc


def list_collections(qdrant_url: str, timeout: int) -> list[str]:
    url = f"{qdrant_url.rstrip('/')}/collections"
    session = qdrant_session()

    def _call() -> list[str]:
        with session.get(url, timeout=timeout) as resp:
            payload = _qdrant_json(resp)
        result = payload.get("result", payload)

        collections: list[str] = []
        if isinstance(result, dict) and "collections" in result:
            items = result["collections"]
        elif isinstance(result, list):
            items = result
        else:
            items = []

        for item in items:
            if isinstance(item, dict) and item.get("name"):
                collections.append(str(item["name"]))
            elif isinstance(item, str) and item.strip():
                collections.append(item.strip())
        return collections

    return retry_call(_call, attempts=RETRY_ATTEMPTS, retriable=(requests.RequestException,))


def request_snapshot_name(qdrant_url: str, collection: str, timeout: int) -> str:
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots"
    session = qdrant_session()

    def _call() -> str:
        with session.post(url, params={"wait": "true"}, timeout=timeout) as resp:
            payload = _qdrant_json(resp)

        candidate = payload.get("result", payload)
        if isinstance(candidate, dict):
            for key in ("name", "snapshot", "snapshot_name"):
                value = candidate.get(key)
                if value:
                    return str(value)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        for key in ("name", "snapshot", "snapshot_name"):
            value = payload.get(key)
            if value:
                return str(value)
        raise RuntimeError(f"Unable to determine snapshot name from Qdrant response: {payload}")

    return retry_call(_call, attempts=RETRY_ATTEMPTS, retriable=(requests.RequestException,))


def download_snapshot(qdrant_url: str, collection: str, snapshot_name: str, dest: Path, timeout: int) -> None:
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
            with session.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with tmp_dest.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
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


def s3_client():
    if boto3 is None:
        raise RuntimeError("boto3 and botocore are required in the runtime.")
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
            if chunk:
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
        except (ClientError, BotoCoreError, OSError, Exception) as exc:
            last_exc = exc
            if attempt >= attempts:
                raise RuntimeError(f"Upload failed for s3://{bucket}/{key}: {exc}") from exc
            log("S3 upload transient error (attempt %d/%d): %s", attempt, attempts, str(exc))
            _sleep_with_backoff(attempt)
    raise RuntimeError(f"Upload failed for s3://{bucket}/{key}: {last_exc}")


def run_service_backup(
    qdrant_url: str,
    s3_bucket: str,
    s3_prefix: str,
    local_dir: str,
    timeout: int,
    env_tag: str,
) -> tuple[str, str]:
    timestamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
    local_tmp = Path(local_dir).resolve() / backup_id
    local_tmp.mkdir(parents=True, exist_ok=True)

    log("Starting backup: id=%s qdrant=%s bucket=%s prefix=%s", backup_id, qdrant_url, s3_bucket, s3_prefix)

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
        "storage": {"provider": "aws_s3", "bucket": s3_bucket, "prefix": s3_prefix},
        "collections": {},
    }

    for collection in collections:
        safe_collection = _safe_fs_name(collection)
        log("[%s] requesting snapshot", collection)
        snapshot_name = request_snapshot_name(qdrant_url, collection, timeout=timeout)
        log("[%s] snapshot name: %s", collection, snapshot_name)

        collection_dir = local_tmp / safe_collection
        local_snapshot_path = collection_dir / snapshot_name

        log("[%s] downloading snapshot to %s", collection, local_snapshot_path)
        download_snapshot(qdrant_url, collection, snapshot_name, local_snapshot_path, timeout=timeout)

        sha = sha256_of_file(local_snapshot_path)
        size = local_snapshot_path.stat().st_size
        s3_key = _join_s3_key(s3_prefix, backup_id, collection, snapshot_name)

        log("[%s] uploading to s3://%s/%s", collection, s3_bucket, s3_key)
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
        log("[%s] uploaded size=%d sha256=%s", collection, size, sha)

    manifest_json = json.dumps(manifest, indent=2, sort_keys=True)
    manifest_local = local_tmp / "manifest.json"
    latest_local = local_tmp / "latest.manifest.json"
    manifest_local.write_text(manifest_json, encoding="utf-8")
    latest_local.write_text(manifest_json, encoding="utf-8")

    manifest_key = _join_s3_key(s3_prefix, backup_id, "manifest.json")
    latest_key = _join_s3_key(s3_prefix, "latest.manifest.json")

    log("Uploading manifest to s3://%s/%s", s3_bucket, manifest_key)
    upload_file_with_retries(s3, s3_bucket, manifest_key, str(manifest_local), content_type="application/json")

    log("Uploading latest manifest to s3://%s/%s", s3_bucket, latest_key)
    upload_file_with_retries(s3, s3_bucket, latest_key, str(latest_local), content_type="application/json")

    log("Backup finished backup_id=%s local=%s", backup_id, str(local_tmp))
    return backup_id, str(local_tmp)


def parse_args():
    parser = argparse.ArgumentParser(description="Qdrant AWS S3 backup")
    parser.add_argument("--data-s3-bucket", default=S3_BUCKET)
    parser.add_argument("--data-s3-prefix", default=DEFAULT_S3_PREFIX)
    parser.add_argument("--local-dir", default=DEFAULT_LOCAL_DIR)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--env", default=DEFAULT_ENV_TAG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    qdrant_url = str(args.qdrant_url).strip().rstrip("/")
    s3_bucket = str(args.data_s3_bucket or "").strip()
    s3_prefix = str(args.data_s3_prefix or DEFAULT_S3_PREFIX).strip("/")
    local_dir = str(args.local_dir or DEFAULT_LOCAL_DIR).strip() or DEFAULT_LOCAL_DIR
    timeout = int(args.timeout)
    env_tag = str(args.env or DEFAULT_ENV_TAG).strip().upper()

    if not s3_bucket:
        print("ERROR: DATA_S3_BUCKET is required.", file=sys.stderr)
        return 2

    try:
        backup_id, local_path = run_service_backup(
            qdrant_url=qdrant_url,
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            local_dir=local_dir,
            timeout=timeout,
            env_tag=env_tag,
        )
        print(f"SUCCESS: {backup_id} {local_path}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        if not KEEP_LOCAL:
            try:
                root = Path(local_dir).resolve()
                if root.exists():
                    shutil.rmtree(root, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
