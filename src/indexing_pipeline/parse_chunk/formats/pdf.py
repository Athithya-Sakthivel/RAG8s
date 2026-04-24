#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
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

DATA_S3_BUCKET = (os.getenv("DATA_S3_BUCKET") or os.getenv("S3_BUCKET") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
AWS_ENDPOINT_URL = (os.getenv("AWS_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip() or None

RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or os.getenv("RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or os.getenv("CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"

FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").strip().lower() == "true"
PDF_DISABLE_OCR = os.getenv("PDF_DISABLE_OCR", "false").strip().lower() == "true"
PDF_FORCE_OCR = os.getenv("PDF_FORCE_OCR", "false").strip().lower() == "true"
PDF_OCR_ENGINE = os.getenv("PDF_OCR_ENGINE", "auto").strip().lower()
PDF_OCR_STRICT = os.getenv("PDF_OCR_STRICT", "false").strip().lower() == "true"
PDF_TESSERACT_LANG = os.getenv("PDF_TESSERACT_LANG", "eng")
PDF_OCR_RENDER_DPI = int(os.getenv("PDF_OCR_RENDER_DPI", "300") or 300)
PDF_MIN_IMG_SIZE_BYTES = int(os.getenv("PDF_MIN_IMG_SIZE_BYTES", "3072") or 3072)

MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512") or 512)
MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100") or 100)
NUMBER_OF_OVERLAPPING_SENTENCES = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2") or 2)
PARSER_VERSION_PDF = os.getenv("PARSER_VERSION_PDF", "pdf-v2")
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")

PUT_RETRIES = int(os.getenv("PUT_RETRIES", "3") or 3)
PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3") or 0.3)
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3") or 3)
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "0.5") or 0.5)
TOKEN_ENCODER = os.getenv("TOKEN_ENCODER", "cl100k_base")

_s3_client = None
_s3_lock = threading.Lock()
_requests = None
_tiktoken_enc = None
_fitz = None
_pdfplumber = None
_ocr_engine = None
_ocr_engine_name = "none"


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


def _ensure_optional_deps() -> None:
    global _requests, _tiktoken_enc, _fitz, _pdfplumber

    if _requests is None:
        try:
            import requests as _r
            _requests = _r
        except Exception:
            _requests = None

    if _tiktoken_enc is None:
        try:
            import tiktoken  # type: ignore
            try:
                _tiktoken_enc = tiktoken.get_encoding(TOKEN_ENCODER)
            except Exception:
                try:
                    _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    _tiktoken_enc = None
        except Exception:
            _tiktoken_enc = None

    if _fitz is None:
        try:
            import fitz as _f
            _fitz = _f
        except Exception:
            try:
                import pymupdf as _f2  # type: ignore
                _fitz = _f2
            except Exception:
                _fitz = None

    if _pdfplumber is None:
        try:
            import pdfplumber as _pp  # type: ignore
            _pdfplumber = _pp
        except Exception:
            _pdfplumber = None


def _get_s3_client():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _s3_lock:
        if _s3_client is not None:
            return _s3_client
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
        _s3_client = session.client("s3", **kwargs)
        return _s3_client


def _storage_head(key: str) -> dict[str, Any]:
    client = _get_s3_client()
    resp = client.head_object(Bucket=DATA_S3_BUCKET, Key=key)
    return {
        "ContentLength": int(resp.get("ContentLength", 0) or 0),
        "ETag": (resp.get("ETag") or "").strip('"'),
        "LastModified": resp.get("LastModified", ""),
        "Metadata": resp.get("Metadata", {}) or {},
        "ContentType": resp.get("ContentType", "") or "",
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


def _token_len(text: str) -> int:
    if not text:
        return 0
    if _tiktoken_enc is not None:
        try:
            return len(_tiktoken_enc.encode(text))
        except Exception:
            pass
    return len(text.split())


def _split_long_sentence(sent_text: str, max_tokens: int) -> list[str]:
    if _token_len(sent_text) <= max_tokens:
        return [sent_text]
    if _tiktoken_enc is not None:
        try:
            toks = _tiktoken_enc.encode(sent_text)
            out = []
            for i in range(0, len(toks), max_tokens):
                piece = _tiktoken_enc.decode(toks[i : i + max_tokens]).strip()
                if piece:
                    out.append(piece)
            return out or [sent_text]
        except Exception:
            pass
    words = sent_text.split()
    if not words:
        return [sent_text[: max(1, max_tokens * 4)].strip() or sent_text]
    out = []
    for i in range(0, len(words), max_tokens):
        piece = " ".join(words[i : i + max_tokens]).strip()
        if piece:
            out.append(piece)
    return out or [sent_text]


def _split_page_text(text: str) -> list[dict[str, Any]]:
    text = canonicalize_text(text)
    if not text:
        return []

    sentences = [s.strip() for s in re.split(r"(?<=[\.\!\?])\s+", text) if s.strip()]
    if not sentences:
        return [{"text": text, "token_count": _token_len(text), "start_idx": 0, "end_idx": 1}]

    expanded: list[str] = []
    for s in sentences:
        expanded.extend(_split_long_sentence(s, MAX_TOKENS_PER_CHUNK))

    chunks: list[dict[str, Any]] = []
    i = 0
    while i < len(expanded):
        current: list[str] = []
        tokens = 0
        start_i = i
        while i < len(expanded):
            part = expanded[i]
            part_tokens = _token_len(part)
            if current and tokens + part_tokens > MAX_TOKENS_PER_CHUNK:
                break
            if not current and part_tokens > MAX_TOKENS_PER_CHUNK:
                current.append(part)
                tokens = part_tokens
                i += 1
                break
            current.append(part)
            tokens += part_tokens
            i += 1

        if current:
            chunks.append(
                {
                    "text": canonicalize_text(" ".join(current)),
                    "token_count": tokens,
                    "start_idx": start_i,
                    "end_idx": i,
                }
            )
        else:
            i += 1

        if i < len(expanded) and NUMBER_OF_OVERLAPPING_SENTENCES > 0:
            i = max(start_i + 1, i - NUMBER_OF_OVERLAPPING_SENTENCES)

    if len(chunks) >= 2 and chunks[-1]["token_count"] < MIN_TOKENS_PER_CHUNK:
        prev = chunks[-2]
        last = chunks[-1]
        if prev["token_count"] + last["token_count"] <= MAX_TOKENS_PER_CHUNK:
            prev["text"] = canonicalize_text(prev["text"] + "\n" + last["text"])
            prev["token_count"] = _token_len(prev["text"])
            chunks.pop()

    return chunks


def _semantic_region(page_number: int, total_pages: int, cumulative_tokens: int, chunk_tokens: int, total_tokens: int) -> str:
    if total_pages <= 0:
        return "middle"
    page_ratio = float(page_number) / float(total_pages)
    token_ratio = 0.0
    if total_tokens > 0:
        token_ratio = float(cumulative_tokens + max(1, chunk_tokens) / 2.0) / float(total_tokens)
    ratio = max(page_ratio, token_ratio)
    if ratio <= 0.10:
        return "intro"
    if ratio <= 0.30:
        return "early"
    if ratio <= 0.80:
        return "middle"
    if ratio <= 0.95:
        return "late"
    return "footer"


def _ensure_pyarrow():
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
        return pa, pq
    except Exception as e:
        raise RuntimeError("pyarrow required to write parquet") from e


def _sanitize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(manifest, ensure_ascii=False, default=str))
    except Exception:
        return {"error": "manifest_serialization_failed"}


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


def _extract_page_text_fitz(page) -> str:
    try:
        text = page.get_text("text") or ""
        if text.strip():
            return text
    except Exception:
        pass
    try:
        blocks = page.get_text("blocks") or []
        parts = []
        for b in blocks:
            if len(b) >= 5:
                parts.append(str(b[4]))
        text = "\n".join(parts)
        if text.strip():
            return text
    except Exception:
        pass
    return ""


def _extract_pdfplumber_tables(pdf_path: str, page_number: int) -> list[str]:
    if _pdfplumber is None:
        return []
    try:
        with _pdfplumber.open(pdf_path) as pdf:
            if page_number < 0 or page_number >= len(pdf.pages):
                return []
            page = pdf.pages[page_number]
            table_texts: list[str] = []
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
            for table in tables:
                if not table:
                    continue
                lines = []
                for row in table:
                    if not row:
                        continue
                    lines.append("\t".join([_safe_str(c, "") for c in row]))
                if lines:
                    table_texts.append("\n".join(lines))
            return table_texts
    except Exception:
        return []


def _create_ocr_engine():
    global _ocr_engine, _ocr_engine_name

    if PDF_DISABLE_OCR and not PDF_FORCE_OCR:
        _ocr_engine_name = "none"
        _ocr_engine = None
        return "none", None

    if _ocr_engine is not None:
        return _ocr_engine_name, _ocr_engine

    choice = PDF_OCR_ENGINE or "auto"

    if choice == "tesseract":
        try:
            import pytesseract  # type: ignore
            try:
                pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", "tesseract")
            except Exception:
                pass
            _ocr_engine_name = "tesseract"
            _ocr_engine = pytesseract
            return _ocr_engine_name, _ocr_engine
        except Exception as e:
            log("warning", "ocr.tesseract_failed", "Requested Tesseract OCR failed", error=str(e))
            if PDF_OCR_STRICT or PDF_FORCE_OCR:
                raise
            return "none", None

    if choice == "rapidocr" or choice == "auto":
        try:
            import rapidocr_onnxruntime  # type: ignore
            RapidOCR = getattr(rapidocr_onnxruntime, "RapidOCR", None)
            if RapidOCR is None:
                raise ImportError("RapidOCR not exposed")
            _ocr_engine_name = "rapidocr"
            try:
                _ocr_engine = RapidOCR(model_dir=os.getenv("RAPIDOCR_MODEL_DIR", "/opt/models/rapidocr"))
            except TypeError:
                _ocr_engine = RapidOCR()
            return _ocr_engine_name, _ocr_engine
        except Exception as e:
            log("warning", "ocr.rapidocr_failed", "RapidOCR not available", error=str(e))
            if choice == "rapidocr" and (PDF_OCR_STRICT or PDF_FORCE_OCR):
                raise
            try:
                import pytesseract  # type: ignore
                try:
                    pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", "tesseract")
                except Exception:
                    pass
                _ocr_engine_name = "tesseract"
                _ocr_engine = pytesseract
                return _ocr_engine_name, _ocr_engine
            except Exception as e2:
                log("warning", "ocr.tesseract_fallback_failed", "No OCR engine available", error=str(e2))
                if PDF_OCR_STRICT or PDF_FORCE_OCR:
                    raise
                return "none", None

    return "none", None


def _ocr_page_image(page) -> str:
    engine_name, engine = _create_ocr_engine()
    if engine_name == "none" or engine is None:
        return ""
    try:
        fitz = _get_fitz()
        mat = fitz.Matrix(PDF_OCR_RENDER_DPI / 72.0, PDF_OCR_RENDER_DPI / 72.0)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")
        if len(png_bytes) < PDF_MIN_IMG_SIZE_BYTES:
            return ""
        try:
            from PIL import Image
        except Exception:
            return ""
        img = Image.open(io.BytesIO(png_bytes))
        if engine_name == "tesseract":
            try:
                return _safe_str(engine.image_to_string(img, lang=PDF_TESSERACT_LANG), "")
            except Exception as e:
                log("warning", "ocr.page_failed", "Tesseract OCR failed", error=str(e))
                return ""
        if engine_name == "rapidocr":
            try:
                import numpy as np  # type: ignore
                arr = np.array(img.convert("RGB"))[:, :, ::-1].copy()
                res = engine(arr)
                if isinstance(res, tuple) and res:
                    res = res[0]
                lines = []
                if isinstance(res, list):
                    for item in res:
                        if isinstance(item, dict):
                            txt = item.get("text") or item.get("rec") or ""
                            if txt:
                                lines.append(_safe_str(txt, "").strip())
                                continue
                        if isinstance(item, (list, tuple)):
                            for el in item:
                                if isinstance(el, str) and el.strip():
                                    lines.append(el.strip())
                                    break
                else:
                    s = _safe_str(res, "").strip()
                    if s:
                        lines.append(s)
                return "\n".join([x for x in lines if x])
            except Exception as e:
                log("warning", "ocr.page_failed", "RapidOCR failed", error=str(e))
                return ""
    except Exception as e:
        log("warning", "ocr.prepare_failed", "OCR preparation failed", error=str(e))
        return ""
    return ""


def _get_fitz():
    global _fitz
    if _fitz is not None:
        return _fitz
    try:
        import fitz as _f
        _fitz = _f
        return _fitz
    except Exception:
        try:
            import pymupdf as _f2  # type: ignore
            _fitz = _f2
            return _fitz
        except Exception as e:
            raise RuntimeError("PyMuPDF is required to parse PDFs") from e


def _download_to_temp(key: str) -> str:
    tmpdir = os.getenv("TMPDIR") or None
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=tmpdir)
    tf.close()
    try:
        with open(tf.name, "wb") as fh:
            client = _get_s3_client()
            client.download_fileobj(DATA_S3_BUCKET, key, fh)
    except Exception:
        try:
            os.unlink(tf.name)
        except Exception:
            pass
        raise
    return tf.name


def _build_raw_manifest(doc_id: str, raw_key: str, chunked_key: str, rows: int, sha256: str, size_bytes: int) -> dict[str, Any]:
    return {
        "raw_key": raw_key,
        "doc_id": doc_id,
        "chunked_key": chunked_key,
        "rows": rows,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "schema_version": CHUNKED_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION_PDF,
        "created_at": _now(),
    }


def _sanitize_payload_for_parquet(payload: dict[str, Any]) -> dict[str, Any]:
    line_range = payload.get("line_range") or [0, 0]
    if not isinstance(line_range, (list, tuple)) or len(line_range) < 2:
        line_range = [0, 0]
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
        "file_type": _safe_str(payload.get("file_type"), "application/pdf"),
        "source_url": _safe_str(payload.get("source_url")),
        "page_number": _safe_int(payload.get("page_number"), 0),
        "line_start": _safe_int(line_range[0], 0),
        "line_end": _safe_int(line_range[1], 0),
        "timestamp": _safe_str(payload.get("timestamp"), _now()),
        "parser_version": _safe_str(payload.get("parser_version"), PARSER_VERSION_PDF),
        "used_ocr": bool(payload.get("used_ocr", False)),
        "semantic_region": _safe_str(payload.get("semantic_region"), "middle"),
        "token_range": _safe_json(token_range),
        "original_manifest": _safe_json(payload.get("original_manifest", {})),
    }


class S3ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._rows: list[dict[str, Any]] = []

    def write_payload(self, payload: dict[str, Any]) -> int:
        self._rows.append(_sanitize_payload_for_parquet(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> tuple[int, str, str, int]:
        if not self._rows:
            return 0, "", "", 0
        pa, pq = _ensure_pyarrow()
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
                pa.field("page_number", pa.int64()),
                pa.field("line_start", pa.int64()),
                pa.field("line_end", pa.int64()),
                pa.field("timestamp", pa.string()),
                pa.field("parser_version", pa.string()),
                pa.field("used_ocr", pa.bool_()),
                pa.field("semantic_region", pa.string()),
                pa.field("token_range", pa.string()),
                pa.field("original_manifest", pa.string()),
            ]
        )
        cols = {name: [] for name in [f.name for f in schema]}
        for r in self._rows:
            for name in cols:
                cols[name].append(r.get(name))
        table = pa.Table.from_pydict(cols, schema=schema)
        md = dict(table.schema.metadata or {})
        md.update(
            {
                b"schema_version": CHUNKED_SCHEMA_VERSION.encode("utf-8"),
                b"parser_version": PARSER_VERSION_PDF.encode("utf-8"),
                b"producer": b"pdf_parser",
                b"created_at": _now().encode("utf-8"),
            }
        )
        table = table.replace_schema_metadata(md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir=tempfile.gettempdir())
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        with open(tmpfile.name, "rb") as fh:
            payload = fh.read()
        sha = _sha256_bytes(payload)
        size = os.path.getsize(tmpfile.name)
        parquet_key = CHUNKED_PREFIX + out_basename + ".parquet"
        _storage_upload_file(tmpfile.name, parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(tmpfile.name)
        except Exception:
            pass
        return len(self._rows), parquet_key, sha, size


def _compute_pdf_semantic_region(page_number: int, total_pages: int, cumulative_tokens: int, chunk_tokens: int, total_tokens: int) -> str:
    if total_pages <= 0:
        return "middle"
    if total_tokens > 0:
        midpoint = (float(cumulative_tokens) + float(max(1, chunk_tokens)) / 2.0) / float(total_tokens)
    else:
        midpoint = float(page_number) / float(total_pages)
    if page_number <= 1 and midpoint <= 0.15:
        return "intro"
    if midpoint <= 0.30:
        return "early"
    if midpoint <= 0.80:
        return "middle"
    if midpoint <= 0.95:
        return "late"
    return "footer"


def _page_text_and_figures(pdf_path: str, page_number: int) -> tuple[str, list[str], bool]:
    fitz = _get_fitz()
    page_text = ""
    figures: list[str] = []
    used_ocr = False
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_number]
        page_text = canonicalize_text(_extract_page_text_fitz(page))
        figures.extend([canonicalize_text(t) for t in _extract_pdfplumber_tables(pdf_path, page_number) if canonicalize_text(t)])
        if not page_text and not PDF_DISABLE_OCR and (PDF_FORCE_OCR or PDF_OCR_ENGINE != "none"):
            ocr_text = canonicalize_text(_ocr_page_image(page))
            if ocr_text:
                page_text = ocr_text
                used_ocr = True
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return page_text, figures, used_ocr


def _build_chunks_for_page(
    doc_id: str,
    raw_key: str,
    file_name: str,
    source_url: str,
    manifest: dict[str, Any],
    page_number: int,
    total_pages: int,
    page_text: str,
    figures: list[str],
    used_ocr: bool,
    cumulative_tokens_before: int,
    total_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    if not page_text and not figures:
        return rows, cumulative_tokens_before

    page_chunks = _split_page_text(page_text) if page_text else []
    if not page_chunks and page_text:
        page_chunks = [{"text": page_text, "token_count": _token_len(page_text), "start_idx": 0, "end_idx": 1}]
    if not page_chunks and figures:
        page_chunks = [{"text": "", "token_count": 0, "start_idx": 0, "end_idx": 1}]
    if not page_chunks:
        return rows, cumulative_tokens_before

    for idx, ch in enumerate(page_chunks):
        chunk_text = canonicalize_text(ch.get("text", ""))
        chunk_tokens = _safe_int(ch.get("token_count"), _token_len(chunk_text))
        region = _semantic_region(page_number, total_pages, cumulative_tokens_before, chunk_tokens, total_tokens)
        row = {
            "document_id": doc_id,
            "file_name": file_name,
            "chunk_id": f"{doc_id}_p{page_number}_{idx + 1}",
            "chunk_type": "pdf_page_chunk",
            "text": chunk_text,
            "token_count": chunk_tokens,
            "figures": figures,
            "file_type": "application/pdf",
            "source_url": source_url,
            "page_number": page_number,
            "timestamp": _now(),
            "parser_version": PARSER_VERSION_PDF,
            "tags": _safe_list(manifest.get("tags", [])),
            "layout_tags": [],
            "used_ocr": used_ocr,
            "heading_path": [],
            "headings": [],
            "line_range": [page_number, page_number],
            "token_range": [cumulative_tokens_before, cumulative_tokens_before + chunk_tokens],
            "semantic_region": region,
            "original_manifest": manifest,
        }
        rows.append(row)
        cumulative_tokens_before += chunk_tokens

    return rows, cumulative_tokens_before


def process_pdf_s3_object(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    start_all = time.perf_counter()
    local_pdf = None
    try:
        if not DATA_S3_BUCKET:
            return {"saved_chunks": 0, "total_parse_duration_ms": int((time.perf_counter() - start_all) * 1000), "skipped": True, "error": "DATA_S3_BUCKET missing"}
        _ensure_optional_deps()

        try:
            head = _storage_head(s3_key)
        except Exception as e:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("error", "head_failed", "Could not head object", key=s3_key, error=str(e))
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

        etag = _safe_str(head.get("ETag"), "")
        last_modified = _safe_str(head.get("LastModified"), "")
        content_len = _safe_int(head.get("ContentLength"), 0)
        doc_id = _safe_str(manifest.get("file_hash"), "")
        if not doc_id:
            doc_id = _sha256_str(s3_key + "|" + etag + "|" + last_modified)

        out_basename = doc_id
        raw_manifest_key = s3_key + ".manifest.json"
        parquet_key = CHUNKED_PREFIX + out_basename + ".parquet"

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

        local_pdf = _download_to_temp(s3_key)
        fitz = _get_fitz()
        doc = fitz.open(local_pdf)
        total_pages = len(doc)
        file_name = _file_name_from_source(_safe_str(manifest.get("source_url"), ""), s3_key)
        source_url = _safe_str(manifest.get("source_url"), f"s3://{DATA_S3_BUCKET}/{s3_key}")
        writer = S3ParquetWriter(doc_id=doc_id)
        saved = 0
        cumulative_tokens = 0

        page_infos: list[dict[str, Any]] = []
        for pageno in range(total_pages):
            try:
                page_text, figures, used_ocr = _page_text_and_figures(local_pdf, pageno)
            except Exception as e:
                log("warning", "page_extract_failed", "Page extraction failed", key=s3_key, page_number=pageno + 1, error=str(e))
                page_text, figures, used_ocr = "", [], False
            page_tokens = _token_len(page_text)
            page_infos.append(
                {
                    "page_text": page_text,
                    "figures": figures,
                    "used_ocr": used_ocr,
                    "page_tokens": page_tokens,
                }
            )

        total_doc_tokens = sum(p["page_tokens"] for p in page_infos)

        for pageno, info in enumerate(page_infos, start=1):
            page_text = info["page_text"]
            figures = info["figures"]
            used_ocr = bool(info["used_ocr"])
            page_rows, cumulative_tokens = _build_chunks_for_page(
                doc_id=doc_id,
                raw_key=s3_key,
                file_name=file_name,
                source_url=source_url,
                manifest=_sanitize_manifest(manifest or {}),
                page_number=pageno,
                total_pages=total_pages,
                page_text=page_text,
                figures=figures,
                used_ocr=used_ocr,
                cumulative_tokens_before=cumulative_tokens,
                total_tokens=total_doc_tokens,
            )
            for row in page_rows:
                writer.write_payload(row)
                saved += 1
            if page_rows:
                log("info", "page_processed", "Processed page", key=s3_key, page_number=pageno, chunks=len(page_rows))

        try:
            doc.close()
        except Exception:
            pass

        if saved == 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log("info", "no_chunks", "No chunks produced", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": False}

        count, uploaded_key, sha, size = writer.finalize_and_upload(out_basename)
        try:
            raw_manifest = _build_raw_manifest(doc_id, s3_key, uploaded_key, count, sha, size)
            _storage_put_bytes(raw_manifest_key, json.dumps(raw_manifest).encode("utf-8"), content_type="application/json")
        except Exception as e:
            log("warning", "manifest_write_failed", "Failed to write raw manifest", key=s3_key, error=str(e))

        total_ms = int((time.perf_counter() - start_all) * 1000)
        log("info", "write_complete", "Wrote chunks", count=count, raw=s3_key, chunked=uploaded_key, duration_ms=total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}

    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log("error", "parse_failed", "parse_file failed", key=s3_key, error=str(e), traceback=traceback.format_exc())
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}
    finally:
        if local_pdf:
            try:
                os.unlink(local_pdf)
            except Exception:
                pass


def parse_file(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    return process_pdf_s3_object(s3_key, manifest or {})


def _ensure_cli_env_or_exit() -> bool:
    missing = []
    if not DATA_S3_BUCKET:
        missing.append("DATA_S3_BUCKET")
    if not AWS_REGION and not AWS_ENDPOINT_URL:
        missing.append("AWS_REGION")
    if missing:
        log("error", "startup_missing_env", "Missing required env vars", missing=missing)
        return False
    return True


def _iter_pdf_keys() -> list[str]:
    client = _get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=DATA_S3_BUCKET, Prefix=RAW_PREFIX):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key")
            if not key or key.endswith("/") or key.lower().endswith(".manifest.json"):
                continue
            if key.lower().endswith(".pdf"):
                keys.append(key)
    return keys


def main() -> int:
    log("info", "startup", "Starting PDF parser", bucket=DATA_S3_BUCKET, region=AWS_REGION)
    if not _ensure_cli_env_or_exit():
        return 2

    try:
        keys = _iter_pdf_keys()
        log("info", "scan", "Found PDF files", count=len(keys))
    except Exception as e:
        log("error", "scan_failed", "Failed to list PDF keys", error=str(e))
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
            log("info", "cli_result", "Parse result", key=key, result=result)
        except Exception as e:
            rc = 1
            log("error", "cli_parse_failed", "Failed to parse file", key=key, error=str(e), traceback=traceback.format_exc())

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
