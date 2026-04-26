from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tempfile
import threading
import time
import traceback
import unicodedata
from datetime import UTC, datetime
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
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None  # type: ignore[assignment]


DATA_S3_BUCKET = (os.getenv("DATA_S3_BUCKET") or os.getenv("S3_BUCKET") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
AWS_ENDPOINT_URL = (os.getenv("AWS_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip() or None

STORAGE_RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or os.getenv("S3_RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
STORAGE_CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or os.getenv("S3_CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"

PARSER_VERSION = os.getenv("PARSER_VERSION_MD", "markdown-it-py-v3")
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").strip().lower() == "true"
SAVE_SNAPSHOT = os.getenv("SAVE_SNAPSHOT", "false").strip().lower() == "true"

TOKEN_ENCODER = os.getenv("TOKEN_ENCODER", "cl100k_base")
MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512") or 512)
MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100") or 100)
DEFAULT_OVERLAP = max(1, int(MAX_TOKENS_PER_CHUNK * 0.1))
OVERLAP_TOKENS = int(os.getenv("OVERLAP_TOKENS", str(DEFAULT_OVERLAP)) or DEFAULT_OVERLAP)
if OVERLAP_TOKENS >= MAX_TOKENS_PER_CHUNK:
    OVERLAP_TOKENS = max(1, MAX_TOKENS_PER_CHUNK - 1)

PUT_RETRIES = int(os.getenv("PUT_RETRIES", "3") or 3)
PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3") or 0.3)
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3") or 3)
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "0.5") or 0.5)

_requests = None
_tiktoken_enc = None
_S3_CLIENT = None
_S3_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    parts = [f"{_now()}", level.lower(), event]
    if msg:
        parts.append(msg)
    if extra:
        extras = " ".join(f"{k}={v}" for k, v in extra.items())
        if extras:
            parts.append(extras)
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


def _safe_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, dict):
        return [v]
    s = _safe_str(v, "").strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        return [s]


def _safe_json_text(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return json.dumps(_safe_str(v, ""), ensure_ascii=False)


def _sha256_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _normalize_text(text: Any) -> str:
    s = _safe_str(text, "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+$", "", ln) for ln in s.split("\n")]
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def try_decode_bytes(b: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return b.decode(encoding)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def retry_call(
    fn,
    retries: int = 3,
    backoff_base: float = 0.5,
    allowed_exceptions: tuple[type[Exception], ...] = (Exception,),
):
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except allowed_exceptions as e:
            last = e
            if attempt >= retries:
                raise
            sleep = backoff_base * (2 ** (attempt - 1))
            time.sleep(sleep + (random.random() * sleep * 0.25))
    if last:
        raise last
    raise RuntimeError("retry_call failed without exception")


def _ensure_optional_deps() -> None:
    global _requests, _tiktoken_enc

    if _requests is None:
        try:
            import requests as _r
            _requests = _r
        except Exception:
            _requests = None

    if _tiktoken_enc is None and tiktoken is not None:
        try:
            _tiktoken_enc = tiktoken.get_encoding(TOKEN_ENCODER)
        except Exception:
            try:
                _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                _tiktoken_enc = None


def _get_s3_client():
    global _S3_CLIENT
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    with _S3_LOCK:
        if _S3_CLIENT is not None:
            return _S3_CLIENT
        if boto3 is None:
            raise RuntimeError("boto3 is required")
        session = boto3.session.Session(region_name=AWS_REGION or None)
        if BotocoreConfig is not None:
            cfg = BotocoreConfig(connect_timeout=5, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})
            client = session.client("s3", config=cfg, endpoint_url=AWS_ENDPOINT_URL)
        else:
            client = session.client("s3", endpoint_url=AWS_ENDPOINT_URL)
        _S3_CLIENT = client
        return _S3_CLIENT


def _storage_head(key: str) -> dict[str, Any]:
    client = _get_s3_client()
    resp = client.head_object(Bucket=DATA_S3_BUCKET, Key=key)
    return {
        "ContentLength": int(resp.get("ContentLength", 0) or 0),
        "ETag": (resp.get("ETag") or "").strip('"'),
        "LastModified": resp.get("LastModified", ""),
        "Metadata": resp.get("Metadata", {}) or {},
    }


def _storage_get_bytes(key: str) -> bytes:
    client = _get_s3_client()

    def _call():
        obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=key)
        body = obj.get("Body")
        return body.read() if body is not None else b""

    return retry_call(_call, retries=FETCH_RETRIES, backoff_base=FETCH_BACKOFF)


def _storage_put_bytes(key: str, payload: bytes, content_type: str = "application/octet-stream") -> None:
    client = _get_s3_client()

    def _call():
        client.put_object(Bucket=DATA_S3_BUCKET, Key=key, Body=payload, ContentType=content_type)

    retry_call(_call, retries=PUT_RETRIES, backoff_base=PUT_BACKOFF)


def _storage_upload_file(local_path: str, key: str, content_type: str = "application/octet-stream") -> None:
    client = _get_s3_client()

    def _call():
        client.upload_file(local_path, DATA_S3_BUCKET, key, ExtraArgs={"ContentType": content_type})

    retry_call(_call, retries=PUT_RETRIES, backoff_base=PUT_BACKOFF)


def _object_exists(key: str) -> bool:
    try:
        _storage_head(key)
        return True
    except Exception:
        return False


def _headline(line: str) -> tuple[int, str] | None:
    m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
    if not m:
        return None
    return len(m.group(1)), _normalize_text(m.group(2))


def _split_markdown_sections(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []

    stack: list[tuple[int, str]] = []
    sections: list[dict[str, Any]] = []
    start = 0
    current_heading = ""
    current_level = 0
    current_path: list[str] = []

    for idx, line in enumerate(lines):
        info = _headline(line)
        if info is None:
            continue

        level, heading = info
        if idx > start:
            sections.append(
                {
                    "heading_path": list(current_path),
                    "heading": current_heading,
                    "level": current_level,
                    "start_line": start,
                    "end_line": idx,
                    "lines": lines[start:idx],
                }
            )

        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        current_path = [h for _, h in stack if h]
        current_heading = heading
        current_level = level
        start = idx + 1

    if start < len(lines):
        sections.append(
            {
                "heading_path": list(current_path),
                "heading": current_heading,
                "level": current_level,
                "start_line": start,
                "end_line": len(lines),
                "lines": lines[start:],
            }
        )
    elif not sections:
        sections.append(
            {
                "heading_path": [],
                "heading": "",
                "level": 0,
                "start_line": 0,
                "end_line": len(lines),
                "lines": lines,
            }
        )

    return sections


def _token_len(text: str) -> int:
    if not text:
        return 0
    if _tiktoken_enc is not None:
        try:
            return len(_tiktoken_enc.encode(text))
        except Exception:
            pass
    return len(text.split())


def _split_long_line(line: str, max_tokens: int) -> list[str]:
    if _token_len(line) <= max_tokens:
        return [line]
    if _tiktoken_enc is not None:
        try:
            toks = _tiktoken_enc.encode(line)
            out = []
            for i in range(0, len(toks), max_tokens):
                piece = _tiktoken_enc.decode(toks[i : i + max_tokens]).strip()
                if piece:
                    out.append(piece)
            return out or [line]
        except Exception:
            pass
    words = line.split()
    if not words:
        return [line[: max(1, max_tokens * 4)].strip() or line]
    out = []
    for i in range(0, len(words), max_tokens):
        piece = " ".join(words[i : i + max_tokens]).strip()
        if piece:
            out.append(piece)
    return out or [line]


def _semantic_region(line_start: int, line_end: int, total_lines: int) -> str:
    try:
        if total_lines <= 0:
            return "middle"
        mid = (float(line_start) + float(line_end)) / 2.0
        ratio = mid / float(total_lines)
        if ratio <= 0.10:
            return "intro"
        if ratio <= 0.30:
            return "early"
        if ratio <= 0.80:
            return "middle"
        if ratio <= 0.95:
            return "late"
        return "footer"
    except Exception:
        return "unknown"


def _ensure_pyarrow():
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
        return pa, pq
    except Exception as e:
        raise RuntimeError("pyarrow required to write parquet") from e


def _derive_file_name_from_source(source_url: str | None, raw_key: str) -> str:
    if source_url:
        candidate = _safe_str(source_url, "")
        try:
            candidate = candidate.split("?", 1)[0].rstrip("/")
            base = os.path.basename(candidate)
            if base:
                return base
        except Exception:
            pass
    return os.path.basename(raw_key)


def _sanitize_manifest_for_output(manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(manifest, ensure_ascii=False, default=str))
    except Exception:
        return {"error": "manifest_serialization_failed"}


def _write_parquet_and_upload(rows: list[dict[str, Any]], out_basename: str) -> tuple[int, str, str, int]:
    pa, pq = _ensure_pyarrow()

    if not rows:
        return 0, "", "", 0

    schema = pa.schema(
        [
            pa.field("document_id", pa.string()),
            pa.field("file_name", pa.string()),
            pa.field("raw_key", pa.string()),
            pa.field("chunk_id", pa.string()),
            pa.field("chunk_type", pa.string()),
            pa.field("chunk_index", pa.int64()),
            pa.field("text", pa.string()),
            pa.field("token_count", pa.int64()),
            pa.field("figures", pa.string()),
            pa.field("tags", pa.string()),
            pa.field("layout_tags", pa.string()),
            pa.field("heading_path", pa.string()),
            pa.field("headings", pa.string()),
            pa.field("file_type", pa.string()),
            pa.field("source_url", pa.string()),
            pa.field("audio_range", pa.string()),
            pa.field("timestamp", pa.string()),
            pa.field("parser_version", pa.string()),
            pa.field("used_ocr", pa.bool_()),
            pa.field("line_start", pa.int64()),
            pa.field("line_end", pa.int64()),
            pa.field("row_range", pa.string()),
            pa.field("token_range", pa.string()),
            pa.field("page_number", pa.int64()),
            pa.field("slide_range", pa.string()),
            pa.field("semantic_region", pa.string()),
            pa.field("original_manifest", pa.string()),
        ]
    )

    cols: dict[str, list[Any]] = {f.name: [] for f in schema}
    for row in rows:
        for key in cols:
            cols[key].append(row.get(key))

    table = pa.Table.from_pydict(cols, schema=schema)

    existing_md = table.schema.metadata or {}
    new_md = dict(existing_md)
    new_md.update(
        {
            b"schema_version": CHUNKED_SCHEMA_VERSION.encode("utf-8"),
            b"parser_version": PARSER_VERSION.encode("utf-8"),
            b"producer": b"md_parser",
            b"created_at": _now().encode("utf-8"),
        }
    )
    table = table.replace_schema_metadata(new_md)

    tmp = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
    tmp.close()
    pq.write_table(table, tmp.name, compression="zstd", flavor="spark")
    with open(tmp.name, "rb") as fh:
        payload = fh.read()

    sha = _sha256_bytes(payload)
    size = os.path.getsize(tmp.name)
    parquet_key = STORAGE_CHUNKED_PREFIX + out_basename + ".parquet"

    _storage_upload_file(tmp.name, parquet_key, content_type="application/octet-stream")

    try:
        os.unlink(tmp.name)
    except Exception:
        pass

    return len(rows), parquet_key, sha, size


def _build_raw_manifest(doc_id: str, raw_key: str, chunked_key: str, rows: int, sha256: str, size_bytes: int) -> dict[str, Any]:
    return {
        "raw_key": raw_key,
        "doc_id": doc_id,
        "chunked_key": chunked_key,
        "rows": rows,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "schema_version": CHUNKED_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "created_at": _now(),
    }


def _build_manifest_result(
    status: str,
    saved_chunks: int,
    *,
    reason: str = "",
    error: str = "",
    total_ms: int = 0,
) -> dict[str, Any]:
    out = {
        "saved_chunks": saved_chunks,
        "total_parse_duration_ms": total_ms,
        "status": status,
        "skipped": status == "skipped",
    }
    if reason:
        out["reason"] = reason
    if error:
        out["error"] = error
    return out


def parse_file(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    start_all = time.perf_counter()
    try:
        if not DATA_S3_BUCKET or not AWS_REGION:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            return _build_manifest_result("error", 0, error="missing env vars", total_ms=total_ms)

        _ensure_optional_deps()

        try:
            head = _storage_head(s3_key)
        except Exception as err:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "head_failed", "could not head object", key=s3_key, error=str(err))
            return _build_manifest_result("error", 0, error=str(err), total_ms=total_ms)

        last_modified = head.get("LastModified", "")
        etag = _safe_str(head.get("ETag"), "")
        content_len = _safe_int(head.get("ContentLength"), 0)

        doc_id = _safe_str(manifest.get("file_hash"), "")
        if not doc_id:
            doc_id = _sha256_str(s3_key + "|" + etag + "|" + _safe_str(last_modified, ""))

        raw_manifest_key = s3_key + ".manifest.json"
        parquet_key = STORAGE_CHUNKED_PREFIX + doc_id + ".parquet"

        if not FORCE_OVERWRITE:
            if _object_exists(raw_manifest_key):
                total_ms = int((time.perf_counter() - start_all) * 1000)
                log("info", "skip_manifest_exists", "raw manifest exists", key=raw_manifest_key)
                return _build_manifest_result("skipped", 0, reason="raw_manifest_exists", total_ms=total_ms)
            if _object_exists(parquet_key):
                total_ms = int((time.perf_counter() - start_all) * 1000)
                log("info", "skip_parquet_exists", "parquet exists", key=parquet_key)
                return _build_manifest_result("skipped", 0, reason="parquet_exists", total_ms=total_ms)

        if content_len <= 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "skip_empty_object", "skipping empty object", key=s3_key)
            return _build_manifest_result("skipped", 0, reason="empty_object", total_ms=total_ms)

        try:
            raw_bytes = _storage_get_bytes(s3_key)
        except Exception as err:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "read_failed", "could not read object", key=s3_key, error=str(err))
            return _build_manifest_result("error", 0, error=str(err), total_ms=total_ms)

        raw_text = try_decode_bytes(raw_bytes)

        if SAVE_SNAPSHOT:
            try:
                snapshot_key = STORAGE_CHUNKED_PREFIX + doc_id + ".snapshot.md"
                _storage_put_bytes(snapshot_key, raw_text.encode("utf-8"), content_type="text/markdown")
            except Exception as e:
                log("warning", "snapshot_write_failed", "failed to save snapshot", key=s3_key, error=str(e))

        canonical_full = _normalize_text(raw_text)
        if not canonical_full:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "no_text", "no parsable text found", key=s3_key)
            return _build_manifest_result("skipped", 0, reason="no_text", total_ms=total_ms)

        sections = _split_markdown_sections(canonical_full)
        if not sections:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "no_sections", "no markdown sections produced", key=s3_key)
            return _build_manifest_result("skipped", 0, reason="no_sections", total_ms=total_ms)

        source_url = f"s3://{DATA_S3_BUCKET}/{s3_key}"
        file_name = _derive_file_name_from_source(_safe_str(manifest.get("source_url"), ""), s3_key)

        rows: list[dict[str, Any]] = []
        chunk_index = 1
        cursor_tokens = 0
        total_lines = sum(len(section.get("lines", []) or []) for section in sections)

        for section in sections:
            section_lines = list(section.get("lines", []))
            if not section_lines:
                continue

            heading_path_base = [h for h in _safe_list(section.get("heading_path")) if _safe_str(h).strip()]
            heading_fallback = _safe_str(section.get("heading")).strip()
            headings_base = heading_path_base[:] if heading_path_base else ([heading_fallback] if heading_fallback else [])
            sec_start_line = _safe_int(section.get("start_line"), 0)
            sec_end_line = _safe_int(section.get("end_line"), sec_start_line)

            buffer: list[str] = []
            buffer_start = sec_start_line
            buffer_tokens = 0

            def emit_chunk(
                text: str,
                line_start: int,
                line_end: int,
                token_count: int,
                *,
                heading_path_snapshot: tuple[str, ...] = tuple(heading_path_base),
                headings_snapshot: tuple[str, ...] = tuple(headings_base),
            ) -> None:
                nonlocal chunk_index, cursor_tokens

                line_start_1b = line_start + 1
                line_end_1b = max(line_start_1b, line_end)
                token_start = cursor_tokens
                token_end = cursor_tokens + max(0, token_count)
                cursor_tokens = token_end

                rows.append(
                    {
                        "document_id": doc_id,
                        "file_name": file_name,
                        "raw_key": s3_key,
                        "chunk_id": f"{doc_id}_{chunk_index}",
                        "chunk_type": "md_chunk",
                        "text": _normalize_text(text),
                        "token_count": token_count,
                        "figures": [],
                        "tags": _safe_list(manifest.get("tags", [])),
                        "layout_tags": [],
                        "heading_path": list(heading_path_snapshot),
                        "headings": list(headings_snapshot),
                        "file_type": "text/markdown",
                        "source_url": source_url,
                        "audio_range": [],
                        "timestamp": _now(),
                        "parser_version": PARSER_VERSION,
                        "used_ocr": False,
                        "line_start": line_start_1b,
                        "line_end": line_end_1b,
                        "row_range": [],
                        "token_range": [token_start, token_end],
                        "page_number": 0,
                        "slide_range": [],
                        "semantic_region": _semantic_region(line_start_1b, line_end_1b, total_lines),
                        "original_manifest": _sanitize_manifest_for_output(manifest or {}),
                    }
                )
                chunk_index += 1

            for rel_idx, line in enumerate(section_lines):
                abs_line = sec_start_line + rel_idx
                line_tokens = _token_len(line)

                if line_tokens > MAX_TOKENS_PER_CHUNK:
                    if buffer:
                        emit_chunk("\n".join(buffer), buffer_start, abs_line, buffer_tokens)
                        buffer = []
                        buffer_tokens = 0
                    for piece in _split_long_line(line, MAX_TOKENS_PER_CHUNK):
                        pt = _normalize_text(piece)
                        if not pt:
                            continue
                        emit_chunk(pt, abs_line, abs_line, _token_len(pt))
                    buffer_start = abs_line + 1
                    continue

                if buffer and buffer_tokens + line_tokens > MAX_TOKENS_PER_CHUNK:
                    emit_chunk("\n".join(buffer), buffer_start, abs_line, buffer_tokens)
                    buffer = []
                    buffer_tokens = 0
                    buffer_start = abs_line

                if not buffer:
                    buffer_start = abs_line

                buffer.append(line)
                buffer_tokens += line_tokens

            if buffer:
                emit_chunk("\n".join(buffer), buffer_start, sec_end_line, buffer_tokens)

        if len(rows) >= 2 and rows[-1]["token_count"] < MIN_TOKENS_PER_CHUNK:
            prev = rows[-2]
            cur = rows[-1]
            if prev["token_count"] + cur["token_count"] <= MAX_TOKENS_PER_CHUNK:
                prev["text"] = _normalize_text(prev["text"] + " " + cur["text"])
                prev["token_count"] = _token_len(prev["text"])
                prev["line_start"] = min(prev["line_start"], cur["line_start"])
                prev["line_end"] = max(prev["line_end"], cur["line_end"])
                prev["token_range"] = [prev["token_range"][0], prev["token_range"][0] + prev["token_count"]]
                prev["semantic_region"] = _semantic_region(prev["line_start"], prev["line_end"], total_lines)
                rows.pop()

        if not rows:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "no_chunks", "no chunks produced", key=s3_key)
            return _build_manifest_result("skipped", 0, reason="no_chunks_produced", total_ms=total_ms)

        count, uploaded_key, sha256, size = _write_parquet_and_upload(rows, doc_id)
        total_ms = int((time.perf_counter() - start_all) * 1000)

        try:
            raw_manifest = _build_raw_manifest(doc_id, s3_key, uploaded_key, count, sha256, size)
            _storage_put_bytes(
                raw_manifest_key,
                json.dumps(raw_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                content_type="application/json",
            )
        except Exception as e:
            log("warning", "manifest_write_failed", "failed to write raw manifest", key=s3_key, error=str(e))

        log("info", "write_complete", "wrote chunks", count=count, raw=s3_key, chunked=uploaded_key, duration_ms=total_ms)
        return _build_manifest_result("ok", count, total_ms=total_ms)

    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        tb = traceback.format_exc()
        log("error", "parse_failed", "parse_file failed", key=s3_key, error=str(e), traceback=tb)
        return _build_manifest_result("error", 0, error=str(e), total_ms=total_ms)


def _ensure_cli_env_or_exit() -> bool:
    missing = []
    if not DATA_S3_BUCKET:
        missing.append("DATA_S3_BUCKET")
    if not AWS_REGION:
        missing.append("AWS_REGION")
    if missing:
        log("error", "startup_missing_env", "missing required env vars", missing=missing)
        return False
    return True


def _iter_md_keys() -> list[str]:
    client = _get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=DATA_S3_BUCKET, Prefix=STORAGE_RAW_PREFIX):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key")
            if not key:
                continue
            if key.endswith("/") or key.lower().endswith(".manifest.json"):
                continue
            if key.lower().endswith(".md") or key.lower().endswith(".markdown"):
                keys.append(key)
    keys.sort()
    return keys


def main() -> int:
    log("info", "startup", "starting markdown parser", bucket=DATA_S3_BUCKET, region=AWS_REGION or "none")
    if not _ensure_cli_env_or_exit():
        return 2

    try:
        keys = _iter_md_keys()
        log("info", "scan", "found markdown files", count=len(keys), prefix=STORAGE_RAW_PREFIX)
    except Exception as err:
        log("error", "scan_failed", "failed to list markdown keys", error=str(err))
        return 1

    rc = 0
    for key in keys:
        manifest_key = key + ".manifest.json"
        manifest: dict[str, Any] = {}
        try:
            body = _storage_get_bytes(manifest_key)
            if body:
                manifest = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            manifest = {}

        try:
            result = parse_file(key, manifest)
            log("info", "cli_result", "parse result", key=key, result=result)
            if _safe_str(result.get("status"), "") == "error":
                rc = 1
        except Exception as err:
            rc = 1
            log("error", "cli_parse_failed", "failed to parse file", key=key, error=str(err), traceback=traceback.format_exc())

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
