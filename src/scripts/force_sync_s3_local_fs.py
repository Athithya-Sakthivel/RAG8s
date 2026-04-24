#!/usr/bin/env python3
"""
force_sync_s3_and_local_fs.py

Mirror local <-> Amazon S3.

Modes:
  --upload         mirror local -> S3 (delete remote orphans)
  --download       mirror S3 -> local (delete local orphans)
  --merge-upload   upload changed only, DO NOT delete remote orphans
  --merge-download download changed only, DO NOT delete local orphans

Deterministic behavior:
 - Sorted iteration for deterministic ordering
 - Pre-validate required envs (fail fast)
 - Dry-run supported

Notes:
 - S3 has no Azure Blob lease-equivalent built-in locking primitive.
 - Change detection uses sha256 metadata first, then size/ETag heuristics.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import boto3
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import ClientError
except Exception as e:
    print(
        json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "level": "ERROR",
                "event": "import_failure",
                "msg": "missing dependency 'boto3'. Install: pip install boto3",
                "exception": str(e),
            }
        )
    )
    raise SystemExit(2) from e

# ---- config ----
DEFAULT_PREFIX = os.environ.get("DEFAULT_PREFIX", "data")
LOCAL_BASE = os.environ.get("LOCAL_BASE", "data")
DEFAULT_CONCURRENCY = int(os.environ.get("CONCURRENT_FILES", "4"))
VERIFY_META_RETRIES = int(os.environ.get("VERIFY_META_RETRIES", "3"))
VERIFY_META_SLEEP = float(os.environ.get("VERIFY_META_SLEEP", "0.7"))

S3_BUCKET = (
    os.environ.get("DATA_S3_BUCKET")
    or os.environ.get("AWS_S3_BUCKET")
    or os.environ.get("AWS_BUCKET")
)
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")

TRANSFER_CONFIG = TransferConfig(use_threads=False)


# ---- helpers ----
def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def log(level: str, event: str, msg: str, **kwargs) -> None:
    o = {"ts": ts(), "level": level, "event": event, "msg": msg}
    if kwargs:
        o.update(kwargs)
    print(json.dumps(o, default=str), flush=True)


def info(event: str, msg: str, **k):
    log("INFO", event, msg, **k)


def warn(event: str, msg: str, **k):
    log("WARN", event, msg, **k)


def error(event: str, msg: str, **k):
    log("ERROR", event, msg, **k)


# ---- hashing and checksum helpers ----
def compute_hashes(path: str, chunk_size: int = 8 * 1024 * 1024) -> tuple[str, str]:
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def _hex_from_base64(b64: str) -> str | None:
    try:
        raw = base64.b64decode(b64)
        return raw.hex()
    except Exception:
        return None


def _normalize_etag(etag: str) -> str:
    if not etag:
        return ""
    e = etag.strip()
    if e.startswith("W/"):
        e = e[2:]
    e = e.strip('"').strip("'")
    if e.startswith("0x") or e.startswith("0X"):
        e = e[2:]
    return e.lower()


def _is_md5_hex(s: str) -> bool:
    s = _normalize_etag(s)
    return len(s) == 32 and all(c in "0123456789abcdef" for c in s)


def _safe_bucket_key_join(prefix: str, rel: str) -> str:
    reln = safe_rel_normalize(rel)
    prefix = (prefix or "").strip("/").rstrip("/")
    if prefix:
        return f"{prefix}/{reln}" if reln else prefix
    return reln


def safe_join_local(base_dir: str, rel: str) -> Path:
    base = Path(base_dir).resolve()
    candidate = (base / safe_rel_normalize(rel)).resolve()
    try:
        candidate.relative_to(base)
    except Exception as e:
        raise ValueError(f"remote path escapes base dir: {rel}") from e
    return candidate


# ---- auth / client ----
def validate_auth_preconditions():
    if not S3_BUCKET:
        error(
            "missing_env",
            "Missing bucket env: set S3_BUCKET (or AWS_S3_BUCKET / AWS_BUCKET)",
        )
        raise SystemExit(2)
    info(
        "auth",
        "Using boto3 credential chain",
        bucket=S3_BUCKET,
        region=AWS_REGION or "default",
        endpoint=AWS_ENDPOINT_URL or "",
    )


def get_s3_client():
    kwargs = {}
    if AWS_REGION:
        kwargs["region_name"] = AWS_REGION
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
    return boto3.client("s3", **kwargs)


# ---- adapter ----
class S3Fs:
    def __init__(self, client):
        self.client = client

    def _parse(self, full: str) -> tuple[str, str | None]:
        parts = full.split("/", 1)
        if len(parts) == 1:
            return parts[0], None
        return parts[0], parts[1]

    def find(self, root: str) -> list[str]:
        bucket, prefix = self._parse(root)
        prefix = (prefix or "").lstrip("/")
        out: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        kwargs = {"Bucket": bucket}
        if prefix:
            kwargs["Prefix"] = prefix
        try:
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    key = obj.get("Key", "")
                    if not key or key.endswith("/"):
                        continue
                    out.append(f"{bucket}/{key}")
        except Exception as e:
            warn("find_failed", "list_objects_v2 failed", root=root, exception=str(e))
        out.sort()
        return out

    def info(self, full: str) -> dict:
        bucket, key = self._parse(full)
        if not key:
            return {}
        try:
            resp = self.client.head_object(Bucket=bucket, Key=key)
            meta = resp.get("Metadata") or {}
            etag = resp.get("ETag") or ""
            size = int(resp.get("ContentLength", 0) or 0)
            content_type = resp.get("ContentType")
            checksum_sha256 = resp.get("ChecksumSHA256")
            info_obj = {
                "name": key,
                "path": full,
                "size": size,
                "etag": etag,
                "metadata": meta,
            }
            if content_type:
                info_obj["content_type"] = content_type
            if checksum_sha256:
                info_obj["checksum_sha256"] = checksum_sha256
            return info_obj
        except Exception as e:
            warn("info_failed", "head_object failed", object=full, exception=str(e))
            return {}

    def put(
        self,
        local_path: str,
        full_remote_path: str,
        metadata: dict[str, str] | None = None,
        content_type: str = "application/octet-stream",
    ):
        bucket, key = self._parse(full_remote_path)
        if not key:
            raise ValueError("remote path must include an object key")
        extra_args = {
            "Metadata": metadata or {},
            "ContentType": content_type,
        }
        with open(local_path, "rb") as data:
            self.client.upload_fileobj(
                data,
                bucket,
                key,
                ExtraArgs=extra_args,
                Config=TRANSFER_CONFIG,
            )

    def get(self, full_remote_path: str, local_target: str):
        bucket, key = self._parse(full_remote_path)
        if not key:
            raise ValueError("remote path must include an object key")
        resp = self.client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]
        target_path = Path(local_target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "wb") as f:
            while True:
                chunk = body.read(8 * 1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

    def rm(self, full_remote_path: str):
        bucket, key = self._parse(full_remote_path)
        if not key:
            warn("rm_skipped", "Refusing to delete bucket root via rm()", bucket=bucket)
            return
        try:
            self.client.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            warn("rm_failed", "delete_object failed", object=full_remote_path, exception=str(e))


# ---- remote helpers ----
def get_fs(_protocol: str | None = None):
    client = get_s3_client()
    fs = S3Fs(client)
    return fs, "s3-boto3"


def safe_rel_normalize(p: str) -> str:
    return p.replace("\\", "/").lstrip("/")


def join_remote(bucket: str, prefix: str, rel: str) -> str:
    reln = safe_rel_normalize(rel)
    key = _safe_bucket_key_join(prefix, reln)
    return f"{bucket}/{key}"


def list_remote_objects(
    fs: S3Fs, bucket: str, prefix: str
) -> list[tuple[str, str, int, dict]]:
    prefix_clean = prefix.strip("/").rstrip("/")
    root = f"{bucket}/{prefix_clean}" if prefix_clean else f"{bucket}"
    out: list[tuple[str, str, int, dict]] = []
    found = fs.find(root)
    for full in sorted(found):
        info_obj = fs.info(full)
        if not info_obj:
            continue

        lead = f"{bucket}/"
        rel = full[len(lead) :] if full.startswith(lead) else full
        if prefix_clean:
            if rel.startswith(prefix_clean + "/"):
                rel = rel[len(prefix_clean) + 1 :]
            elif rel == prefix_clean:
                rel = ""

        rel = safe_rel_normalize(rel)
        size = int(info_obj.get("size", 0) or 0)
        out.append((full, rel, size, info_obj))
    out.sort(key=lambda x: x[0])
    return out


def extract_remote_values(info_obj: dict) -> dict[str, str | None]:
    meta = (info_obj.get("metadata") or {}) if isinstance(info_obj, dict) else {}
    metadata_sha = None
    for k in ("sha256", "SHA256", "Sha256"):
        if meta.get(k):
            metadata_sha = meta.get(k)
            break

    checksum_sha256_b64 = info_obj.get("checksum_sha256") or info_obj.get("ChecksumSHA256")
    etag = info_obj.get("etag") or info_obj.get("ETag") or ""
    content_type = info_obj.get("content_type") or info_obj.get("ContentType") or ""
    return {
        "metadata_sha256": metadata_sha,
        "checksum_sha256": checksum_sha256_b64,
        "etag": etag,
        "content_type": content_type,
        "raw_info": info_obj,
    }


def upload_file_fs(
    fs: S3Fs,
    local_path: str,
    full_remote_path: str,
    sha256: str | None,
    content_type: str = "application/octet-stream",
    dry_run: bool = False,
    verify_retries: int = VERIFY_META_RETRIES,
):
    if dry_run:
        return {"rel_path": full_remote_path, "action": "dry_run"}

    metadata = {"sha256": sha256} if sha256 else {}
    fs.put(local_path, full_remote_path, metadata=metadata, content_type=content_type)

    for _attempt in range(1, verify_retries + 1):
        try:
            info_obj = fs.info(full_remote_path)
            meta = (info_obj.get("metadata") or {}) if isinstance(info_obj, dict) else {}
            remote_sha = meta.get("sha256") or meta.get("SHA256") or meta.get("Sha256")
            if sha256 and remote_sha and remote_sha.lower() == sha256.lower():
                return {"rel_path": full_remote_path, "action": "uploaded", "verified": True}
        except Exception:
            pass
        time.sleep(VERIFY_META_SLEEP)

    return {"rel_path": full_remote_path, "action": "uploaded", "verified": False}


def download_file_fs(
    fs: S3Fs,
    full_remote_path: str,
    local_target: str,
    dry_run: bool = False,
):
    if dry_run:
        return {"rel_path": full_remote_path, "action": "dry_run"}
    fs.get(full_remote_path, local_target)
    return {"rel_path": full_remote_path, "action": "downloaded"}


def delete_remote_file_fs(fs: S3Fs, full_remote_path: str, dry_run: bool = False):
    if dry_run:
        return full_remote_path
    fs.rm(full_remote_path)
    return full_remote_path


# ---- local helpers ----
def list_local_files(base_dir: str) -> list[tuple[str, str]]:
    base = Path(base_dir)
    if not base.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            try:
                rel = p.relative_to(base).as_posix()
            except Exception:
                rel = p.name
            out.append((str(p.resolve()), safe_rel_normalize(rel)))
    return out


def safe_remove_local(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
            os.remove(path)
            return True
        except Exception as e:
            warn("delete_local_perm_failed", "chmod+delete failed", path=path, error=str(e))
            return False
    except FileNotFoundError:
        return True
    except Exception as e:
        warn("delete_local_failed", "delete local failed", path=path, error=str(e))
        return False


# ---- skip logic ----
def should_skip_upload(
    local_path: str,
    remote_info: dict | None,
    verbose: bool = False,
) -> tuple[bool, str, str | None]:
    if not remote_info:
        return False, "remote_missing", None

    local_sha: str | None = None
    local_size: int | None = None
    try:
        local_size = Path(local_path).stat().st_size
    except Exception:
        pass

    remote_meta_sha = remote_info.get("metadata_sha256")
    remote_checksum_sha256 = remote_info.get("checksum_sha256")
    remote_etag = (remote_info.get("etag") or "") or ""
    remote_content_type = remote_info.get("content_type") or ""

    if remote_meta_sha:
        try:
            _, local_sha = compute_hashes(local_path)
            if local_sha.lower() == str(remote_meta_sha).lower():
                return True, "match_metadata_sha256", local_sha
            return False, "metadata_sha256_mismatch", local_sha
        except Exception as e:
            return False, f"local_hash_failed:{e}", None

    if remote_checksum_sha256:
        try:
            _, local_sha = compute_hashes(local_path)
            remote_sha = _hex_from_base64(str(remote_checksum_sha256))
            if remote_sha and local_sha.lower() == remote_sha.lower():
                return True, "match_checksum_sha256", local_sha
            return False, "checksum_sha256_mismatch", local_sha
        except Exception as e:
            return False, f"local_hash_failed:{e}", None

    if local_size is not None:
        remote_size = int(remote_info.get("size", 0) or 0)
        if local_size == remote_size and remote_etag:
            norm = _normalize_etag(remote_etag)
            if _is_md5_hex(norm):
                try:
                    local_md5, local_sha = compute_hashes(local_path)
                    if local_md5 == norm:
                        return True, "match_etag_md5", local_sha
                    return False, "etag_mismatch", local_sha
                except Exception as e:
                    return False, f"local_hash_failed:{e}", None

    if verbose and remote_content_type:
        pass

    return False, "no_reliable_remote_checksum", local_sha


def should_skip_download(local_path: str, remote_info: dict) -> bool:
    try:
        if not Path(local_path).exists():
            return False
        local_size = Path(local_path).stat().st_size
    except Exception:
        return False

    remote_meta_sha = remote_info.get("metadata_sha256")
    remote_checksum_sha256 = remote_info.get("checksum_sha256")
    remote_etag = (remote_info.get("etag") or "") or ""

    if remote_meta_sha:
        try:
            _, local_sha = compute_hashes(local_path)
            return local_sha.lower() == str(remote_meta_sha).lower()
        except Exception:
            return False

    if remote_checksum_sha256:
        try:
            _, local_sha = compute_hashes(local_path)
            remote_sha = _hex_from_base64(str(remote_checksum_sha256))
            if remote_sha and local_sha.lower() == remote_sha.lower():
                return True
        except Exception:
            return False

    remote_size = int(remote_info.get("size", 0) or 0)
    if local_size == remote_size and remote_etag:
        norm = _normalize_etag(remote_etag)
        if _is_md5_hex(norm):
            try:
                local_md5, _ = compute_hashes(local_path)
                return local_md5 == norm
            except Exception:
                return False

    return False


# ---- core operations ----
def upload_directory(
    base_dir: str,
    bucket: str,
    prefix: str,
    concurrency: int,
    dry_run: bool = False,
    verbose: bool = False,
    delete_orphans: bool = True,
):
    info(
        "upload_start",
        "Upload mirror starting",
        local=base_dir,
        bucket=bucket,
        prefix=prefix,
        concurrency=concurrency,
        delete_orphans=delete_orphans,
    )
    fs, _proto = get_fs(None)

    local_entries = list_local_files(base_dir)
    local_rel_map = {rel: abs_path for abs_path, rel in local_entries}

    remote_entries = list_remote_objects(fs, bucket, prefix)
    remote_map: dict[str, dict] = {}
    for full, rel, size, info_obj in remote_entries:
        vals = extract_remote_values(info_obj)
        vals["full"] = full
        vals["size"] = size
        remote_map[safe_rel_normalize(rel)] = vals

    remote_rels = set(remote_map.keys())
    local_rels = set(local_rel_map.keys())
    stale_remote = sorted(remote_rels - local_rels)
    info(
        "delete_orphans",
        "Deleting remote orphans (if enabled)",
        orphan_count=len(stale_remote),
        delete_orphans=delete_orphans,
    )
    if delete_orphans and stale_remote:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = {
                ex.submit(delete_remote_file_fs, fs, remote_map[rel]["full"], dry_run): rel
                for rel in stale_remote
            }
            for fut in as_completed(futures):
                rel = futures[fut]
                try:
                    key = fut.result()
                    info("deleted_remote_orphan", "Deleted remote orphan", rel=rel, remote=key)
                except Exception as e:
                    warn("delete_orphan_failed", "Failed deleting remote orphan", rel=rel, error=str(e))

    # refresh remote map deterministically
    remote_entries = list_remote_objects(fs, bucket, prefix)
    remote_map = {}
    for full, rel, size, info_obj in remote_entries:
        vals = extract_remote_values(info_obj)
        vals["full"] = full
        vals["size"] = size
        remote_map[safe_rel_normalize(rel)] = vals

    successes = skipped = failed = 0
    errors: list[str] = []
    tasks = {}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for rel in sorted(local_rel_map.keys()):
            local_path = local_rel_map[rel]
            remote_info = remote_map.get(rel)

            try:
                skip, reason, cached_sha = should_skip_upload(local_path, remote_info, verbose=verbose)
            except Exception as e:
                warn("skip_check_failed", "Checksum decision failed; will upload", rel=rel, error=str(e))
                skip, reason, cached_sha = False, "skip_check_error", None

            if skip:
                skipped += 1
                info("skipped_upload", "Skipped upload (unchanged)", rel=rel, local=local_path, reason=reason)
                continue

            local_sha = cached_sha
            if local_sha is None:
                try:
                    _, local_sha = compute_hashes(local_path)
                except Exception as e:
                    warn(
                        "hash_failed",
                        "Failed computing hashes; will upload without sha metadata",
                        rel=rel,
                        error=str(e),
                    )
                    local_sha = None

            full_remote = join_remote(bucket, prefix, rel)
            tasks[ex.submit(upload_file_fs, fs, local_path, full_remote, local_sha, "application/octet-stream", dry_run)] = (
                rel,
                local_path,
                full_remote,
                local_sha,
            )

        for fut in as_completed(tasks):
            rel, local_path, full_remote, sha256 = tasks[fut]
            try:
                result = fut.result()
                action = result.get("action")
                verified = result.get("verified", False)
                if action == "dry_run":
                    info("upload_dryrun", "Dry-run would upload", rel=rel, remote=full_remote, sha256=sha256)
                else:
                    successes += 1
                    info(
                        "uploaded",
                        "Uploaded file",
                        rel=rel,
                        remote=full_remote,
                        verified=bool(verified),
                        sha256=sha256,
                    )
            except Exception as e:
                failed += 1
                errors.append(f"{rel}: {e}")
                warn("upload_failed", "Upload failed", rel=rel, error=str(e))

    info("upload_finished", "Upload finished", succeeded=successes, skipped=skipped, failed=failed)
    for e in errors[:20]:
        warn("upload_error", "Upload error detail", detail=e)


def download_directory(
    bucket: str,
    base_dir: str,
    prefix: str,
    concurrency: int,
    dry_run: bool = False,
    verbose: bool = False,
    delete_orphans: bool = True,
):
    info(
        "download_start",
        "Download mirror starting",
        bucket=bucket,
        local=base_dir,
        prefix=prefix,
        concurrency=concurrency,
        delete_orphans=delete_orphans,
    )
    fs, _proto = get_fs(None)

    remote_entries = list_remote_objects(fs, bucket, prefix)
    remote_map: dict[str, dict] = {}
    for full, rel, size, info_obj in remote_entries:
        vals = extract_remote_values(info_obj)
        vals["full"] = full
        vals["size"] = size
        remote_map[safe_rel_normalize(rel)] = vals

    local_entries = list_local_files(base_dir)
    local_rel_map = {rel: abs_path for abs_path, rel in local_entries}

    remote_rels = set(remote_map.keys())
    local_rels = set(local_rel_map.keys())
    stale_local = sorted(local_rels - remote_rels)
    info(
        "delete_local_orphans",
        "Deleting local orphans (if enabled)",
        orphan_count=len(stale_local),
        delete_orphans=delete_orphans,
    )
    if delete_orphans and stale_local:
        for rel in stale_local:
            path = local_rel_map[rel]
            try:
                if dry_run:
                    info("delete_local_dryrun", "Would delete local orphan", rel=rel, path=path)
                    continue
                ok = safe_remove_local(path)
                if ok:
                    info("deleted_local_orphan", "Deleted local orphan", rel=rel, path=path)
                else:
                    warn("delete_local_failed", "Failed to delete local orphan", rel=rel, path=path)
            except Exception as e:
                warn("delete_local_failed", "Failed to delete local orphan", rel=rel, error=str(e))

    # refresh local map deterministically
    local_entries = list_local_files(base_dir)
    local_rel_map = {rel: abs_path for abs_path, rel in local_entries}

    successes = skipped = failed = 0
    errors: list[str] = []
    tasks = {}

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for rel in sorted(remote_map.keys()):
            rinfo = remote_map[rel]
            full = rinfo["full"]

            try:
                local_path = str(safe_join_local(base_dir, rel))
            except Exception as e:
                warn("skip_invalid_remote_key", "Skipping remote key that escapes local base", rel=rel, error=str(e))
                skipped += 1
                continue

            try:
                if should_skip_download(local_path, rinfo):
                    skipped += 1
                    info("skipped_download", "Skipped download (unchanged)", rel=rel, local=local_path)
                    continue
            except Exception as e:
                warn(
                    "skip_download_failed",
                    "Checksum decision failed for download; will attempt download",
                    rel=rel,
                    error=str(e),
                )

            tasks[ex.submit(download_file_fs, fs, full, local_path, dry_run)] = rel

        for fut in as_completed(tasks):
            rel = tasks[fut]
            try:
                result = fut.result()
                if result.get("action") == "dry_run":
                    info("download_dryrun", "Dry-run would download", rel=rel)
                else:
                    successes += 1
                    info("downloaded", "Downloaded file", rel=rel)
            except Exception as e:
                failed += 1
                errors.append(f"{rel}: {e}")
                warn("download_failed", "Download failed", rel=rel, error=str(e))

    info("download_finished", "Download finished", succeeded=successes, skipped=skipped, failed=failed)
    for e in errors[:20]:
        warn("download_error", "Download error detail", detail=e)


# ---- CLI ----
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smart deterministic mirror sync local <-> Amazon S3.")
    gp = p.add_mutually_exclusive_group(required=True)
    gp.add_argument("--upload", action="store_true", help="Mirror local -> S3 (skip unchanged, delete remote orphans)")
    gp.add_argument("--download", action="store_true", help="Mirror S3 -> local (skip unchanged, delete local orphans)")
    gp.add_argument(
        "--merge-upload",
        action="store_true",
        help="Merge upload: upload changed files only, DO NOT delete remote orphans",
    )
    gp.add_argument(
        "--merge-download",
        action="store_true",
        help="Merge download: download changed files only, DO NOT delete local orphans",
    )
    p.add_argument("--max-concurrency", type=int, default=0, help="Override concurrency (0 = auto/env)")
    p.add_argument("--dry-run", action="store_true", help="Do not perform state-changing operations; print actions only")
    p.add_argument("--verbose", action="store_true", help="Emit additional debug logs")
    return p.parse_args()


def compute_concurrency(override: int = 0) -> int:
    if override and override > 0:
        return max(1, override)
    return max(1, DEFAULT_CONCURRENCY)


def main() -> None:
    args = parse_args()

    validate_auth_preconditions()
    concurrency = compute_concurrency(args.max_concurrency)
    prefix = os.environ.get("DEFAULT_PREFIX", DEFAULT_PREFIX).strip("/")
    dry_run = args.dry_run
    verbose = args.verbose

    try:
        fs, _proto = get_fs(None)
    except Exception as e:
        error("fs_init_failed", "Filesystem initialization failed", exception=str(e))
        raise SystemExit(3) from e

    try:
        fs.client.head_bucket(Bucket=S3_BUCKET)
        info("bucket_ok", "Bucket access probe OK", protocol=_proto, bucket=S3_BUCKET)
    except ClientError as e:
        warn("bucket_access", "Bucket probe failed", bucket=S3_BUCKET, error=str(e))
    except Exception as e:
        warn("bucket_access", "Bucket probe failed", bucket=S3_BUCKET, error=str(e))

    if args.upload:
        upload_directory(LOCAL_BASE, S3_BUCKET, prefix, concurrency, dry_run=dry_run, verbose=verbose, delete_orphans=True)
    elif args.download:
        download_directory(S3_BUCKET, LOCAL_BASE, prefix, concurrency, dry_run=dry_run, verbose=verbose, delete_orphans=True)
    elif args.merge_upload:
        upload_directory(
            LOCAL_BASE,
            S3_BUCKET,
            prefix,
            concurrency,
            dry_run=dry_run,
            verbose=verbose,
            delete_orphans=False,
        )
    elif args.merge_download:
        download_directory(
            S3_BUCKET,
            LOCAL_BASE,
            prefix,
            concurrency,
            dry_run=dry_run,
            verbose=verbose,
            delete_orphans=False,
        )
    else:
        error("cli_usage", "Please specify --upload/--download/--merge-upload/--merge-download")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
