#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError


class LoggerShim:
    def __init__(self, name: str):
        self.name = name

    def _emit(self, level: str, event: str, msg: str = "", **extra):
        payload = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": level,
            "event": event,
            "msg": msg,
        }
        if extra:
            payload.update(extra)
        line = json.dumps(payload, ensure_ascii=False)
        if level in ("ERROR", "WARN"):
            print(line, file=sys.stderr, flush=True)
        else:
            print(line, flush=True)

    def _unpack(self, a, b, fmt_args, kwargs, default_event):
        if b is None:
            event = kwargs.pop("event", default_event)
            msg = a
        else:
            event = a
            msg = b
        if fmt_args:
            try:
                msg = msg % fmt_args
            except Exception:
                try:
                    msg = msg.format(*fmt_args)
                except Exception:
                    pass
        return event, msg, kwargs

    def info(self, a, b=None, *fmt_args, **kwargs):
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "info")
        self._emit("INFO", event, msg, **kw)

    def warning(self, a, b=None, *fmt_args, **kwargs):
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "warn")
        self._emit("WARN", event, msg, **kw)

    def warn(self, a, b=None, *fmt_args, **kwargs):
        self.warning(a, b, *fmt_args, **kwargs)

    def error(self, a, b=None, *fmt_args, **kwargs):
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "error")
        self._emit("ERROR", event, msg, **kw)

    def exception(self, a, b=None, *fmt_args, **kwargs):
        tb = traceback.format_exc()
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "exception")
        kw.update({"traceback": tb})
        self._emit("ERROR", event, msg, **kw)


log = LoggerShim("html_trafilatura")

DATA_S3_BUCKET = (os.getenv("DATA_S3_BUCKET") or os.getenv("S3_BUCKET") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION") or "").strip()

if not DATA_S3_BUCKET:
    log.error("startup_missing_bucket", "DATA_S3_BUCKET must be set")
    raise SystemExit(2)

if not AWS_REGION:
    log.error("startup_missing_region", "AWS_REGION must be set")
    raise SystemExit(2)

STORAGE_RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or os.getenv("S3_RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
STORAGE_CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or os.getenv("S3_CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"
PARSER_VERSION = os.getenv("PARSER_VERSION_HTML", "trafilatura-only-v2")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
SAVE_SNAPSHOT = os.getenv("SAVE_SNAPSHOT", "false").lower() == "true"
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15") or 15)
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3") or 3)
FETCH_BACKOFF = float(os.getenv("FETCH_BACKOFF", "0.5") or 0.5)

try:
    MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512"))
except Exception:
    MAX_TOKENS_PER_CHUNK = 512

try:
    MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100"))
except Exception:
    MIN_TOKENS_PER_CHUNK = 100

try:
    NUMBER_OF_OVERLAPPING_SENTENCES = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2"))
except Exception:
    NUMBER_OF_OVERLAPPING_SENTENCES = 2

CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")

try:
    PUT_RETRIES = int(os.getenv("PUT_RETRIES", "3"))
except Exception:
    PUT_RETRIES = 3

try:
    PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3"))
except Exception:
    PUT_BACKOFF = 0.3


_requests = None
_trafilatura = None
_tiktoken = None
_ENCODER = None
_ENCODER_ENCODE = None
_ENCODER_DECODE = None
_ENCODER_BACKEND = "whitespace"
_spacy = None
_Sentencizer = None
_NLP_SENTENCIZER = None


def retry_call(fn, retries: int = 3, backoff_base: float = 0.5, allowed_exceptions: tuple = (Exception,)):
    attempt = 0
    last = None
    while attempt < retries:
        attempt += 1
        try:
            return fn()
        except allowed_exceptions as e:
            last = e
            if attempt >= retries:
                raise
            sleep = backoff_base * (2 ** (attempt - 1))
            time.sleep(sleep)
    raise last


def full_path_from_key(key: str) -> str:
    return f"s3://{DATA_S3_BUCKET.rstrip('/')}/" + key.lstrip("/")


def strip_root_from_path(full: str) -> str:
    root = f"s3://{DATA_S3_BUCKET.rstrip('/')}/"
    if full.startswith(root):
        return full[len(root):]
    if full.startswith("s3://"):
        rest = full[len("s3://"):]
        bucket_prefix = DATA_S3_BUCKET.rstrip("/") + "/"
        if rest.startswith(bucket_prefix):
            return rest[len(bucket_prefix):]
        if rest == DATA_S3_BUCKET.rstrip("/"):
            return ""
    if full.startswith(DATA_S3_BUCKET.rstrip("/") + "/"):
        return full[len(DATA_S3_BUCKET.rstrip("/")) + 1:]
    return full


class S3StorageClient:
    def __init__(self, s3_client, bucket: str, root: str):
        self.s3 = s3_client
        self.bucket = bucket
        self.root = root

    def head_object(self, Bucket, Key):
        resp = self.s3.head_object(Bucket=self.bucket, Key=Key)
        return {
            "ContentLength": int(resp.get("ContentLength", 0) or 0),
            "ETag": (resp.get("ETag") or "").strip('"'),
            "LastModified": resp.get("LastModified", ""),
            "Metadata": resp.get("Metadata", {}) or {},
        }

    def get_object(self, Bucket, Key):
        resp = self.s3.get_object(Bucket=self.bucket, Key=Key)
        return {"Body": resp["Body"]}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        extra = {"ContentType": ContentType} if ContentType else {}
        if isinstance(Body, (bytes, bytearray)):
            data = Body
        elif isinstance(Body, str):
            data = Body.encode("utf-8")
        elif hasattr(Body, "read"):
            data = Body.read()
            if isinstance(data, str):
                data = data.encode("utf-8")
        else:
            data = str(Body).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=Key, Body=data, **extra)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_file(self, LocalFile, Bucket, Key, ExtraArgs=None):
        extra = ExtraArgs or {}
        self.s3.upload_file(LocalFile, self.bucket, Key, ExtraArgs=extra)

    def delete_object(self, Bucket, Key):
        self.s3.delete_object(Bucket=self.bucket, Key=Key)

    def exists(self, full_path: str) -> bool:
        key = strip_root_from_path(full_path)
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = str((e.response or {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound", "NotFoundException"}:
                return False
            return False
        except Exception:
            return False

    def get_paginator(self, name):
        paginator = self.s3.get_paginator(name)

        class P:
            def __init__(self, inner, bucket):
                self.inner = inner
                self.bucket = bucket

            def paginate(self, Bucket, Prefix, PaginationConfig=None):
                kwargs = {"Bucket": self.bucket, "Prefix": Prefix}
                if PaginationConfig:
                    kwargs["PaginationConfig"] = PaginationConfig
                # use yield from to forward pages directly
                yield from self.inner.paginate(**kwargs)

        return P(paginator, self.bucket)


_storage_client = None
_storage_lock = threading.Lock()


def get_storage_client_singleton():
    global _storage_client
    if _storage_client is None:
        with _storage_lock:
            if _storage_client is None:
                session = boto3.session.Session(region_name=AWS_REGION)
                s3 = session.client("s3")
                _storage_client = S3StorageClient(
                    s3_client=s3,
                    bucket=DATA_S3_BUCKET,
                    root=f"s3://{DATA_S3_BUCKET.rstrip('/')}/",
                )
    return _storage_client


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


def _ensure_optional_deps():
    global _requests, _trafilatura, _tiktoken, _ENCODER, _ENCODER_ENCODE, _ENCODER_DECODE, _ENCODER_BACKEND, _spacy, _Sentencizer, _NLP_SENTENCIZER
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
                _ENCODER = _tiktoken.get_encoding(ENC_NAME)
            except Exception:
                try:
                    _ENCODER = _tiktoken.encoding_for_model("gpt2")
                except Exception:
                    _ENCODER = None
        except Exception:
            _tiktoken = None
            _ENCODER = None

    if _ENCODER is not None:
        def _encoder_encode(txt: str):
            return _ENCODER.encode(txt)

        def _encoder_decode(toks):
            return _ENCODER.decode(toks)

        _ENCODER_ENCODE = _encoder_encode
        _ENCODER_DECODE = _encoder_decode
        _ENCODER_BACKEND = "tiktoken"
        log.info("encoder_init", "tiktoken encoder loaded", backend=_ENCODER_BACKEND)
    else:
        def _encoder_encode_whitespace(txt: str):
            return txt.split()

        def _encoder_decode_whitespace(toks):
            return " ".join(toks)

        _ENCODER_ENCODE = _encoder_encode_whitespace
        _ENCODER_DECODE = _encoder_decode_whitespace
        _ENCODER_BACKEND = "whitespace"

    if _spacy is None:
        try:
            import spacy as _s
            from spacy.pipeline import Sentencizer as _S
            _spacy = _s
            _Sentencizer = _S
        except Exception:
            _spacy = None
            _Sentencizer = None


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
    client = get_storage_client_singleton()
    key = f"{STORAGE_CHUNKED_PREFIX}{doc_id}.snapshot.html"
    try:
        client.put_object(Bucket=DATA_S3_BUCKET, Key=key, Body=snapshot_html.encode("utf-8"), ContentType="text/html")
        return f"s3://{DATA_S3_BUCKET}/{key}"
    except Exception:
        return None


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
        def _encoder_encode_whitespace(txt: str):
            return txt.split()

        def _encoder_decode_whitespace(toks):
            return " ".join(toks)

        _ENCODER_ENCODE = _encoder_encode_whitespace
        _ENCODER_DECODE = _encoder_decode_whitespace
        _ENCODER_BACKEND = "whitespace"
    return _ENCODER_ENCODE, _ENCODER_DECODE, _ENCODER_BACKEND


def split_into_token_windows(
    text: str,
    max_tokens: int = MAX_TOKENS_PER_CHUNK,
    min_tokens: int = MIN_TOKENS_PER_CHUNK,
    overlap_sentences: int = NUMBER_OF_OVERLAPPING_SENTENCES,
) -> Iterator[dict[str, Any]]:
    if not text:
        yield {"window_index": 0, "text": "", "token_count": 0, "token_start": 0, "token_end": 0}
        return
    text = canonicalize_text(text)
    sentences = _sentences_with_offsets(text)
    enc_encode, enc_decode, enc_backend = _make_encoder_clients()
    sent_items = []
    token_cursor = 0
    for sent_text, sc, ec in sentences:
        toks = enc_encode(sent_text)
        tok_len = len(toks)
        sent_items.append({"text": sent_text, "start_char": sc, "end_char": ec, "token_len": tok_len, "tokens": toks})
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
                                sent_items[i] = {
                                    "text": remainder_text,
                                    "start_char": None,
                                    "end_char": None,
                                    "token_len": len(remainder_tok_ids),
                                    "tokens": remainder_tok_ids,
                                    "token_start_idx": None,
                                    "token_end_idx": None,
                                }
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
                                sent_items[i] = {
                                    "text": remainder_text,
                                    "start_char": None,
                                    "end_char": None,
                                    "token_len": len(remainder),
                                    "tokens": remainder,
                                    "token_start_idx": None,
                                    "token_end_idx": None,
                                }
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
        chunk_meta = {
            "window_index": window_index,
            "text": chunk_text,
            "token_count": cur_token_count,
            "token_start": chunk_token_start,
            "token_end": chunk_token_end,
            "start_sentence_idx": start_i,
            "end_sentence_idx": i,
            "is_truncated_sentence": is_truncated_sentence,
        }
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
    # forward windows using yield from for clarity
    yield from (w for w in windows)


def storage_object_exists(key: str) -> bool:
    full = full_path_from_key(key)
    client = get_storage_client_singleton()
    try:
        return client.exists(full)
    except Exception:
        return False


def storage_upload_file_atomic(local_path: str, key: str, content_type: str = "application/octet-stream") -> None:
    client = get_storage_client_singleton()
    for attempt in range(1, PUT_RETRIES + 1):
        try:
            client.upload_file(local_path, DATA_S3_BUCKET, key, ExtraArgs={"ContentType": content_type})
            return
        except Exception as e:
            log.warning("upload_retry", "attempt=%d key=%s error=%s", attempt, key, str(e))
            time.sleep(PUT_BACKOFF * attempt)
    raise Exception(f"upload failed for {key} after {PUT_RETRIES} attempts")


def derive_html_semantic_region(token_start: int, token_end: int, document_total_tokens: int) -> str:
    try:
        if document_total_tokens is None:
            return "unknown"
        dt = int(document_total_tokens)
        if dt <= 0:
            return "unknown"
        ts = int(token_start) if token_start is not None else 0
        if ts < 0:
            return "unknown"
        ratio = float(ts) / float(dt)
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


class ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._rows: list[dict[str, Any]] = []

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        fields["document_id"] = payload.get("document_id") or ""
        fields["file_name"] = payload.get("file_name") or ""
        fields["raw_key"] = payload.get("raw_key") or ""
        fields["chunk_id"] = payload.get("chunk_id") or ""
        fields["chunk_type"] = payload.get("chunk_type") or ""
        fields["chunk_index"] = payload.get("chunk_index") or 0
        fields["text"] = payload.get("text") or ""
        try:
            fields["token_count"] = int(payload.get("token_count") or 0)
        except Exception:
            fields["token_count"] = 0
        for k in ("figures", "tags", "layout_tags", "heading_path", "headings", "line_range"):
            v = payload.get(k, None)
            try:
                fields[k] = json.dumps(v, ensure_ascii=False, sort_keys=True) if v is not None else "[]"
            except Exception:
                fields[k] = "[]"
        fields["file_type"] = payload.get("file_type") or ""
        fields["source_url"] = payload.get("source_url") or ""
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
        try:
            fields["document_total_tokens"] = int(payload.get("document_total_tokens") or 0)
        except Exception:
            fields["document_total_tokens"] = 0
        fields["semantic_region"] = payload.get("semantic_region") or "unknown"
        fields["timestamp"] = payload.get("timestamp") or ""
        fields["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        fields["used_ocr"] = bool(payload.get("used_ocr", False))
        try:
            om = payload.get("original_manifest")
            fields["original_manifest"] = json.dumps(om, ensure_ascii=False, sort_keys=True) if om is not None else ""
        except Exception:
            fields["original_manifest"] = ""
        return fields

    def write_payload(self, payload: dict[str, Any]) -> int:
        self._rows.append(self._normalize(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> tuple[int, str, str, int]:
        if not self._rows:
            return 0, "", "", 0
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception as e:
            raise RuntimeError("pyarrow required to write parquet") from e
        schema = pa.schema([
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
            pa.field("line_range", pa.string()),
            pa.field("file_type", pa.string()),
            pa.field("source_url", pa.string()),
            pa.field("token_start", pa.int64()),
            pa.field("token_end", pa.int64()),
            pa.field("document_total_tokens", pa.int64()),
            pa.field("semantic_region", pa.string()),
            pa.field("timestamp", pa.string()),
            pa.field("parser_version", pa.string()),
            pa.field("used_ocr", pa.bool_()),
            pa.field("original_manifest", pa.string()),
        ])
        cols = {name: [] for name in [f.name for f in schema]}
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
                b"producer": b"html_trafilatura",
                b"created_at": datetime.utcnow().isoformat().encode("utf-8"),
            }
        )
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        with open(tmpfile.name, "rb") as fh:
            b = fh.read()
        sha = sha256_hex_bytes(b)
        size = os.path.getsize(tmpfile.name)
        parquet_key = out_basename + ".parquet"
        storage_upload_file_atomic(tmpfile.name, STORAGE_CHUNKED_PREFIX + parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(tmpfile.name)
        except Exception:
            pass
        return len(self._rows), STORAGE_CHUNKED_PREFIX + parquet_key, sha, size


def sanitize_payload_for_raw_manifest(doc_id: str, raw_key: str, chunked_key: str, rows: int, sha: str, size: int) -> dict[str, Any]:
    return {
        "raw_key": raw_key,
        "doc_id": doc_id,
        "chunked_key": chunked_key,
        "rows": rows,
        "sha256": sha,
        "size_bytes": size,
        "schema_version": CHUNKED_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }


def _derive_file_name_from_source(source: str | None, raw_key: str) -> str:
    if source:
        try:
            base = source.split("?")[0].rstrip("/")
            base_name = os.path.basename(base)
            if base_name:
                return base_name
        except Exception:
            pass
    return os.path.basename(raw_key)


def parse_file(s3_key: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """
    Full parse pipeline:
    - read object from S3
    - extract markdown via trafilatura (if available)
    - canonicalize text
    - split into token windows
    - write parquet and upload
    - write raw manifest
    """
    start_all = time.perf_counter()
    try:
        _ensure_optional_deps()
    except Exception as e:
        log.error("deps_init_failed", "Optional deps init failed: %s", str(e))
    client = get_storage_client_singleton()
    try:
        head = client.head_object(Bucket=DATA_S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.error("head_failed", "Could not head object %s: %s", s3_key, str(e))
        return {"saved_chunks": 0, "total_parse_duration_ms": 0, "error": str(e)}
    last_modified = head.get("LastModified", "")
    try:
        doc_id = manifest.get("file_hash") or sha256_hex_str(s3_key + str(last_modified or ""))
    except Exception:
        doc_id = sha256_hex_str(s3_key + str(last_modified or ""))
    out_basename = f"{doc_id}"
    raw_manifest_key = s3_key + ".manifest.json"

    try:
        if not FORCE_OVERWRITE and storage_object_exists(raw_manifest_key):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("skip_manifest_exists", "raw_manifest_exists", key=raw_manifest_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
        if not FORCE_OVERWRITE and storage_object_exists(STORAGE_CHUNKED_PREFIX + out_basename + ".parquet"):
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("skip_parquet_exists", "parquet_exists", key=out_basename + ".parquet")
            try:
                if not storage_object_exists(raw_manifest_key):
                    head2 = client.head_object(Bucket=DATA_S3_BUCKET, Key=STORAGE_CHUNKED_PREFIX + out_basename + ".parquet")
                    etag2 = head2.get("ETag", "")
                    if isinstance(etag2, str):
                        etag2 = etag2.strip('"')
                    size = head2.get("ContentLength", 0)
                    raw_manifest = sanitize_payload_for_raw_manifest(
                        doc_id, s3_key, STORAGE_CHUNKED_PREFIX + out_basename + ".parquet", 0, etag2, size
                    )
                    client.put_object(
                        Bucket=DATA_S3_BUCKET,
                        Key=raw_manifest_key,
                        Body=json.dumps(raw_manifest).encode("utf-8"),
                        ContentType="application/json",
                    )
            except Exception:
                pass
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
    except Exception:
        pass

    # read object
    try:
        obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=s3_key)
    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("read_failed", "Could not read object", key=s3_key, error=str(e))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

    raw_body = obj.get("Body", b"")
    try:
        if isinstance(raw_body, (bytes, bytearray)):
            raw_text = raw_body.decode("utf-8", errors="replace")
        else:
            raw_text = raw_body.read().decode("utf-8", errors="replace")
    except Exception:
        try:
            raw_text = str(raw_body)
        except Exception:
            raw_text = ""

    if SAVE_SNAPSHOT:
        try:
            key = f"{STORAGE_CHUNKED_PREFIX}{doc_id}.snapshot.html"
            client.put_object(
                Bucket=DATA_S3_BUCKET,
                Key=key,
                Body=raw_text.encode("utf-8"),
                ContentType="text/html",
            )
        except Exception:
            pass

    # extract markdown via trafilatura if available
    md_text, parsed_meta = trafilatura_extract_markdown(raw_text)
    if md_text:
        canonical_full = canonicalize_text(md_text)
    else:
        canonical_full = canonicalize_text(raw_text)

    # compute windows
    windows = list(split_into_token_windows(canonical_full, MAX_TOKENS_PER_CHUNK, MIN_TOKENS_PER_CHUNK, NUMBER_OF_OVERLAPPING_SENTENCES))
    total_tokens = 0
    for w in windows:
        try:
            total_tokens = max(total_tokens, int(w.get("token_end", 0)))
        except Exception:
            pass

    if not windows:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.info("no_windows", "No windows produced", key=s3_key)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": False}

    # prepare writer
    writer = ParquetWriter(doc_id=doc_id)
    file_name = _derive_file_name_from_source(parsed_meta.get("source_url") if isinstance(parsed_meta, dict) else None, s3_key)

    saved = 0
    for idx, w in enumerate(windows):
        chunk_id = f"{doc_id}_{idx+1}"
        token_start = int(w.get("token_start", 0))
        token_end = int(w.get("token_end", 0))
        text_chunk = canonicalize_text(w.get("text", "") or "")
        payload = {
            "document_id": doc_id or "",
            "file_name": file_name,
            "raw_key": s3_key,
            "chunk_id": chunk_id or "",
            "chunk_type": "html_window",
            "chunk_index": idx + 1,
            "text": text_chunk,
            "token_count": int(w.get("token_count", 0)),
            "figures": [],
            "file_type": "text/html",
            "source_url": parsed_meta.get("source_url") if isinstance(parsed_meta, dict) else f"s3://{DATA_S3_BUCKET}/{s3_key}",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "parser_version": PARSER_VERSION or "",
            "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
            "layout_tags": [],
            "used_ocr": False,
            "heading_path": [],
            "headings": [],
            "line_range": [],
            "token_range": [token_start, token_end],
            "document_total_tokens": total_tokens,
            "semantic_region": derive_html_semantic_region(token_start, token_end, total_tokens),
            "original_manifest": manifest or {},
        }
        try:
            writer.write_payload(payload)
            saved += 1
        except Exception:
            log.exception("write_payload_failed", "Failed to buffer payload", chunk_id=chunk_id)

    # finalize and upload
    try:
        count, uploaded_key, sha, size = writer.finalize_and_upload(out_basename)
        total_ms = int((time.perf_counter() - start_all) * 1000)
        try:
            raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, uploaded_key, count, sha, size)
            client.put_object(
                Bucket=DATA_S3_BUCKET,
                Key=raw_manifest_key,
                Body=json.dumps(raw_manifest).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception:
            log.warning("manifest_write_failed", "Failed to write raw manifest", key=s3_key)
        log.info("write_complete", "Wrote chunks", count=count, raw=s3_key, chunked=uploaded_key, duration_ms=total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}
    except Exception as e_up:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("upload_failed", "Failed to upload chunked file", key=s3_key, error=str(e_up))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_up)}


def _ensure_cli_env_or_exit():
    missing = []
    if not DATA_S3_BUCKET:
        missing.append("DATA_S3_BUCKET")
    if not AWS_REGION:
        missing.append("AWS_REGION")
    if missing:
        print(f"ERROR: Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    log.info("startup", "Starting HTML -> parquet parser (CLI mode)", region=AWS_REGION, bucket=DATA_S3_BUCKET)
    _ensure_cli_env_or_exit()
    client = get_storage_client_singleton()
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=DATA_S3_BUCKET, Prefix=STORAGE_RAW_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not (key.lower().endswith(".html") or key.lower().endswith(".htm")):
                    continue
                log.info("cli_route", "routing parse_file", key=key)
                manifest_key = key + ".manifest.json"
                try:
                    mf_obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=manifest_key)
                    body = mf_obj.get("Body", b"")
                    if isinstance(body, (bytes, bytearray)):
                        manifest = json.loads(body.decode("utf-8"))
                    else:
                        manifest = json.loads(body.read())
                except Exception:
                    manifest = {}
                try:
                    result = parse_file(key, manifest)
                    log.info("cli_result", "Result", key=key, result=result)
                except Exception:
                    log.exception("cli_parse_failed", "Failed to parse", key=key)
    except Exception:
        log.exception("cli_loop_failed", "CLI pagination loop failed")
    sys.exit(0)
