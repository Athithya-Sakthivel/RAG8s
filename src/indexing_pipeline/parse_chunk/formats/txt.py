#!/usr/bin/env python3
"""
Import-safe txt format module for parse_chunk.router.

Changes from the original:
- No sys.exit() or heavy work at import time.
- boto3/pyarrow imported lazily; missing optional deps produce warnings and
  raise only when parse_file actually needs them.
- Environment validation moved into parse_file().
- API compatibility preserved: `parse_file(s3_key, manifest) -> dict`.
"""
from __future__ import annotations
import os
import sys
import json
import time
import logging
import hashlib
import tempfile
import unicodedata
import re
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional, Generator, Tuple
from botocore.exceptions import ClientError

# ---------- logging (safe at import) ---------------------------------------
RESET = "\033[0m"
COLORS = {
    logging.DEBUG: "\033[90m",
    logging.INFO: "\033[37m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;41m",
}
class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = COLORS.get(record.levelno, RESET)
        message = super().format(record)
        return f"{color}{message}{RESET}"

log = logging.getLogger("txt_parser")
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
log.handlers[:] = [_handler]

# ---------- config (read, but do not validate here) ------------------------
# keep values readable at import; validation happens inside parse_file()
S3_BUCKET = os.getenv("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
PARSER_VERSION = os.getenv("PARSER_VERSION_TXT", "plain-txt-v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
S3_PUT_RETRIES = int(os.getenv("S3_PUT_RETRIES", "3"))
S3_PUT_BACKOFF = float(os.getenv("S3_PUT_BACKOFF", "0.3"))
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")

# ---------- optional deps (import lazily) ---------------------------------
_tiktoken = None
try:
    import tiktoken as _tiktoken  # type: ignore
except Exception:
    _tiktoken = None

# boto3 client (lazily created)
_s3_client = None
_s3_lock = threading.Lock()

def get_s3():
    global _s3_client
    if _s3_client is None:
        with _s3_lock:
            if _s3_client is None:
                try:
                    import boto3  # local import
                except Exception as e:
                    log.error("boto3 is required but failed to import: %s", e)
                    raise
                _s3_client = boto3.client("s3")
    return _s3_client

# pyarrow: optional; imported when writing parquet
def _import_pyarrow():
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
        return pa, pq
    except Exception:
        return None, None

# ---------- utilities -----------------------------------------------------
def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def canonicalize_text(s: str) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+$", "", ln) for ln in s.split("\n")]
    return "\n".join(lines).strip()

def try_decode_bytes(b: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return b.decode(encoding)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")

def token_count_for(text: str) -> int:
    if not text:
        return 0
    try:
        enc = None
        if _tiktoken is not None:
            try:
                enc = _tiktoken.get_encoding(ENC_NAME)
            except Exception:
                try:
                    enc = _tiktoken.encoding_for_model("gpt2")
                except Exception:
                    enc = None
        if enc is not None:
            try:
                return len(enc.encode(text))
            except Exception:
                pass
    except Exception:
        pass
    return len(text.split())

def s3_object_exists(key: str) -> bool:
    try:
        client = get_s3()
        client.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError:
        return False
    except Exception:
        return False

def s3_upload_file_atomic(local_path: str, bucket: str, key: str, content_type: str = "application/octet-stream") -> None:
    client = get_s3()
    tmp_key = f"{key}.tmp.{os.getpid()}.{int(time.time())}"
    for attempt in range(1, S3_PUT_RETRIES + 1):
        try:
            client.upload_file(local_path, bucket, tmp_key, ExtraArgs={"ContentType": content_type})
            copy_source = {"Bucket": bucket, "Key": tmp_key}
            client.copy_object(CopySource=copy_source, Bucket=bucket, Key=key)
            client.delete_object(Bucket=bucket, Key=tmp_key)
            return
        except Exception as e:
            log.warning("s3 atomic upload attempt %d failed for %s: %s", attempt, key, e)
            time.sleep(S3_PUT_BACKOFF * attempt)
    raise Exception(f"s3 atomic upload failed for {key} after {S3_PUT_RETRIES} attempts")

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

def sanitize_payload_for_raw_manifest(doc_id: str, s3_raw_key: str, chunked_s3_key: str, rows: int, sha: str, size: int) -> Dict[str, Any]:
    return {
        "raw_key": s3_raw_key,
        "doc_id": doc_id,
        "chunked_key": chunked_s3_key,
        "rows": rows,
        "sha256": sha,
        "size_bytes": size,
        "schema_version": CHUNKED_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

# ---------- NLP helpers ---------------------------------------------------
try:
    import spacy  # type: ignore
    from spacy.pipeline import Sentencizer  # type: ignore
except Exception:
    spacy = None
    Sentencizer = None

class TokenEncoder:
    def __init__(self, model_name: str = "gpt2", enc_name: Optional[str] = None):
        self.model_name = model_name
        self.enc_name = enc_name or os.getenv("TOKEN_ENCODER", ENC_NAME)
        self.backend = "whitespace"
        self._enc = None
        try:
            if _tiktoken is None:
                raise RuntimeError("tiktoken not installed")
            try:
                self._enc = _tiktoken.get_encoding(self.enc_name)
            except Exception:
                try:
                    self._enc = _tiktoken.encoding_for_model(self.model_name)
                except Exception:
                    self._enc = None
            if self._enc is not None:
                self.encode = lambda txt: self._enc.encode(txt)
                self.decode = lambda toks: self._enc.decode(toks)
                self.backend = "tiktoken"
                log.info("Using tiktoken encoder: enc_name=%s model=%s", self.enc_name, self.model_name)
            else:
                raise RuntimeError("tiktoken imported but no encoder available")
        except Exception as e:
            log.warning("tiktoken unavailable or failed to load encoder (%s); falling back to whitespace tokeniser", str(e))
            self.encode = lambda txt: txt.split()
            self.decode = lambda toks: " ".join(toks)

class SentenceChunker:
    def __init__(
        self,
        max_tokens_per_chunk: Optional[int] = None,
        overlap_sentences: Optional[int] = None,
        token_model: str = "gpt2",
        min_tokens_per_chunk: Optional[int] = None,
    ):
        self.max_tokens_per_chunk = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512")) if max_tokens_per_chunk is None else int(max_tokens_per_chunk)
        self.overlap_sentences = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2")) if overlap_sentences is None else int(overlap_sentences)
        if self.overlap_sentences < 0:
            raise ValueError("overlap_sentences must be >= 0")
        self.min_tokens_per_chunk = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100")) if min_tokens_per_chunk is None else int(min_tokens_per_chunk)
        if self.min_tokens_per_chunk < 0:
            raise ValueError("min_tokens_per_chunk must be >= 0")
        self.encoder = TokenEncoder(model_name=token_model, enc_name=os.getenv("TOKEN_ENCODER", ENC_NAME))
        self.nlp = self._make_sentencizer()
        log.info(
            "SentenceChunker initialized: max_tokens=%d,min_tokens=%d,overlap_sentences=%d,token_backend=%s",
            self.max_tokens_per_chunk,
            self.min_tokens_per_chunk,
            self.overlap_sentences,
            getattr(self.encoder, "backend", "unknown"),
        )

    def _make_sentencizer(self):
        if spacy is None:
            return None
        try:
            nlp = spacy.blank("en")
            try:
                nlp.add_pipe("sentencizer")
            except Exception:
                if Sentencizer is not None:
                    nlp.add_pipe(Sentencizer())
                else:
                    nlp.add_pipe("sentencizer")
            return nlp
        except Exception:
            log.warning("spaCy sentencizer unavailable")
            return None

    def _sentences_with_offsets_spacy(self, text: str):
        doc = self.nlp(text)
        return [(sent.text.strip(), int(sent.start_char), int(sent.end_char)) for sent in doc.sents if sent.text.strip()]

    def _sentences_with_offsets_regex(self, text: str):
        pattern = re.compile(r'(?s).*?[\.\!\?]["\']?\s+|.+$')
        items = []
        pos = 0
        for m in pattern.finditer(text):
            s = m.group(0)
            if not s or s.strip() == "":
                pos = m.end()
                continue
            start = pos
            end = pos + len(s)
            items.append((s.strip(), start, end))
            pos = m.end()
        if not items:
            if text.strip():
                items = [(text.strip(), 0, len(text))]
        return items

    def _sentences_with_offsets(self, text: str):
        if self.nlp is not None:
            try:
                return self._sentences_with_offsets_spacy(text)
            except Exception:
                pass
        return self._sentences_with_offsets_regex(text)

    def chunk_document(self, text: str) -> Generator[Dict, None, None]:
        sentences = self._sentences_with_offsets(text)
        sent_items = [{"text": s, "start_char": sc, "end_char": ec, "orig_idx": i, "is_remainder": False} for i, (s, sc, ec) in enumerate(sentences)]
        i = 0
        n = len(sent_items)
        prev_chunk = None
        while i < n:
            cur_token_count = 0
            chunk_sent_texts = []
            chunk_start_idx = i
            chunk_start_char = sent_items[i]["start_char"] if i < n else None
            chunk_end_char = None
            is_truncated_sentence = False
            while i < n:
                sent_text = sent_items[i]["text"]
                tok_ids = self.encoder.encode(sent_text)
                sent_tok_len = len(tok_ids)
                if cur_token_count + sent_tok_len > self.max_tokens_per_chunk:
                    if not chunk_sent_texts:
                        prefix_tok_ids = tok_ids[: self.max_tokens_per_chunk]
                        try:
                            prefix_text = self.encoder.decode(prefix_tok_ids)
                        except Exception:
                            prefix_text = " ".join(str(x) for x in prefix_tok_ids)
                        chunk_sent_texts.append(prefix_text)
                        cur_token_count = len(prefix_tok_ids)
                        is_truncated_sentence = True
                        remainder_tok_ids = tok_ids[self.max_tokens_per_chunk :]
                        if remainder_tok_ids:
                            try:
                                remainder_text = self.encoder.decode(remainder_tok_ids)
                            except Exception:
                                remainder_text = " ".join(str(x) for x in remainder_tok_ids)
                            sent_items[i] = {"text": remainder_text, "start_char": None, "end_char": None, "orig_idx": sent_items[i]["orig_idx"], "is_remainder": True}
                        else:
                            i += 1
                        break
                    else:
                        break
                else:
                    chunk_sent_texts.append(sent_text)
                    cur_token_count += sent_tok_len
                    chunk_end_char = sent_items[i]["end_char"]
                    i += 1
            if not chunk_sent_texts:
                log.warning("Empty chunk at idx %d; advancing one sentence to avoid infinite loop", i)
                i += 1
                continue
            chunk_text = " ".join(chunk_sent_texts).strip()
            chunk_meta = {
                "text": chunk_text,
                "token_count": cur_token_count,
                "start_sentence_idx": chunk_start_idx,
                "end_sentence_idx": i,
                "start_char": chunk_start_char,
                "end_char": chunk_end_char,
                "is_truncated_sentence": is_truncated_sentence,
            }
            new_start = max(chunk_start_idx + 1, chunk_meta["end_sentence_idx"] - self.overlap_sentences)
            if prev_chunk is None:
                prev_chunk = chunk_meta
            else:
                if chunk_meta["token_count"] < self.min_tokens_per_chunk:
                    prev_chunk["text"] = prev_chunk["text"] + " " + chunk_meta["text"]
                    prev_chunk["token_count"] = prev_chunk["token_count"] + chunk_meta["token_count"]
                    prev_chunk["end_sentence_idx"] = chunk_meta["end_sentence_idx"]
                    prev_chunk["end_char"] = chunk_meta["end_char"]
                    prev_chunk["is_truncated_sentence"] = prev_chunk["is_truncated_sentence"] or chunk_meta["is_truncated_sentence"]
                else:
                    yield prev_chunk
                    prev_chunk = chunk_meta
            i = new_start
            n = len(sent_items)
        if prev_chunk is not None:
            yield prev_chunk

def _charpos_to_line_range(start_char: Optional[int], end_char: Optional[int], lines: List[str]) -> Tuple[int, int]:
    if start_char is None or end_char is None or not lines:
        return 1, max(1, len(lines))
    cum = [0]
    for ln in lines:
        cum.append(cum[-1] + len(ln))
    start_line = 1
    end_line = len(lines)
    for i in range(len(cum) - 1):
        if cum[i] <= start_char < cum[i + 1]:
            start_line = i + 1
            break
    for j in range(len(cum) - 1, -1, -1):
        if j == 0:
            end_line = 1
            break
        if cum[j - 1] <= max(0, end_char - 1) < cum[j]:
            end_line = j
            break
    return start_line, end_line

def _derive_doc_id_from_head(s3_key: str, head_obj: dict, manifest: dict) -> str:
    if isinstance(manifest, dict) and manifest.get("file_hash"):
        return manifest.get("file_hash")
    etag = head_obj.get("ETag", "")
    if isinstance(etag, str):
        etag = etag.strip('"')
    if etag:
        return sha256_hex_str(s3_key + str(etag))
    lm = head_obj.get("LastModified", "")
    if lm:
        return sha256_hex_str(s3_key + str(lm))
    base = os.path.basename(s3_key)
    if base:
        return base
    return sha256_hex_str(s3_key)

# ---------- Parquet writer (lazy pyarrow import) --------------------------
class S3ParquetWriter:
    def __init__(self, doc_id: str, s3_path: str, content_type: str = "application/octet-stream"):
        self.doc_id = doc_id
        self.s3_path = s3_path or ""
        self.content_type = content_type
        self._rows: List[Dict[str, Any]] = []

    def _normalize_for_parquet(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
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
        fields["audio_range"] = json.dumps(payload.get("audio_range")) if payload.get("audio_range") is not None else ""
        fields["timestamp"] = payload.get("timestamp") or ""
        fields["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        fields["used_ocr"] = bool(payload.get("used_ocr", False))
        lr = payload.get("line_range") or []
        if isinstance(lr, (list, tuple)) and len(lr) >= 2:
            try:
                fields["line_start"] = int(lr[0])
                fields["line_end"] = int(lr[1])
            except Exception:
                fields["line_start"] = 1
                fields["line_end"] = 1
        else:
            fields["line_start"] = 1
            fields["line_end"] = 1
        return fields

    def write_payload(self, payload: Dict[str, Any]) -> int:
        normalized = self._normalize_for_parquet(payload)
        self._rows.append(normalized)
        return 1

    def finalize_and_upload(self, out_basename: str) -> Tuple[int, str]:
        if not self._rows:
            return 0, ""
        pa, pq = _import_pyarrow()
        if pa is None or pq is None:
            raise RuntimeError("pyarrow is required to write parquet output; install pyarrow")
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
            ]
        )
        cols: Dict[str, List[Any]] = {name: [] for name in [f.name for f in schema]}
        for r in self._rows:
            for name in cols:
                cols[name].append(r.get(name) if name in r else None)
        table = pa.Table.from_pydict(cols, schema=schema)
        existing_md = table.schema.metadata or {}
        new_md = dict(existing_md)
        new_md.update(
            {
                b"schema_version": CHUNKED_SCHEMA_VERSION.encode("utf-8"),
                b"parser_version": PARSER_VERSION.encode("utf-8"),
                b"producer": b"txt_parser",
                b"created_at": datetime.utcnow().isoformat().encode("utf-8"),
            }
        )
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        local_parquet_path = tmpfile.name
        parquet_key = out_basename + ".parquet"
        with open(local_parquet_path, "rb") as fh:
            b = fh.read()
        sha = sha256_hex_bytes(b)
        size = os.path.getsize(local_parquet_path)
        s3_upload_file_atomic(local_parquet_path, S3_BUCKET, S3_CHUNKED_PREFIX + parquet_key, content_type=self.content_type)
        try:
            os.unlink(local_parquet_path)
        except Exception:
            pass
        return len(self._rows), S3_CHUNKED_PREFIX + parquet_key

# ---------- main parse_file API -------------------------------------------
def parse_file(s3_key: str, manifest: dict) -> dict:
    """
    Validate runtime environment, read S3 object, chunk text and write parquet
    chunks and raw manifest. Raises only on runtime errors; router will catch.
    """
    # runtime validation (fail here rather than at import-time)
    if not S3_BUCKET:
        raise RuntimeError("S3_BUCKET must be set")
    start_all = time.perf_counter()
    client = get_s3()
    try:
        head_obj = client.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.warning("HEAD failed for %s: %s -- will try GET", s3_key, e)
        head_obj = {}

    doc_id = _derive_doc_id_from_head(s3_key, head_obj or {}, manifest or {})
    out_basename = f"{doc_id}"
    out_parquet_key = f"{out_basename}.parquet"
    raw_manifest_key = s3_key + ".manifest.json"

    if not FORCE_OVERWRITE:
        if s3_object_exists(raw_manifest_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("Skipping because raw manifest exists: %s", raw_manifest_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
        if s3_object_exists(S3_CHUNKED_PREFIX + out_parquet_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("Skipping because parquet chunked file exists: %s", out_parquet_key)
            # attempt to create raw manifest if missing (best-effort)
            try:
                if not s3_object_exists(raw_manifest_key):
                    head = client.head_object(Bucket=S3_BUCKET, Key=S3_CHUNKED_PREFIX + out_parquet_key)
                    etag = head.get("ETag", "")
                    if isinstance(etag, str):
                        etag = etag.strip('"')
                    size = head.get("ContentLength", 0)
                    raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, S3_CHUNKED_PREFIX + out_parquet_key, 0, etag, size)
                    client.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
            except Exception:
                pass
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    try:
        obj = client.get_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("Could not read S3 object %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

    raw_body = obj["Body"].read()
    raw_text = try_decode_bytes(raw_body)

    if isinstance(manifest, dict) and manifest.get("file_hash"):
        doc_id = manifest.get("file_hash")
        out_parquet_key = f"{doc_id}.parquet"

    if not FORCE_OVERWRITE:
        if s3_object_exists(S3_CHUNKED_PREFIX + out_parquet_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("Skipping because parquet chunked file exists (post-download): %s", out_parquet_key)
            # best-effort raw manifest
            try:
                if not s3_object_exists(raw_manifest_key):
                    head = client.head_object(Bucket=S3_BUCKET, Key=S3_CHUNKED_PREFIX + out_parquet_key)
                    etag = head.get("ETag", "")
                    if isinstance(etag, str):
                        etag = etag.strip('"')
                    size = head.get("ContentLength", 0)
                    raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, S3_CHUNKED_PREFIX + out_parquet_key, 0, etag, size)
                    client.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
            except Exception:
                pass
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    s3_path = f"s3://{S3_BUCKET}/{s3_key}"
    canonical_full = canonicalize_text(raw_text)
    lines = [ln + ("\n" if not ln.endswith("\n") else "") for ln in canonical_full.split("\n")]
    max_tokens = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512"))
    min_tokens = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100"))
    overlap_sentences = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2"))
    full_token_count = token_count_for(canonical_full)
    saved = 0
    chunk_index = 1
    writer = S3ParquetWriter(doc_id=doc_id, s3_path=s3_path)
    file_name = os.path.basename(s3_key)

    try:
        if full_token_count <= max_tokens:
            chunk_id = f"{doc_id}_{chunk_index}"
            chunk_index += 1
            payload = {
                "document_id": doc_id or "",
                "file_name": file_name,
                "chunk_id": chunk_id or "",
                "chunk_type": "txt_subchunk",
                "text": canonical_full or "",
                "token_count": int(full_token_count or 0),
                "figures": "[]",
                "embedding": None,
                "file_type": "text/plain",
                "source_url": s3_path,
                "page_number": None,
                "slide_range": None,
                "row_range": None,
                "token_range": None,
                "audio_range": None,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "parser_version": PARSER_VERSION,
                "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                "layout_tags": [],
                "used_ocr": False,
                "heading_path": [],
                "headings": [],
                "line_range": [1, len(lines)],
            }
            sanitize_payload_for_weaviate(payload)
            writer.write_payload(payload)
            log.info("Buffered single chunk %s", payload["chunk_id"])
            saved += 1
        else:
            chunker = SentenceChunker(max_tokens_per_chunk=max_tokens, overlap_sentences=overlap_sentences, token_model="gpt2", min_tokens_per_chunk=min_tokens)
            for sub in chunker.chunk_document(canonical_full):
                chunk_text = sub.get("text", "")
                token_ct = sub.get("token_count", 0)
                schar = sub.get("start_char")
                echar = sub.get("end_char")
                start_line, end_line = _charpos_to_line_range(schar, echar, lines)
                chunk_id = f"{doc_id}_{chunk_index}"
                chunk_index += 1
                payload = {
                    "document_id": doc_id or "",
                    "file_name": file_name,
                    "chunk_id": chunk_id or "",
                    "chunk_type": "txt_subchunk",
                    "text": chunk_text or "",
                    "token_count": int(token_ct or 0),
                    "figures": "[]",
                    "embedding": None,
                    "file_type": "text/plain",
                    "source_url": s3_path,
                    "page_number": None,
                    "slide_range": None,
                    "row_range": None,
                    "token_range": None,
                    "audio_range": None,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "parser_version": PARSER_VERSION,
                    "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                    "layout_tags": [],
                    "used_ocr": False,
                    "heading_path": [],
                    "headings": [],
                    "line_range": [int(start_line), int(end_line)],
                }
                writer.write_payload(payload)
                log.info("Buffered subchunk %s (lines %d-%d)", payload["chunk_id"], start_line, end_line)
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
        count, uploaded_s3_key = writer.finalize_and_upload(doc_id)
        total_ms = int((time.perf_counter() - start_all) * 1000)
        try:
            head = client.head_object(Bucket=S3_BUCKET, Key=uploaded_s3_key)
            etag = head.get("ETag", "")
            if isinstance(etag, str):
                etag = etag.strip('"')
            size = head.get("ContentLength", 0)
            raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, uploaded_s3_key, count, etag, size)
            client.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
        except Exception:
            log.warning("Failed to write raw manifest for %s", s3_key)
        log.info("Wrote %d chunks for %s → %s (%d ms)", count, s3_key, uploaded_s3_key, total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}
    except Exception as e_up:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("Failed to upload chunked file for %s error=%s", s3_key, str(e_up))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_up)}

# ---------- CLI entrypoint (unchanged behavior) ---------------------------
if __name__ == "__main__":
    log.info("TOKEN_ENCODER env: %s tiktoken_present: %s", os.getenv("TOKEN_ENCODER", ENC_NAME), "yes" if _tiktoken is not None else "no")
    client = get_s3()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_RAW_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.lower().endswith(".txt"):
                continue
            log.info("Routing parse_file for s3://%s/%s", S3_BUCKET, key)
            manifest_key = key + ".manifest.json"
            try:
                mf = client.get_object(Bucket=S3_BUCKET, Key=manifest_key)
                manifest = json.load(mf["Body"])
            except Exception:
                manifest = {}
            try:
                parse_file(key, manifest)
            except Exception as e:
                log.exception("Failed to parse %s: %s", key, e)
