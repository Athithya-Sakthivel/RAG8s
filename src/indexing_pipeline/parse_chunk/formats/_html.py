#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import html as html_lib
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

PARSER_VERSION = os.getenv("PARSER_VERSION_HTML", "trafilatura-only-v4")
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").strip().lower() == "true"
SAVE_SNAPSHOT = os.getenv("SAVE_SNAPSHOT", "false").strip().lower() == "true"

TOKEN_ENCODER = os.getenv("TOKEN_ENCODER", "cl100k_base")
MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512") or 512)
MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100") or 100)
NUMBER_OF_OVERLAPPING_SENTENCES = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2") or 2)

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15") or 15)
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3") or 3)
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "0.5") or 0.5)
PUT_RETRIES = int(os.getenv("PUT_RETRIES", "3") or 3)
PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3") or 0.3)

_requests = None
_trafilatura = None
_tiktoken = None
_ENCODER = None
_ENCODER_BACKEND = "whitespace"
_spacy = None
_NLP_SENTENCIZER = None

_S3_CLIENT = None
_S3_LOCK = threading.Lock()


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


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


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


def _safe_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        s = str(v)
    except Exception:
        return default
    return s if s else default


def _safe_json_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    s = _safe_str(v, "").strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        return [s]


def _safe_json_text(v: Any) -> str:
    if v is None:
        return "[]"
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return json.dumps(_safe_str(v, ""), ensure_ascii=False)


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
    global _requests, _trafilatura, _tiktoken, _ENCODER, _ENCODER_BACKEND, _spacy, _NLP_SENTENCIZER

    if _requests is None:
        try:
            import requests as _r
            _requests = _r
        except Exception:
            _requests = None

    if _trafilatura is None:
        try:
            import trafilatura as _t
            _trafilatura = _t
        except Exception:
            _trafilatura = None

    if _tiktoken is None:
        try:
            import tiktoken as _tk
            _tiktoken = _tk
            try:
                _ENCODER = _tiktoken.get_encoding(TOKEN_ENCODER)
            except Exception:
                try:
                    _ENCODER = _tiktoken.encoding_for_model("gpt2")
                except Exception:
                    _ENCODER = None
        except Exception:
            _tiktoken = None
            _ENCODER = None

    if _ENCODER is not None:
        _ENCODER_BACKEND = "tiktoken"
        log("info", "encoder_init", "tiktoken encoder loaded", backend=_ENCODER_BACKEND)
    else:
        _ENCODER_BACKEND = "whitespace"

    if _spacy is None:
        try:
            import spacy as _s
            _spacy = _s
        except Exception:
            _spacy = None

    _NLP_SENTENCIZER = None


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
            cfg = BotocoreConfig(
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            )
            client = session.client("s3", config=cfg, endpoint_url=AWS_ENDPOINT_URL)
        else:
            client = session.client("s3", endpoint_url=AWS_ENDPOINT_URL)
        _S3_CLIENT = client
        return _S3_CLIENT


def _s3_key_to_full(key: str) -> str:
    return f"s3://{DATA_S3_BUCKET.rstrip('/')}/" + key.lstrip("/")


def _strip_root_from_path(full: str) -> str:
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


def _head_object(key: str) -> dict[str, Any]:
    client = _get_s3_client()
    resp = client.head_object(Bucket=DATA_S3_BUCKET, Key=key)
    return {
        "ContentLength": int(resp.get("ContentLength", 0) or 0),
        "ETag": (resp.get("ETag") or "").strip('"'),
        "LastModified": resp.get("LastModified", ""),
        "Metadata": resp.get("Metadata", {}) or {},
    }


def _object_exists(key: str) -> bool:
    try:
        _head_object(key)
        return True
    except Exception:
        return False


def _get_object_bytes(key: str) -> bytes:
    client = _get_s3_client()

    def _call():
        obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=key)
        body = obj.get("Body")
        if body is None:
            return b""
        return body.read()

    return retry_call(_call, retries=FETCH_RETRIES, backoff_base=FETCH_BACKOFF)


def _put_bytes(key: str, payload: bytes, content_type: str = "application/octet-stream") -> None:
    client = _get_s3_client()

    def _call():
        client.put_object(Bucket=DATA_S3_BUCKET, Key=key, Body=payload, ContentType=content_type)

    retry_call(_call, retries=PUT_RETRIES, backoff_base=PUT_BACKOFF)


def _upload_file(local_path: str, key: str, content_type: str = "application/octet-stream") -> None:
    client = _get_s3_client()

    def _call():
        client.upload_file(local_path, DATA_S3_BUCKET, key, ExtraArgs={"ContentType": content_type})

    retry_call(_call, retries=PUT_RETRIES, backoff_base=PUT_BACKOFF)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_str(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _normalize_text(text: Any) -> str:
    s = _safe_str(text, "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_html_tags(html_text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html_text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript.*?>.*?</noscript>", " ", text)
    text = re.sub(r"(?is)<svg.*?>.*?</svg>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_html_text(html_text: str) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {}

    if _trafilatura is not None:
        try:
            extracted = _trafilatura.extract(
                html_text,
                output_format="markdown",
                include_tables=True,
                include_comments=False,
                favor_precision=True,
            )
            if extracted:
                text = _normalize_text(extracted)
                try:
                    metadata_obj = getattr(_trafilatura, "extract_metadata", None)
                    if callable(metadata_obj):
                        md = metadata_obj(html_text)
                        if md:
                            metadata["original_source_url"] = _safe_str(getattr(md, "url", None), "")
                            metadata["title"] = _safe_str(getattr(md, "title", None), "")
                except Exception:
                    pass
                if text:
                    return text, metadata
        except Exception as e:
            log("warning", "trafilatura_failed", "trafilatura extraction failed; falling back to tag stripping", error=str(e))

    return _normalize_text(_strip_html_tags(html_text)), metadata


def _sentence_split(text: str) -> list[str]:
    text = _normalize_text(text)
    if not text:
        return []
    parts = re.split(r"(?<=[\.\!\?])\s+", text)
    out = []
    for part in parts:
        p = _normalize_text(part)
        if p:
            out.append(p)
    return out


def _encoder_encode(text: str):
    if _ENCODER is not None:
        return _ENCODER.encode(text)
    return text.split()


def _encoder_decode(tokens):
    if _ENCODER is not None:
        return _ENCODER.decode(tokens)
    return " ".join(tokens)


def _token_len(text: str) -> int:
    if _ENCODER is not None:
        try:
            return len(_ENCODER.encode(text))
        except Exception:
            return len(text.split())
    return len(text.split())


def _split_long_sentence(sentence: str, max_tokens: int) -> list[str]:
    if _token_len(sentence) <= max_tokens:
        return [sentence]
    if _ENCODER is not None:
        try:
            tokens = _ENCODER.encode(sentence)
            out = []
            for i in range(0, len(tokens), max_tokens):
                out.append(_ENCODER.decode(tokens[i : i + max_tokens]).strip())
            return [x for x in out if x]
        except Exception:
            pass
    words = sentence.split()
    if not words:
        return [sentence[: max_tokens * 4].strip() or sentence]
    out = []
    for i in range(0, len(words), max_tokens):
        chunk = " ".join(words[i : i + max_tokens]).strip()
        if chunk:
            out.append(chunk)
    return out or [sentence]


def _chunk_text(text: str, max_tokens: int, min_tokens: int, overlap_sentences: int) -> list[dict[str, Any]]:
    sentences = _sentence_split(text)
    if not sentences:
        t = _normalize_text(text)
        if not t:
            return []
        return [
            {
                "chunk_index": 0,
                "text": t,
                "token_count": _token_len(t),
                "token_start": 0,
                "token_end": _token_len(t),
                "sentence_start": 0,
                "sentence_end": 1,
            }
        ]

    expanded: list[str] = []
    for sent in sentences:
        expanded.extend(_split_long_sentence(sent, max_tokens))

    windows: list[dict[str, Any]] = []
    i = 0
    chunk_index = 0
    while i < len(expanded):
        cur_parts: list[str] = []
        cur_tokens = 0
        start_i = i
        while i < len(expanded):
            part = expanded[i]
            part_tokens = _token_len(part)
            if cur_parts and cur_tokens + part_tokens > max_tokens:
                break
            if not cur_parts and part_tokens > max_tokens:
                cur_parts.append(part)
                cur_tokens = part_tokens
                i += 1
                break
            cur_parts.append(part)
            cur_tokens += part_tokens
            i += 1

        chunk_text = _normalize_text(" ".join(cur_parts))
        if not chunk_text:
            continue

        windows.append(
            {
                "chunk_index": chunk_index,
                "text": chunk_text,
                "token_count": cur_tokens,
                "token_start": 0,
                "token_end": cur_tokens,
                "sentence_start": start_i,
                "sentence_end": i,
            }
        )
        chunk_index += 1

        if i < len(expanded) and overlap_sentences > 0:
            i = max(start_i + 1, i - overlap_sentences)

    if len(windows) >= 2 and windows[-1]["token_count"] < min_tokens:
        prev = windows[-2]
        cur = windows[-1]
        prev["text"] = _normalize_text(prev["text"] + " " + cur["text"])
        prev["token_count"] = _token_len(prev["text"])
        prev["token_end"] = prev["token_count"]
        windows.pop()

    return windows


def _semantic_region(token_start: int, token_end: int, total_tokens: int) -> str:
    try:
        if total_tokens <= 0:
            return "unknown"
        ratio = float(token_start) / float(total_tokens)
        if ratio < 0.10:
            return "intro"
        if ratio < 0.30:
            return "early"
        if ratio < 0.70:
            return "middle"
        if ratio < 0.90:
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
            pa.field("original_source_url", pa.string()),
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
            b"producer": b"html_parser",
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

    _upload_file(tmp.name, parquet_key, content_type="application/octet-stream")

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


def _prepare_chunk_rows(
    doc_id: str,
    raw_key: str,
    file_name: str,
    source_url: str,
    original_source_url: str,
    original_manifest: dict[str, Any],
    windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    total_tokens = 0
    for w in windows:
        try:
            total_tokens = max(total_tokens, _safe_int(w.get("token_end"), 0))
        except Exception:
            pass

    rows: list[dict[str, Any]] = []
    for idx, w in enumerate(windows):
        token_start = _safe_int(w.get("token_start"), 0)
        token_end = _safe_int(w.get("token_end"), 0)
        text = _normalize_text(w.get("text", ""))
        rows.append(
            {
                "document_id": doc_id,
                "file_name": file_name,
                "raw_key": raw_key,
                "chunk_id": f"{doc_id}_{idx + 1}",
                "chunk_type": "html_window",
                "chunk_index": idx + 1,
                "text": text,
                "token_count": _safe_int(w.get("token_count"), _token_len(text)),
                "figures": _safe_json_text([]),
                "tags": _safe_json_text(original_manifest.get("tags", [])),
                "layout_tags": _safe_json_text([]),
                "heading_path": _safe_json_text([]),
                "headings": _safe_json_text([]),
                "file_type": "text/html",
                "source_url": source_url,
                "audio_range": None,
                "timestamp": _now(),
                "parser_version": PARSER_VERSION,
                "used_ocr": False,
                "line_start": idx + 1,
                "line_end": idx + 1,
                "row_range": None,
                "token_range": _safe_json_text([token_start, token_end]),
                "page_number": None,
                "slide_range": None,
                "semantic_region": _semantic_region(token_start, token_end, total_tokens),
                "original_manifest": _safe_json_text(original_manifest),
            }
        )
    return rows


def parse_file(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    start_all = time.perf_counter()
    try:
        if not DATA_S3_BUCKET or not AWS_REGION:
            return {
                "saved_chunks": 0,
                "total_parse_duration_ms": int((time.perf_counter() - start_all) * 1000),
                "skipped": True,
                "error": "missing env vars",
            }

        _ensure_optional_deps()

        head = _head_object(s3_key)
        last_modified = head.get("LastModified", "")
        doc_id = _safe_str(manifest.get("file_hash"), "")
        if not doc_id:
            doc_id = _sha256_str(s3_key + "|" + _safe_str(last_modified, ""))

        raw_manifest_key = s3_key + ".manifest.json"
        if not FORCE_OVERWRITE and _object_exists(raw_manifest_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "skip_manifest_exists", "raw_manifest_exists", key=raw_manifest_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

        chunked_key = STORAGE_CHUNKED_PREFIX + doc_id + ".parquet"
        if not FORCE_OVERWRITE and _object_exists(chunked_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "skip_parquet_exists", "parquet_exists", key=chunked_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

        raw_bytes = _get_object_bytes(s3_key)
        raw_text = raw_bytes.decode("utf-8", errors="replace")

        if SAVE_SNAPSHOT:
            try:
                snapshot_key = STORAGE_CHUNKED_PREFIX + doc_id + ".snapshot.html"
                _put_bytes(snapshot_key, raw_text.encode("utf-8"), content_type="text/html")
            except Exception as e:
                log("warning", "snapshot_write_failed", "Failed to save HTML snapshot", key=s3_key, error=str(e))

        extracted_text, extracted_meta = _extract_html_text(raw_text)
        canonical_full = _normalize_text(extracted_text or raw_text)

        if not canonical_full:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "no_text", "No parsable text found", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": False}

        windows = _chunk_text(
            canonical_full,
            max_tokens=MAX_TOKENS_PER_CHUNK,
            min_tokens=MIN_TOKENS_PER_CHUNK,
            overlap_sentences=NUMBER_OF_OVERLAPPING_SENTENCES,
        )

        if not windows:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "no_windows", "No chunks produced", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": False}

        file_name = _derive_file_name_from_source(
            _safe_str(extracted_meta.get("original_source_url"), ""),
            s3_key,
        )
        if not file_name:
            file_name = os.path.basename(s3_key)

        s3_source_url = _s3_key_to_full(s3_key)
        rows = _prepare_chunk_rows(
            doc_id=doc_id,
            raw_key=s3_key,
            file_name=file_name,
            source_url=s3_source_url,
            original_source_url=_safe_str(extracted_meta.get("original_source_url"), ""),
            original_manifest=_sanitize_manifest_for_output(manifest),
            windows=windows,
        )

        count, uploaded_key, sha256, size = _write_parquet_and_upload(rows, doc_id)
        total_ms = int((time.perf_counter() - start_all) * 1000)

        try:
            raw_manifest = _build_raw_manifest(doc_id, s3_key, uploaded_key, count, sha256, size)
            _put_bytes(
                raw_manifest_key,
                json.dumps(raw_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                content_type="application/json",
            )
        except Exception as e:
            log("warning", "manifest_write_failed", "Failed to write raw manifest", key=s3_key, error=str(e))

        log("info", "write_complete", "Wrote chunks", count=count, raw=s3_key, chunked=uploaded_key, duration_ms=total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}

    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        tb = traceback.format_exc()
        log("error", "parse_failed", "parse_file failed", key=s3_key, error=str(e), traceback=tb)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}


def _ensure_cli_env_or_exit() -> bool:
    missing = []
    if not DATA_S3_BUCKET:
        missing.append("DATA_S3_BUCKET")
    if not AWS_REGION:
        missing.append("AWS_REGION")
    if missing:
        log("error", "startup_missing_env", "Missing required env vars", missing=missing)
        return False
    return True


def _iter_html_keys() -> list[str]:
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
            if key.lower().endswith(".html") or key.lower().endswith(".htm"):
                keys.append(key)
    return keys


def main() -> int:
    log("info", "startup", "Starting HTML parser", region=AWS_REGION, bucket=DATA_S3_BUCKET)
    if not _ensure_cli_env_or_exit():
        return 2

    try:
        keys = _iter_html_keys()
        log("info", "scan", "Found HTML files", count=len(keys))
    except Exception as e:
        log("error", "scan_failed", "Failed to list HTML keys", error=str(e))
        return 1

    rc = 0
    for key in keys:
        manifest_key = key + ".manifest.json"
        manifest: dict[str, Any] = {}
        try:
            body = _get_object_bytes(manifest_key)
            if body:
                manifest = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            manifest = {}

        try:
            result = parse_file(key, manifest)
            log("info", "cli_result", "Parse result", key=key, result=result)
        except Exception as e:
            rc = 1
            log("error", "cli_parse_failed", "Failed to parse file", key=key, error=str(e), traceback=traceback.format_exc())

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
