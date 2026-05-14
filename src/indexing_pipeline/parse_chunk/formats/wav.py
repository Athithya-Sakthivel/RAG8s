#!/usr/bin/env python3
"""
wav.py — faster-whisper WAV/audio parser (import-safe, router-friendly)

Notes:
- Refactored to avoid exiting or performing heavy work at import time.
- Optional/optional-heavy deps (pyarrow, faster_whisper, soundfile, numpy, spacy)
  are loaded lazily or warned about. Runtime errors are raised from parse_file(),
  which router catches and logs.
- Keeps public API: `def parse_file(s3_key: str, manifest: dict) -> dict`
"""
from __future__ import annotations
import os
import sys
import json
import time
import logging
import hashlib
import tempfile
import shutil
import re
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

# logging
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

log = logging.getLogger("wav_parser_fw")
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(ColorFormatter("%(asctime)s %(levelname)s %(message)s"))
log.handlers[:] = [_h]

# config (read lazily where necessary)
S3_BUCKET = os.getenv("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
PARSER_VERSION = os.getenv("PARSER_VERSION_WAV", "faster-whisper-v1")

def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        log.warning("Invalid int for env %s=%r; using %d", name, v, default)
        return default

MAX_TOKENS_PER_CHUNK = _env_int("MAX_TOKENS_PER_CHUNK", 500)
MIN_TOKENS_PER_CHUNK = _env_int("MIN_TOKENS_PER_CHUNK", 100)
NUMBER_OF_OVERLAPPING_SENTENCES = _env_int("NUMBER_OF_OVERLAPPING_SENTENCES", 2)
TOKEN_OVERLAP = max(1, int(round(MAX_TOKENS_PER_CHUNK * 0.10))) if MAX_TOKENS_PER_CHUNK > 1 else 1
S3_RETRIES = _env_int("S3_RETRIES", 4)
S3_RETRY_BASE = float(os.getenv("S3_RETRY_BASE", "0.5"))
WHISPER_BEAM = _env_int("WHISPER_BEAM_SIZE", 1)
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FW_COMPUTE = os.getenv("FW_COMPUTE", "int8")
FW_CPU_THREADS = _env_int("FW_CPU_THREADS", max(1, (os.cpu_count() or 1) // 2))
WORKSPACE_MODELS = Path(os.getenv("WORKSPACE_MODELS", "/indexing_pipeline/models")).resolve()
FW_MODEL_PATH = WORKSPACE_MODELS / "faster_whisper" / "faster-whisper-base"
FW_MODEL_BIN = FW_MODEL_PATH / "model.bin"

# optional deps: imported lazily or attempted now with graceful fallback
_boto3 = None
_s3_client = None
try:
    import boto3 as _boto3
    try:
        _s3_client = _boto3.client("s3")
    except Exception:
        _s3_client = None
except Exception:
    log.warning("boto3 not available at import time; will attempt to create client when needed")
    _boto3 = None
    _s3_client = None

_pa = None
_pq = None
_HAS_PYARROW = False
try:
    import pyarrow as _pa
    import pyarrow.parquet as _pq
    _HAS_PYARROW = True
    log.info("pyarrow available: %s", getattr(_pa, "__version__", "unknown"))
except Exception:
    _HAS_PYARROW = False
    log.warning("pyarrow not available at import time; parquet outputs will fail unless pyarrow is installed")

_sf = None
_np = None
try:
    import soundfile as _sf
    import numpy as _np
except Exception:
    # warn but do not exit on import — conversion fallback via ffmpeg may still work
    if _sf is None:
        log.warning("soundfile (pysoundfile) not available at import time; attempted reads may fail")
    if _np is None:
        log.warning("numpy not available at import time; attempted reads may fail")

_tiktoken = None
try:
    import tiktoken as _tiktoken
    _tiktoken = _tiktoken
except Exception:
    _tiktoken = None

_WhisperModel = None
try:
    from faster_whisper import WhisperModel as _WhisperModel
except Exception:
    _WhisperModel = None
    log.info("faster_whisper not importable at import time; model load will fail until installed")

_spacy = None
_Sentencizer = None
try:
    import spacy as _spacy
    from spacy.pipeline import Sentencizer as _Sentencizer
    _spacy = _spacy
    _Sentencizer = _Sentencizer
except Exception:
    _spacy = None
    _Sentencizer = None

# helpers
def get_s3_client():
    global _s3_client, _boto3
    if _s3_client is not None:
        return _s3_client
    if _boto3 is None:
        try:
            import boto3 as _boto3_local
            _boto3 = _boto3_local
        except Exception as e:
            raise RuntimeError(f"boto3 not available: {e}")
    _s3_client = _boto3.client("s3")
    return _s3_client

def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def canonicalize_text(s: Any) -> str:
    if not isinstance(s, str):
        s = str(s or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join([re.sub(r"[ \t]+$", "", ln) for ln in s.split("\n")]).strip()

def retry_s3(fn, retries: int = S3_RETRIES, base: float = S3_RETRY_BASE):
    last = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i + 1 == retries:
                raise
            time.sleep(base * (2 ** i))
    raise last

def run_cmd(cmd: List[str], timeout: int = 60):
    import subprocess
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise RuntimeError("command timeout")
    if proc.returncode != 0:
        raise RuntimeError(err.strip() or out.strip())
    return out, err

def format_ts_ms(seconds: float) -> str:
    ms = int(round(max(0.0, float(seconds)) * 1000.0))
    s_total, msecs = divmod(ms, 1000)
    h, rem = divmod(s_total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{int(h):02d}:{int(mm):02d}:{int(ss):02d}.{int(msecs):03d}"

# audio helpers (lazy-dep checks)
def read_wav(path: str):
    global _sf, _np
    if _sf is None or _np is None:
        # try import on demand
        try:
            import soundfile as _sf_local
            import numpy as _np_local
            globals()["_sf"] = _sf_local
            globals()["_np"] = _np_local
            _sf = _sf_local
            _np = _np_local
        except Exception as e:
            raise RuntimeError(f"soundfile/numpy required to read WAV files: {e}")
    data, sr = _sf.read(path, dtype="float32")
    if getattr(data, "ndim", 1) > 1:
        data = _np.mean(data, axis=1)
    return data, sr

def get_tiktoken_encoder_safe():
    if _tiktoken is None:
        return (lambda txt: txt.split(), lambda toks: " ".join(toks), "whitespace")
    try:
        enc = None
        try:
            enc = _tiktoken.get_encoding(ENC_NAME)
        except Exception:
            try:
                if hasattr(_tiktoken, "encoding_for_model"):
                    enc = _tiktoken.encoding_for_model("gpt2")
            except Exception:
                enc = None
        if enc is not None:
            return (lambda txt: enc.encode(txt), lambda toks: enc.decode(toks), "tiktoken")
    except Exception:
        pass
    return (lambda txt: txt.split(), lambda toks: " ".join(toks), "whitespace")

# model loader (lazy)
def _load_model_if_needed():
    global _WhisperModel
    if globals().get("_model", None) is not None:
        return globals()["_model"]
    if _WhisperModel is None:
        raise RuntimeError("faster_whisper library not available")
    if not FW_MODEL_PATH.exists() or not FW_MODEL_BIN.exists():
        raise RuntimeError(f"Missing faster-whisper model at {FW_MODEL_PATH}")
    m = _WhisperModel(str(FW_MODEL_PATH), device="cpu", compute_type=FW_COMPUTE, cpu_threads=FW_CPU_THREADS)
    globals()["_model"] = m
    log.info("Loaded faster-whisper model from %s compute=%s cpu_threads=%d", FW_MODEL_PATH, FW_COMPUTE, FW_CPU_THREADS)
    return m

# sentence splitting / segmentation helpers (kept same logic)
def make_sentences_with_offsets(text: str) -> List[Tuple[str, int, int]]:
    if _spacy is not None:
        try:
            nlp = _spacy.blank("en")
            try:
                nlp.add_pipe("sentencizer")
            except Exception:
                if _Sentencizer is not None:
                    nlp.add_pipe(_Sentencizer())
                else:
                    nlp.add_pipe("sentencizer")
            doc = nlp(text)
            return [(sent.text.strip(), int(sent.start_char), int(sent.end_char)) for sent in doc.sents if sent.text.strip()]
        except Exception:
            pass
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
    if not items and text.strip():
        items = [(text.strip(), 0, len(text))]
    return items

def sentences_from_segments(segments: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for seg in segments:
        seg_text = getattr(seg, "text", "") or ""
        seg_start = getattr(seg, "start", None)
        seg_end = getattr(seg, "end", None)
        if seg_start is None or seg_end is None:
            continue
        seg_text_clean = seg_text.strip()
        if not seg_text_clean:
            continue
        seg_len = len(seg_text_clean)
        seg_duration = max(0.0, float(seg_end) - float(seg_start))
        sents = make_sentences_with_offsets(seg_text_clean)
        if not sents:
            out.append({"text": seg_text_clean, "start": float(seg_start), "end": float(seg_end)})
            continue
        for s_text, s_sc, s_ec in sents:
            rel_start = max(0, min(seg_len, s_sc))
            rel_end = max(0, min(seg_len, s_ec))
            if seg_len > 0:
                frac_start = rel_start / seg_len
                frac_end = rel_end / seg_len
            else:
                frac_start = 0.0
                frac_end = 1.0
            abs_start = float(seg_start) + frac_start * seg_duration
            abs_end = float(seg_start) + frac_end * seg_duration
            out.append({"text": s_text.strip(), "start": abs_start, "end": abs_end})
    return out

def split_long_sentence_by_tokens(sentence_text: str, sent_start: float, sent_end: float, max_tokens: int, encode_fn, decode_fn) -> List[Dict[str, Any]]:
    enc_tokens = encode_fn(sentence_text)
    if not enc_tokens:
        return []
    L = len(enc_tokens)
    duration = max(0.0, float(sent_end) - float(sent_start))
    out = []
    i = 0
    while i < L:
        j = min(i + max_tokens, L)
        slice_tokens = enc_tokens[i:j]
        start_frac = (i + 0.0) / L
        end_frac = (j - 1 + 0.5) / L
        abs_start = sent_start + start_frac * duration
        abs_end = sent_start + min(1.0, end_frac) * duration
        try:
            text = decode_fn(slice_tokens)
        except Exception:
            text = " ".join(str(x) for x in slice_tokens)
        out.append({"text": text, "start": float(abs_start), "end": float(abs_end), "token_count": len(slice_tokens)})
        if j == L:
            break
        i = j
    return out

def make_sentence_chunks_from_segments(segments: List[Any]) -> List[Dict[str, Any]]:
    encode_fn, decode_fn, _ = get_tiktoken_encoder_safe()
    max_tokens = _env_int("MAX_TOKENS_PER_CHUNK", MAX_TOKENS_PER_CHUNK)
    min_tokens = _env_int("MIN_TOKENS_PER_CHUNK", MIN_TOKENS_PER_CHUNK)
    overlap_sentences = _env_int("NUMBER_OF_OVERLAPPING_SENTENCES", NUMBER_OF_OVERLAPPING_SENTENCES)
    sentences = sentences_from_segments(segments)
    sent_items = []
    for i, s in enumerate(sentences):
        sent_items.append({"text": s.get("text", ""), "start": float(s.get("start", 0.0)), "end": float(s.get("end", 0.0)), "orig_idx": i, "is_remainder": False})
    out_chunks = []
    i = 0
    n = len(sent_items)
    prev_chunk = None
    while i < n:
        cur_token_count = 0
        chunk_sent_texts = []
        chunk_start_idx = i
        chunk_start_time = sent_items[i]["start"] if i < n else None
        chunk_end_time = None
        is_truncated_sentence = False
        while i < n:
            sent_text = sent_items[i]["text"]
            sent_start = sent_items[i]["start"]
            sent_end = sent_items[i]["end"]
            tok_ids = encode_fn(sent_text)
            sent_tok_len = len(tok_ids)
            if cur_token_count + sent_tok_len > max_tokens:
                if not chunk_sent_texts:
                    pieces = split_long_sentence_by_tokens(sent_text, sent_start, sent_end, max_tokens, encode_fn, decode_fn)
                    if not pieces:
                        i += 1
                        break
                    first = pieces[0]
                    chunk_sent_texts.append(first["text"])
                    cur_token_count += int(first.get("token_count", 0))
                    chunk_end_time = first["end"]
                    is_truncated_sentence = True
                    if len(pieces) > 1:
                        remainders = [{"text": p["text"], "start": p["start"], "end": p["end"], "orig_idx": sent_items[i]["orig_idx"], "is_remainder": True} for p in pieces[1:]]
                        sent_items[i] = {"text": "", "start": None, "end": None, "orig_idx": sent_items[i]["orig_idx"], "is_remainder": True}
                        sent_items[i + 1:i + 1] = remainders
                    else:
                        i += 1
                    break
                else:
                    break
            else:
                chunk_sent_texts.append(sent_text)
                cur_token_count += sent_tok_len
                chunk_end_time = sent_end
                i += 1
        if not chunk_sent_texts:
            log.warning("Empty chunk at idx %d; advancing to avoid infinite loop", i)
            i += 1
            continue
        chunk_text = " ".join(chunk_sent_texts).strip()
        chunk_meta = {"text": chunk_text, "token_count": cur_token_count, "start_time": chunk_start_time, "end_time": chunk_end_time, "start_sentence_idx": chunk_start_idx, "end_sentence_idx": i, "is_truncated_sentence": is_truncated_sentence}
        new_start = max(chunk_start_idx + 1, chunk_meta["end_sentence_idx"] - overlap_sentences)
        if prev_chunk is None:
            prev_chunk = chunk_meta
        else:
            if chunk_meta["token_count"] < min_tokens:
                prev_chunk["text"] = (prev_chunk["text"] + " " + chunk_meta["text"]).strip()
                prev_chunk["token_count"] = prev_chunk["token_count"] + chunk_meta["token_count"]
                prev_chunk["end_sentence_idx"] = chunk_meta["end_sentence_idx"]
                prev_chunk["end_time"] = chunk_meta["end_time"]
                prev_chunk["is_truncated_sentence"] = prev_chunk["is_truncated_sentence"] or chunk_meta["is_truncated_sentence"]
            else:
                out_chunks.append(prev_chunk)
                prev_chunk = chunk_meta
        i = new_start
        n = len(sent_items)
    if prev_chunk is not None:
        out_chunks.append(prev_chunk)
    formatted = []
    for c in out_chunks:
        st = c.get("start_time", 0.0) or 0.0
        ed = c.get("end_time", 0.0) or 0.0
        formatted.append({"text": c.get("text", "") or "", "token_count": int(c.get("token_count", 0) or 0), "audio_range": [float(st), float(ed)], "parse_ms": 0})
    return formatted

# s3 helpers
def s3_object_exists(key: str) -> bool:
    s3 = get_s3_client()
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False

def s3_upload_file_atomic(local_path: str, bucket: str, key: str, content_type: str = "application/octet-stream") -> None:
    tmp_key = f"{key}.tmp.{os.getpid()}.{int(time.time())}"
    retries = _env_int("S3_PUT_RETRIES", 3)
    backoff = float(os.getenv("S3_PUT_BACKOFF", "0.3"))
    s3 = get_s3_client()
    for attempt in range(1, retries + 1):
        try:
            s3.upload_file(local_path, bucket, tmp_key, ExtraArgs={"ContentType": content_type})
            copy_source = {"Bucket": bucket, "Key": tmp_key}
            s3.copy_object(CopySource=copy_source, Bucket=bucket, Key=key)
            s3.delete_object(Bucket=bucket, Key=tmp_key)
            return
        except Exception as e:
            log.warning("s3 atomic upload attempt %d failed for %s: %s", attempt, key, e)
            time.sleep(backoff * attempt)
    raise Exception(f"s3 atomic upload failed for {key} after {retries} attempts")

# parquet writer — lazy-imports pyarrow inside finalize
class S3ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._rows: List[Dict[str, Any]] = []

    def _normalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        f = {}
        f["document_id"] = payload.get("document_id") or ""
        f["file_name"] = payload.get("file_name") or ""
        f["chunk_id"] = payload.get("chunk_id") or ""
        f["chunk_type"] = payload.get("chunk_type") or ""
        f["text"] = payload.get("text") or ""
        try:
            f["token_count"] = int(payload.get("token_count") or 0)
        except Exception:
            f["token_count"] = 0
        for k in ("figures", "tags", "layout_tags", "heading_path", "headings"):
            v = payload.get(k, None)
            try:
                f[k] = json.dumps(v, ensure_ascii=False, sort_keys=True) if v is not None else "[]"
            except Exception:
                f[k] = "[]"
        f["file_type"] = payload.get("file_type") or ""
        f["source_url"] = payload.get("source_url") or ""
        ar = payload.get("audio_range") or [None, None]
        try:
            f["audio_start"] = float(ar[0]) if ar and ar[0] is not None else 0.0
            f["audio_end"] = float(ar[1]) if ar and ar[1] is not None else 0.0
        except Exception:
            f["audio_start"] = 0.0
            f["audio_end"] = 0.0
        f["timestamp"] = payload.get("timestamp") or ""
        f["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        f["used_ocr"] = bool(payload.get("used_ocr", False))
        try:
            f["parse_ms"] = int(payload.get("parse_ms", 0) or 0)
        except Exception:
            f["parse_ms"] = 0
        return f

    def write_payload(self, payload: Dict[str, Any]) -> int:
        self._rows.append(self._normalize(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> Tuple[int, str, str, int]:
        if not self._rows:
            return 0, "", "", 0
        # lazy import pyarrow here so missing pyarrow is surfaced at runtime only
        try:
            import pyarrow as pa_local
            import pyarrow.parquet as pq_local
        except Exception as e:
            raise RuntimeError(f"pyarrow required to write parquet: {e}")
        schema = pa_local.schema([
            pa_local.field("document_id", pa_local.string()),
            pa_local.field("file_name", pa_local.string()),
            pa_local.field("chunk_id", pa_local.string()),
            pa_local.field("chunk_type", pa_local.string()),
            pa_local.field("text", pa_local.string()),
            pa_local.field("token_count", pa_local.int64()),
            pa_local.field("figures", pa_local.string()),
            pa_local.field("tags", pa_local.string()),
            pa_local.field("layout_tags", pa_local.string()),
            pa_local.field("heading_path", pa_local.string()),
            pa_local.field("headings", pa_local.string()),
            pa_local.field("file_type", pa_local.string()),
            pa_local.field("source_url", pa_local.string()),
            pa_local.field("audio_start", pa_local.float64()),
            pa_local.field("audio_end", pa_local.float64()),
            pa_local.field("timestamp", pa_local.string()),
            pa_local.field("parser_version", pa_local.string()),
            pa_local.field("used_ocr", pa_local.bool_()),
            pa_local.field("parse_ms", pa_local.int64()),
        ])
        cols = {name: [] for name in [f.name for f in schema]}
        for r in self._rows:
            for name in cols:
                cols[name].append(r.get(name) if name in r else None)
        table = pa_local.Table.from_pydict(cols, schema=schema)
        existing_md = table.schema.metadata or {}
        new_md = dict(existing_md)
        new_md.update({
            b"schema_version": os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1").encode("utf-8"),
            b"parser_version": PARSER_VERSION.encode("utf-8"),
            b"producer": b"wav_parser_fw",
            b"created_at": datetime.utcnow().isoformat().encode("utf-8"),
        })
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq_local.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
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
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

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
    base = Path(s3_key).name
    if base:
        return base
    return sha256_hex_str(s3_key)

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

# core parsing using faster-whisper
def parse_file_with_fw(s3_key: str, manifest: dict) -> dict:
    start_all = time.perf_counter()
    s3 = get_s3_client()
    try:
        head_obj = s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        log.exception("HEAD failed for %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0}
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
            log.info("Skipping because parquet exists: %s", out_parquet_key)
            try:
                if not s3_object_exists(raw_manifest_key):
                    head = s3.head_object(Bucket=S3_BUCKET, Key=S3_CHUNKED_PREFIX + out_parquet_key)
                    etag = head.get("ETag", "")
                    etag = etag.strip('"') if isinstance(etag, str) else etag
                    size = head.get("ContentLength", 0)
                    raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, S3_CHUNKED_PREFIX + out_parquet_key, 0, etag, size)
                    s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
            except Exception:
                pass
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    # load model lazily: surface clear error if model/faster_whisper not available
    try:
        model = _load_model_if_needed()
    except Exception as e:
        log.exception("Model load failed: %s", e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0, "skipped": True, "error": "model-load-failed"}

    # download object
    try:
        obj = retry_s3(lambda: s3.get_object(Bucket=S3_BUCKET, Key=s3_key))
        body = obj["Body"].read()
    except Exception as e:
        log.exception("Failed to get s3 object %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0}

    tmp_dir = Path(tempfile.mkdtemp(prefix="wavproc_fw_"))
    tmp_wav = tmp_dir / Path(s3_key).name
    with open(tmp_wav, "wb") as f:
        f.write(body)

    # try to read raw wav; fall back to ffmpeg conversion if needed
    try:
        audio_array, sr = read_wav(str(tmp_wav))
    except Exception:
        try:
            conv_path = Path(str(tmp_wav) + ".conv.wav")
            run_cmd([FFMPEG, "-y", "-i", str(tmp_wav), "-ar", "16000", "-ac", "1", "-f", "wav", str(conv_path)], timeout=120)
            audio_array, sr = read_wav(str(conv_path))
            tmp_wav = conv_path
        except Exception as e:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.exception("Failed to read/convert audio %s: %s", s3_key, e)
            return {"saved_chunks": 0, "total_parse_duration_ms": 0}

    t0 = time.perf_counter()
    try:
        segments, info = model.transcribe(str(tmp_wav), beam_size=WHISPER_BEAM, vad_filter=False)
    except Exception as e:
        log.exception("Transcription failed for %s (%s): %s", s3_key, FW_COMPUTE, e)
        try:
            segments, info = model.transcribe(str(tmp_wav), beam_size=1, vad_filter=False)
        except Exception as e2:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.exception("Fallback transcription failed for %s: %s", s3_key, e2)
            return {"saved_chunks": 0, "total_parse_duration_ms": 0}
    parse_ms = int((time.perf_counter() - t0) * 1000)

    chunks = make_sentence_chunks_from_segments(segments)
    total_parse_ms = int(parse_ms)
    total_audio = 0.0
    for c in chunks:
        try:
            st = float(c["audio_range"][0])
            ed = float(c["audio_range"][1])
        except Exception:
            st = 0.0
            ed = 0.0
        dur = max(0.0, ed - st)
        c["_dur_for_weight"] = dur
        total_audio += dur

    if total_audio > 0.0:
        assigned_ms = 0
        for c in chunks:
            dur = c.get("_dur_for_weight", 0.0)
            c["parse_ms"] = int(round(total_parse_ms * (dur / total_audio))) if dur > 0 else 0
            assigned_ms += c["parse_ms"]
    else:
        total_tokens = sum(max(0, int(c.get("token_count", 0))) for c in chunks) or 1
        assigned_ms = 0
        for c in chunks:
            tc = max(0, int(c.get("token_count", 0)))
            c["parse_ms"] = int(round(total_parse_ms * (tc / total_tokens))) if tc > 0 else 0
            assigned_ms += c["parse_ms"]

    diff = total_parse_ms - assigned_ms
    if chunks:
        chunks[-1]["parse_ms"] = int(max(0, chunks[-1].get("parse_ms", 0) + diff))
    for c in chunks:
        c.pop("_dur_for_weight", None)

    saved = 0
    writer = S3ParquetWriter(doc_id=doc_id)
    file_name = manifest.get("file_name") if isinstance(manifest, dict) and manifest.get("file_name") else Path(s3_key).name

    try:
        for idx, c in enumerate(chunks):
            chunk_id = f"{doc_id}_{idx+1}"
            start_s, end_s = c.get("audio_range", [0.0, 0.0])
            audio_range = [float(start_s), float(end_s)]
            payload = {
                "document_id": doc_id or "",
                "file_name": file_name,
                "chunk_id": chunk_id or "",
                "chunk_type": "audio",
                "text": canonicalize_text(c.get("text", "") or ""),
                "token_count": int(c.get("token_count", 0) or 0),
                "figures": "[]",
                "embedding": None,
                "file_type": "audio/wav",
                "source_url": f"s3://{S3_BUCKET}/{s3_key}" if S3_BUCKET else None,
                "audio_range": audio_range,
                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "parser_version": PARSER_VERSION,
                "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                "layout_tags": [],
                "used_ocr": False,
                "heading_path": [],
                "headings": [],
                "parse_ms": int(c.get("parse_ms", 0)),
            }
            sanitize_payload_for_weaviate(payload)
            writer.write_payload(payload)
            saved += 1
            log.info("Buffered chunk %s audio_range=%s", chunk_id, audio_range)
    except Exception as e:
        try:
            # best-effort cleanup
            pass
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.exception("Error while buffering chunks for %s: %s", s3_key, str(e))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

    try:
        if saved == 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.info("No chunks produced for %s", s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms}
        count, uploaded_s3_key, sha, size = writer.finalize_and_upload(out_basename)
        total_ms = int((time.perf_counter() - start_all) * 1000)
        try:
            raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, uploaded_s3_key, count, sha, size)
            s3.put_object(Bucket=S3_BUCKET, Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
        except Exception:
            log.warning("Failed to write raw manifest for %s", s3_key)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.info("Wrote %d chunks for %s → %s (%d ms total)", count, s3_key, uploaded_s3_key, total_ms)
        return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}
    except Exception as e_up:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.error("Failed to upload chunked file for %s error=%s", s3_key, str(e_up))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e_up)}

def parse_file(s3_key: str, manifest: dict) -> dict:
    """
    Public API used by router. Returns dict with at least 'saved_chunks'.
    Any runtime validation errors are raised/caught here and translated to a
    returned dict so router can continue processing other files.
    """
    if not S3_BUCKET:
        log.error("S3_BUCKET not configured")
        return {"saved_chunks": 0, "total_parse_duration_ms": 0}
    try:
        return parse_file_with_fw(s3_key, manifest)
    except Exception:
        log.exception("Unhandled exception in parse_file for %s", s3_key)
        return {"saved_chunks": 0, "total_parse_duration_ms": 0}

__all__ = ["parse_file"]
