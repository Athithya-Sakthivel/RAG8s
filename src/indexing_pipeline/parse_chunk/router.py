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
import io
import json
import logging
import mimetypes
import os
import sys
import time
import traceback
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------- logging ----------------
_root = logging.getLogger()
_root.setLevel(logging.WARNING)
for n in ("urllib3", "requests", "httpx", "boto3", "botocore"):
    lg = logging.getLogger(n)
    lg.setLevel(logging.WARNING)
    lg.propagate = False


def now_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(level: str, event: str, msg: str, **extra):
    o = {"ts": now_ts(), "level": level, "event": event, "msg": msg}
    if extra:
        o.update(extra)
    out = json.dumps(o, ensure_ascii=False)
    if level in ("error", "warn", "warning"):
        print(out, file=sys.stderr, flush=True)
    else:
        print(out, flush=True)


# ---------------- Config & validation ----------------
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

if not DATA_S3_BUCKET:
    log("error", "startup", "DATA_S3_BUCKET (or STORAGE_BUCKET or S3_BUCKET) must be set")
    sys.exit(1)

if not AWS_REGION:
    log("error", "startup", "AWS_REGION (or AWS_DEFAULT_REGION) must be set")
    sys.exit(1)

# prefixes
RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or os.getenv("S3_RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or os.getenv("S3_CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"

# ---------------- AWS client factory (deterministic) ----------------
def build_s3_client():
    try:
        import boto3  # type: ignore
        from botocore.config import Config  # type: ignore
    except Exception as e:
        log("error", "aws_import", "boto3 and botocore are required", error=str(e))
        raise SystemExit(2) from e

    try:
        cfg = Config(
            region_name=AWS_REGION,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        client = boto3.client("s3", region_name=AWS_REGION, config=cfg)
        # validate bucket access early
        try:
            client.head_bucket(Bucket=DATA_S3_BUCKET)
        except Exception as e_check:
            log(
                "error",
                "bucket_validation_failed",
                "S3 client created but bucket validation failed; verify IAM, region, and bucket name",
                error=str(e_check),
            )
            raise SystemExit(2) from e_check
        log("info", "client_init", "Initialized S3 client", bucket=DATA_S3_BUCKET, region=AWS_REGION)
        return client
    except SystemExit:
        raise
    except Exception as e:
        log("error", "client_failed", "Failed to initialize S3 client", error=str(e))
        raise SystemExit(2) from e


# ---------------- StorageBackend ----------------
class StorageBackend:
    def __init__(self, bucket: str):
        self.bucket = bucket
        self.storage_url = f"s3://{bucket.rstrip('/')}/"
        self.s3 = build_s3_client()

    def _strip_s3_prefix(self, full: str) -> str:
        full = full.strip()
        if full.startswith("s3://"):
            rest = full[len("s3://") :]
            if rest.startswith(self.bucket + "/"):
                return rest[len(self.bucket) + 1 :]
            if rest == self.bucket:
                return ""
            return rest
        if full.startswith(self.bucket + "/"):
            return full[len(self.bucket) + 1 :]
        return full

    def _normalize_key(self, key: str) -> str:
        return self._strip_s3_prefix(key).lstrip("/")

    def find(self, root_path: str) -> list[str]:
        prefix = self._strip_s3_prefix(root_path).lstrip("/")
        out: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    out.append(f"s3://{self.bucket}/{obj['Key']}")
        except Exception as e:
            log("warn", "list_objects_failed", "list_objects_v2 error", error=str(e))
        return out

    def glob(self, pattern: str) -> list[str]:
        # Minimal S3-native behavior: use the prefix portion before the first wildcard.
        cleaned = self._strip_s3_prefix(pattern)
        wildcard_pos = min([i for i in (cleaned.find("*"), cleaned.find("?"), cleaned.find("[")) if i != -1], default=-1)
        if wildcard_pos != -1:
            cleaned = cleaned[:wildcard_pos]
        return self.find(cleaned)

    def info(self, full_path: str) -> dict[str, Any]:
        key = self._normalize_key(full_path)
        try:
            props = self.s3.head_object(Bucket=self.bucket, Key=key)
            meta = props.get("Metadata", {}) or {}
            content_type = props.get("ContentType", "") or ""
            last_modified = props.get("LastModified", "")
            if hasattr(last_modified, "isoformat"):
                last_modified = last_modified.isoformat()
            return {
                "size": int(props.get("ContentLength", 0) or 0),
                "etag": (props.get("ETag", "") or "").strip('"'),
                "Content-Type": content_type,
                "content-type": content_type,
                "content_type": content_type,
                "last_modified": last_modified,
                "metadata": meta,
                "type": "file",
            }
        except Exception:
            raise

    def exists(self, full_path: str) -> bool:
        key = self._normalize_key(full_path)
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        return

    def open(self, full_path: str, mode: str = "rb"):
        key = self._normalize_key(full_path)
        if "r" in mode:
            try:
                obj = self.s3.get_object(Bucket=self.bucket, Key=key)
                body = obj["Body"].read()
                return io.BytesIO(body)
            except Exception:
                raise

        class _S3Writer(io.BytesIO):
            def __init__(self, client, bucket: str, key: str, content_type: str | None = None):
                super().__init__()
                self._client = client
                self._bucket = bucket
                self._key = key
                self._content_type = content_type

            def close(self):
                try:
                    self.seek(0)
                    data = self.read()
                    extra: dict[str, Any] = {}
                    if self._content_type:
                        extra["ContentType"] = self._content_type
                    self._client.put_object(Bucket=self._bucket, Key=self._key, Body=data, **extra)
                except Exception as e:
                    log("warn", "upload_failed", "S3 upload failed in writer.close", error=str(e), key=self._key)
                    raise
                finally:
                    super().close()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        return _S3Writer(self.s3, self.bucket, key)

    def rm(self, full_path: str) -> None:
        key = self._normalize_key(full_path)
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def delete(self, full_path: str) -> None:
        self.rm(full_path)


# instantiate
storage = StorageBackend(DATA_S3_BUCKET)
STORAGE_URL = f"s3://{DATA_S3_BUCKET.rstrip('/')}/"


# ---------------- helpers ----------------
def ts_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def full_path_from_key(key: str) -> str:
    return STORAGE_URL + key.lstrip("/")


def strip_root_from_path(full: str) -> str:
    if full.startswith(STORAGE_URL):
        return full[len(STORAGE_URL):]
    proto_prefix = "s3://"
    if full.startswith(proto_prefix):
        rest = full[len(proto_prefix):]
        if rest.startswith(DATA_S3_BUCKET + "/"):
            return rest[len(DATA_S3_BUCKET) + 1 :]
        if rest == DATA_S3_BUCKET:
            return ""
    if full.startswith(DATA_S3_BUCKET + "/"):
        return full[len(DATA_S3_BUCKET) + 1 :]
    return full


def retry(func, retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            if attempt == retries - 1:
                raise
            log("warn", "retry", f"attempt={attempt+1} error={e!s}")
            time.sleep(delay)
            delay *= backoff


def list_raw_files() -> list[str]:
    base = RAW_PREFIX
    root_path = STORAGE_URL + base
    out: list[str] = []
    try:
        found = storage.find(root_path)
    except Exception:
        try:
            found = storage.glob(root_path + "**")
        except Exception:
            found = []
    for full in found:
        try:
            info_obj = storage.info(full)
        except Exception:
            continue
        if info_obj.get("type") == "directory":
            continue
        rel = strip_root_from_path(full)
        if rel.endswith("/"):
            continue
        if rel.lower().endswith(".manifest.json"):
            continue
        out.append(rel)
    return out


def file_sha256(s3_key: str) -> str:
    full = full_path_from_key(s3_key)

    def _read():
        h = hashlib.sha256()
        with storage.open(full, "rb") as stream:
            for chunk in iter(lambda: stream.read(8192), b""):
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    return retry(_read)


def save_manifest(s3_key: str, manifest: dict) -> bool:
    key = f"{s3_key}.manifest.json"
    full = full_path_from_key(key)
    try:
        parent = str(Path(full).parent)
        try:
            storage.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        with storage.open(full, "wb") as f:
            payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            f.write(payload)
        log("info", "saved_manifest", "manifest_written", key=full)
        return True
    except Exception as e:
        log("error", "save_manifest_failed", str(e), key=full)
        return False


def detect_mime(key: str) -> str:
    mime, _ = mimetypes.guess_type(key)
    return mime or "application/octet-stream"


def detect_ext_from_key(key: str) -> str:
    k = urllib.parse.unquote(key.split("?", 1)[0].split("#", 1)[0])
    _base, ext = os.path.splitext(k)
    ext = ext.lstrip(".").lower()
    if ext in ("markdown", "mdown"):
        ext = "md"
    if ext:
        return ext
    try:
        full = full_path_from_key(key)
        head = storage.info(full)
        ctype = (head.get("Content-Type") or head.get("content-type") or head.get("content_type") or "").lower()
        metadata = head.get("metadata") or head.get("meta") or head.get("Metadata") or {}
        meta_fn = metadata.get("filename") or metadata.get("originalname") or ""
        if meta_fn:
            _, mext = os.path.splitext(meta_fn)
            mext = mext.lstrip(".").lower()
            if mext in ("markdown", "mdown"):
                return "md"
            if mext:
                return mext
        if "markdown" in ctype or "text/markdown" in ctype:
            return "md"
        if "text/html" in ctype:
            return "html"
        if ctype.startswith("text/"):
            return "txt"
        if "application/pdf" in ctype:
            return "pdf"
        if "presentation" in ctype or "powerpoint" in ctype or "officedocument.presentationml" in ctype:
            return "pptx"
        if "wordprocessingml" in ctype or "officedocument.wordprocessingml" in ctype:
            return "docx"
        if "officedocument.spreadsheetml" in ctype or "excel" in ctype:
            return "xlsx"
        if ctype.startswith("image/"):
            return "jpg"
        if ctype.startswith("audio/"):
            return "wav"
    except Exception:
        pass
    return ""


# mapping ext -> module name
def get_format_module(ext: str) -> str | None:
    mapping = {
        "pdf": "pdf",
        "pptx": "_pptx",
        "ppt": "_pptx",
        "html": "_html",
        "htm": "_html",
        "md": "md",
        "markdown": "md",
        "mdown": "md",
        "txt": "txt",
        "wav": "wav",
        "mp3": "wav",
        "jpg": "images",
        "jpeg": "images",
        "png": "images",
        "webp": "images",
        "tiff": "images",
        "tif": "images",
        "gif": "images",
        "bmp": "images",
        "csv": "_csv",
        "jsonl": "jsonl",
        "ndjson": "jsonl",
    }
    return mapping.get(ext.lower())


# ---------------- robust import machinery ----------------
MODULE_CACHE: dict[str, Any] = {}


def load_module_by_name(pkg_candidates: list[str]) -> Any:
    """
    Try to import by package-qualified name(s). Raise last exception on failure.
    """
    last_exc = None
    for name in pkg_candidates:
        try:
            return importlib.import_module(name)
        except Exception as e:
            last_exc = e
    raise last_exc


def load_module_from_path(module_name: str, path: Path):
    """Load module from a specific file path with full traceback on error"""
    loader_name = f"local_formats_{module_name}"
    spec = importlib.util.spec_from_file_location(loader_name, str(path))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore
            return mod
        except Exception:
            raise
    raise ImportError(f"Cannot load module {module_name} from {path}")


def make_fallback_parser(module_name: str, ext: str, reason: str = ""):
    """
    Create a fallback module object with parse_file(key, manifest) that:
      - Writes an error manifest (saved_chunks = 0) with import traceback
      - Returns {'saved_chunks': 0}
    This ensures the router never silently skips files.
    """
    import types

    mod = types.SimpleNamespace()
    mod._fallback_reason = reason

    def parse_file(key: str, manifest: dict) -> dict:
        err_msg = manifest.get("error", "")
        fallback_error = (
            f"Fallback parser used for module '{module_name}' (ext='{ext}'). "
            f"Original error: {err_msg or reason}"
        )
        manifest.setdefault("error", fallback_error)
        return {"saved_chunks": 0}

    mod.parse_file = parse_file
    return mod


def _import_format_module(module_name: str, ext_hint: str):
    """
    Robust loader: try package imports, then file-system loads; on any import failure
    produce a fallback parser instead of raising. Full tracebacks are logged.
    """
    if module_name in MODULE_CACHE:
        return MODULE_CACHE[module_name]
    tried = []
    pkg_roots = ("indexing_pipeline.parse_chunk.formats", "parse_chunk.formats")
    pkg_candidates = [f"{root}.{module_name}" for root in pkg_roots]
    try:
        try:
            m = load_module_by_name(pkg_candidates)
            MODULE_CACHE[module_name] = m
            return m
        except Exception as e_pkg:
            tried.extend(pkg_candidates)
            workdir = Path(__file__).resolve().parent.parent
            candidates = [
                workdir / "parse_chunk" / "formats" / f"{module_name}.py",
                workdir / "indexing_pipeline" / "parse_chunk" / "formats" / f"{module_name}.py",
                Path(__file__).resolve().parent / "formats" / f"{module_name}.py",
            ]
            for p in candidates:
                try:
                    p_res = p.resolve()
                except Exception:
                    continue
                if p_res.exists():
                    try:
                        m = load_module_from_path(module_name, p_res)
                        MODULE_CACHE[module_name] = m
                        return m
                    except Exception:
                        tb = traceback.format_exc()
                        tried.append(str(p_res))
                        log(
                            "error",
                            "import_failed_traceback",
                            f"Failed importing module file {p_res}",
                            module=module_name,
                            traceback=tb,
                        )
            tb_pkg = "".join(traceback.format_exception_only(type(e_pkg), e_pkg)).strip()
            log(
                "error",
                "import_failed",
                f"Cannot import module '{module_name}' (ext hint '{ext_hint}'). Will use fallback parser. Package error: {tb_pkg}",
                tried=";".join(tried),
            )
            fallback = make_fallback_parser(module_name, ext_hint, reason=tb_pkg)
            MODULE_CACHE[module_name] = fallback
            return fallback
    except Exception:
        tb = traceback.format_exc()
        log("error", "import_unexpected", f"Unexpected import error for module '{module_name}'; using fallback", module=module_name, traceback=tb)
        fallback = make_fallback_parser(module_name, ext_hint, reason=tb)
        MODULE_CACHE[module_name] = fallback
        return fallback


# ---------------- main processing ----------------
def is_already_processed(file_hash: str) -> bool:
    if os.getenv("FORCE_PROCESS", "false").lower() == "true":
        return False
    base_prefix = CHUNKED_PREFIX
    search_prefix = f"{base_prefix}{file_hash}_"
    glob_pattern = STORAGE_URL + search_prefix + "*"
    try:
        matches = storage.glob(glob_pattern)
    except Exception:
        matches = []
    if matches:
        return True
    for ext in ("json", "jsonl"):
        test_key = f"{base_prefix}{file_hash}_1.{ext}"
        full = full_path_from_key(test_key)
        try:
            if storage.exists(full):
                return True
        except Exception:
            pass
    return False


def main() -> None:
    run_id = os.getenv("RUN_ID") or str(uuid.uuid4())
    parser_version = os.getenv("PARSER_VERSION", "2.42.1")

    keys = list_raw_files()
    log("info", "scan", "found_files", count=len(keys))

    for key in keys:
        try:
            if key.lower().endswith(".manifest.json"):
                log("debug", "skip", "manifest", key=key)
                continue

            ext = detect_ext_from_key(key)
            module_name = get_format_module(ext)
            if not module_name:
                log("warn", "skip_unsupported", "unsupported_ext", key=key, ext=ext)
                fake_module = make_fallback_parser("unknown", ext, reason="unsupported_ext")
                mod = fake_module
            else:
                mod = _import_format_module(module_name, ext)

            if not hasattr(mod, "parse_file"):
                log("warn", "skip_no_parse", "no_parse_file", module=getattr(mod, "__name__", str(mod)), key=key)
                mod = make_fallback_parser(getattr(mod, "__name__", "anon"), ext, reason="no_parse_file")

            try:
                file_hash = file_sha256(key)
            except Exception as e:
                log("error", "hash_failed", f"file hash failed: {e!s}", key=key)
                manifest = {
                    "file_hash": None,
                    "s3_key": key,
                    "pipeline_run_id": run_id,
                    "mime_type": detect_mime(key),
                    "timestamp": now_ts(),
                    "parser_version": parser_version,
                    "saved_chunks": 0,
                    "status": "error",
                    "error": f"hash_failed: {e!s}",
                }
                try:
                    save_manifest(key, manifest)
                except Exception:
                    pass
                continue

            if is_already_processed(file_hash):
                log("info", "already_processed", "skipping", file_hash=file_hash, key=key)
                continue

            ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            manifest = {
                "file_hash": file_hash,
                "s3_key": key,
                "pipeline_run_id": run_id,
                "mime_type": detect_mime(key),
                "timestamp": ts,
                "parser_version": parser_version,
                "saved_chunks": 0,
                "status": "pending",
            }

            try:
                result = mod.parse_file(key, manifest)
                if not isinstance(result, dict) or "saved_chunks" not in result:
                    raise ValueError("Invalid parse_file() return. Expected dict with 'saved_chunks'.")
            except Exception as e:
                tb = traceback.format_exc()
                manifest["saved_chunks"] = 0
                manifest["status"] = "error"
                manifest.setdefault("error", str(e))
                manifest.setdefault("traceback", tb)
                try:
                    save_manifest(key, manifest)
                except Exception:
                    pass
                log(
                    "error",
                    "parse_failed",
                    f"parse_file raised: {e!s}",
                    key=key,
                    module=getattr(mod, "__name__", str(mod)),
                    traceback=tb,
                )
                continue

            count = int(result.get("saved_chunks", 0) or 0)
            manifest["saved_chunks"] = count
            manifest["status"] = "ok" if count > 0 else "error"
            log("info", "parsed", "parsed_and_stored", key=key, saved_chunks=count)
            try:
                save_manifest(key, manifest)
            except Exception as e:
                log("warn", "manifest_save_failed", "failed to save manifest after parse", key=key, error=str(e))
        except Exception as exc_outer:
            tb = traceback.format_exc()
            log("error", "loop_failure", str(exc_outer), key=key if "key" in locals() else None, traceback=tb)
            try:
                if "key" in locals():
                    save_manifest(
                        key,
                        {
                            "file_hash": None,
                            "s3_key": key,
                            "pipeline_run_id": run_id,
                            "saved_chunks": 0,
                            "status": "error",
                            "error": str(exc_outer),
                            "traceback": tb,
                        },
                    )
            except Exception:
                pass
            continue


if __name__ == "__main__":
    main()
