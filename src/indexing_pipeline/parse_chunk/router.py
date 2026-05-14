#!/usr/bin/env python3
"""
Safe, resilient router for parse_chunk formats.

- Ensures import paths so both `indexing_pipeline.parse_chunk.formats.X` and
  `parse_chunk.formats.X` resolve when running from different cwd.
- Tries package import first, then falls back to loading the formats/*.py file
  directly from known locations.
- Keeps existing router behavior (S3 listing, hashing, dedupe, manifest write).
"""
from __future__ import annotations
import os
import sys
import time
import json
import uuid
import boto3
import hashlib
import importlib
import importlib.util
import mimetypes
import logging
import urllib.parse
from datetime import datetime
from botocore.exceptions import ClientError
from pathlib import Path
from typing import Optional

# ---------- ensure import paths so package imports resolve --------------------
def ensure_import_paths():
    """
    Make sure `parse_chunk` and `indexing_pipeline.parse_chunk` packages are
    importable regardless of where router is executed from.
    """
    here = Path(__file__).resolve()                  # .../apps/index/parse_chunk/router.py
    pkg_dir = here.parent                             # .../apps/index/parse_chunk
    app_dir = pkg_dir.parent                          # .../apps/index
    repo_root = app_dir.parent                        # .../workspace (optional)
    # Insert pkg_dir first so local package imports win, then app_dir, then repo_root
    candidates = [str(pkg_dir), str(app_dir), str(repo_root)]
    for p in reversed(candidates):
        if p and p not in sys.path:
            sys.path.insert(0, p)

ensure_import_paths()

# -------------------- logging setup -----------------------------------------
try:
    import colorama
    colorama.init()
except Exception:
    pass

RESET = "\033[0m"
COLORS = {
    logging.DEBUG: "\033[90m",
    logging.INFO: "\033[97m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;41m"
}

class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelno, RESET)
        message = super().format(record)
        return f"{color}{message}{RESET}"

logger = logging.getLogger("router")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler(sys.stdout)
fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(ColorFormatter(fmt._fmt))
logger.handlers[:] = [handler]

def env_or_fail(var, default=None, mandatory=True):
    val = os.environ.get(var, default)
    if mandatory and val is None:
        print(f"ERROR: Required env var '{var}' not set.", file=sys.stderr)
        sys.exit(1)
    return val

# -------------------- config/env --------------------------------------------
S3_BUCKET = env_or_fail("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
CHUNK_FORMAT = os.getenv("CHUNK_FORMAT", "json").lower()
FORCE_PROCESS = os.getenv("FORCE_PROCESS", "false").lower() == "true"

assert CHUNK_FORMAT in ("json", "jsonl"), f"Invalid CHUNK_FORMAT '{CHUNK_FORMAT}'"

s3 = boto3.client("s3")

# -------------------- helpers & core ---------------------------------------
def log(*args, level="INFO", **kwargs):
    msg = " ".join(str(a) for a in args)
    lvl = level.upper()
    if lvl == "WARN":
        lvl = "WARNING"
    levelno = getattr(logging, lvl, logging.INFO)
    logger.log(levelno, msg, **kwargs)

def _is_not_found_client_error(e: ClientError) -> bool:
    try:
        code = e.response.get("Error", {}).get("Code", "")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound") or status == 404:
            return True
    except Exception:
        pass
    return False

def retry(func, retries=3, delay=2, backoff=2):
    for attempt in range(retries):
        try:
            return func()
        except ClientError as e:
            if _is_not_found_client_error(e):
                raise
            if attempt == retries - 1:
                raise
            log(f"Retry {attempt + 1}/{retries} after error: {e}", level="WARN")
            time.sleep(delay)
            delay *= backoff
        except Exception as e:
            if attempt == retries - 1:
                raise
            log(f"Retry {attempt + 1}/{retries} after error: {e}", level="WARN")
            time.sleep(delay)
            delay *= backoff

def list_raw_files():
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX)
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            if key.lower().endswith(".manifest.json"):
                continue
            yield key

def file_sha256(s3_key):
    h = hashlib.sha256()
    obj = retry(lambda: s3.get_object(Bucket=S3_BUCKET, Key=s3_key))
    stream = obj["Body"]
    for chunk in iter(lambda: stream.read(8192), b""):
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()

def manifest_path(s3_key, file_hash=None):
    return f"{s3_key}.manifest.json"

def is_already_processed(file_hash):
    if FORCE_PROCESS:
        return False
    base_prefix = S3_CHUNKED_PREFIX.rstrip("/") + "/"
    search_prefix = f"{base_prefix}{file_hash}_"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=S3_BUCKET, Prefix=search_prefix, PaginationConfig={"MaxItems": 1})
        for page in pages:
            if page.get("Contents"):
                return True
    except ClientError as e:
        log(f"S3 list_objects_v2 error while checking {search_prefix}: {e}", level="WARN")
    for ext in ("json", "jsonl"):
        test_key = f"{base_prefix}{file_hash}_1.{ext}"
        try:
            s3.head_object(Bucket=S3_BUCKET, Key=test_key)
            return True
        except ClientError as e:
            if _is_not_found_client_error(e):
                continue
            raise
    return False

def save_manifest(s3_key, manifest):
    key = manifest_path(s3_key, manifest.get("file_hash"))
    try:
        retry(lambda: s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json"
        ))
        log(f"Saved manifest to s3://{S3_BUCKET}/{key}")
        return True
    except Exception as e:
        log(f"Failed to save manifest: {e}", level="ERROR")
        return False

# -------------------- format module lookup & loading ------------------------
def get_format_module(ext: str) -> Optional[str]:
    return {
        "pdf": "pdf",
        "pptx": "pptx",
        "html": "_html",
        "md": "md",
        "markdown": "md",
        "txt": "txt",
        "wav": "wav",
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
        "ndjson": "jsonl"
    }.get(ext.lower())

def detect_mime(key):
    mime, _ = mimetypes.guess_type(key)
    return mime or "application/octet-stream"

def detect_ext_from_key(s3_client, bucket, key):
    k = urllib.parse.unquote(key.split("?", 1)[0].split("#", 1)[0])
    base, ext = os.path.splitext(k)
    ext = ext.lstrip(".").lower()
    if ext in ("markdown", "mdown"):
        ext = "md"
    if ext:
        return ext
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        ctype = (head.get("ContentType") or "").lower()
        metadata = head.get("Metadata") or {}
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
        if ctype.startswith("text/"):
            return "txt"
    except Exception:
        pass
    return ""

def load_module_from_path(module_name: str, path: Path):
    """
    Load a module from a file path and return the module object.
    """
    loader_name = f"local_formats_{module_name}"
    spec = importlib.util.spec_from_file_location(loader_name, str(path))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise ImportError(f"Cannot load module {module_name} from {path}")

def _import_format_module(module_name: str):
    """
    Try package imports first, then fallback to loading the module file directly
    from known locations relative to this router/app directory.
    """
    tried = []
    for pkg in ("indexing_pipeline.parse_chunk.formats", "parse_chunk.formats"):
        fq = f"{pkg}.{module_name}"
        try:
            return importlib.import_module(fq)
        except Exception as e:
            tried.append(fq)
    # fallback: try file paths (apps/index relative locations)
    workdir = Path(__file__).resolve().parent.parent   # .../apps/index
    candidates = [
        workdir / "parse_chunk" / "formats" / f"{module_name}.py",
        workdir / "indexing_pipeline" / "parse_chunk" / "formats" / f"{module_name}.py",
        Path(__file__).resolve().parent / "formats" / f"{module_name}.py",
    ]
    for p in candidates:
        try:
            p = p.resolve()
        except Exception:
            continue
        if p.exists():
            try:
                return load_module_from_path(module_name, p)
            except Exception as e:
                tried.append(str(p))
    raise ImportError(f"Failed to import module for format '{module_name}', tried: {', '.join(tried)}")

# -------------------- main pipeline ----------------------------------------
def main():
    log("Router pipeline started")
    run_id = os.getenv("RUN_ID") or str(uuid.uuid4())
    parser_version = os.getenv("PARSER_VERSION", "2.42.1")
    keys = list(list_raw_files())
    log(f"Found {len(keys)} files")
    for key in keys:
        if key.lower().endswith(".manifest.json"):
            log(f"Skipping manifest file {key}")
            continue
        ext = detect_ext_from_key(s3, S3_BUCKET, key)
        module_name = get_format_module(ext)
        if not module_name:
            log(f"Skipping unsupported '{ext or 'unknown'}': {key}", level="WARN")
            continue
        try:
            mod = _import_format_module(module_name)
            if not hasattr(mod, "parse_file"):
                log(f"No parse_file() in {module_name}, skipping {key}", level="WARN")
                continue
        except Exception as e:
            log(f"Import error in module {module_name}: {e}", level="ERROR")
            continue
        try:
            file_hash = file_sha256(key)
        except Exception as e:
            log(f"Hash error for {key}: {e}", level="ERROR")
            continue
        if is_already_processed(file_hash):
            log(f"Already processed {file_hash}, skipping")
            continue
        sd = os.getenv("SOURCE_DATE_EPOCH")
        if sd:
            try:
                ts = datetime.utcfromtimestamp(int(sd)).isoformat() + "Z"
            except Exception:
                ts = datetime.utcnow().isoformat() + "Z"
        else:
            ts = datetime.utcnow().isoformat() + "Z"
        manifest = {
            "file_hash": file_hash,
            "s3_key": key,
            "pipeline_run_id": run_id,
            "mime_type": detect_mime(key),
            "timestamp": ts,
            "parser_version": parser_version
        }
        try:
            result = mod.parse_file(key, manifest)
            if not isinstance(result, dict) or "saved_chunks" not in result:
                raise ValueError("Invalid parse_file() return. Expected dict with 'saved_chunks'.")
        except Exception as e:
            log(f"Parse error for {key}: {e}", level="ERROR")
            continue
        log(f"Parsed and stored {result['saved_chunks']} chunks for {key}")
        save_manifest(key, manifest)

if __name__ == "__main__":
    main()
