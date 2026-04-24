"""
Goals:
 - Deterministic startup and strict config validation.
 - Try to import user format modules; if import fails, record full traceback and
   attach a fallback parser so files are not skipped. Every file gets a manifest:
   either successful parse (saved_chunks > 0) or an error manifest (saved_chunks=0).
 - Log full tracebacks for import failures and parse exceptions.
 - Be defensive when reading numeric envs and when calling external libs.
"""
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
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().lower()

MODULE_CACHE: dict[str, Any] = {}
_S3_CLIENT = None


def now_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    payload: dict[str, Any] = {"ts": now_ts(), "level": level.lower(), "event": event}
    if msg:
        payload["msg"] = msg
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


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
        log("error", "startup_missing_env", "Missing required env vars", missing=missing)
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


def list_raw_files() -> list[str]:
    found: list[str] = []
    try:
        keys = _list_keys(RAW_PREFIX)
    except Exception as e:
        log("error", "list_failed", "failed to list raw prefix", error=str(e), prefix=RAW_PREFIX)
        return found

    for key in keys:
        if not key or key.endswith("/") or key.lower().endswith(".manifest.json"):
            continue
        ext = detect_ext_from_key(key)
        if ext in {"pdf", "html", "htm", "md", "markdown", "mdown"}:
            found.append(key)
        else:
            found.append(key)
    return found


def detect_mime(key: str) -> str:
    mime, _ = mimetypes.guess_type(key)
    return mime or "application/octet-stream"


def detect_ext_from_key(key: str) -> str:
    k = urllib.parse.unquote(key.split("?", 1)[0].split("#", 1)[0])
    _base, ext = os.path.splitext(k)
    ext = ext.lstrip(".").lower()
    if ext in ("markdown", "mdown"):
        return "md"
    if ext in ("htm",):
        return "html"
    return ext


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
    manifest_key = f"{s3_key}.manifest.json"
    return _read_json_object(manifest_key)


def write_error_manifest_if_missing(
    s3_key: str,
    run_id: str,
    parser_version: str,
    error: str,
    extra: dict[str, Any] | None = None,
) -> None:
    manifest_key = f"{s3_key}.manifest.json"
    if _object_exists(manifest_key):
        return
    payload: dict[str, Any] = {
        "file_hash": None,
        "s3_key": s3_key,
        "pipeline_run_id": run_id,
        "mime_type": detect_mime(s3_key),
        "timestamp": now_ts(),
        "parser_version": parser_version,
        "saved_chunks": 0,
        "status": "error",
        "error": error,
    }
    if extra:
        payload.update(extra)
    try:
        _write_json_object(manifest_key, payload)
        log("info", "saved_manifest", "error manifest written", key=manifest_key)
    except Exception as e:
        log("warning", "manifest_write_failed", "failed to write error manifest", key=manifest_key, error=str(e))


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
        raise ImportError(f"Cannot load module {module_name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def make_fallback_parser(module_name: str, ext: str, reason: str = ""):
    mod = SimpleNamespace()
    mod.__name__ = f"fallback_{module_name}"

    def parse_file(key: str, manifest: dict) -> dict:
        manifest.setdefault(
            "error",
            f"fallback parser used for module={module_name} ext={ext} reason={reason or 'unknown'}",
        )
        return {"saved_chunks": 0}

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
        error_text = f"hash_failed: {e}"
        manifest = {
            "file_hash": None,
            "s3_key": key,
            "pipeline_run_id": run_id,
            "mime_type": detect_mime(key),
            "timestamp": now_ts(),
            "parser_version": parser_version,
            "saved_chunks": 0,
            "status": "error",
            "error": error_text,
        }
        write_error_manifest_if_missing(key, run_id, parser_version, error_text, extra={"traceback": traceback.format_exc()})
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
        }

    manifest = parse_manifest_for_key(key)
    if not isinstance(manifest, dict):
        manifest = {}

    manifest.update(
        {
            "file_hash": file_hash,
            "s3_key": key,
            "pipeline_run_id": run_id,
            "mime_type": detect_mime(key),
            "timestamp": now_ts(),
            "parser_version": parser_version,
            "saved_chunks": 0,
            "status": "pending",
        }
    )

    try:
        result = mod.parse_file(key, manifest)
        if not isinstance(result, dict) or "saved_chunks" not in result:
            raise ValueError("Invalid parse_file() return. Expected dict with 'saved_chunks'.")
    except Exception as e:
        tb = traceback.format_exc()
        manifest["saved_chunks"] = 0
        manifest["status"] = "error"
        manifest["error"] = str(e)
        manifest["traceback"] = tb
        write_error_manifest_if_missing(key, run_id, parser_version, str(e), extra={"traceback": tb})
        log(
            "error",
            "parse_failed",
            "parse_file raised",
            key=key,
            module=getattr(mod, "__name__", str(mod)),
            error=str(e),
        )
        return manifest

    saved_chunks = _safe_int(result.get("saved_chunks", 0), 0)
    manifest["saved_chunks"] = saved_chunks
    manifest["status"] = "ok" if saved_chunks > 0 else "error"
    if saved_chunks <= 0:
        manifest.setdefault("error", _safe_str(result.get("error"), "no chunks saved"))
        write_error_manifest_if_missing(key, run_id, parser_version, manifest["error"], extra={"result": result})
        log("info", "parsed", "parsed with zero saved chunks", key=key, saved_chunks=saved_chunks)
    else:
        log("info", "parsed", "parsed_and_stored", key=key, saved_chunks=saved_chunks)

    try:
        manifest_key = f"{key}.manifest.json"
        _write_json_object(manifest_key, manifest)
        log("info", "saved_manifest", "manifest written", key=manifest_key)
    except Exception as e:
        log("warning", "manifest_save_failed", "failed to save manifest after parse", key=key, error=str(e))

    return manifest


def main() -> int:
    if not _require_runtime_envs():
        return 2

    run_id = os.getenv("RUN_ID") or str(uuid.uuid4())
    parser_version = os.getenv("PARSER_VERSION", "2.42.1")

    log("info", "startup", "router starting", bucket=DATA_S3_BUCKET, region=AWS_REGION, run_id=run_id)
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
            if key.lower().endswith(".manifest.json"):
                skipped += 1
                continue
            result = process_one_key(run_id, parser_version, key)
            processed += 1
            saved_total += _safe_int(result.get("saved_chunks", 0), 0)
            if result.get("status") == "error":
                failed += 1
            if result.get("skipped"):
                skipped += 1
        except Exception as e:
            failed += 1
            tb = traceback.format_exc()
            log("error", "loop_failure", "unexpected failure while processing key", key=key, error=str(e), traceback=tb)
            try:
                write_error_manifest_if_missing(
                    key,
                    run_id,
                    parser_version,
                    str(e),
                    extra={"traceback": tb, "pipeline_run_id": run_id},
                )
            except Exception:
                pass
            continue

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
