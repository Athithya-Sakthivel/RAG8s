#!/usr/bin/env python3
"""
run_qdrant_backup_service.py

Service-mode Qdrant backup (data-plane / app-layer). Designed for resilient operation:
 - Creates snapshots via Qdrant HTTP API for each collection.
 - Downloads snapshot archive(s) via Qdrant service endpoints.
 - Uploads snapshot files + manifest.json + latest.manifest.json to Azure Blob Storage.
 - Uses AZURE_STORAGE_CONNECTION_STRING (preferred) or AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY,
   or falls back to DefaultAzureCredential (managed identity) when available.
 - Retries transient network/storage failures with exponential backoff + jitter.
 - Exits 0 on success, non-zero on any fatal failure.

Exit codes:
 - 0: success
 - 2: user error / missing required args/env
 - 3: operation failure (snapshot/download/upload/permission/etc.)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

import requests

try:
    from azure.core.exceptions import AzureError
    from azure.storage.blob import BlobServiceClient, ContainerClient
except Exception:
    BlobServiceClient = None
    AzureError = Exception

# Configuration defaults
DEFAULT_QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
DEFAULT_BACKUP_PREFIX = os.environ.get("BACKUP_PREFIX", "qdrant/backups").rstrip("/")
DEFAULT_LOCAL_DIR = os.environ.get("BACKUP_LOCAL_DIR", "tmp")
DEFAULT_TIMEOUT = int(os.environ.get("BACKUP_TIMEOUT", "300"))
DEFAULT_ENV_TAG = os.environ.get("BACKUP_ENV", os.environ.get("ENV", "STAGING")).upper()

# Retry settings (can be tuned via environment)
RETRY_ATTEMPTS = int(os.environ.get("BACKUP_RETRY_ATTEMPTS", "4"))
RETRY_BASE_SECONDS = float(os.environ.get("BACKUP_RETRY_BASE", "1.5"))
RETRY_CAP_SECONDS = float(os.environ.get("BACKUP_RETRY_CAP", "60.0"))
CHUNK_SIZE = 8192

SENSITIVE_ENVS = {"AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT_KEY"}


def log(msg: str, /, *args) -> None:
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    print(f"{ts} {msg % args}", flush=True)


def _sleep_with_backoff(attempt: int) -> None:
    backoff = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    jitter = backoff * (0.5 + random.random() * 0.5)
    time.sleep(jitter)


def retry_call(func, attempts: int = RETRY_ATTEMPTS, on_except: tuple | None = None):
    """
    Retry helper. `on_except` is a tuple of exception types to treat as retriable;
    if None, all exceptions are retriable.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as e:
            last_exc = e
            retriable = True if on_except is None else isinstance(e, on_except)
            if not retriable or attempt == attempts:
                raise
            log("Transient error (attempt %d/%d): %s", attempt, attempts, str(e))
            _sleep_with_backoff(attempt)
    raise last_exc  # pragma: no cover


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def azure_client_from_env(allow_managed_identity: bool = True):
    """
    Build an Azure BlobServiceClient from env:
     - AZURE_STORAGE_CONNECTION_STRING
     - or AZURE_STORAGE_ACCOUNT_NAME + AZURE_STORAGE_ACCOUNT_KEY
     - or managed identity (DefaultAzureCredential) if allow_managed_identity=True
    """
    if BlobServiceClient is None:
        raise RuntimeError("azure-storage-blob (and azure-core) packages are required; install them in the runtime.")

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
    key = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY", "").strip()

    if conn:
        return BlobServiceClient.from_connection_string(conn)

    if account and key:
        url = f"https://{account}.blob.core.windows.net"
        return BlobServiceClient(account_url=url, credential=key)

    if allow_managed_identity:
        try:
            from azure.identity import DefaultAzureCredential  # type: ignore
            cred = DefaultAzureCredential()
            if not account:
                # if account name not set use env var AZURE_STORAGE_ACCOUNT_NAME (required for account_url)
                raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME required for managed identity mode")
            url = f"https://{account}.blob.core.windows.net"
            return BlobServiceClient(account_url=url, credential=cred)
        except Exception as e:
            raise RuntimeError("Managed identity fallback failed or not configured: " + str(e)) from e

    raise RuntimeError("No Azure storage credentials found (set AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_NAME+AZURE_STORAGE_ACCOUNT_KEY)")


def ensure_container(client: BlobServiceClient, container_name: str) -> ContainerClient:
    """
    Ensure the container exists. If not able to create due to permissions but it exists, proceed.
    """
    container_client = client.get_container_client(container_name)
    try:
        # create_container will fail if container exists; that's fine
        container_client.create_container()
        log("Created container '%s' (or it did not exist).", container_name)
    except Exception:
        # if access denied or already exists, do not fail immediately — verify existence
        try:
            container_client.get_container_properties()
            log("Using existing container '%s'.", container_name)
        except Exception as e2:
            raise RuntimeError(f"Cannot access or create Azure container '{container_name}': {e2}") from e2
    return container_client


def list_collections(qdrant_url: str, timeout: int = 10) -> list[str]:
    url = f"{qdrant_url.rstrip('/')}/collections"
    def _call():
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        result = j.get("result", j)
        cols: list[str] = []
        if isinstance(result, dict) and "collections" in result:
            for c in result["collections"]:
                if isinstance(c, dict) and "name" in c:
                    cols.append(c["name"])
                elif isinstance(c, str):
                    cols.append(c)
        elif isinstance(result, list):
            for c in result:
                if isinstance(c, dict) and "name" in c:
                    cols.append(c["name"])
                elif isinstance(c, str):
                    cols.append(c)
        return cols

    return retry_call(lambda: _call(), attempts=RETRY_ATTEMPTS, on_except=(requests.RequestException,))


def request_snapshot_and_get_name(qdrant_url: str, collection: str, wait: bool = True, timeout: int = DEFAULT_TIMEOUT) -> str:
    url = f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots"
    params = {"wait": "true"} if wait else {}
    def _call():
        r = requests.post(url, params=params, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        cand = j.get("result", j)
        if isinstance(cand, dict):
            for key in ("name", "snapshot", "snapshot_name"):
                if key in cand:
                    return str(cand[key])
        if isinstance(cand, str):
            return cand
        for key in ("snapshot", "snapshot_name", "name"):
            if key in j:
                return str(j[key])
        raise RuntimeError(f"Unable to determine snapshot name from Qdrant response: {j}")
    return retry_call(_call, attempts=RETRY_ATTEMPTS, on_except=(requests.RequestException,))


def download_snapshot(qdrant_url: str, collection: str, snapshot_name: str, dest: Path, timeout: int = DEFAULT_TIMEOUT) -> None:
    """
    Attempt to download the snapshot via the service endpoints. Retries internally.
    """
    urls = [
        f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots/{snapshot_name}/download",
        f"{qdrant_url.rstrip('/')}/collections/{collection}/snapshots/{snapshot_name}",
    ]
    def _try_download():
        last_err = None
        for u in urls:
            try:
                with requests.get(u, stream=True, timeout=timeout) as r:
                    if r.status_code == 200:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with dest.open("wb") as f:
                            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                        return
                    else:
                        last_err = f"{r.status_code} {r.text[:200]}"
            except Exception as e:
                last_err = str(e)
        raise RuntimeError(f"Failed to download snapshot via service endpoints: {last_err}")

    return retry_call(_try_download, attempts=RETRY_ATTEMPTS, on_except=(requests.RequestException,))


def azure_upload_file_with_retries(container_client: ContainerClient, blob_path: str, local_file: str, attempts: int = RETRY_ATTEMPTS) -> None:
    """
    Upload a file to Azure with retries. Overwrites if exists.
    """
    blob_client = container_client.get_blob_client(blob_path)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            with open(local_file, "rb") as data:
                blob_client.upload_blob(data, overwrite=True)
            return
        except AzureError as ae:
            last_exc = ae
            log("Azure upload transient error (attempt %d/%d): %s", attempt, attempts, str(ae))
            if attempt < attempts:
                _sleep_with_backoff(attempt)
                continue
            raise RuntimeError(f"Azure upload failed for {local_file}: {ae}") from ae
        except Exception as e:
            last_exc = e
            log("Azure upload error (attempt %d/%d): %s", attempt, attempts, str(e))
            if attempt < attempts:
                _sleep_with_backoff(attempt)
                continue
            raise
    raise RuntimeError(f"Azure upload failed for {local_file}: {last_exc}")


def run_service_backup(qdrant_url: str, azure_container: str, azure_prefix: str, local_dir: str | None, timeout: int, env_tag: str) -> tuple[str, str]:
    """
    Main orchestration:
     - enumerate collections
     - request snapshot for each collection
     - download snapshot files
     - upload snapshot files to Azure under <azure_prefix>/<backup_id>/
     - write manifest.json and latest.manifest.json
    Returns (backup_id, local_tmp_dir)
    """
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"{timestamp}-{str(uuid.uuid4())[:8]}"
    local_tmp = Path(local_dir or DEFAULT_LOCAL_DIR) / backup_id
    local_tmp.mkdir(parents=True, exist_ok=True)

    log("Starting service-mode backup: id=%s qdrant=%s", backup_id, qdrant_url)

    # build azure client and ensure container
    client = azure_client_from_env(allow_managed_identity=True)
    try:
        # quick service check
        client.get_service_properties()
    except Exception:
        log("Warning: unable to query blob service properties; will still attempt uploads (permissions may fail).")

    container_client = ensure_container(client, azure_container)

    # list collections
    collections = list_collections(qdrant_url, timeout=min(10, timeout))
    if not collections:
        raise RuntimeError("No collections found to backup from Qdrant")

    manifest: dict = {
        "backup_id": backup_id,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "env": env_tag,
        "qdrant_url": qdrant_url,
        "collections": {},
        "mode": "service",
    }

    for col in collections:
        log("[%s] requesting snapshot...", col)
        snap_name = request_snapshot_and_get_name(qdrant_url, col, wait=True, timeout=timeout)
        log("[%s] snapshot name: %s", col, snap_name)
        fname = f"{col}-{snap_name}.snapshot"
        local_path = local_tmp / fname
        log("[%s] downloading snapshot to: %s", col, local_path)
        download_snapshot(qdrant_url, col, snap_name, local_path, timeout=timeout)
        sha = sha256_of_file(local_path)
        size = local_path.stat().st_size
        blob_path = f"{azure_prefix.rstrip('/')}/{backup_id}/{local_path.name}"
        log("[%s] uploading to azure container=%s blob=%s", col, azure_container, blob_path)
        azure_upload_file_with_retries(container_client, blob_path, str(local_path))
        manifest["collections"][col] = {
            "snapshot_name": snap_name,
            "azure_container": azure_container,
            "blob_path": blob_path,
            "sha256": sha,
            "size": size,
            "local_path": str(local_path),
        }
        log("[%s] uploaded (size=%d sha256=%s)", col, size, sha)

    # write manifest files
    manifest_json = json.dumps(manifest, indent=2)
    manifest_local = local_tmp / "manifest.json"
    manifest_local.write_text(manifest_json)
    latest_local = local_tmp / "latest.manifest.json"
    latest_local.write_text(manifest_json)

    manifest_blob_path = f"{azure_prefix.rstrip('/')}/{backup_id}/manifest.json"
    latest_blob_path = f"{azure_prefix.rstrip('/')}/latest.manifest.json"

    log("Uploading manifest -> azure://%s/%s", azure_container, manifest_blob_path)
    azure_upload_file_with_retries(container_client, manifest_blob_path, str(manifest_local))
    log("Uploading latest manifest -> azure://%s/%s", azure_container, latest_blob_path)
    azure_upload_file_with_retries(container_client, latest_blob_path, str(latest_local))

    log("Backup manifest: %s", manifest_json)
    log("Backup finished. backup_id: %s local: %s", backup_id, str(local_tmp))
    return backup_id, str(local_tmp)


def parse_args():
    p = argparse.ArgumentParser(description="Qdrant service-mode backup (Azure).")
    p.add_argument("--azure-container", required=False, help="Azure container to store backups. Can also be set via BACKUP_AZ_CONTAINER env.")
    p.add_argument("--azure-prefix", default=DEFAULT_BACKUP_PREFIX, help="Azure prefix for backups (default qdrant/backups).")
    p.add_argument("--local-dir", default=DEFAULT_LOCAL_DIR, help="Local directory to store temporary backup files.")
    p.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL, help="Qdrant service URL.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Timeout seconds for HTTP/storage operations.")
    p.add_argument("--env", default=DEFAULT_ENV_TAG, help="ENV tag for manifest (STAGING/PROD).")
    return p.parse_args()


def main():
    args = parse_args()
    azure_container = args.azure_container or os.environ.get("BACKUP_AZ_CONTAINER")
    azure_prefix = args.azure_prefix or os.environ.get("BACKUP_PREFIX", DEFAULT_BACKUP_PREFIX)
    qdrant_url = args.qdrant_url or DEFAULT_QDRANT_URL
    local_dir = args.local_dir or DEFAULT_LOCAL_DIR
    timeout = int(args.timeout)
    env_tag = (args.env or DEFAULT_ENV_TAG).upper()

    if not azure_container:
        print("ERROR: BACKUP container not specified (use --azure-container or BACKUP_AZ_CONTAINER env).", file=sys.stderr)
        sys.exit(2)

    try:
        bid, localpath = run_service_backup(qdrant_url=qdrant_url, azure_container=azure_container, azure_prefix=azure_prefix, local_dir=local_dir, timeout=timeout, env_tag=env_tag)
        print("SUCCESS:", bid, localpath)
        sys.exit(0)
    except Exception as e:
        print("ERROR:", str(e), file=sys.stderr)
        sys.exit(3)

if __name__ == "__main__":
    main()
