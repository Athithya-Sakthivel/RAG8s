#!/usr/bin/env python3
"""
Refactored HTML parser for chunking and indexing pipeline.
Aligns chunk schema with index.py expectations.
Adds semantic_region field using token position.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from typing import Any

# Minimal logging configured on import but not fatal
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("html_trafilatura")

# ---- config from env (safe to read at import) --------------------------------
S3_BUCKET = os.getenv("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
PARSER_VERSION = os.getenv("PARSER_VERSION_HTML", "trafilatura-only-v2")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
SAVE_SNAPSHOT = os.getenv("SAVE_SNAPSHOT", "false").lower() == "true"
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "0.5"))
MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512"))
MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100"))
NUMBER_OF_OVERLAPPING_SENTENCES = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2"))
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")

# ---- module-level lazy dependency holders (populated by _ensure_runtime) ------
_s3_client = None
_requests = None
_trafilatura = None
_tiktoken = None
_ENC = None  # tiktoken encoder object if available
_ENCODER_ENCODE = None
_ENCODER_DECODE = None
_ENCODER_BACKEND = "whitespace"
_spacy = None
_Sentencizer = None
_NLP_SENTENCIZER = None

# ---- helper utilities (safe to import) --------------------------------------
def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def canonicalize_text(s: Any) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ---- lazy runtime setup -----------------------------------------------------
def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        try:
            import boto3  # local import so module can import even if boto3 missing
        except Exception as e:
            raise RuntimeError("boto3 is required but not available") from e
        _s3_client = boto3.client("s3")
    return _s3_client

def _ensure_optional_deps():
    """
    Import optional libraries lazily and set encoder & sentencizer clients.
    Raise RuntimeError for strictly required items (boto3 always required; pyarrow only needed at finalize).
    """
    global _requests, _trafilatura, _tiktoken, _ENC, _ENCODER_ENCODE, _ENCODER_DECODE, _ENCODER_BACKEND, _spacy, _Sentencizer, _NLP_SENTENCIZER
    # requests
    if _requests is None:
        try:
            import requests as _r
            _requests = _r
        except Exception:
            _requests = None
    # trafilatura
    if _trafilatura is None:
        try:
            import trafilatura as _t
            _trafilatura = _t
        except Exception:
            _trafilatura = None
    # tiktoken
    if _tiktoken is None:
        try:
            import tiktoken as _tk
            _tiktoken = _tk
            try:
                _ENC = _tiktoken.get_encoding(ENC_NAME)
            except Exception:
                try:
                    _ENC = _tiktoken.encoding_for_model("gpt2")
                except Exception:
                    _ENC = None
        except Exception:
            _tiktoken = None
            _ENC = None
    # encoder functions
    if _ENC is not None:
        _ENCODER_ENCODE = lambda txt: _ENC.encode(txt)
        _ENCODER_DECODE = lambda toks: _ENC.decode(toks)
        _ENCODER_BACKEND = "tiktoken"
        log.info("Using tiktoken encoder backend")
    else:
        _ENCODER_ENCODE = lambda txt: txt.split()
        _ENCODER_DECODE = lambda toks: " ".join(toks)
        _ENCODER_BACKEND = "whitespace"
    # spacy/sentencizer
    if _spacy is None:
        try:
            import spacy as _s
            from spacy.pipeline import Sentencizer as _S
            _spacy = _s
            _Sentencizer = _S
        except Exception:
            _spacy = None
            _Sentencizer = None
    # build sentencizer lazily; leave _NLP_SENTENCIZER None until used
    return

# ---- HTTP and fetch helpers (use _requests or fail at runtime) ----------------
def fetch_html_with_retries(url: str, timeout: int = REQUEST_TIMEOUT, retries: int = FETCH_RETRIES, backoff: float = FETCH_BACKOFF) -> str:
    if _requests is None:
        raise RuntimeError("requests is required to fetch remote HTML")
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = _requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last

def upload_snapshot_to_s3(snapshot_html: str, doc_id: str) -> str | None:
    if not SAVE_SNAPSHOT:
        return None
    s3 = _get_s3_client()
    key = f"{S3_CHUNKED_PREFIX}{doc_id}.snapshot.html"
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=snapshot_html.encode("utf-8"), ContentType="text/html")
    return f"s3://{S3_BUCKET}/{key}"

def trafilatura_extract_markdown(html_text: str) -> tuple[str | None, dict[str, Any]]:
    if _trafilatura is None:
        return None, {}
    try:
        md = _trafilatura.extract(html_text, output_format="markdown", with_metadata=True)
    except Exception:
        md = None
    parsed = {}
    try:
        json_doc = _trafilatura.extract(html_text, output_format="json", with_metadata=True)
        if json_doc:
            parsed = json.loads(json_doc)
    except Exception:
        parsed = {}
    return md, parsed

# ---- sentencizer & encoder helpers ------------------------------------------
def _make_sentencizer():
    global _NLP_SENTENCIZER
    if _NLP_SENTENCIZER is not None:
        return _NLP_SENTENCIZER
    if _spacy is None:
        _NLP_SENTENCIZER = None
        return None
    try:
        nlp = _spacy.blank("en")
        try:
            nlp.add_pipe("sentencizer")
        except Exception:
            if _Sentencizer is not None:
                nlp.add_pipe(_Sentencizer())
            else:
                nlp.add_pipe("sentencizer")
        _NLP_SENTENCIZER = nlp
        return nlp
    except Exception:
        _NLP_SENTENCIZER = None
        return None

def _regex_sentences_with_offsets(text: str):
    spans = []
    pattern = re.compile(r'(.+?[\.\?\!]["\']?\s+)|(.+?$)', re.DOTALL)
    cursor = 0
    for m in pattern.finditer(text):
        s = (m.group(1) or m.group(2) or "").strip()
        if not s:
            continue
        start = text.find(s, cursor)
        if start == -1:
            start = cursor
        end = start + len(s)
        spans.append((s, start, end))
        cursor = end
    return spans

def _sentences_with_offsets(text: str):
    nlp = _make_sentencizer()
    if nlp is not None:
        doc = nlp(text)
        return [(sent.text.strip(), int(sent.start_char), int(sent.end_char)) for sent in doc.sents if sent.text.strip()]
    return _regex_sentences_with_offsets(text)

def _make_encoder_clients():
    global _ENCODER_ENCODE, _ENCODER_DECODE, _ENCODER_BACKEND
    if _ENCODER_ENCODE is None:
        # _ensure_optional_deps should have set these
        _ENCODER_ENCODE = lambda txt: txt.split()
        _ENCODER_DECODE = lambda toks: " ".join(toks)
        _ENCODER_BACKEND = "whitespace"
    return _ENCODER_ENCODE, _ENCODER_DECODE, _ENCODER_BACKEND

# ---- token windowing -------------------------------------------------------
def split_into_token_windows(text: str, max_tokens: int = MAX_TOKENS_PER_CHUNK, min_tokens: int = MIN_TOKENS_PER_CHUNK, overlap_sentences: int = NUMBER_OF_OVERLAPPING_SENTENCES) -> Iterator[dict[str, Any]]:
    if not text:
        yield {"window_index": 0, "text": "", "token_count": 0, "token_start": 0, "token_end": 0}
        return
    text = canonicalize_text(text)
    sentences = _sentences_with_offsets(text)
    # ensure encoders are ready
    enc_encode, enc_decode, enc_backend = _make_encoder_clients()
    sent_items = []
    token_cursor = 0
    for s, sc, ec in sentences:
        toks = enc_encode(s)
        tok_len = len(toks)
        sent_items.append({"text": s, "start_char": sc, "end_char": ec, "token_len": tok_len, "tokens": toks})
    if not sent_items:
        all_toks = enc_encode(text)
        yield {"window_index": 0, "text": text, "token_count": len(all_toks), "token_start": 0, "token_end": len(all_toks)}
        return
    for si in sent_items:
        si["token_start_idx"] = token_cursor
        si["token_end_idx"] = token_cursor + si["token_len"]
        token_cursor = si["token_end_idx"]
    windows = []
    i = 0
    window_index = 0
    while i < len(sent_items):
        cur_token_count = 0
        chunk_sent_texts = []
        chunk_token_start = sent_items[i]["token_start_idx"]
        chunk_token_end = chunk_token_start
        is_truncated_sentence = False
        start_i = i
        while i < len(sent_items):
            sent = sent_items[i]
            sent_tok_len = sent["token_len"]
            if cur_token_count + sent_tok_len > max_tokens:
                if not chunk_sent_texts:
                    if sent_tok_len > 0:
                        if enc_backend == "tiktoken":
                            prefix_tok_ids = sent["tokens"][:max_tokens]
                            prefix_text = enc_decode(prefix_tok_ids)
                            chunk_sent_texts.append(prefix_text)
                            cur_token_count = len(prefix_tok_ids)
                            is_truncated_sentence = True
                            remainder_tok_ids = sent["tokens"][max_tokens:]
                            if remainder_tok_ids:
                                remainder_text = enc_decode(remainder_tok_ids)
                                sent_items[i] = {"text": remainder_text, "start_char": None, "end_char": None, "token_len": len(remainder_tok_ids), "tokens": remainder_tok_ids, "token_start_idx": None, "token_end_idx": None}
                            else:
                                i += 1
                            chunk_token_end = chunk_token_start + cur_token_count
                            break
                        else:
                            tokens = sent["tokens"]
                            prefix = tokens[:max_tokens]
                            prefix_text = " ".join(prefix)
                            chunk_sent_texts.append(prefix_text)
                            cur_token_count = len(prefix)
                            is_truncated_sentence = True
                            remainder = tokens[max_tokens:]
                            if remainder:
                                remainder_text = " ".join(remainder)
                                sent_items[i] = {"text": remainder_text, "start_char": None, "end_char": None, "token_len": len(remainder), "tokens": remainder, "token_start_idx": None, "token_end_idx": None}
                            else:
                                i += 1
                            chunk_token_end = chunk_token_start + cur_token_count
                            break
                    else:
                        i += 1
                        break
                else:
                    break
            else:
                chunk_sent_texts.append(sent["text"])
                cur_token_count += sent_tok_len
                chunk_token_end = sent.get("token_end_idx", chunk_token_start + cur_token_count)
                i += 1
        if not chunk_sent_texts:
            i += 1
            continue
        chunk_text = " ".join(chunk_sent_texts).strip()
        chunk_meta = {"window_index": window_index, "text": chunk_text, "token_count": cur_token_count, "token_start": chunk_token_start, "token_end": chunk_token_end, "start_sentence_idx": start_i, "end_sentence_idx": i, "is_truncated_sentence": is_truncated_sentence}
        window_index += 1
        new_start = max(start_i + 1, chunk_meta["end_sentence_idx"] - overlap_sentences)
        if windows and chunk_meta["token_count"] < min_tokens:
            prev = windows[-1]
            prev["text"] = prev["text"] + " " + chunk_meta["text"]
            prev["token_count"] = prev["token_count"] + chunk_meta["token_count"]
            prev["token_end"] = chunk_meta["token_end"]
            prev["end_sentence_idx"] = chunk_meta["end_sentence_idx"]
            prev["is_truncated_sentence"] = prev.get("is_truncated_sentence", False) or chunk_meta.get("is_truncated_sentence", False)
        else:
            windows.append(chunk_meta)
        i = new_start
    for w in windows:
        yield w

# ---- s3 helpers -------------------------------------------------------------
def s3_object_exists(key: str) -> bool:
    s3 = _get_s3_client()
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False

def s3_upload_file_atomic(local_path: str, bucket: str, key: str, content_type: str = "application/octet-stream") -> None:
    tmp_key = f"{key}.tmp.{os.getpid()}.{int(time.time())}"
    retries = int(os.getenv("S3_PUT_RETRIES", "3"))
    backoff = float(os.getenv("S3_PUT_BACKOFF", "0.3"))
    s3 = _get_s3_client()
    for attempt in range(1, retries + 1):
        try:
            s3.upload_file(local_path, bucket, tmp_key, ExtraArgs={"ContentType": content_type})
            copy_source = {"Bucket": bucket, "Key": tmp_key}
            s3.copy_object(CopySource=copy_source, Bucket=bucket, Key=key)
            s3.delete_object(Bucket=bucket, Key=tmp_key)
            return
        except Exception as e:
            log.warning("s3 upload attempt %d failed for %s: %s", attempt, key, e)
            time.sleep(backoff * attempt)
    raise Exception(f"s3 atomic upload failed for {key}")

# ---- semantic region computation (HTML logic) -------------------------------
def _html_semantic_region(token_start: int, total_tokens: int) -> str:
    """Determine semantic region based on token position in the document.
    
    Uses HTML parser's token-based logic:
      - intro  : first 10%
      - early  : 10-30%
      - middle : 30-70%
      - late   : 70-90%
      - footer : last 10%
    """
    if total_tokens <= 0:
        return "intro"
    ratio = token_start / total_tokens
    if ratio < 0.10:
        return "intro"
    if ratio < 0.30:
        return "early"
    if ratio < 0.70:
        return "middle"
    if ratio < 0.90:
        return "late"
    return "footer"

# ---- parquet writer (imports pyarrow at finalize time) ----------------------
class S3ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._rows: list[dict[str, Any]] = []

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        fields["document_id"] = payload.get("document_id") or ""
        fields["file_name"] = payload.get("file_name") or ""
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
        try:
            ts = payload.get("timestamp") or ""
            fields["timestamp"] = ts
        except Exception:
            fields["timestamp"] = ""
        fields["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        fields["used_ocr"] = bool(payload.get("used_ocr", False))
        try:
            tr = payload.get("token_range")
            if isinstance(tr, (list, tuple)) and len(tr) >= 2:
                fields["token_start"] = int(tr[0])
                fields["token_end"] = int(tr[1])
            else:
                fields["token_start"] = 0
                fields["token_end"] = 0
        except Exception:
            fields["token_start"] = 0
            fields["token_end"] = 0
        # Add semantic_region
        fields["semantic_region"] = payload.get("semantic_region") or ""
        return fields

    def write_payload(self, payload: dict[str, Any]) -> int:
        self._rows.append(self._normalize(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> tuple[int, str, str, int]:
        if not self._rows:
            return 0, "", "", 0
        # import pyarrow lazily
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception as e:
            raise RuntimeError("pyarrow is required to finalize parquet output") from e

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
            pa.field("token_start", pa.int64()),
            pa.field("token_end", pa.int64()),
            pa.field("timestamp", pa.string()),
            pa.field("parser_version", pa.string()),
            pa.field("used_ocr", pa.bool_()),
            pa.field("semantic_region", pa.string()),
        ])
        cols = {name: [] for name in [f.name for f in schema]}
        for r in self._rows:
            for name in cols:
                cols[name].append(r.get(name) if name in r else None)
        table = pa.Table.from_pydict(cols, schema=schema)
        existing_md = table.schema.metadata or {}
        new_md = dict(existing_md)
        new_md.update({
            b"schema_version": CHUNKED_SCHEMA_VERSION.encode("utf-8"),
            b"parser_version": PARSER_VERSION.encode("utf-8"),
            b"producer": b"html_trafilatura",
            b"created_at": datetime.utcnow().isoformat().encode("utf-8")
        })
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        with open(tmpfile.name, "rb") as fh:
            b = fh.read()
        sha = sha256_hex_bytes(b)
        size = os.path.getsize(tmpfile.name)
        parquet_key = out_basename + ".parquet"
        s3_upload_file_atomic(tmpfile.name, S3_BUCKET, S3_CHUNKED_PREFIX + parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(tmpfile.name)
        except Exception:
            pass
        return len(self._rows), S3_CHUNKED_PREFIX + parquet_key, sha, size

# ---- small helpers ----------------------------------------------------------
def sanitize_payload_for_raw_manifest(doc_id: str, s3_raw_key: str, chunked_s3_key: str, rows: int, sha: str, size: int) -> dict[str, Any]:
    return {
        "raw_key": s3_raw_key,
        "doc_id": doc_id,
        "chunked_key": chunked_s3_key,
        "rows": rows,
        "sha256": sha,
        "size_bytes": size,
        "schema_version": CHUNKED_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

def _derive_file_name_from_source(source: str | None, s3_key: str) -> str:
    if source:
        try:
            base = source.split("?")[0].rstrip("/")
            base_name = os.path.basename(base)
            if base_name:
                return base_name
        except Exception:
            pass
    return os.path.basename(s3_key)

def sanitize_payload_for_weaviate(payload: dict[str, Any]) -> None:
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

# ---- public parse_file API (validates runtime env & deps) --------------------
def parse_file(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """
    Parse s3_key (raw HTML or URL) and write chunked parquet to S3.
    Raises RuntimeError on missing runtime requirements (caught by router).
    Returns dict containing saved_chunks and total_parse_duration_ms.
    """
    start_all = time.perf_counter()

    # Validate runtime env
    if not S3_BUCKET:
        raise RuntimeError("S3_BUCKET environment variable must be set")

    # ensure deps & encoder/sentencizer are available
    _ensure_optional_deps()

    s3 = _get_s3_client()

    # HEAD the object (best-effort)
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.error("Could not head S3 object %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0}

    last_modified = head.get("LastModified", "")
    doc_id = manifest.get("file_hash") or sha256_hex_str(s3_key + str(last_modified or ""))
    out_basename = f"{doc_id}"
    raw_manifest_key = s3_key + ".manifest.json"

    # skip if already exists (behaviour preserved)
    if not FORCE_OVERWRITE and s3_object_exists(raw_manifest_key):
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.info("Skipping because raw manifest exists: %s", raw_manifest_key)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    if not FORCE_OVERWRITE and s3_object_exists(S3_CHUNKED_PREFIX + out_basename + ".parquet"):
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.info("Skipping because parquet exists: %s", out_basename + ".parquet")
        try:
            if not s3_object_exists(raw_manifest_key):
                head2 = s3.head_object(Bucket=S3_BUCKET, Key=S3_CHUNKED_PREFIX + out_basename + ".parquet")
                etag = head2.get("ETag", "")
                if isinstance(etag, str):
                    etag = etag.strip('"')
                size = head2.get("ContentLength", 0)
                raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, S3_CHUNKED_PREFIX + out_basename + ".parquet", 0, etag, size)
                s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
        except Exception:
            pass
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    # GET the object
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.error("Could not get S3 object %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0}

    raw_body = obj["Body"].read()
    try:
        raw_text = raw_body.decode("utf-8", errors="replace")
    except Exception:
        raw_text = raw_body.decode("latin-1", errors="replace")

    s3_path = f"s3://{S3_BUCKET}/{s3_key}"
    stripped = raw_text.strip()
    use_remote_fetch = False
    remote_url = None
    if stripped.startswith("http://") or stripped.startswith("https://"):
        use_remote_fetch = True
        remote_url = stripped.splitlines()[0].strip()

    if use_remote_fetch:
        try:
            html_text = fetch_html_with_retries(remote_url)
            source_url = remote_url
        except Exception:
            html_text = raw_text
            source_url = s3_path
    else:
        html_text = raw_text
        source_url = s3_path

    # optionally snapshot
    try:
        _ = upload_snapshot_to_s3(html_text, doc_id)
    except Exception:
        pass

    # extract markdown via trafilatura (if available)
    t0 = time.perf_counter()
    md, parsed = trafilatura_extract_markdown(html_text)
    extract_duration_ms = int((time.perf_counter() - t0) * 1000)
    if not md:
        fallback = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', html_text)
        fallback = re.sub(r'(?is)<.*?>', ' ', fallback)
        md = re.sub(r'\s+', ' ', fallback).strip()
    title = parsed.get("title") if isinstance(parsed, dict) else None
    canonical_full = canonicalize_text(md or "")
    # ensure encoder functions are available
    enc_encode = _ENCODER_ENCODE if _ENCODER_ENCODE is not None else (lambda txt: txt.split())
    token_ct = len(enc_encode(canonical_full)) if canonical_full else 0

    saved = 0
    writer = S3ParquetWriter(doc_id=doc_id)
    file_name = _derive_file_name_from_source(source_url, s3_key)

    try:
        windows = list(split_into_token_windows(canonical_full))
        if len(windows) <= 1:
            chunk_index = 1
            chunk_id = f"{doc_id}_{chunk_index}"
            semantic_region = _html_semantic_region(0, token_ct)
            payload = {
                "document_id": doc_id,
                "file_name": file_name,
                "chunk_id": chunk_id,
                "chunk_type": "page",
                "text": canonical_full,
                "token_count": int(token_ct),
                "figures": "[]",
                "embedding": None,
                "file_type": "text/html",
                "source_url": source_url,
                "token_range": [0, token_ct],
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "parser_version": PARSER_VERSION,
                "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                "layout_tags": ["page"],
                "used_ocr": False,
                "heading_path": [],
                "headings": [title] if title else [],
                "line_range": None,
                "semantic_region": semantic_region,
            }
            sanitize_payload_for_weaviate(payload)
            writer.write_payload(payload)
            saved += 1
        else:
            for w in windows:
                window_idx = int(w.get("window_index", 0))
                chunk_index = window_idx + 1
                chunk_id = f"{doc_id}_{chunk_index}"
                wtext = w.get("text", "")
                w_token_count = int(w.get("token_count", 0))
                token_range = [int(w.get("token_start", 0)), int(w.get("token_end", 0))]
                semantic_region = _html_semantic_region(token_range[0], token_ct)
                payload = {
                    "document_id": doc_id,
                    "file_name": file_name,
                    "chunk_id": chunk_id,
                    "chunk_type": "token_window",
                    "text": wtext,
                    "token_count": int(w_token_count),
                    "figures": "[]",
                    "embedding": None,
                    "file_type": "text/html",
                    "source_url": source_url,
                    "token_range": token_range,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "parser_version": PARSER_VERSION,
                    "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                    "layout_tags": ["page"],
                    "used_ocr": False,
                    "heading_path": [],
                    "headings": [title] if title else [],
                    "line_range": None,
                    "semantic_region": semantic_region,
                }
                sanitize_payload_for_weaviate(payload)
                writer.write_payload(payload)
                saved += 1
    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.exception("Error while buffering chunks for %s: %s", s3_key, str(e))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

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

# ---- CLI runner (kept in __main__ only) ------------------------------------
if __name__ == "__main__":
    if not S3_BUCKET:
        log.error("S3_BUCKET environment variable not set. Exiting.")
        sys.exit(1)
    try:
        _ensure_optional_deps()
        s3 = _get_s3_client()
    except Exception as e:
        log.error("Runtime initialization failed: %s", e)
        sys.exit(1)

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not (key.lower().endswith(".html") or key.lower().endswith(".htm")):
                continue
            log.info("Routing parse_file for s3://%s/%s", S3_BUCKET, key)
            manifest_key = key + ".manifest.json"
            try:
                mf_obj = s3.get_object(Bucket=S3_BUCKET, Key=manifest_key)
                manifest = json.load(mf_obj["Body"])
            except Exception:
                manifest = {}
            try:
                res = parse_file(key, manifest)
                log.info("Result for %s: %s", key, res)
            except Exception as e:
                log.exception("Failed to parse %s: %s", key, e)