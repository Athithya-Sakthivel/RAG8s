#!/usr/bin/env python3
"""
Safe, importable CSV -> parquet chunker for parse_chunk/formats.

Changes made for safety and compatibility with the new router:
- No `sys.exit()` at import-time. Environment validation occurs inside parse_file().
- Optional libraries (polars, tiktoken, pyarrow) are handled gracefully; pyarrow is
  imported lazily inside the writer that needs it so import won't fail the router.
- boto3 client is created lazily.
- Keeps the original behavior and outputs identical chunk/parquet format.
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
import csv
import urllib.parse
from datetime import datetime
from typing import Any, Dict, Iterator, Tuple, List, Optional
from botocore.exceptions import ClientError

# -------------------- logging ------------------------------------------------
RESET = "\033[0m"
COLORS = {
    logging.DEBUG: "\033[90m",
    logging.INFO: "\033[97m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;41m",
}
class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelno, RESET)
        message = super().format(record)
        return f"{color}{message}{RESET}"

logger = logging.getLogger("csv_parser")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
logger.handlers[:] = [handler]
log = logger

# -------------------- config (no import-time exit) ---------------------------
# Read envs but do not fail at import time. parse_file() will validate required envs.
S3_BUCKET = os.getenv("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
PARSER_VERSION = os.getenv("PARSER_VERSION_CSV", "polars-csv-firstrow-auto-v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
TARGET_TOKENS_PER_CHUNK = int(os.getenv("CSV_TARGET_TOKENS_PER_CHUNK", "1000"))
ROWS_PER_CHUNK_OVERRIDE = os.getenv("CSV_ROWS_PER_CHUNK", "")
MIN_ROWS_PER_CHUNK = int(os.getenv("CSV_MIN_ROWS_PER_CHUNK", "1"))
MAX_ROWS_PER_CHUNK = int(os.getenv("CSV_MAX_ROWS_PER_CHUNK", "100"))
S3_PUT_RETRIES = int(os.getenv("S3_PUT_RETRIES", "3"))
S3_PUT_BACKOFF = float(os.getenv("S3_PUT_BACKOFF", "0.5"))
S3_RANGE_BYTES = int(os.getenv("S3_RANGE_BYTES", "131072"))

# Optional libraries — import if present (polars/tiktoken). No sys.exit on missing.
try:
    import polars as pl  # type: ignore
except Exception:
    pl = None

try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None

# boto3 client will be created lazily by get_s3_client()
_boto3 = None
_s3_client = None

def get_s3_client():
    global _boto3, _s3_client
    if _s3_client is None:
        try:
            import boto3 as _boto3  # local import
            _s3_client = _boto3.client("s3")
        except Exception as e:
            log.error("boto3 is required and must be configured: %s", e)
            raise
    return _s3_client

# Token encoder (lazy)
_ENCODER = None
def get_token_encoder():
    global _ENCODER
    if _ENCODER is not None:
        return _ENCODER
    if tiktoken is None:
        _ENCODER = None
        return None
    try:
        _ENCODER = tiktoken.get_encoding(ENC_NAME)
    except Exception:
        try:
            _ENCODER = tiktoken.encoding_for_model("gpt2")
        except Exception:
            _ENCODER = None
    return _ENCODER

# -------------------- small helpers -----------------------------------------
def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def canonicalize_text(s: Any) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(s.split()).strip()

def token_count_for(text: str) -> int:
    if not text:
        return 0
    enc = get_token_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return len(text.split())

def split_into_token_windows(text: str, window_tokens: int, overlap: int = 0) -> Iterator[Dict[str, Any]]:
    if not text:
        yield {"window_index": 0, "text": "", "token_count": 0, "token_start": 0, "token_end": 0}
        return
    enc = get_token_encoder()
    if enc is None:
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
    tokens = enc.encode(text)
    total = len(tokens)
    if window_tokens <= overlap:
        raise ValueError("window_tokens must be greater than overlap")
    step = window_tokens - overlap
    idx = 0
    for start in range(0, total, step):
        end = start + window_tokens
        slice_tokens = tokens[start:end]
        window_text = enc.decode(slice_tokens)
        yield {"window_index": idx, "text": canonicalize_text(window_text), "token_count": len(slice_tokens), "token_start": start, "token_end": min(end, total)}
        idx += 1
        if end >= total:
            break

def make_doc_id(s3_key: str, last_modified: Any) -> str:
    return sha256_hex_str(s3_key + str(last_modified or ""))

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

def s3_object_exists(key: str) -> bool:
    try:
        get_s3_client().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError:
        return False
    except Exception:
        return False

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
    raise Exception(f"s3 atomic upload failed for {key}")

# -------------------- writer (lazy pyarrow) ---------------------------------
class S3ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._rows: List[Dict[str, Any]] = []

    def _normalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        out["document_id"] = payload.get("document_id") or ""
        out["file_name"] = payload.get("file_name") or ""
        out["chunk_id"] = payload.get("chunk_id") or ""
        out["chunk_type"] = payload.get("chunk_type") or ""
        out["text"] = payload.get("text") or ""
        try:
            out["token_count"] = int(payload.get("token_count") or 0)
        except Exception:
            out["token_count"] = 0
        out["figures"] = json.dumps(payload.get("figures", []), ensure_ascii=False)
        out["tags"] = json.dumps(payload.get("tags", []), ensure_ascii=False)
        out["layout_tags"] = json.dumps(payload.get("layout_tags", []), ensure_ascii=False)
        out["heading_path"] = json.dumps(payload.get("heading_path", []), ensure_ascii=False)
        out["headings"] = json.dumps(payload.get("headings", []), ensure_ascii=False)
        out["file_type"] = payload.get("file_type") or "text/csv"
        out["source_url"] = payload.get("source_url") or ""
        rs, re = None, None
        if payload.get("row_range") and isinstance(payload.get("row_range"), (list, tuple)):
            try:
                rs = int(payload["row_range"][0]); re = int(payload["row_range"][1])
            except Exception:
                rs, re = None, None
        out["row_start"] = rs
        out["row_end"] = re
        ts, te = None, None
        if payload.get("token_range") and isinstance(payload.get("token_range"), (list, tuple)):
            try:
                ts = int(payload["token_range"][0]); te = int(payload["token_range"][1])
            except Exception:
                ts, te = None, None
        out["token_start"] = ts
        out["token_end"] = te
        out["timestamp"] = payload.get("timestamp") or ""
        out["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        out["used_ocr"] = bool(payload.get("used_ocr", False))
        return out

    def write_payload(self, payload: Dict[str, Any]) -> int:
        self._rows.append(self._normalize(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> Tuple[int, str, str, int]:
        """
        Lazily import pyarrow and write parquet. Returns (count, s3_key, sha256, size)
        """
        if not self._rows:
            return 0, "", "", 0
        try:
            import pyarrow as pa  # local import to avoid failing module import
            import pyarrow.parquet as pq  # local import
        except Exception as e:
            log.error("pyarrow is required for parquet writing: %s", e)
            raise

        schema = pa.schema([
            pa.field("document_id", pa.string()),
            pa.field("file_name", pa.string()),
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
            b"producer": b"csv_parser",
            b"created_at": datetime.utcnow().isoformat().encode("utf-8")
        })
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        with open(tmpfile.name, "rb") as fh:
            data = fh.read()
        sha = sha256_hex_bytes(data)
        size = os.path.getsize(tmpfile.name)
        parquet_key = out_basename + ".parquet"
        s3_upload_file_atomic(tmpfile.name, S3_BUCKET, S3_CHUNKED_PREFIX + parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(tmpfile.name)
        except Exception:
            pass
        return len(self._rows), S3_CHUNKED_PREFIX + parquet_key, sha, size

# -------------------- manifest helpers -------------------------------------
def sanitize_payload_for_raw_manifest(doc_id: str, s3_raw_key: str, chunked_s3_key: str, rows: int, sha: str, size: int) -> Dict[str, Any]:
    return {
        "raw_key": s3_raw_key,
        "doc_id": doc_id,
        "chunked_key": chunked_s3_key,
        "rows": rows,
        "sha256": sha,
        "size_bytes": size,
        "schema_version": os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1"),
        "parser_version": PARSER_VERSION,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

# -------------------- CSV heuristics & chunking -----------------------------
def get_header_and_sample_tokens(s3_key: str) -> Tuple[str, int]:
    """
    Attempt to read a small byte range and infer header and sample token counts.
    Uses polars if available (robust), otherwise simple heuristic.
    """
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
        if pl is not None:
            try:
                buf = io.BytesIO(body_bytes)
                df = pl.read_csv(buf, n_rows=2, dtype=str, ignore_errors=True)
                if df.shape[1] == 0 or df.shape[0] == 0:
                    return "", 32
                header_cols = df.columns
                header_text = canonicalize_text(" | ".join(header_cols))
                sample_tokens = 32
                if df.shape[0] > 0:
                    row = df.row(0)
                    sample_dict = {c: (row[i] if i < len(row) else "") for i, c in enumerate(header_cols)}
                    sample_text = " | ".join([f"{k}: {v}" for k, v in sample_dict.items()])
                    sample_tokens = max(1, token_count_for(sample_text))
                return header_text, sample_tokens
            except Exception:
                pass
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return "", 32
        header = lines[0]
        sample_line = lines[1] if len(lines) > 1 else ""
        header_cols = [c.strip() for c in header.split(",")] if header else []
        header_text = canonicalize_text(" | ".join(header_cols))
        sample_tokens = max(1, token_count_for(canonicalize_text(sample_line)))
        return header_text, sample_tokens
    except Exception:
        return "", 32

def _flush_rows_chunk(writer: S3ParquetWriter, doc_id: str, chunk_index: int, header_text: str, rows_text: List[str], start_row_num: int, manifest_tags: List[str] = None) -> Tuple[int, int]:
    if not rows_text:
        return 0, chunk_index
    chunk_index += 1
    chunk_id = f"{doc_id}_{chunk_index}"
    chunk_text = (header_text + "\n" + "\n".join(rows_text)) if header_text else "\n".join(rows_text)
    token_ct = token_count_for(chunk_text)
    end_row_num = start_row_num + len(rows_text) - 1
    source_url = f"s3://{S3_BUCKET}/{writer._rows and writer._rows[0].get('source_url') or ''}"
    payload = {
        "document_id": doc_id or "",
        "chunk_id": chunk_id or "",
        "chunk_type": "row_group",
        "text": canonicalize_text(chunk_text) or "",
        "token_count": int(token_ct or 0),
        "figures": [],
        "embedding": None,
        "file_type": "text/csv",
        "source_url": f"s3://{S3_BUCKET}/{writer._rows[0].get('source_url')}" if writer._rows and writer._rows[0].get("source_url") else f"s3://{S3_BUCKET}/",
        "file_name": filename_from_source_url(f"s3://{S3_BUCKET}/{writer._rows[0].get('source_url')}" if writer._rows and writer._rows[0].get("source_url") else ""),
        "row_range": [int(start_row_num), int(end_row_num)],
        "token_range": None,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "parser_version": PARSER_VERSION,
        "tags": manifest_tags or [],
        "layout_tags": [],
        "used_ocr": False,
        "heading_path": [],
        "headings": [],
        "line_range": None
    }
    writer.write_payload(payload)
    log.info("Buffered CSV row_group chunk %s", payload.get("chunk_id"))
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
        if row_tokens > TARGET_TOKENS_PER_CHUNK:
            if rows_text:
                wrote, chunk_index = _flush_rows_chunk(writer, doc_id, chunk_index, header_text, rows_text, start_row_of_current, manifest_tags)
                saved += wrote
                rows_text = []
            windows = list(split_into_token_windows(row_text, TARGET_TOKENS_PER_CHUNK, overlap=int(TARGET_TOKENS_PER_CHUNK * 0.1)))
            for w in windows:
                chunk_index += 1
                chunk_id = f"{doc_id}_{chunk_index}"
                candidate_text = header_text + "\n" + w["text"] if header_text and (token_count_for(header_text) + w["token_count"] <= TARGET_TOKENS_PER_CHUNK) else w["text"]
                token_ct = token_count_for(candidate_text)
                payload = {
                    "document_id": doc_id or "",
                    "chunk_id": chunk_id or "",
                    "chunk_type": "token_window",
                    "text": canonicalize_text(candidate_text) or "",
                    "token_count": int(token_ct or 0),
                    "figures": [],
                    "embedding": None,
                    "file_type": "text/csv",
                    "source_url": f"s3://{S3_BUCKET}/{s3_path}",
                    "file_name": filename_from_source_url(f"s3://{S3_BUCKET}/{s3_path}"),
                    "row_range": [int(row_num), int(row_num)],
                    "token_range": [int(w.get("token_start")), int(w.get("token_end"))],
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "parser_version": PARSER_VERSION,
                    "tags": manifest_tags or [],
                    "layout_tags": [],
                    "used_ocr": False,
                    "heading_path": [],
                    "headings": [],
                    "line_range": None
                }
                writer.write_payload(payload)
                log.info("Buffered CSV token_window %s", payload.get("chunk_id"))
                saved += 1
            start_row_of_current = next_row_num
            continue
        candidate_text = (header_text + "\n\n".join(rows_text + [row_text])) if header_text else "\n".join(rows_text + [row_text])
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

# -------------------- public parse_file ------------------------------------
def parse_file(s3_key: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry used by router.py. Validates envs at runtime and performs parsing.
    Returns same contract: {"saved_chunks": N, "total_parse_duration_ms": ms, ...}
    """
    start_all = time.perf_counter()

    # runtime validation
    if not S3_BUCKET:
        raise RuntimeError("S3_BUCKET environment variable must be set")

    s3 = get_s3_client()

    try:
        head_obj = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.error("Could not HEAD S3 object %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0, "skipped": True, "error": str(e)}

    last_modified = head_obj.get("LastModified", "")
    doc_id = manifest.get("file_hash") if isinstance(manifest, dict) and manifest.get("file_hash") else make_doc_id(s3_key, last_modified)
    out_basename = f"{doc_id}"
    raw_manifest_key = s3_key + ".manifest.json"
    out_parquet_key = S3_CHUNKED_PREFIX + out_basename + ".parquet"

    # dedupe checks
    if not FORCE_OVERWRITE:
        if s3_object_exists(raw_manifest_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("Skipping because raw manifest exists: %s", raw_manifest_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
        if s3_object_exists(out_parquet_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("Skipping because parquet chunk exists: %s", out_parquet_key)
            try:
                if not s3_object_exists(raw_manifest_key):
                    head = s3.head_object(Bucket=S3_BUCKET, Key=out_parquet_key)
                    etag = head.get("ETag", "")
                    if isinstance(etag, str):
                        etag = etag.strip('"')
                    size = head.get("ContentLength", 0)
                    raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, out_parquet_key, 0, etag, size)
                    s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
            except Exception:
                pass
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    # heuristics
    header_text, sample_row_tokens = get_header_and_sample_tokens(s3_key)
    header_tokens = token_count_for(header_text) if header_text else 0
    if header_tokens >= TARGET_TOKENS_PER_CHUNK:
        log.warning("CSV header token count >= target chunk size. Header will not be prepended to row_group chunks.")
        header_text = ""; header_tokens = 0

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
    writer = S3ParquetWriter(doc_id=doc_id)

    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
        body = obj.get("Body")
        # TextIOWrapper over StreamingBody is fine for CSV reading
        text_stream = io.TextIOWrapper(body, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text_stream, delimiter=",")
        buffer: List[Dict[str, Any]] = []
        for row in reader:
            buffer.append(row)
            if len(buffer) >= rows_per_chunk:
                indexed_iter = ((i, r) for i, r in enumerate(buffer))
                saved_chunk, chunk_index, next_row_num = _process_batch_rows(indexed_iter, doc_id, s3_key, chunk_index, header_text, next_row_num, writer, manifest_tags)
                saved += saved_chunk
                buffer = []
        if buffer:
            indexed_iter = ((i, r) for i, r in enumerate(buffer))
            saved_chunk, chunk_index, next_row_num = _process_batch_rows(indexed_iter, doc_id, s3_key, chunk_index, header_text, next_row_num, writer, manifest_tags)
            saved += saved_chunk
    except Exception as e_pd:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("Skipping malformed or unreadable CSV %s error=%s", s3_key, str(e_pd))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_pd)}

    try:
        if saved == 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("No chunks produced for %s", s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms}
        count, uploaded_s3_key, sha, size = writer.finalize_and_upload(out_basename)
        total_ms = int((time.perf_counter() - start_all) * 1000)
        try:
            raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, uploaded_s3_key, count, sha, size)
            s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
        except Exception:
            log.warning("Failed to write raw manifest for %s", s3_key)
        log.info("Wrote %d chunks for %s → %s (%d ms)", count, s3_key, uploaded_s3_key, total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}
    except Exception as e_up:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("Failed to upload chunked file for %s error=%s", s3_key, str(e_up))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_up)}

# -------------------- CLI (preserve original behavior) ----------------------
if __name__ == "__main__":
    log.info("Starting CSV -> Parquet parser (CLI mode)")
    # ensure s3 client available and S3_BUCKET present
    if not S3_BUCKET:
        log.error("S3_BUCKET env var required for CLI mode")
        sys.exit(1)
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".csv"):
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
