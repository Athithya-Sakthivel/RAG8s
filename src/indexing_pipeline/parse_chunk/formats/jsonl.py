#!/usr/bin/env python3
"""
jsonl format parser — import-safe and compatible with router.py.

This module exposes:
    def parse_file(s3_key: str, manifest: Dict[str, Any]) -> Dict[str, Any]
and does not perform heavy work or exit at import time.
"""
from __future__ import annotations
import os
import sys
import io
import json
import time
import logging
import hashlib
import tempfile
import unicodedata
import urllib.parse
from datetime import datetime
from typing import Any, Dict, Iterator, Tuple, List, Optional
import botocore

# optional/slow deps: imported lazily where needed
try:
    import polars as pl  # optional
except Exception:
    pl = None

try:
    import tiktoken  # optional
except Exception:
    tiktoken = None

try:
    import colorama
    colorama.init()
except Exception:
    pass

# --- logging ---
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

logger = logging.getLogger("jsonl_parser")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
logger.handlers[:] = [handler]
log = logger

# --- basic config (read from env but do not fail on import) ---
S3_BUCKET = os.getenv("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "").rstrip("/") + "/"
PARSER_VERSION = os.getenv("PARSER_VERSION_JSONL", "polars-jsonl-v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
TARGET_TOKENS_PER_CHUNK = int(os.getenv("JSONL_TARGET_TOKENS_PER_CHUNK", os.getenv("CSV_TARGET_TOKENS_PER_CHUNK", "1000")))
ROWS_PER_CHUNK_OVERRIDE = os.getenv("JSONL_ROWS_PER_CHUNK", os.getenv("CSV_ROWS_PER_CHUNK", ""))
MIN_ROWS_PER_CHUNK = int(os.getenv("JSONL_MIN_ROWS_PER_CHUNK", os.getenv("CSV_MIN_ROWS_PER_CHUNK", "1")))
MAX_ROWS_PER_CHUNK = int(os.getenv("JSONL_MAX_ROWS_PER_CHUNK", os.getenv("CSV_MAX_ROWS_PER_CHUNK", "100")))
S3_PUT_RETRIES = int(os.getenv("S3_PUT_RETRIES", "3"))
S3_PUT_BACKOFF = float(os.getenv("S3_PUT_BACKOFF", "0.5"))
S3_RANGE_BYTES = int(os.getenv("S3_RANGE_BYTES", "131072"))

# lazy boto3 client
_s3_client = None
def get_s3_client():
    global _s3_client
    if _s3_client is None:
        try:
            import boto3
        except Exception as e:
            raise RuntimeError("boto3 must be installed to use jsonl parser") from e
        _s3_client = boto3.client("s3")
    return _s3_client

# tiktoken encoder (optional)
ENCODER = None
if tiktoken is not None:
    try:
        ENCODER = tiktoken.get_encoding(ENC_NAME)
    except Exception:
        try:
            ENCODER = tiktoken.encoding_for_model("gpt2")
        except Exception:
            ENCODER = None

# helper utilities
def sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def canonicalize_text(s: Any) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(s.split()).strip()

def s3_object_exists(key: str) -> bool:
    try:
        s3 = get_s3_client()
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False
    except Exception:
        return False

def s3_put_object_with_retries(key: str, body: bytes, content_type: str = "application/json") -> None:
    s3 = get_s3_client()
    attempt = 0
    while True:
        try:
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=content_type)
            return
        except Exception:
            attempt += 1
            if attempt >= max(1, S3_PUT_RETRIES):
                raise
            time.sleep(S3_PUT_BACKOFF * attempt)

def token_count_for(text: str) -> int:
    if not text:
        return 0
    if ENCODER is not None:
        try:
            return len(ENCODER.encode(text))
        except Exception:
            pass
    return len(text.split())

def split_into_token_windows(text: str, window_tokens: int, overlap: int = 0) -> Iterator[Dict[str, Any]]:
    if not text:
        yield {"window_index": 0, "text": "", "token_count": 0, "token_start": 0, "token_end": 0}
        return
    if ENCODER is None:
        tokens = text.split()
        total = len(tokens)
        if window_tokens <= overlap:
            raise ValueError("window_tokens must be greater than overlap")
        step = window_tokens - overlap
        idx = 0
        for start in range(0, total, step):
            end = min(start + window_tokens, total)
            window_text = " ".join(tokens[start:end])
            yield {"window_index": idx, "text": canonicalize_text(window_text), "token_count": end - start, "token_start": start, "token_end": end}
            idx += 1
            if end >= total:
                break
        return
    tokens = ENCODER.encode(text)
    total = len(tokens)
    if window_tokens <= overlap:
        raise ValueError("window_tokens must be greater than overlap")
    step = window_tokens - overlap
    idx = 0
    for start in range(0, total, step):
        end = start + window_tokens
        slice_tokens = tokens[start:end]
        try:
            window_text = ENCODER.decode(slice_tokens)
        except Exception:
            window_text = " ".join(str(x) for x in slice_tokens)
        yield {"window_index": idx, "text": canonicalize_text(window_text), "token_count": len(slice_tokens), "token_start": start, "token_end": min(end, total)}
        idx += 1
        if end >= total:
            break

def row_to_schema_text(row: Any) -> str:
    parts: List[str] = []
    if pl is not None and hasattr(pl, "Series") and isinstance(row, pl.Series):
        try:
            vals = row.to_list()
            cols = getattr(row, "_dtype_names", None) or []
            for i, v in enumerate(vals):
                name = cols[i] if i < len(cols) else str(i)
                parts.append(f"{name}: {'' if v is None else v}")
        except Exception:
            parts.append(str(row))
    elif isinstance(row, dict):
        for k, v in row.items():
            parts.append(f"{k}: {'' if v is None else v}")
    else:
        parts.append(str(row))
    return canonicalize_text(" | ".join(parts))

def detect_total_memory_bytes() -> int:
    try:
        path_v2 = "/sys/fs/cgroup/memory.max"
        if os.path.exists(path_v2):
            with open(path_v2, "r") as f:
                val = f.read().strip()
                if val.isdigit():
                    v = int(val)
                    if v > 0 and v < 2**60:
                        return v
        path_v1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        if os.path.exists(path_v1):
            with open(path_v1, "r") as f:
                v = int(f.read().strip())
                if v > 0:
                    return v
    except Exception:
        pass
    try:
        import psutil  # optional
        return int(psutil.virtual_memory().total)
    except Exception:
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except Exception:
        return 512 * (1024**2)

def compute_streaming_chunk_size() -> int:
    total = detect_total_memory_bytes()
    size = max(32_000_000, min(256_000_000, max(16_000_000, int(total // 8))))
    return int(size)

if pl is not None:
    try:
        pl.Config.set_streaming_chunk_size(compute_streaming_chunk_size())
    except Exception:
        pass

def get_header_and_sample_tokens(s3_key: str) -> Tuple[str, int]:
    s3 = get_s3_client()
    try:
        range_header = {"Range": f"bytes=0-{S3_RANGE_BYTES-1}"}
        resp = s3.get_object(Bucket=S3_BUCKET, Key=s3_key, Range=range_header["Range"])
        body_bytes = resp.get("Body").read()
    except Exception:
        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
            body_bytes = obj.get("Body").read()
        except Exception:
            return "", 32
    try:
        text = body_bytes.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        parsed = []
        for ln in lines[:4]:
            try:
                parsed.append(json.loads(ln))
            except Exception:
                continue
        if not parsed:
            return "", 32
        keys = sorted(set().union(*(list(p.keys()) for p in parsed if isinstance(p, dict))))
        header_text = canonicalize_text(" | ".join(keys))
        sample_obj = parsed[0]
        sample_text = row_to_schema_text(sample_obj)
        sample_tokens = max(1, token_count_for(sample_text))
        return header_text, sample_tokens
    except Exception:
        return "", 32

def make_doc_id(s3_key: str, last_modified: Any) -> str:
    return sha256_hex(s3_key + str(last_modified or ""))

def filename_from_source_url(source_url: Optional[str]) -> str:
    if not source_url:
        return ""
    try:
        if source_url.startswith("s3://"):
            return os.path.basename(source_url)
        parsed = urllib.parse.urlparse(source_url)
        if parsed.path:
            return os.path.basename(parsed.path)
        return os.path.basename(source_url)
    except Exception:
        return os.path.basename(str(source_url))

def s3_upload_file_atomic(local_path: str, bucket: str, key: str, content_type: str = "application/octet-stream") -> None:
    s3 = get_s3_client()
    tmp_key = f"{key}.tmp.{os.getpid()}.{int(time.time())}"
    for attempt in range(1, S3_PUT_RETRIES + 1):
        try:
            s3.upload_file(local_path, bucket, tmp_key, ExtraArgs={"ContentType": content_type})
            copy_source = {"Bucket": bucket, "Key": tmp_key}
            s3.copy_object(CopySource=copy_source, Bucket=bucket, Key=key)
            s3.delete_object(Bucket=bucket, Key=tmp_key)
            return
        except Exception as e:
            log.warning("s3 atomic upload attempt %d failed for %s: %s", attempt, key, e)
            time.sleep(S3_PUT_BACKOFF * attempt)
    raise Exception(f"s3 atomic upload failed for {key} after {S3_PUT_RETRIES} attempts")

# Defer pyarrow import until needed (finalize/upload step)
PA_AVAILABLE = False
_pa = None
_pq = None
def _ensure_pyarrow():
    global PA_AVAILABLE, _pa, _pq
    if PA_AVAILABLE:
        return
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        _pa = pa
        _pq = pq
        PA_AVAILABLE = True
    except Exception:
        PA_AVAILABLE = False
        _pa = None
        _pq = None
        # do not exit; raise later when parquet actually needed

class S3ParquetWriter:
    def __init__(self, doc_id: str, s3_path: str):
        self.doc_id = doc_id
        self.s3_path = s3_path
        self._rows: List[Dict[str, Any]] = []

    def _normalize_for_parquet(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        fields["document_id"] = payload.get("document_id") or ""
        fields["chunk_id"] = payload.get("chunk_id") or ""
        fields["chunk_type"] = payload.get("chunk_type") or ""
        fields["text"] = payload.get("text") or ""
        try:
            fields["token_count"] = int(payload.get("token_count") or 0)
        except Exception:
            fields["token_count"] = 0
        for k in ("figures", "tags", "layout_tags", "heading_path", "headings"):
            v = payload.get(k, None)
            try:
                fields[k] = json.dumps(v, ensure_ascii=False, sort_keys=True) if v is not None else "[]"
            except Exception:
                fields[k] = "[]"
        fields["file_type"] = payload.get("file_type") or ""
        fields["source_url"] = payload.get("source_url") or ""
        fields["file_name"] = payload.get("file_name") or ""
        rr = payload.get("row_range")
        if isinstance(rr, (list, tuple)) and len(rr) >= 2:
            try:
                fields["row_start"] = int(rr[0])
                fields["row_end"] = int(rr[1])
            except Exception:
                fields["row_start"] = None
                fields["row_end"] = None
        else:
            fields["row_start"] = None
            fields["row_end"] = None
        tr = payload.get("token_range")
        if isinstance(tr, (list, tuple)) and len(tr) >= 2:
            try:
                fields["token_start"] = int(tr[0])
                fields["token_end"] = int(tr[1])
            except Exception:
                fields["token_start"] = None
                fields["token_end"] = None
        else:
            fields["token_start"] = None
            fields["token_end"] = None
        fields["timestamp"] = payload.get("timestamp") or ""
        fields["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        fields["used_ocr"] = bool(payload.get("used_ocr", False))
        return fields

    def write_payload(self, payload: Dict[str, Any]) -> int:
        self._rows.append(self._normalize_for_parquet(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> Tuple[int, str, str, int]:
        """
        Returns: (rows_count, uploaded_s3_key, sha256_of_parquet, size_bytes)
        Raises RuntimeError if pyarrow is not available.
        """
        if not self._rows:
            return 0, "", "", 0
        _ensure_pyarrow()
        if not PA_AVAILABLE or _pa is None or _pq is None:
            raise RuntimeError("pyarrow is required to finalize parquet output (install pyarrow)")

        pa = _pa
        pq = _pq

        schema = pa.schema([
            pa.field("document_id", pa.string()),
            pa.field("chunk_id", pa.string()),
            pa.field("chunk_type", pa.string()),
            pa.field("text", pa.string()),
            pa.field("token_count", pa.int64()),
            pa.field("figures", pa.string()),
            pa.field("tags", pa.string()),
            pa.field("layout_tags", pa.string()),
            pa.field("heading_path", pa.string()),
            pa.field("headings", pa.string()),
            pa.field("file_type", pa.string()),
            pa.field("source_url", pa.string()),
            pa.field("file_name", pa.string()),
            pa.field("row_start", pa.int64()),
            pa.field("row_end", pa.int64()),
            pa.field("token_start", pa.int64()),
            pa.field("token_end", pa.int64()),
            pa.field("timestamp", pa.string()),
            pa.field("parser_version", pa.string()),
            pa.field("used_ocr", pa.bool_())
        ])
        cols = {name: [] for name in [f.name for f in schema]}
        for r in self._rows:
            for name in cols:
                cols[name].append(r.get(name) if name in r else None)
        table = pa.Table.from_pydict(cols, schema=schema)
        existing_md = table.schema.metadata or {}
        new_md = dict(existing_md)
        new_md.update({
            b"schema_version": os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1").encode("utf-8"),
            b"parser_version": PARSER_VERSION.encode("utf-8"),
            b"producer": b"jsonl_parser",
            b"created_at": datetime.utcnow().isoformat().encode("utf-8")
        })
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        local_parquet_path = tmpfile.name
        with open(local_parquet_path, "rb") as fh:
            b = fh.read()
        sha = sha256_hex_bytes(b)
        size = os.path.getsize(local_parquet_path)
        parquet_key = out_basename + ".parquet"
        s3_upload_file_atomic(local_parquet_path, S3_BUCKET, S3_CHUNKED_PREFIX + parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(local_parquet_path)
        except Exception:
            pass
        return len(self._rows), S3_CHUNKED_PREFIX + parquet_key, sha, size

def sanitize_payload_for_weaviate(payload: Dict[str, Any]) -> None:
    for k in list(payload.keys()):
        v = payload.get(k)
        if k == "tags":
            if v is None:
                payload[k] = []
            elif isinstance(v, (list, tuple)):
                payload[k] = [str(x) for x in v]
            else:
                payload[k] = [str(v)]
            continue
        if v is None:
            payload.pop(k, None)
            continue
        if isinstance(v, (list, tuple, dict)):
            try:
                payload[k] = json.dumps(v)
            except Exception:
                payload[k] = str(v)
            continue
        if not isinstance(v, (str, int, float, bool)):
            payload[k] = str(v)

def _flush_rows_chunk(writer: S3ParquetWriter, doc_id: str, chunk_index: int, header_text: str, rows_text: List[str], start_row_num: int, manifest_tags: List[str] = None) -> Tuple[int, int]:
    if not rows_text:
        return 0, chunk_index
    chunk_index += 1
    chunk_id = f"{doc_id}_{chunk_index}"
    chunk_text = header_text + "\n" + "\n".join(rows_text) if header_text else "\n".join(rows_text)
    token_ct = token_count_for(chunk_text)
    end_row_num = start_row_num + len(rows_text) - 1
    source_url = f"s3://{S3_BUCKET}/{writer.s3_path}" if S3_BUCKET else None
    payload: Dict[str, Any] = {
        "document_id": doc_id or "",
        "chunk_id": chunk_id or "",
        "chunk_type": "row_group",
        "text": canonicalize_text(chunk_text) or "",
        "token_count": int(token_ct or 0),
        "figures": "[]",
        "embedding": None,
        "file_type": "application/x-ndjson",
        "source_url": source_url,
        "file_name": filename_from_source_url(source_url) if source_url else "",
        "row_range": [int(start_row_num), int(end_row_num)],
        "token_range": None,
        "audio_range": None,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "parser_version": PARSER_VERSION or "",
        "tags": manifest_tags or [],
        "layout_tags": [],
        "used_ocr": False,
        "heading_path": [],
        "headings": []
    }
    sanitize_payload_for_weaviate(payload)
    writer.write_payload(payload)
    log.info("Buffered chunk %s", payload["chunk_id"])
    return 1, chunk_index

def _process_batch_rows(rows_iterable, doc_id, s3_path, chunk_index, header_text, next_row_num, writer: S3ParquetWriter, manifest_tags: List[str] = None):
    saved = 0
    rows_text: List[str] = []
    start_row_of_current = next_row_num
    for _, row in rows_iterable:
        if isinstance(row, dict):
            row_text = canonicalize_text(" | ".join([f"{k}: {v}" for k, v in row.items()]))
        else:
            row_text = canonicalize_text(str(row))
        row_num = next_row_num
        next_row_num += 1
        row_tokens = token_count_for(row_text)
        header_tokens = token_count_for(header_text) if header_text else 0
        if row_tokens > TARGET_TOKENS_PER_CHUNK:
            if rows_text:
                wrote, chunk_index = _flush_rows_chunk(writer, doc_id, chunk_index, header_text, rows_text, start_row_of_current, manifest_tags)
                saved += wrote
                rows_text = []
            windows = list(split_into_token_windows(row_text, TARGET_TOKENS_PER_CHUNK, overlap=int(TARGET_TOKENS_PER_CHUNK * 0.1)))
            for w in windows:
                chunk_index += 1
                chunk_id = f"{doc_id}_{chunk_index}"
                candidate_text = header_text + "\n" + w["text"] if header_text and (header_tokens + w["token_count"] <= TARGET_TOKENS_PER_CHUNK) else w["text"]
                token_ct = token_count_for(candidate_text)
                source_url = f"s3://{S3_BUCKET}/{s3_path}" if S3_BUCKET else None
                payload: Dict[str, Any] = {
                    "document_id": doc_id or "",
                    "chunk_id": chunk_id or "",
                    "chunk_type": "token_window",
                    "text": canonicalize_text(candidate_text) or "",
                    "figures": "[]",
                    "token_count": int(token_ct or 0),
                    "embedding": None,
                    "file_type": "application/x-ndjson",
                    "source_url": source_url,
                    "file_name": filename_from_source_url(source_url) if source_url else "",
                    "row_range": [int(row_num), int(row_num)],
                    "token_range": [int(w.get("token_start")), int(w.get("token_end"))],
                    "audio_range": None,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "parser_version": PARSER_VERSION or "",
                    "tags": manifest_tags or [],
                    "layout_tags": [],
                    "used_ocr": False,
                    "heading_path": [],
                    "headings": []
                }
                sanitize_payload_for_weaviate(payload)
                writer.write_payload(payload)
                log.info("Buffered token_window %s", payload["chunk_id"])
                saved += 1
            start_row_of_current = next_row_num
            continue
        candidate_text = header_text + "\n" + "\n".join(rows_text + [row_text]) if header_text else "\n".join(rows_text + [row_text])
        candidate_tokens = token_count_for(candidate_text)
        if candidate_tokens <= TARGET_TOKENS_PER_CHUNK:
            if not rows_text:
                start_row_of_current = row_num
            rows_text.append(row_text)
            continue
        else:
            wrote, chunk_index = _flush_rows_chunk(writer, doc_id, chunk_index, header_text, rows_text, start_row_of_current, manifest_tags)
            saved += wrote
            rows_text = [row_text]
            start_row_of_current = row_num
    if rows_text:
        wrote, chunk_index = _flush_rows_chunk(writer, doc_id, chunk_index, header_text, rows_text, start_row_of_current, manifest_tags)
        saved += wrote
    return saved, chunk_index, next_row_num

def parse_file(s3_key: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main router-callable function. Validates runtime envs and performs parsing.
    Returns dict with at least 'saved_chunks'.
    """
    if not S3_BUCKET:
        raise RuntimeError("S3_BUCKET env must be set to run parse_file()")
    start_all = time.perf_counter()
    s3 = get_s3_client()
    try:
        head_obj = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.error("Could not head S3 object %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0, "skipped": True, "error": str(e)}
    last_modified = head_obj.get("LastModified", "")
    doc_id = manifest.get("file_hash") or make_doc_id(s3_key, last_modified)
    s3_path = f"{s3_key}"
    out_basename = f"{doc_id}"
    out_parquet_key = f"{out_basename}.parquet"
    raw_manifest_key = s3_key + ".manifest.json"
    if not FORCE_OVERWRITE and s3_object_exists(S3_CHUNKED_PREFIX + out_parquet_key):
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.info("Skipping entire file because parquet chunked file exists: %s", out_parquet_key)
        try:
            if not s3_object_exists(raw_manifest_key):
                head = s3.head_object(Bucket=S3_BUCKET, Key=S3_CHUNKED_PREFIX + out_parquet_key)
                etag = head.get("ETag", "")
                if isinstance(etag, str):
                    etag = etag.strip('"')
                size = head.get("ContentLength", 0)
                raw_manifest = {"s3_key": S3_CHUNKED_PREFIX + out_parquet_key, "doc_id": doc_id, "rows": 0, "sha256": etag, "size_bytes": size, "schema_version": os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1"), "parser_version": PARSER_VERSION, "created_at": datetime.utcnow().isoformat() + "Z"}
                s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
        except Exception:
            pass
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
    header_text, sample_row_tokens = get_header_and_sample_tokens(s3_key)
    header_tokens = token_count_for(header_text) if header_text else 0
    if header_tokens >= TARGET_TOKENS_PER_CHUNK:
        log.warning("JSONL header token count >= target chunk size. Header will not be prepended to row_group chunks to avoid exceeding target.")
        header_text = ""
        header_tokens = 0
    if ROWS_PER_CHUNK_OVERRIDE:
        rows_per_chunk = max(MIN_ROWS_PER_CHUNK, min(MAX_ROWS_PER_CHUNK, int(ROWS_PER_CHUNK_OVERRIDE)))
    else:
        available_for_rows = max(1, TARGET_TOKENS_PER_CHUNK - header_tokens)
        estimated_rows = max(1, int(available_for_rows / max(1, sample_row_tokens)))
        rows_per_chunk = max(MIN_ROWS_PER_CHUNK, min(MAX_ROWS_PER_CHUNK, estimated_rows))
    log.info("%s sample_row_tokens=%d header_tokens=%d rows_per_chunk=%d", s3_key, sample_row_tokens, header_tokens, rows_per_chunk)
    saved = 0
    chunk_index = 0
    next_row_num = 1
    manifest_tags = manifest.get("tags", []) if isinstance(manifest, dict) else []
    writer = S3ParquetWriter(doc_id=doc_id, s3_path=s3_path)
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        body = obj.get("Body")
        try:
            iter_lines = body.iter_lines(chunk_size=4096, keepends=False)
            buffer: List[Dict[str, Any]] = []
            for ln in iter_lines:
                if not ln:
                    continue
                try:
                    rec = json.loads(ln.decode("utf-8"))
                except Exception:
                    continue
                buffer.append(rec)
                if len(buffer) >= rows_per_chunk:
                    indexed_iter = ((i, row) for i, row in enumerate(buffer))
                    saved_chunk, chunk_index, next_row_num = _process_batch_rows(indexed_iter, doc_id, s3_path, chunk_index, header_text, next_row_num, writer, manifest_tags)
                    saved += saved_chunk
                    buffer = []
            if buffer:
                indexed_iter = ((i, row) for i, row in enumerate(buffer))
                saved_chunk, chunk_index, next_row_num = _process_batch_rows(indexed_iter, doc_id, s3_path, chunk_index, header_text, next_row_num, writer, manifest_tags)
                saved += saved_chunk
        except Exception:
            body_bytes = body.read()
            text = body_bytes.decode("utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            buffer = []
            for ln in lines:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                buffer.append(rec)
                if len(buffer) >= rows_per_chunk:
                    indexed_iter = ((i, row) for i, row in enumerate(buffer))
                    saved_chunk, chunk_index, next_row_num = _process_batch_rows(indexed_iter, doc_id, s3_path, chunk_index, header_text, next_row_num, writer, manifest_tags)
                    saved += saved_chunk
                    buffer = []
            if buffer:
                indexed_iter = ((i, row) for i, row in enumerate(buffer))
                saved_chunk, chunk_index, next_row_num = _process_batch_rows(indexed_iter, doc_id, s3_path, chunk_index, header_text, next_row_num, writer, manifest_tags)
                saved += saved_chunk
    except Exception as e_pd:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("Skipping malformed or unreadable JSONL %s error=%s", s3_key, str(e_pd))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_pd)}
    try:
        if saved == 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("No chunks produced for %s", s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": False}
        count, uploaded_s3_key, sha, size = writer.finalize_and_upload(out_basename)
        total_ms = int((time.perf_counter() - start_all) * 1000)
        try:
            raw_manifest = {"s3_key": uploaded_s3_key, "doc_id": doc_id, "rows": count, "sha256": sha, "size_bytes": size, "schema_version": os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1"), "parser_version": PARSER_VERSION, "created_at": datetime.utcnow().isoformat() + "Z"}
            s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
        except Exception:
            log.warning("Failed to write raw manifest for %s", s3_key)
        log.info("Wrote %d chunks for %s → %s (%d ms)", count, s3_key, uploaded_s3_key, total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}
    except Exception as e_up:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("Failed to upload chunked file for %s error=%s", s3_key, str(e_up))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_up)}

# CLI behavior only
if __name__ == "__main__":
    if not S3_BUCKET:
        log.error("S3_BUCKET env required")
        sys.exit(1)
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not (key.lower().endswith(".jsonl") or key.lower().endswith(".ndjson") or key.lower().endswith(".json")):
                continue
            log.info("Routing parse_file for s3://%s/%s", S3_BUCKET, key)
            manifest_key = key + ".manifest.json"
            try:
                mf_obj = s3.get_object(Bucket=S3_BUCKET, Key=manifest_key)
                manifest = json.load(mf_obj["Body"])
            except Exception:
                manifest = {}
            try:
                result = parse_file(key, manifest)
                log.info("Result for %s: %s", key, result)
            except Exception as e:
                log.exception("Failed to parse %s: %s", key, e)
