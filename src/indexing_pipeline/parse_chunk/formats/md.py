#!/usr/bin/env python3
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
    from botocore.exceptions import ClientError  # type: ignore
except Exception:
    ClientError = Exception  # type: ignore[assignment]

try:
    from markdown_it import MarkdownIt  # type: ignore
except Exception:
    MarkdownIt = None  # type: ignore[assignment]

try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None  # type: ignore[assignment]

DATA_S3_BUCKET = (os.getenv("DATA_S3_BUCKET") or os.getenv("S3_BUCKET") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
AWS_ENDPOINT_URL = (os.getenv("AWS_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip() or None

STORAGE_RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or os.getenv("S3_RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
STORAGE_CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or os.getenv("S3_CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"

PARSER_VERSION = os.getenv("PARSER_VERSION_MD", "markdown-it-py-v2")
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").strip().lower() == "true"
SAVE_SNAPSHOT = os.getenv("SAVE_SNAPSHOT", "false").strip().lower() == "true"

TOKEN_ENCODER = os.getenv("TOKEN_ENCODER", "cl100k_base")
MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512") or 512)
MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100") or 100)
OVERLAP_TOKENS = int(os.getenv("OVERLAP_TOKENS", str(max(1, int(MAX_TOKENS_PER_CHUNK * 0.1)))) or 1)
if OVERLAP_TOKENS >= MAX_TOKENS_PER_CHUNK:
    OVERLAP_TOKENS = max(1, MAX_TOKENS_PER_CHUNK - 1)

PUT_RETRIES = int(os.getenv("PUT_RETRIES", "3") or 3)
PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3") or 0.3)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15") or 15)
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3") or 3)
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "0.5") or 0.5)

_requests = None
_tiktoken_enc = None
_md_parser = None
_S3_CLIENT = None
_S3_LOCK = threading.Lock()

if boto3 is None:
    def _bootstrap_log(level: str, event: str, msg: str = "", **extra: Any) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": level,
            "event": event,
            "msg": msg,
        }
        if extra:
            payload.update(extra)
        print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)

    _bootstrap_log("error", "startup", "boto3 is required")
    raise SystemExit(2)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    payload: dict[str, Any] = {
        "ts": _now(),
        "level": level.lower(),
        "event": event,
    }
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


def _safe_json(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except Exception:
        return json.dumps(_safe_str(v, ""), ensure_ascii=False)


def _sha256_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonicalize_text(text: Any) -> str:
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
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except allowed_exceptions as e:
            last = e
            if attempt >= retries:
                raise
            sleep = backoff_base * (2 ** (attempt - 1))
            time.sleep(sleep + random.random() * max(0.05, sleep * 0.25))
    if last is not None:
        raise last
    raise RuntimeError("retry_call failed")


def _ensure_optional_deps() -> None:
    global _requests, _tiktoken_enc, _md_parser

    if _requests is None:
        try:
            import requests as _r
            _requests = _r
        except Exception:
            _requests = None

    if _tiktoken_enc is None:
        try:
            if tiktoken is not None:
                try:
                    _tiktoken_enc = tiktoken.get_encoding(TOKEN_ENCODER)
                except Exception:
                    try:
                        _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
                    except Exception:
                        _tiktoken_enc = None
        except Exception:
            _tiktoken_enc = None

    if _md_parser is None:
        try:
            if MarkdownIt is not None:
                _md_parser = MarkdownIt()
            else:
                _md_parser = None
        except Exception:
            _md_parser = None


def _get_s3_client():
    global _S3_CLIENT
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    with _S3_LOCK:
        if _S3_CLIENT is not None:
            return _S3_CLIENT
        session = boto3.session.Session(region_name=AWS_REGION or None)
        if BotocoreConfig is not None:
            cfg = BotocoreConfig(
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            )
            _S3_CLIENT = session.client("s3", config=cfg, endpoint_url=AWS_ENDPOINT_URL)
        else:
            _S3_CLIENT = session.client("s3", endpoint_url=AWS_ENDPOINT_URL)
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

    return retry_call(_call, retries=FETCH_RETRIES, backoff_base=FETCH_BACKOFF, allowed_exceptions=(Exception,))


def _storage_put_bytes(key: str, payload: bytes, content_type: str = "application/octet-stream") -> None:
    client = _get_s3_client()

    def _call():
        client.put_object(Bucket=DATA_S3_BUCKET, Key=key, Body=payload, ContentType=content_type)

    retry_call(_call, retries=PUT_RETRIES, backoff_base=PUT_BACKOFF, allowed_exceptions=(Exception,))


def _storage_upload_file(local_path: str, key: str, content_type: str = "application/octet-stream") -> None:
    client = _get_s3_client()

    def _call():
        extra = {"ContentType": content_type} if content_type else None
        if extra is None:
            client.upload_file(local_path, DATA_S3_BUCKET, key)
        else:
            client.upload_file(local_path, DATA_S3_BUCKET, key, ExtraArgs=extra)

    retry_call(_call, retries=PUT_RETRIES, backoff_base=PUT_BACKOFF, allowed_exceptions=(Exception,))


def _storage_exists(key: str) -> bool:
    try:
        _storage_head(key)
        return True
    except Exception:
        return False


def _is_not_found(err: Exception) -> bool:
    code = None
    if hasattr(err, "response"):
        try:
            code = err.response.get("Error", {}).get("Code")
        except Exception:
            code = None
    return code in ("404", "NoSuchKey", "NotFound", "NoSuchBucket")


def _headline(line: str) -> tuple[int, str] | None:
    m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
    if not m:
        return None
    return len(m.group(1)), canonicalize_text(m.group(2))


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


def _chunk_section(section: dict[str, Any]) -> list[dict[str, Any]]:
    lines = list(section.get("lines", []))
    start_line = _safe_int(section.get("start_line"), 0)
    if not lines:
        return []

    chunks: list[dict[str, Any]] = []
    buffer_lines: list[str] = []
    buffer_tokens = 0
    buffer_start = start_line

    def flush(end_line_abs: int) -> None:
        nonlocal buffer_lines, buffer_tokens, buffer_start
        if not buffer_lines:
            return
        text = canonicalize_text("\n".join(buffer_lines))
        if text:
            chunks.append(
                {
                    "text": text,
                    "token_count": buffer_tokens,
                    "line_start": buffer_start + 1,
                    "line_end": end_line_abs,
                }
            )
        buffer_lines = []
        buffer_tokens = 0

    for rel_idx, line in enumerate(lines):
        abs_line = start_line + rel_idx
        line_tokens = _token_len(line)

        if line_tokens > MAX_TOKENS_PER_CHUNK:
            flush(abs_line)
            for piece in _split_long_line(line, MAX_TOKENS_PER_CHUNK):
                pt = canonicalize_text(piece)
                if not pt:
                    continue
                pt_tokens = _token_len(pt)
                chunks.append(
                    {
                        "text": pt,
                        "token_count": pt_tokens,
                        "line_start": abs_line + 1,
                        "line_end": abs_line + 1,
                    }
                )
            buffer_start = abs_line + 1
            continue

        if buffer_lines and buffer_tokens + line_tokens > MAX_TOKENS_PER_CHUNK:
            flush(abs_line)
            buffer_start = abs_line

        if not buffer_lines:
            buffer_start = abs_line

        buffer_lines.append(line)
        buffer_tokens += line_tokens

    flush(start_line + len(lines))

    if len(chunks) >= 2 and chunks[-1]["token_count"] < MIN_TOKENS_PER_CHUNK:
        prev = chunks[-2]
        cur = chunks[-1]
        if prev["token_count"] + cur["token_count"] <= MAX_TOKENS_PER_CHUNK:
            prev["text"] = canonicalize_text(prev["text"] + "\n" + cur["text"])
            prev["token_count"] = _token_len(prev["text"])
            prev["line_end"] = cur["line_end"]
            chunks.pop()

    return chunks


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


def _derive_file_name(source_url: str | None, raw_key: str) -> str:
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


def _sanitize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(manifest, ensure_ascii=False, default=str))
    except Exception:
        return {"error": "manifest_serialization_failed"}


class ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self.rows: list[dict[str, Any]] = []

    def add(self, payload: dict[str, Any]) -> None:
        self.rows.append(payload)

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        line_range = payload.get("line_range") or [1, 1]
        if not isinstance(line_range, (list, tuple)) or len(line_range) < 2:
            line_range = [1, 1]
        token_range = payload.get("token_range") or [0, _safe_int(payload.get("token_count"), 0)]
        if not isinstance(token_range, (list, tuple)) or len(token_range) < 2:
            token_range = [0, _safe_int(payload.get("token_count"), 0)]
        return {
            "document_id": _safe_str(payload.get("document_id")),
            "file_name": _safe_str(payload.get("file_name")),
            "chunk_id": _safe_str(payload.get("chunk_id")),
            "chunk_type": _safe_str(payload.get("chunk_type")),
            "text": _safe_str(payload.get("text")),
            "token_count": _safe_int(payload.get("token_count")),
            "figures": _safe_json(payload.get("figures", [])),
            "tags": _safe_json(payload.get("tags", [])),
            "layout_tags": _safe_json(payload.get("layout_tags", [])),
            "heading_path": _safe_json(payload.get("heading_path", [])),
            "headings": _safe_json(payload.get("headings", [])),
            "file_type": _safe_str(payload.get("file_type"), "text/markdown"),
            "source_url": _safe_str(payload.get("source_url")),
            "audio_range": _safe_json(payload.get("audio_range", [])),
            "timestamp": _safe_str(payload.get("timestamp")),
            "parser_version": _safe_str(payload.get("parser_version"), PARSER_VERSION),
            "used_ocr": bool(payload.get("used_ocr", False)),
            "line_start": _safe_int(line_range[0], 1),
            "line_end": _safe_int(line_range[1], 1),
            "row_range": _safe_json(payload.get("row_range", [])),
            "token_range": _safe_json(token_range),
            "page_number": _safe_int(payload.get("page_number"), 0),
            "slide_range": _safe_json(payload.get("slide_range", [])),
            "semantic_region": _safe_str(payload.get("semantic_region"), "middle"),
            "original_manifest": _safe_json(payload.get("original_manifest", {})),
        }

    def finalize_and_upload(self, out_basename: str) -> tuple[int, str, str, int]:
        if not self.rows:
            return 0, "", "", 0

        pa, pq = _ensure_pyarrow()
        normalized = [self._normalize(r) for r in self.rows]
        schema = pa.schema(
            [
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

        cols: dict[str, list[Any]] = {name: [] for name in [f.name for f in schema]}
        for r in normalized:
            for name in cols:
                cols[name].append(r.get(name))

        table = pa.Table.from_pydict(cols, schema=schema)
        md = dict(table.schema.metadata or {})
        md.update(
            {
                b"schema_version": CHUNKED_SCHEMA_VERSION.encode("utf-8"),
                b"parser_version": PARSER_VERSION.encode("utf-8"),
                b"producer": b"md_parser",
                b"created_at": _now().encode("utf-8"),
            }
        )
        table = table.replace_schema_metadata(md)

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

        return len(normalized), parquet_key, sha, size


def _split_long_line_into_windows(line: str, max_tokens: int) -> list[str]:
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


def _build_rows_from_sections(
    doc_id: str,
    raw_key: str,
    file_name: str,
    source_url: str,
    original_manifest: dict[str, Any],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    total_lines = len("".join(sec.get("lines", []) for sec in sections).splitlines())
    rows: list[dict[str, Any]] = []
    chunk_index = 1
    cursor_tokens = 0

    for sec in sections:
        heading_path = [h for h in _safe_list(sec.get("heading_path")) if _safe_str(h).strip()]
        headings = heading_path[:] if heading_path else ([] if not _safe_str(sec.get("heading")).strip() else [_safe_str(sec.get("heading")).strip()])
        section_lines = list(sec.get("lines", []))
        sec_start_line = _safe_int(sec.get("start_line"), 0)
        sec_end_line = _safe_int(sec.get("end_line"), sec_start_line)

        if not section_lines:
            continue

        buffer: list[str] = []
        buffer_start = sec_start_line
        buffer_tokens = 0

        def emit_chunk(text: str, line_start: int, line_end: int, token_count: int, _heading_path=heading_path, _headings=headings) -> None:
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
                    "chunk_id": f"{doc_id}_{chunk_index}",
                    "chunk_type": "md_chunk",
                    "text": canonicalize_text(text),
                    "token_count": token_count,
                    "figures": [],
                    "tags": _safe_list(original_manifest.get("tags", [])),
                    "layout_tags": [],
                    "heading_path": _heading_path,
                    "headings": _headings,
                    "file_type": "text/markdown",
                    "source_url": source_url,
                    "audio_range": [],
                    "timestamp": _now(),
                    "parser_version": PARSER_VERSION,
                    "used_ocr": False,
                    "line_range": [line_start_1b, line_end_1b],
                    "row_range": [],
                    "token_range": [token_start, token_end],
                    "page_number": 0,
                    "slide_range": [],
                    "semantic_region": _semantic_region(line_start_1b, line_end_1b, total_lines),
                    "original_manifest": original_manifest,
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
                for piece in _split_long_line_into_windows(line, MAX_TOKENS_PER_CHUNK):
                    pt = canonicalize_text(piece)
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
        last = rows[-1]
        if prev["token_count"] + last["token_count"] <= MAX_TOKENS_PER_CHUNK:
            prev["text"] = canonicalize_text(prev["text"] + "\n" + last["text"])
            prev["token_count"] = _token_len(prev["text"])
            prev["line_end"] = last["line_end"]
            prev["token_range"] = [prev["token_range"][0], prev["token_range"][0] + prev["token_count"]]
            rows.pop()

    return rows


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


def _file_name_from_source(source_url: str | None, raw_key: str) -> str:
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


def parse_file(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    start_all = time.perf_counter()
    try:
        if not DATA_S3_BUCKET:
            return {
                "saved_chunks": 0,
                "total_parse_duration_ms": int((time.perf_counter() - start_all) * 1000),
                "skipped": True,
                "error": "DATA_S3_BUCKET missing",
            }
        _ensure_optional_deps()

        try:
            head = _storage_head(s3_key)
        except Exception as e:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "head_failed", "Could not head object", key=s3_key, error=str(e))
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

        last_modified = head.get("LastModified", "")
        etag = _safe_str(head.get("ETag"), "")
        content_len = _safe_int(head.get("ContentLength"), 0)

        doc_id = _safe_str(manifest.get("file_hash"), "")
        if not doc_id:
            doc_id = _sha256_str(s3_key + "|" + etag + "|" + _safe_str(last_modified, ""))

        out_basename = doc_id
        raw_manifest_key = s3_key + ".manifest.json"
        parquet_key = STORAGE_CHUNKED_PREFIX + out_basename + ".parquet"

        if not FORCE_OVERWRITE:
            if _storage_exists(raw_manifest_key):
                total_ms = int((time.perf_counter() - start_all) * 1000)
                log("info", "skip_manifest_exists", "raw_manifest_exists", key=raw_manifest_key)
                return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
            if _storage_exists(parquet_key):
                total_ms = int((time.perf_counter() - start_all) * 1000)
                log("info", "skip_parquet_exists", "parquet_exists", key=parquet_key)
                return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

        if content_len <= 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "skip_empty_object", "Skipping empty object", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

        try:
            raw_bytes = _storage_get_bytes(s3_key)
        except Exception as e:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "get_failed", "Could not get object", key=s3_key, error=str(e))
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

        try:
            text = try_decode_bytes(raw_bytes)
        except Exception:
            text = ""

        if SAVE_SNAPSHOT:
            try:
                snap_key = STORAGE_RAW_PREFIX + out_basename + ".raw"
                tmpf = tempfile.NamedTemporaryFile(delete=False)
                tmpf.write(raw_bytes)
                tmpf.flush()
                tmpf.close()
                _storage_upload_file(tmpf.name, snap_key, content_type="application/octet-stream")
                try:
                    os.unlink(tmpf.name)
                except Exception:
                    pass
            except Exception:
                pass

        sections = _split_markdown_sections(text)
        if not sections:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("warning", "no_sections", "No sections parsed", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

        file_name = _file_name_from_source(_safe_str(manifest.get("source_url")), s3_key)
        rows = _build_rows_from_sections(doc_id, s3_key, file_name, _safe_str(manifest.get("source_url")), manifest, sections)

        if not rows:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("warning", "no_rows", "No rows produced", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

        writer = ParquetWriter(doc_id)
        for r in rows:
            writer.add(r)

        try:
            count, uploaded_parquet_key, sha, size = writer.finalize_and_upload(out_basename)
        except Exception as e:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "parquet_failed", "Failed to write/upload parquet", key=s3_key, error=str(e), tb=traceback.format_exc())
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

        raw_manifest = _build_raw_manifest(doc_id, s3_key, uploaded_parquet_key, count, sha, size)
        raw_manifest_json = json.dumps(raw_manifest, ensure_ascii=False)

        try:
            tmpm = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8")
            tmpm.write(raw_manifest_json)
            tmpm.flush()
            tmpm.close()
            _storage_upload_file(tmpm.name, raw_manifest_key, content_type="application/json")
            try:
                os.unlink(tmpm.name)
            except Exception:
                pass
        except Exception as e:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "manifest_upload_failed", "Failed to upload manifest", key=raw_manifest_key, error=str(e))
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

        total_ms = int((time.perf_counter() - start_all) * 1000)
        log("info", "parse_ok", "Parsed and uploaded parquet", key=s3_key, parquet=uploaded_parquet_key, rows=count, sha=sha, size=size)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False, "manifest_key": raw_manifest_key, "parquet_key": uploaded_parquet_key}

    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log("error", "parse_unhandled", "Unhandled exception during parse", key=s3_key, error=str(e), tb=traceback.format_exc())
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}
