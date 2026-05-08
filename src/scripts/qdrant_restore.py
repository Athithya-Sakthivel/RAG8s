#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except Exception as exc:  # pragma: no cover
    boto3 = None
    Config = None
    BotoCoreError = Exception
    ClientError = Exception
    _BOTO3_ERROR = exc
else:
    _BOTO3_ERROR = None


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def now_ts() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(event: str, msg: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"ts": now_ts(), "level": "info", "event": event, "msg": msg}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def warn(event: str, msg: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"ts": now_ts(), "level": "warning", "event": event, "msg": msg}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def err(event: str, msg: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"ts": now_ts(), "level": "error", "event": event, "msg": msg}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def require_cmd(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"{name} not found in PATH")


def s3_bucket() -> str:
    return (
        _env_str("BACKUP_S3_BUCKET", "")
        or _env_str("QDRANT_BACKUP_S3_BUCKET", "")
        or _env_str("BACKUP_BUCKET", "")
        or _env_str("DATA_S3_BUCKET", "")
    ).strip()


def s3_prefix() -> str:
    raw = (
        _env_str("BACKUP_S3_PREFIX", "")
        or _env_str("QDRANT_BACKUP_S3_PREFIX", "")
        or _env_str("BACKUP_PREFIX", "")
        or _env_str("QDRANT_BACKUP_PREFIX", "")
        or "data/backups/qdrant/"
    )
    return raw.strip().lstrip("/").rstrip("/")


def qdrant_url() -> str:
    return _env_str("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").rstrip("/")


def qdrant_api_key() -> str:
    return _env_str("QDRANT_API_KEY", "") or _env_str("QDRANT__SERVICE__API_KEY", "")


def aws_region() -> str:
    return _env_str("AWS_REGION", "") or _env_str("AWS_DEFAULT_REGION", "")


def aws_endpoint_url() -> str | None:
    return (_env_str("AWS_ENDPOINT_URL", "") or _env_str("AWS_S3_ENDPOINT_URL", "") or "").strip() or None


def retry_sleep(base: float, attempt: int, cap: float = 60.0) -> None:
    backoff = min(cap, base * (2 ** max(0, attempt - 1)))
    time.sleep(backoff * (0.5 + random.random() * 0.5))


def retry_call(func, attempts: int, retriable: tuple[type[BaseException], ...]) -> Any:
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retriable as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            warn("retry", "Transient error", attempt=attempt, attempts=attempts, error=str(exc))
            retry_sleep(_env_float("RESTORE_RETRY_BASE", 1.5), attempt, cap=_env_float("RESTORE_RETRY_CAP", 60.0))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry failed")


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def _s3_client():
    if boto3 is None:
        raise RuntimeError("boto3 and botocore are required for S3 restore")
    kwargs: dict[str, Any] = {"region_name": aws_region() or None}
    ep = aws_endpoint_url()
    if ep:
        kwargs["endpoint_url"] = ep
    if Config is not None:
        kwargs["config"] = Config(retries={"max_attempts": 3, "mode": "standard"})
    return boto3.client("s3", **kwargs)


def _s3_get_json(bucket: str, key: str) -> dict[str, Any]:
    s3 = _s3_client()

    def call() -> dict[str, Any]:
        obj = s3.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"Expected JSON object in s3://{bucket}/{key}")
        return data

    return retry_call(call, attempts=_env_int("RESTORE_RETRY_ATTEMPTS", 4), retriable=(ClientError, BotoCoreError, OSError, json.JSONDecodeError, RuntimeError))


def _s3_download_file(bucket: str, key: str, dest: Path) -> None:
    s3 = _s3_client()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink(missing_ok=True)

    def call() -> None:
        s3.download_file(bucket, key, str(tmp))
        tmp.replace(dest)

    try:
        retry_call(call, attempts=_env_int("RESTORE_RETRY_ATTEMPTS", 4), retriable=(ClientError, BotoCoreError, OSError))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _presigned_get_url(bucket: str, key: str, expires: int = 3600) -> str:
    s3 = _s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )


def _qdrant_headers() -> dict[str, str]:
    headers = {"accept": "application/json"}
    api_key = qdrant_api_key()
    if api_key:
        headers["api-key"] = api_key
    return headers


def _qdrant_recover_from_url(base_url: str, collection: str, location: str, timeout: int) -> tuple[bool, str]:
    endpoint = f"{base_url.rstrip('/')}/collections/{collection}/snapshots/recover"
    try:
        resp = requests.put(endpoint, json={"location": location}, headers=_qdrant_headers(), timeout=timeout)
        if 200 <= resp.status_code < 300:
            return True, f"recovered via URL: {endpoint}"
        return False, f"{resp.status_code} {resp.text}"
    except Exception as exc:
        return False, str(exc)


def _qdrant_upload_snapshot(base_url: str, collection: str, snapshot_path: Path, timeout: int) -> tuple[bool, str]:
    endpoint = f"{base_url.rstrip('/')}/collections/{collection}/snapshots/upload?priority=snapshot"
    try:
        with snapshot_path.open("rb") as fh:
            files = {"snapshot": (snapshot_path.name, fh, "application/octet-stream")}
            resp = requests.post(endpoint, files=files, headers=_qdrant_headers(), timeout=timeout)
        if 200 <= resp.status_code < 300:
            return True, f"uploaded via file: {endpoint}"
        return False, f"{resp.status_code} {resp.text}"
    except Exception as exc:
        return False, str(exc)


@contextmanager
def _port_forward(namespace: str, local_port: int, remote_port: int = 6333):
    require_cmd("kubectl")
    pod = _discover_qdrant_pod(namespace)
    if not pod:
        raise RuntimeError(f"No qdrant pod found in namespace {namespace}")
    cmd = ["kubectl", "port-forward", f"pod/{pod}", f"{local_port}:{remote_port}", "-n", namespace]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for_http(f"http://127.0.0.1:{local_port}/collections", timeout=_env_int("PORT_FORWARD_TIMEOUT", 20))
        yield f"http://127.0.0.1:{local_port}", proc
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _wait_for_http(url: str, timeout: int = 20) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=2)
            if resp.status_code < 500:
                return
        except Exception as exc:
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


def _discover_qdrant_pod(namespace: str) -> str | None:
    selectors = [
        "app.kubernetes.io/name=qdrant",
        "app=qdrant",
        "app.kubernetes.io/instance=qdrant",
    ]
    for sel in selectors:
        cmd = ["kubectl", "get", "pods", "-n", namespace, "-l", sel, "-o", "jsonpath={.items[*].metadata.name}"]
        rc, out, _ = _run_cmd(cmd, timeout=15)
        if rc == 0 and out.strip():
            return out.strip().split()[0]
    rc, out, _ = _run_cmd(["kubectl", "get", "pods", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"], timeout=15)
    if rc == 0 and out.strip():
        for pod in out.strip().split():
            if pod.startswith("qdrant-"):
                return pod
    return None


def _run_cmd(cmd: list[str], timeout: int | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _normalize_prefix(prefix: str) -> str:
    return prefix.strip().lstrip("/").rstrip("/")


def _parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise RuntimeError(f"Invalid S3 URI: {s3_uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _manifest_candidates(prefix: str, backup_id: str | None) -> list[tuple[str, str]]:
    clean = _normalize_prefix(prefix)
    if backup_id:
        return [(s3_bucket(), f"{clean}/{backup_id}/manifest.json")]
    return [
        (s3_bucket(), f"{clean}/latest.manifest.json"),
        (s3_bucket(), f"{clean}/manifest.json"),
    ]


def _load_latest_manifest(bucket: str, prefix: str, backup_id: str | None) -> tuple[str, dict[str, Any]]:
    prefix = _normalize_prefix(prefix)
    s3 = _s3_client()
    if backup_id:
        key = f"{prefix}/{backup_id}/manifest.json"
        return backup_id, _s3_get_json(bucket, key)

    latest_key = f"{prefix}/latest.manifest.json"
    try:
        latest = _s3_get_json(bucket, latest_key)
        if "backup_id" not in latest:
            raise RuntimeError("latest.manifest.json missing backup_id")
        resolved = str(latest["backup_id"])
        manifest_key = f"{prefix}/{resolved}/manifest.json"
        return resolved, _s3_get_json(bucket, manifest_key)
    except Exception:
        prefix_marker = f"{prefix}/"
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix_marker)
        keys = []
        for obj in resp.get("Contents", []) or []:
            k = obj.get("Key")
            if k and k.endswith("/manifest.json") and k != latest_key:
                keys.append(k)
        if not keys:
            raise RuntimeError(f"No manifest found under s3://{bucket}/{prefix}") from None
        keys.sort()
        manifest_key = keys[-1]
        manifest = _s3_get_json(bucket, manifest_key)
        resolved = str(manifest.get("backup_id") or Path(manifest_key).parent.name)
        return resolved, manifest


def _collection_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = manifest.get("collections") or manifest.get("pods") or {}
    if isinstance(entries, list):
        out: dict[str, dict[str, Any]] = {}
        for item in entries:
            if isinstance(item, dict):
                name = str(item.get("collection") or item.get("name") or item.get("id") or "")
                if name:
                    out[name] = item
        return out
    if isinstance(entries, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in entries.items()}
    return {}


def _resolve_snapshot_location(bucket: str, prefix: str, backup_id: str, collection: str, entry: dict[str, Any]) -> tuple[str, str, str]:
    if entry.get("s3_uri"):
        b, k = _parse_s3_uri(str(entry["s3_uri"]))
        return b, k, f"s3://{b}/{k}"
    if entry.get("s3_bucket") and entry.get("s3_key"):
        b = str(entry["s3_bucket"])
        k = str(entry["s3_key"])
        return b, k, f"s3://{b}/{k}"
    if entry.get("bucket") and entry.get("key"):
        b = str(entry["bucket"])
        k = str(entry["key"])
        return b, k, f"s3://{b}/{k}"
    if entry.get("snapshot_name"):
        snap = str(entry["snapshot_name"])
        k = f"{_normalize_prefix(prefix)}/{backup_id}/{collection}/{snap}"
        return bucket, k, f"s3://{bucket}/{k}"
    if entry.get("s3_key"):
        k = str(entry["s3_key"])
        if k.startswith("s3://"):
            return _parse_s3_uri(k)
        if "/" in k and k.split("/")[0] != bucket:
            parts = k.split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                if parts[0] == bucket:
                    return bucket, parts[1], f"s3://{bucket}/{parts[1]}"
        return bucket, k.lstrip("/"), f"s3://{bucket}/{k.lstrip('/')}"
    raise RuntimeError(f"Manifest entry for collection {collection} does not contain a usable snapshot reference")


def _restore_collection(base_url: str, bucket: str, prefix: str, backup_id: str, collection: str, entry: dict[str, Any], timeout: int) -> dict[str, Any]:
    s3_bucket_name, s3_key, s3_uri = _resolve_snapshot_location(bucket, prefix, backup_id, collection, entry)
    presigned = _presigned_get_url(s3_bucket_name, s3_key, expires=_env_int("PRESIGNED_URL_EXPIRES", 3600))
    ok, detail = _qdrant_recover_from_url(base_url, collection, presigned, timeout=timeout)
    if ok:
        return {"ok": True, "mode": "url", "detail": detail, "s3_uri": s3_uri}

    tmp_root = Path(tempfile.mkdtemp(prefix=f"qdrant-restore-{backup_id}-"))
    tmp_file = tmp_root / Path(s3_key).name
    try:
        _s3_download_file(s3_bucket_name, s3_key, tmp_file)
        ok2, detail2 = _qdrant_upload_snapshot(base_url, collection, tmp_file, timeout=timeout)
        if ok2:
            return {"ok": True, "mode": "upload", "detail": detail2, "s3_uri": s3_uri}
        return {"ok": False, "mode": "upload", "detail": detail2, "s3_uri": s3_uri}
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _resolve_base_url(per_pod: bool, namespace: str, desired_url: str, port_base: int) -> tuple[str, object | None]:
    if not per_pod:
        return desired_url.rstrip("/"), None
    require_cmd("kubectl")
    pod = _discover_qdrant_pod(namespace)
    if not pod:
        raise RuntimeError(f"No qdrant pod discovered in namespace {namespace}")
    proc_cm = _port_forward(namespace, port_base)
    ctx = proc_cm.__enter__()
    base_url = ctx[0]
    return base_url.rstrip("/"), (proc_cm, ctx[1])


def _close_base_url(handle: object | None) -> None:
    if not handle:
        return
    proc_cm, proc = handle  # type: ignore[misc]
    try:
        proc_cm.__exit__(None, None, None)
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def restore_backup() -> dict[str, Any]:
    bucket = s3_bucket()
    prefix = s3_prefix()
    backup_id_arg = _env_str("BACKUP_ID", "") or _env_str("RESTORE_BACKUP_ID", "")
    namespace = _env_str("QDRANT_NAMESPACE", "qdrant")
    timeout = _env_int("RESTORE_TIMEOUT", 1800)
    per_pod = _env_bool("PER_POD", False)
    port_base = _env_int("PORT_BASE", 7000)
    qdrant = qdrant_url()

    if not bucket:
        raise RuntimeError("BACKUP_S3_BUCKET (or alias) is required")
    if not aws_region():
        raise RuntimeError("AWS_REGION or AWS_DEFAULT_REGION is required")

    log("startup", "restore starting", bucket=bucket, prefix=prefix, qdrant_url=qdrant, namespace=namespace, per_pod=per_pod)

    resolved_backup_id, manifest = _load_latest_manifest(bucket, prefix, backup_id_arg or None)
    log("manifest.loaded", "manifest loaded", backup_id=resolved_backup_id, collections=len(_collection_entries(manifest)))

    base_url, handle = _resolve_base_url(per_pod, namespace, qdrant, port_base)
    try:
        entries = _collection_entries(manifest)
        if not entries:
            raise RuntimeError("manifest contains no collections")

        results: dict[str, Any] = {
            "backup_id": resolved_backup_id,
            "bucket": bucket,
            "prefix": prefix,
            "qdrant_url": base_url,
            "collections": {},
        }

        for collection, entry in entries.items():
            log("restore.collection.start", "restoring collection", collection=collection)
            result = _restore_collection(base_url, bucket, prefix, resolved_backup_id, collection, entry, timeout=timeout)
            results["collections"][collection] = result
            log("restore.collection.done", "collection restored", collection=collection, **result)

        print(json.dumps(results, indent=2, sort_keys=True), flush=True)
        return results
    finally:
        _close_base_url(handle)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restore Qdrant from AWS S3 backup manifest.")
    parser.add_argument("--backup-id", default=_env_str("BACKUP_ID", "") or _env_str("RESTORE_BACKUP_ID", ""), help="Backup ID to restore. Uses latest.manifest.json when omitted.")
    parser.add_argument("--namespace", default=_env_str("QDRANT_NAMESPACE", "qdrant"), help="Kubernetes namespace for qdrant.")
    parser.add_argument("--qdrant-url", default=qdrant_url(), help="Qdrant URL or service address.")
    parser.add_argument("--bucket", default=s3_bucket(), help="Backup S3 bucket.")
    parser.add_argument("--prefix", default=s3_prefix(), help="Backup S3 prefix.")
    parser.add_argument("--per-pod", action="store_true", default=_env_bool("PER_POD", False), help="Restore through a port-forward to a discovered pod.")
    parser.add_argument("--port-base", type=int, default=_env_int("PORT_BASE", 7000), help="Base local port for port-forward mode.")
    parser.add_argument("--timeout", type=int, default=_env_int("RESTORE_TIMEOUT", 1800), help="Timeout for Qdrant restore operations.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.environ["BACKUP_ID"] = args.backup_id or ""
    os.environ["QDRANT_NAMESPACE"] = args.namespace
    os.environ["QDRANT_URL"] = args.qdrant_url
    os.environ["BACKUP_S3_BUCKET"] = args.bucket
    os.environ["QDRANT_BACKUP_S3_BUCKET"] = args.bucket
    os.environ["BACKUP_S3_PREFIX"] = args.prefix
    os.environ["QDRANT_BACKUP_S3_PREFIX"] = args.prefix
    os.environ["PER_POD"] = "true" if args.per_pod else "false"
    os.environ["PORT_BASE"] = str(args.port_base)
    os.environ["RESTORE_TIMEOUT"] = str(args.timeout)

    try:
        res = restore_backup()
        log("restore.ok", "restore finished", backup_id=res["backup_id"], collections=len(res["collections"]))
        sys.exit(0)
    except Exception as exc:
        err("restore.failed", "restore failed", error=str(exc))
        sys.exit(3)


if __name__ == "__main__":
    main()
