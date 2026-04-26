from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import mimetypes
import os
import time
import traceback
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import boto3  # type: ignore
except Exception:
    boto3 = None  # type: ignore[assignment]

try:
    from botocore.config import Config as BotocoreConfig  # type: ignore
except Exception:
    BotocoreConfig = None  # type: ignore[assignment]

try:
    from botocore.exceptions import ClientError  # type: ignore
except Exception:
    ClientError = Exception  # type: ignore[assignment]


DATA_S3_BUCKET = (
    os.getenv("DATA_S3_BUCKET")
    or os.getenv("STORAGE_BUCKET")
    or os.getenv("S3_BUCKET")
    or ""
).strip()

AWS_REGION = (
    os.getenv("AWS_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or ""
).strip()

AWS_ENDPOINT_URL = (
    os.getenv("AWS_S3_ENDPOINT_URL")
    or os.getenv("S3_ENDPOINT_URL")
    or ""
).strip() or None

RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or os.getenv("S3_RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or os.getenv("S3_CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"

STRICT_BUCKET_VALIDATE = os.getenv("STRICT_BUCKET_VALIDATE", "false").strip().lower() == "true"
PUT_RETRIES = int(os.getenv("PUT_RETRIES", "3") or 3)
PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3") or 0.3)

MODULE_CACHE: dict[str, Any] = {}
_S3_CLIENT = None


def now_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fmt(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (dict, list, tuple, set)):
        try:
            return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(v)
    s = str(v)
    if not s:
        return '""'
    if any(ch.isspace() for ch in s) or "=" in s or "|" in s:
        return json.dumps(s, ensure_ascii=False)
    return s


def log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    parts = [now_ts(), level.upper(), event]
    if msg:
        parts.append(msg)
    if extra:
        parts.extend(f"{k}={_fmt(v)}" for k, v in sorted(extra.items()))
    print(" | ".join(parts), flush=True)


def _safe_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        s = str(v)
    except Exception:
        return default
    return s if s else default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        return int(v)
    except Exception:
        try:
            return int(float(str(v).strip()))
        except Exception:
            return default


def _safe_json(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return json.dumps(_safe_str(v, ""), ensure_ascii=False)


def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def retry(func, retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    last = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except Exception as e:
            last = e
            if attempt >= retries:
                raise
            log("warning", "retry", "transient failure", attempt=attempt, error=str(e))
            time.sleep(delay)
            delay *= backoff
    if last is not None:
        raise last
    raise RuntimeError("retry failed")


def _require_runtime_envs() -> bool:
    missing = []
    if not DATA_S3_BUCKET:
        missing.append("DATA_S3_BUCKET")
    if not AWS_REGION and not AWS_ENDPOINT_URL:
        missing.append("AWS_REGION")
    if missing:
        log("error", "startup_missing_env", "missing required env vars", missing=missing)
        return False
    return True


def _build_s3_client():
    global _S3_CLIENT
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    if boto3 is None:
        raise RuntimeError("boto3 is required")
    session = boto3.session.Session(region_name=AWS_REGION or None)
    kwargs: dict[str, Any] = {}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
    if BotocoreConfig is not None:
        kwargs["config"] = BotocoreConfig(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
        )
    _S3_CLIENT = session.client("s3", **kwargs)
    if STRICT_BUCKET_VALIDATE:
        _S3_CLIENT.head_bucket(Bucket=DATA_S3_BUCKET)
    return _S3_CLIENT


def _get_s3_client():
    return _build_s3_client()


def _storage_key_to_url(key: str) -> str:
    return f"s3://{DATA_S3_BUCKET.rstrip('/')}/{key.lstrip('/')}"


def _strip_root(full: str) -> str:
    root = f"s3://{DATA_S3_BUCKET.rstrip('/')}/"
    if full.startswith(root):
        return full[len(root) :]
    if full.startswith("s3://"):
        rest = full[len("s3://") :]
        bucket_prefix = DATA_S3_BUCKET.rstrip("/") + "/"
        if rest.startswith(bucket_prefix):
            return rest[len(bucket_prefix) :]
        if rest == DATA_S3_BUCKET.rstrip("/"):
            return ""
    if full.startswith(DATA_S3_BUCKET.rstrip("/") + "/"):
        return full[len(DATA_S3_BUCKET.rstrip("/")) + 1 :]
    return full


def _object_exists(key: str) -> bool:
    client = _get_s3_client()
    try:
        client.head_object(Bucket=DATA_S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _head_object(key: str) -> dict[str, Any]:
    client = _get_s3_client()
    try:
        props = client.head_object(Bucket=DATA_S3_BUCKET, Key=key)
        meta = props.get("Metadata", {}) or {}
        content_type = props.get("ContentType", "") or ""
        last_modified = props.get("LastModified", "")
        if hasattr(last_modified, "isoformat"):
            last_modified = last_modified.isoformat()
        return {
            "size": int(props.get("ContentLength", 0) or 0),
            "etag": (props.get("ETag", "") or "").strip('"'),
            "last_modified": last_modified,
            "metadata": meta,
            "content_type": content_type,
        }
    except Exception:
        return {}


def _read_json_object(key: str) -> dict[str, Any]:
    client = _get_s3_client()
    try:
        obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=key)
        body = obj.get("Body")
        if body is None:
            return {}
        data = body.read()
        if not data:
            return {}
        parsed = json.loads(data.decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _write_json_object(key: str, payload: dict[str, Any]) -> None:
    client = _get_s3_client()
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")

    def _call():
        client.put_object(
            Bucket=DATA_S3_BUCKET,
            Key=key,
            Body=data,
            ContentType="application/json",
        )

    retry(_call, retries=PUT_RETRIES, delay=PUT_BACKOFF, backoff=2.0)


def _list_keys(prefix: str) -> list[str]:
    client = _get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=DATA_S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            k = obj.get("Key")
            if k:
                keys.append(k)
    return keys


def detect_mime(key: str) -> str:
    mime, _ = mimetypes.guess_type(key)
    if mime:
        return mime
    head = _head_object(key)
    ctype = _safe_str(head.get("content_type"), "")
    return ctype or "application/octet-stream"


def detect_ext_from_key(key: str) -> str:
    k = urllib.parse.unquote(key.split("?", 1)[0].split("#", 1)[0])
    _base, ext = os.path.splitext(k)
    ext = ext.lstrip(".").lower()
    if ext in ("markdown", "mdown"):
        return "md"
    if ext in ("htm",):
        return "html"
    if ext:
        return ext

    head = _head_object(key)
    meta = head.get("metadata") or {}
    meta_fn = _safe_str(meta.get("filename") or meta.get("originalname") or meta.get("name") or "")
    if meta_fn:
        _base, mext = os.path.splitext(meta_fn)
        mext = mext.lstrip(".").lower()
        if mext in ("markdown", "mdown"):
            return "md"
        if mext in ("htm",):
            return "html"
        if mext:
            return mext

    ctype = _safe_str(head.get("content_type"), "").lower()
    if "markdown" in ctype or "text/markdown" in ctype:
        return "md"
    if "text/html" in ctype:
        return "html"
    if "application/pdf" in ctype:
        return "pdf"
    return ""


def file_sha256(s3_key: str) -> str:
    client = _get_s3_client()
    h = hashlib.sha256()
    obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=s3_key)
    body = obj.get("Body")
    if body is None:
        return h.hexdigest()
    for chunk in iter(lambda: body.read(8192), b""):
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def parse_manifest_for_key(s3_key: str) -> dict[str, Any]:
    return _read_json_object(f"{s3_key}.manifest.json")


def write_manifest(
    s3_key: str,
    payload: dict[str, Any],
) -> None:
    _write_json_object(f"{s3_key}.manifest.json", payload)
    log("info", "saved_manifest", "manifest written", key=f"{s3_key}.manifest.json")


def make_manifest_base(
    s3_key: str,
    run_id: str,
    parser_version: str,
    file_hash: str | None,
) -> dict[str, Any]:
    return {
        "file_hash": file_hash,
        "s3_key": s3_key,
        "pipeline_run_id": run_id,
        "mime_type": detect_mime(s3_key),
        "timestamp": now_ts(),
        "parser_version": parser_version,
        "saved_chunks": 0,
        "status": "pending",
    }


def load_module_by_name(candidates: list[str]) -> Any:
    last_exc = None
    for name in candidates:
        try:
            return importlib.import_module(name)
        except Exception as e:
            last_exc = e
    if last_exc is not None:
        raise last_exc
    raise ImportError("no candidates provided")


def load_module_from_path(module_name: str, path: Path) -> Any:
    loader_name = f"local_formats_{module_name}"
    spec = importlib.util.spec_from_file_location(loader_name, str(path))
    if not spec or not spec.loader:
        raise ImportError(f"cannot load module {module_name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def make_fallback_parser(module_name: str, ext: str, reason: str = ""):
    mod = SimpleNamespace()
    mod.__name__ = f"fallback_{module_name}"

    def parse_file(key: str, manifest: dict) -> dict:
        manifest.setdefault(
            "status",
            "skipped",
        )
        manifest.setdefault(
            "reason",
            f"fallback parser used for module={module_name} ext={ext} reason={reason or 'unknown'}",
        )
        return {
            "status": "skipped",
            "saved_chunks": 0,
            "reason": manifest.get("reason"),
        }

    mod.parse_file = parse_file
    return mod


def get_format_module(ext: str) -> str | None:
    mapping = {
        "pdf": "pdf",
        "html": "_html",
        "htm": "_html",
        "md": "md",
        "markdown": "md",
        "mdown": "md",
    }
    return mapping.get(ext.lower())


def _import_format_module(module_name: str, ext_hint: str):
    if module_name in MODULE_CACHE:
        return MODULE_CACHE[module_name]

    tried: list[str] = []
    pkg_candidates = [
        f"parse_chunk.formats.{module_name}",
        f"indexing_pipeline.parse_chunk.formats.{module_name}",
    ]

    try:
        mod = load_module_by_name(pkg_candidates)
        MODULE_CACHE[module_name] = mod
        return mod
    except Exception as e_pkg:
        tried.extend(pkg_candidates)
        base = Path(__file__).resolve().parent / "formats"
        candidates = [
            base / f"{module_name}.py",
            Path(__file__).resolve().parent.parent / "parse_chunk" / "formats" / f"{module_name}.py",
            Path(__file__).resolve().parent.parent.parent / "parse_chunk" / "formats" / f"{module_name}.py",
        ]
        for p in candidates:
            try:
                if p.exists():
                    mod = load_module_from_path(module_name, p)
                    MODULE_CACHE[module_name] = mod
                    return mod
            except Exception:
                tried.append(str(p))
                log(
                    "error",
                    "import_failed_traceback",
                    "failed importing format module from path",
                    module=module_name,
                    path=str(p),
                    traceback=traceback.format_exc(),
                )

        reason = "".join(traceback.format_exception_only(type(e_pkg), e_pkg)).strip()
        log(
            "warning",
            "import_failed",
            "using fallback parser",
            module=module_name,
            ext=ext_hint,
            reason=reason,
            tried=";".join(tried),
        )
        mod = make_fallback_parser(module_name, ext_hint, reason=reason)
        MODULE_CACHE[module_name] = mod
        return mod


def list_raw_files() -> list[str]:
    try:
        keys = _list_keys(RAW_PREFIX)
    except Exception as e:
        log("error", "list_failed", "failed to list raw prefix", error=str(e), prefix=RAW_PREFIX)
        return []

    found: list[str] = []
    for key in keys:
        if not key or key.endswith("/") or key.lower().endswith(".manifest.json"):
            continue
        found.append(key)
    found.sort()
    return found


def is_already_processed(file_hash: str) -> bool:
    if os.getenv("FORCE_PROCESS", "false").strip().lower() == "true":
        return False
    search_prefix = f"{CHUNKED_PREFIX}{file_hash}_"
    try:
        keys = _list_keys(search_prefix)
    except Exception:
        keys = []
    if keys:
        return True
    for ext in ("json", "jsonl", "parquet"):
        test_key = f"{CHUNKED_PREFIX}{file_hash}_1.{ext}"
        if _object_exists(test_key):
            return True
    return False


def normalize_parse_result(result: dict[str, Any]) -> dict[str, Any]:
    status = _safe_str(result.get("status"), "").strip().lower()
    saved_chunks = _safe_int(result.get("saved_chunks", 0), 0)
    error = _safe_str(result.get("error"), "").strip()
    reason = _safe_str(result.get("reason"), "").strip()
    skipped = bool(result.get("skipped", False)) or status == "skipped" or bool(result.get("skip_reason"))

    if status in ("error", "failed", "failure"):
        normalized_status = "error"
    elif status in ("ok", "success", "parsed"):
        normalized_status = "ok" if saved_chunks > 0 else "skipped"
    elif skipped:
        normalized_status = "skipped"
    else:
        normalized_status = "ok" if saved_chunks > 0 else "skipped"

    if normalized_status == "error" and not error:
        error = reason or _safe_str(result.get("skip_reason"), "") or "parse failed"

    if normalized_status == "skipped" and not reason:
        reason = _safe_str(result.get("skip_reason"), "") or _safe_str(result.get("message"), "") or "no chunks saved"

    out = dict(result)
    out["status"] = normalized_status
    out["saved_chunks"] = saved_chunks
    if error:
        out["error"] = error
    if reason:
        out["reason"] = reason
    return out


def process_one_key(run_id: str, parser_version: str, key: str) -> dict[str, Any]:
    ext = detect_ext_from_key(key)
    module_name = get_format_module(ext)

    if not module_name:
        mod = make_fallback_parser("unknown", ext, reason="unsupported_ext")
        log("warning", "unsupported_ext", "unsupported file extension", key=key, ext=ext)
    else:
        mod = _import_format_module(module_name, ext)

    if not hasattr(mod, "parse_file"):
        mod = make_fallback_parser(getattr(mod, "__name__", "anon"), ext, reason="no_parse_file")
        log("warning", "skip_no_parse", "module has no parse_file", key=key, module=getattr(mod, "__name__", "unknown"))

    try:
        file_hash = file_sha256(key)
    except Exception as e:
        tb = traceback.format_exc()
        manifest = make_manifest_base(key, run_id, parser_version, None)
        manifest["status"] = "error"
        manifest["error"] = f"hash_failed: {e}"
        manifest["traceback"] = tb
        try:
            write_manifest(key, manifest)
        except Exception as e2:
            log("warning", "manifest_write_failed", "failed to write error manifest", key=key, error=str(e2))
        log("error", "hash_failed", "file hash failed", key=key, error=str(e))
        return manifest

    if is_already_processed(file_hash):
        log("info", "already_processed", "skipping already processed file", key=key, file_hash=file_hash)
        return {
            "file_hash": file_hash,
            "s3_key": key,
            "pipeline_run_id": run_id,
            "mime_type": detect_mime(key),
            "timestamp": now_ts(),
            "parser_version": parser_version,
            "saved_chunks": 0,
            "status": "skipped",
            "skipped": True,
            "reason": "already_processed",
        }

    manifest = parse_manifest_for_key(key)
    if not isinstance(manifest, dict):
        manifest = {}

    manifest.update(make_manifest_base(key, run_id, parser_version, file_hash))
    manifest["status"] = "pending"

    try:
        result = mod.parse_file(key, manifest)
        if not isinstance(result, dict) or "saved_chunks" not in result:
            raise ValueError("invalid parse_file() return; expected dict with 'saved_chunks'")
        result = normalize_parse_result(result)
    except Exception as e:
        tb = traceback.format_exc()
        manifest["saved_chunks"] = 0
        manifest["status"] = "error"
        manifest["error"] = str(e)
        manifest["traceback"] = tb
        try:
            write_manifest(key, manifest)
        except Exception as e2:
            log("warning", "manifest_write_failed", "failed to write error manifest", key=key, error=str(e2))
        log(
            "error",
            "parse_failed",
            "parse_file raised",
            key=key,
            module=getattr(mod, "__name__", str(mod)),
            error=str(e),
        )
        return manifest

    manifest["saved_chunks"] = _safe_int(result.get("saved_chunks", 0), 0)
    manifest["status"] = _safe_str(result.get("status"), "skipped").strip().lower() or "skipped"

    if "reason" in result and _safe_str(result.get("reason"), ""):
        manifest["reason"] = _safe_str(result.get("reason"), "")
    if "error" in result and _safe_str(result.get("error"), ""):
        manifest["error"] = _safe_str(result.get("error"), "")
    if "traceback" in result and _safe_str(result.get("traceback"), ""):
        manifest["traceback"] = _safe_str(result.get("traceback"), "")

    if manifest["status"] == "ok":
        log("info", "parsed", "parsed_and_stored", key=key, saved_chunks=manifest["saved_chunks"])
    elif manifest["status"] == "skipped":
        log(
            "info",
            "parsed",
            "parsed_with_zero_chunks",
            key=key,
            saved_chunks=manifest["saved_chunks"],
            reason=_safe_str(manifest.get("reason"), "no chunks saved"),
        )
    else:
        log("error", "parsed_error", "parser reported error", key=key, error=_safe_str(manifest.get("error"), "unknown"))

    try:
        write_manifest(key, manifest)
    except Exception as e:
        log("warning", "manifest_save_failed", "failed to save manifest after parse", key=key, error=str(e))

    return manifest


def main() -> int:
    if not _require_runtime_envs():
        return 2

    run_id = os.getenv("RUN_ID") or str(uuid.uuid4())
    parser_version = os.getenv("PARSER_VERSION", "2.42.1")

    log("info", "startup", "router starting", bucket=DATA_S3_BUCKET, region=AWS_REGION or "none", run_id=run_id)
    if AWS_ENDPOINT_URL:
        log("info", "startup", "using custom AWS endpoint", endpoint=AWS_ENDPOINT_URL)

    try:
        keys = list_raw_files()
    except Exception as e:
        log("error", "scan_failed", "failed to list raw files", error=str(e), traceback=traceback.format_exc())
        return 1

    log("info", "scan", "found_files", count=len(keys), prefix=RAW_PREFIX)

    processed = 0
    failed = 0
    skipped = 0
    saved_total = 0

    for key in keys:
        try:
            result = process_one_key(run_id, parser_version, key)
            processed += 1
            saved_total += _safe_int(result.get("saved_chunks", 0), 0)
            status = _safe_str(result.get("status"), "skipped").strip().lower()
            if status == "error":
                failed += 1
            elif status == "skipped":
                skipped += 1
        except Exception as e:
            failed += 1
            tb = traceback.format_exc()
            log("error", "loop_failure", "unexpected failure while processing key", key=key, error=str(e), traceback=tb)
            try:
                manifest = make_manifest_base(key, run_id, parser_version, None)
                manifest["status"] = "error"
                manifest["error"] = str(e)
                manifest["traceback"] = tb
                write_manifest(key, manifest)
            except Exception:
                pass

    log(
        "info",
        "router.done",
        "router finished",
        processed=processed,
        failed=failed,
        skipped=skipped,
        saved_chunks=saved_total,
        run_id=run_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
