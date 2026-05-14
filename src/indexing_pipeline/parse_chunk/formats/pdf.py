#!/usr/bin/env python3
from __future__ import annotations
import os
import io
import sys
import json
import time
import logging
import hashlib
import unicodedata
import re
import tempfile
import importlib
from datetime import datetime
from contextlib import contextmanager
from typing import List, Tuple, Dict, Generator, Optional, Any

# --- logger ---
logger = logging.getLogger("pdf_parser")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.handlers[:] = [handler]

# --- environment defaults (read-only at import; validated at runtime) ---
S3_BUCKET = os.getenv("DATA_S3_BUCKET")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/").rstrip("/") + "/"
S3_CHUNKED_PREFIX = os.getenv("S3_CHUNKED_PREFIX", "data/chunked/").rstrip("/") + "/"
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
PDF_DISABLE_OCR = os.getenv("PDF_DISABLE_OCR", "false").lower() == "true"
PDF_FORCE_OCR = os.getenv("PDF_FORCE_OCR", "false").lower() == "true"
PDF_OCR_ENGINE = os.getenv("PDF_OCR_ENGINE", "auto").lower()
PDF_OCR_STRICT = os.getenv("PDF_OCR_STRICT", "false").lower() == "true"
PDF_TESSERACT_LANG = os.getenv("PDF_TESSERACT_LANG", "eng")
PDF_OCR_RENDER_DPI = int(os.getenv("PDF_OCR_RENDER_DPI", "300"))
PDF_MIN_IMG_SIZE_BYTES = int(os.getenv("PDF_MIN_IMG_SIZE_BYTES", "3072"))
MAX_TOKENS_PER_CHUNK = int(os.getenv("MAX_TOKENS_PER_CHUNK", "512"))
MIN_TOKENS_PER_CHUNK = int(os.getenv("MIN_TOKENS_PER_CHUNK", "100"))
NUMBER_OF_OVERLAPPING_SENTENCES = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", "2"))
PARSER_VERSION_PDF = os.getenv("PARSER_VERSION_PDF", "pdf-v1")
S3_PUT_RETRIES = int(os.getenv("S3_PUT_RETRIES", "3"))
S3_PUT_BACKOFF = float(os.getenv("S3_PUT_BACKOFF", "0.3"))
ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")

# --- lazy clients / caches ---
_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        try:
            import boto3
        except Exception as e:
            logger.exception("boto3 is required but failed to import: %s", e)
            raise
        _s3_client = boto3.client("s3")
    return _s3_client


# --- small helpers ---
def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def local_file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


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
        import tiktoken as _tiktoken  # lazy
        try:
            enc = _tiktoken.get_encoding(ENC_NAME)
        except Exception:
            try:
                enc = _tiktoken.encoding_for_model("gpt2")
            except Exception:
                enc = None
        if enc is not None:
            return len(enc.encode(text))
    except Exception:
        pass
    return len(text.split())


def s3_object_exists(key: str) -> bool:
    s3 = get_s3_client()
    try:
        s3.head_object(Bucket=os.getenv("S3_BUCKET"), Key=key)
        return True
    except Exception as e:
        try:
            from botocore.exceptions import ClientError

            if isinstance(e, ClientError):
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return False
        except Exception:
            pass
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
            logger.warning("s3 atomic upload attempt %d failed for %s: %s", attempt, key, e)
            time.sleep(S3_PUT_BACKOFF * attempt)
    raise Exception(f"s3 atomic upload failed for {key} after {S3_PUT_RETRIES} attempts")


def sanitize_payload_for_weaviate(payload: Dict[str, Any]) -> None:
    # Kept for potential external use, but no longer called during chunk writing
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


# --- semantic region classification ---
def _classify_semantic_region(ratio: float) -> str:
    """Classify a 0..1 progress ratio into a semantic region label."""
    if ratio <= 0.10:
        return "intro"
    if ratio <= 0.30:
        return "early"
    if ratio <= 0.80:
        return "middle"
    if ratio <= 0.95:
        return "late"
    return "footer"


# --- parquet writer (lazy pyarrow) ---
class S3ParquetWriter:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self._rows: List[Dict[str, Any]] = []

    def _normalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
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
        # Serialize complex fields to JSON strings for parquet storage
        fields["figures"] = json.dumps(payload.get("figures") if payload.get("figures") is not None else [], ensure_ascii=False)
        fields["tags"] = json.dumps(payload.get("tags") if payload.get("tags") is not None else [], ensure_ascii=False)
        fields["layout_tags"] = json.dumps(payload.get("layout_tags") if payload.get("layout_tags") is not None else [], ensure_ascii=False)
        fields["heading_path"] = json.dumps(payload.get("heading_path") if payload.get("heading_path") is not None else [], ensure_ascii=False)
        fields["headings"] = json.dumps(payload.get("headings") if payload.get("headings") is not None else [], ensure_ascii=False)
        fields["file_type"] = payload.get("file_type") or ""
        fields["source_url"] = payload.get("source_url") or ""
        page_num = payload.get("page_number")
        fields["page_number"] = int(page_num) if page_num is not None else None
        # Line range extraction (for backward compatibility with index.py that reads line_start/line_end)
        lr = payload.get("line_range") or []
        if isinstance(lr, (list, tuple)) and len(lr) >= 2:
            try:
                fields["line_start"] = int(lr[0])
                fields["line_end"] = int(lr[1])
            except Exception:
                fields["line_start"] = None
                fields["line_end"] = None
        else:
            fields["line_start"] = None
            fields["line_end"] = None
        fields["timestamp"] = payload.get("timestamp") or ""
        fields["parser_version"] = payload.get("parser_version") or PARSER_VERSION_PDF
        fields["used_ocr"] = bool(payload.get("used_ocr", False))
        # New fields aligned with index.py
        fields["semantic_region"] = str(payload.get("semantic_region") or "")
        fields["token_range"] = json.dumps(payload.get("token_range") if payload.get("token_range") is not None else [], ensure_ascii=False)
        fields["row_range"] = json.dumps(payload.get("row_range") if payload.get("row_range") is not None else [], ensure_ascii=False)
        fields["audio_range"] = json.dumps(payload.get("audio_range") if payload.get("audio_range") is not None else [], ensure_ascii=False)
        fields["slide_range"] = json.dumps(payload.get("slide_range") if payload.get("slide_range") is not None else [], ensure_ascii=False)
        fields["layout"] = json.dumps(payload.get("layout") if payload.get("layout") is not None else {}, ensure_ascii=False)
        return fields

    def write_payload(self, payload: Dict[str, Any]) -> int:
        self._rows.append(self._normalize(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> Tuple[int, str, str, int]:
        if not self._rows:
            return 0, "", "", 0

        # lazy import pyarrow
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception as e:
            logger.exception("pyarrow is required to write parquet: %s", e)
            raise RuntimeError("pyarrow is required to write parquet") from e

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
            pa.field("page_number", pa.int64()),
            pa.field("line_start", pa.int64()),
            pa.field("line_end", pa.int64()),
            pa.field("timestamp", pa.string()),
            pa.field("parser_version", pa.string()),
            pa.field("used_ocr", pa.bool_()),
            pa.field("semantic_region", pa.string()),
            pa.field("token_range", pa.string()),
            pa.field("row_range", pa.string()),
            pa.field("audio_range", pa.string()),
            pa.field("slide_range", pa.string()),
            pa.field("layout", pa.string()),
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
            b"parser_version": PARSER_VERSION_PDF.encode("utf-8"),
            b"producer": b"pdf_parser",
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
        s3_upload_file_atomic(local_parquet_path, os.getenv("S3_BUCKET"), S3_CHUNKED_PREFIX + parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(local_parquet_path)
        except Exception:
            pass
        return len(self._rows), S3_CHUNKED_PREFIX + parquet_key, sha, size


# --- small sys.path helper for local imports ---
@contextmanager
def without_cwd_on_syspath():
    saved = list(sys.path)
    try:
        cwd = os.getcwd()
        sys.path = [p for p in sys.path if p not in ("", cwd)]
        yield
    finally:
        sys.path[:] = saved


# --- encoder / chunker (lazy tiktoken / spacy usage) ---
class TokenEncoder:
    def __init__(self, model_name: str = "gpt2"):
        self.model_name = model_name
        self.backend = "whitespace"
        try:
            import tiktoken as _tiktoken  # lazy
            enc_local = None
            try:
                enc_local = _tiktoken.get_encoding(ENC_NAME)
            except Exception:
                try:
                    enc_local = _tiktoken.encoding_for_model(self.model_name)
                except Exception:
                    enc_local = None
            if enc_local is not None:
                self.encode = lambda txt: enc_local.encode(txt)
                self.decode = lambda toks: enc_local.decode(toks)
                self.backend = "tiktoken"
            else:
                raise RuntimeError("tiktoken available but encoder not found")
        except Exception:
            logger.warning("tiktoken unavailable; using whitespace tokeniser")
            self.encode = lambda txt: txt.split()
            self.decode = lambda toks: " ".join(toks)


def split_long_sentence_by_words(sent_text: str, max_tokens: int, encoder: TokenEncoder) -> List[str]:
    words = sent_text.split()
    pieces: List[str] = []
    cur_words: List[str] = []
    cur_tok = 0
    for w in words:
        toks = encoder.encode(w)
        l = len(toks)
        if cur_tok + l > max_tokens:
            if cur_words:
                pieces.append(" ".join(cur_words))
                cur_words = []
                cur_tok = 0
            if l > max_tokens:
                tok_ids = encoder.encode(w)
                i = 0
                while i < len(tok_ids):
                    chunk_ids = tok_ids[i:i + max_tokens]
                    pieces.append(encoder.decode(chunk_ids))
                    i += max_tokens
                continue
        cur_words.append(w)
        cur_tok += l
    if cur_words:
        pieces.append(" ".join(cur_words))
    return pieces


class SentenceChunker:
    def __init__(
        self,
        max_tokens_per_chunk: Optional[int] = None,
        overlap_sentences: Optional[int] = None,
        token_model: str = "gpt2",
        nlp=None,
        min_tokens_per_chunk: Optional[int] = None,
    ):
        self.max_tokens_per_chunk = int(os.getenv("MAX_TOKENS_PER_CHUNK", MAX_TOKENS_PER_CHUNK)) if max_tokens_per_chunk is None else int(max_tokens_per_chunk)
        self.overlap_sentences = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", NUMBER_OF_OVERLAPPING_SENTENCES)) if overlap_sentences is None else int(overlap_sentences)
        if self.overlap_sentences < 0:
            raise ValueError("overlap_sentences must be >= 0")
        self.min_tokens_per_chunk = int(os.getenv("MIN_TOKENS_PER_CHUNK", MIN_TOKENS_PER_CHUNK)) if min_tokens_per_chunk is None else int(min_tokens_per_chunk)
        if self.min_tokens_per_chunk < 0:
            raise ValueError("min_tokens_per_chunk must be >= 0")
        self.encoder = TokenEncoder(model_name=token_model)
        self.nlp = nlp or self._make_sentencizer()

    @staticmethod
    def _make_sentencizer():
        try:
            import spacy as _spacy  # lazy
            try:
                return _spacy.load("en_core_web_sm")
            except Exception:
                nlp = _spacy.blank("en")
                try:
                    nlp.add_pipe("sentencizer")
                except Exception:
                    try:
                        from spacy.pipeline import Sentencizer as _Sentencizer
                        nlp.add_pipe(_Sentencizer())
                    except Exception:
                        logger.warning("spaCy sentencizer unavailable; falling back to regex-based splitter")
                        return None
                return nlp
        except Exception:
            logger.warning("spaCy not available; falling back to regex-based splitter")
            return None

    def _sentences_with_offsets_regex(self, text: str):
        pattern = re.compile(r"(?s).*?[\.\!\?][\"']?\s+|.+$")
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

    def _sentences_with_offsets(self, text: str):
        if self.nlp is not None:
            try:
                doc = self.nlp(text)
                return [(sent.text.strip(), int(sent.start_char), int(sent.end_char)) for sent in doc.sents if sent.text.strip()]
            except Exception:
                pass
        return self._sentences_with_offsets_regex(text)

    def chunk_document(self, text: str):
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
                if sent_tok_len > self.max_tokens_per_chunk:
                    pieces = split_long_sentence_by_words(sent_text, self.max_tokens_per_chunk, self.encoder)
                    if not pieces:
                        pieces = [sent_text[:1000]]
                    sent_items[i]["text"] = pieces[0]
                    for j, rem in enumerate(pieces[1:], 1):
                        sent_items.insert(i + j, {"text": rem, "start_char": None, "end_char": None, "orig_idx": sent_items[i]["orig_idx"], "is_remainder": True})
                    n = len(sent_items)
                    tok_ids = self.encoder.encode(sent_items[i]["text"])
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
                    break
                chunk_sent_texts.append(sent_text)
                cur_token_count += sent_tok_len
                chunk_end_char = sent_items[i]["end_char"]
                i += 1
            if not chunk_sent_texts:
                i += 1
                continue
            chunk_meta = {"text": " ".join(chunk_sent_texts).strip(), "token_count": cur_token_count, "start_sentence_idx": chunk_start_idx, "end_sentence_idx": i, "start_char": chunk_start_char, "end_char": chunk_end_char, "is_truncated_sentence": is_truncated_sentence}
            new_start = max(chunk_start_idx + 1, chunk_meta["end_sentence_idx"] - self.overlap_sentences)
            if prev_chunk is None:
                prev_chunk = chunk_meta
            else:
                if chunk_meta["token_count"] < self.min_tokens_per_chunk:
                    prev_chunk["text"] += " " + chunk_meta["text"]
                    prev_chunk["token_count"] += chunk_meta["token_count"]
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

    @classmethod
    def from_env(cls, **kwargs):
        max_tokens = int(os.getenv("MAX_TOKENS_PER_CHUNK", MAX_TOKENS_PER_CHUNK))
        overlap = int(os.getenv("NUMBER_OF_OVERLAPPING_SENTENCES", NUMBER_OF_OVERLAPPING_SENTENCES))
        min_tokens = int(os.getenv("MIN_TOKENS_PER_CHUNK", MIN_TOKENS_PER_CHUNK))
        token_model = os.getenv("TOKEN_ENCODER_MODEL", os.getenv("TOKEN_ENCODER", "gpt2"))
        return cls(max_tokens_per_chunk=max_tokens, overlap_sentences=overlap, token_model=token_model, nlp=None, min_tokens_per_chunk=min_tokens)


# --- pdf and ocr helpers (lazy imports where needed) ---
def import_fitz_local():
    with without_cwd_on_syspath():
        try:
            return importlib.import_module("fitz")
        except Exception:
            return importlib.import_module("pymupdf")


def import_pdfplumber():
    with without_cwd_on_syspath():
        return importlib.import_module("pdfplumber")


def crop_page_to_pil_and_bytes(page, bbox: Tuple[float, float, float, float], dpi: int = PDF_OCR_RENDER_DPI) -> Tuple[Any, bytes]:
    fitz = import_fitz_local()
    rect = fitz.Rect(bbox)
    mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=mat, clip=rect, alpha=False)
    png_bytes = pix.tobytes("png")
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes))
    return img, png_bytes


def run_ocr_on_pil_image(engine_name: str, engine_obj, pil_img) -> str:
    if engine_name == "rapidocr" and engine_obj is not None:
        try:
            import numpy as _np
            import cv2 as _cv2

            img_arr = None
            if hasattr(pil_img, "convert"):
                img_arr = _np.array(pil_img.convert("RGB"))[:, :, ::-1].copy()
            elif isinstance(pil_img, (bytes, bytearray)):
                nparr = _np.frombuffer(pil_img, np.uint8)
                img_arr = _cv2.imdecode(nparr, _cv2.IMREAD_COLOR)
            else:
                try:
                    img_arr = _np.asarray(pil_img)
                except Exception:
                    img_arr = None
            if img_arr is None:
                logger.error("RapidOCR input image conversion failed")
                return ""
            res = engine_obj(img_arr)
            if isinstance(res, tuple) and len(res) >= 1:
                ocr_result = res[0]
            else:
                ocr_result = res
            lines: List[str] = []
            if isinstance(ocr_result, list):
                for item in ocr_result:
                    if isinstance(item, dict) and "text" in item:
                        txt = item.get("text") or item.get("rec") or ""
                        if txt:
                            lines.append(str(txt))
                            continue
                    if isinstance(item, (list, tuple)):
                        found = False
                        for element in item:
                            if isinstance(element, str) and element.strip():
                                lines.append(element.strip())
                                found = True
                                break
                            if isinstance(element, (list, tuple)) and element and isinstance(element[0], str):
                                lines.append(element[0].strip())
                                found = True
                                break
                        if found:
                            continue
                        try:
                            joined = " ".join([str(x) for x in item if x is not None])
                            if joined.strip():
                                lines.append(joined.strip())
                                continue
                        except Exception:
                            pass
                    try:
                        s = str(item)
                        if s and s.strip():
                            lines.append(s.strip())
                    except Exception:
                        pass
            else:
                try:
                    s = str(ocr_result)
                    if s and s.strip():
                        lines.append(s.strip())
                except Exception:
                    pass
            return "\n".join([ln for ln in lines if ln])
        except Exception:
            logger.exception("RapidOCR failed to OCR image")
            return ""
    if engine_name == "tesseract" and engine_obj is not None:
        try:
            pytesseract = engine_obj
            return pytesseract.image_to_string(pil_img, lang=PDF_TESSERACT_LANG)
        except Exception:
            logger.exception("Tesseract OCR failed to OCR image")
            return ""
    return ""


def _create_rapidocr_engine(model_dir: str | None = None):
    models_path = model_dir or os.getenv("RAPIDOCR_MODEL_DIR", "/opt/models/rapidocr")
    tried = []
    last_exc = None
    candidates = ("rapidocr_onnxruntime", "rapidocr")
    for module_name in candidates:
        try:
            with without_cwd_on_syspath():
                mod = importlib.import_module(module_name)
            RapidOCR = getattr(mod, "RapidOCR", None)
            if RapidOCR is None:
                raise ImportError(f"module {module_name} does not expose RapidOCR")
            try:
                eng = RapidOCR(model_dir=models_path)
            except TypeError:
                eng = RapidOCR(models_path)
            return eng
        except Exception as e:
            tried.append((module_name, repr(e)))
            last_exc = e
    raise ImportError("RapidOCR import failed; tried: " + "; ".join(f"{m}:{err}" for m, err in tried)) from last_exc


def get_pdf_image_ocr_engine():
    if PDF_DISABLE_OCR and not PDF_FORCE_OCR:
        logger.info("PDF_DISABLE_OCR=true and PDF_FORCE_OCR=false -> skipping OCR")
        return "none", None
    choice = (PDF_OCR_ENGINE or "auto").lower()
    if choice == "rapidocr":
        try:
            eng = _create_rapidocr_engine()
            logger.info("Using RapidOCR model_dir=%s", os.getenv("RAPIDOCR_MODEL_DIR", "/opt/models/rapidocr"))
            return "rapidocr", eng
        except Exception as e:
            logger.exception("Requested RapidOCR but import/create failed: %s", e)
            if PDF_OCR_STRICT or PDF_FORCE_OCR:
                raise
            return "none", None
    if choice == "tesseract":
        try:
            with without_cwd_on_syspath():
                import pytesseract as _pytesseract
                _pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", "tesseract")
                logger.info("Using Tesseract OCR")
                return "tesseract", _pytesseract
        except Exception as e:
            logger.exception("Requested Tesseract but import failed: %s", e)
            if PDF_OCR_STRICT or PDF_FORCE_OCR:
                raise
            return "none", None
    # auto
    try:
        eng = _create_rapidocr_engine()
        logger.info("Auto-selected RapidOCR model_dir=%s", os.getenv("RAPIDOCR_MODEL_DIR", "/opt/models/rapidocr"))
        return "rapidocr", eng
    except Exception as rapid_ex:
        logger.warning("RapidOCR auto-select failed: %s", repr(rapid_ex))
        try:
            with without_cwd_on_syspath():
                import pytesseract as _pytesseract
                _pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", "tesseract")
                logger.info("Auto-selected Tesseract")
                return "tesseract", _pytesseract
        except Exception as tess_ex:
            logger.warning("Tesseract auto-select failed: %s", repr(tess_ex))
            logger.error("No OCR engine available. OCR will be skipped.")
            return "none", None


# --- geometry / layout helpers ---
def rect_area(rect: Tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = rect
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection_area(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1]); x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def overlap_fraction(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    a_area = rect_area(a)
    if a_area == 0:
        return 0.0
    return intersection_area(a, b) / a_area


def cluster_blocks_into_columns(blocks: List[dict], gap_multiplier: float = 1.5) -> List[List[dict]]:
    if not blocks:
        return []
    centers = [((b["bbox"][0] + b["bbox"][2]) / 2.0, i) for i, b in enumerate(blocks)]
    centers.sort(key=lambda x: x[0])
    xs = [c for c, _ in centers]
    gaps = [xs[i+1] - xs[i] for i in range(len(xs)-1)] or [0]
    med_gap = sorted(gaps)[len(gaps)//2] if gaps else 0
    if med_gap == 0:
        med_gap = max(gaps) if gaps else 50
    split_indices = []
    for idx, g in enumerate(gaps):
        if g > med_gap * gap_multiplier:
            split_indices.append(idx)
    groups = []
    start = 0
    for si in split_indices:
        group_idxs = [centers[j][1] for j in range(start, si+1)]
        groups.append([blocks[k] for k in group_idxs])
        start = si + 1
    group_idxs = [centers[j][1] for j in range(start, len(centers))]
    groups.append([blocks[k] for k in group_idxs])
    return groups


def assemble_column_text(column_blocks: List[dict]) -> str:
    if not column_blocks:
        return ""
    col_sorted = sorted(column_blocks, key=lambda b: b["bbox"][1])
    pieces = []
    prev_y = None
    for b in col_sorted:
        y0 = b["bbox"][1]
        if prev_y is None or (y0 - prev_y) > 50:
            pieces.append(b["text"].strip())
        else:
            pieces.append(" " + b["text"].strip())
        prev_y = b["bbox"][3]
    return "\n\n".join([p.strip() for p in "".join(pieces).split("\n\n") if p.strip()])


def reflow_and_clean_text(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'\(cid:\d+\)', ' ', text)
    text = re.sub(r'[\x00-\x1F]+', ' ', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n{2,}', '\n\n', text)
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_page_clean_and_figures(pdf_path: str, pageno: int, overlap_threshold: float = 0.25, image_ocr_engine_name: str = "none", image_ocr_engine_obj=None):
    fitz = import_fitz_local()
    pdfplumber = import_pdfplumber()
    doc = fitz.open(pdf_path)
    plumb = pdfplumber.open(pdf_path)
    try:
        page = doc[pageno]
    except Exception:
        plumb.close(); doc.close(); raise
    p_plumb = plumb.pages[pageno]
    table_bboxes: List[Tuple[float, float, float, float]] = []
    tables = []
    try:
        tables = p_plumb.find_tables()
    except Exception:
        tables = []
    for t in tables:
        try:
            table_bboxes.append(tuple(t.bbox))
        except Exception:
            pass
    blocks = page.get_text("dict").get("blocks", [])
    text_blocks: List[dict] = []
    image_bboxes: List[Tuple[float, float, float, float]] = []
    for b in blocks:
        if b.get("type") == 0:
            bbox = tuple(b.get("bbox"))
            text = ""
            for line in b.get("lines", []):
                spans = [s.get("text", "") for s in line.get("spans", [])]
                text += " ".join(spans) + "\n"
            text_blocks.append({"bbox": bbox, "text": text.strip()})
        elif b.get("type") == 1:
            bbox = tuple(b.get("bbox"))
            try:
                img, png_bytes = crop_page_to_pil_and_bytes(page, bbox, dpi=PDF_OCR_RENDER_DPI)
                if len(png_bytes) >= PDF_MIN_IMG_SIZE_BYTES:
                    image_bboxes.append(bbox)
            except Exception:
                pass
    figure_bboxes = table_bboxes + image_bboxes
    caption_map = {}
    content_blocks = []
    for tb in text_blocks:
        tb_bbox = tb["bbox"]
        overlapped = False
        for fb in figure_bboxes:
            if overlap_fraction(tb_bbox, fb) > overlap_threshold:
                overlapped = True
                if tb_bbox[1] >= fb[3] and (tb_bbox[1] - fb[3]) < 80:
                    caption_map.setdefault(fb, []).append(tb["text"])
                break
        if not overlapped:
            content_blocks.append(tb)
    columns = cluster_blocks_into_columns(content_blocks)
    col_texts = [assemble_column_text(col) for col in columns]
    clean_text = "\n\n".join([ct for ct in col_texts if ct]).strip()
    clean_text = reflow_and_clean_text(clean_text)
    figures_texts: List[str] = []
    for t in tables:
        try:
            rows = t.extract()
            if rows:
                lines: List[str] = []
                for row in rows:
                    lines.append("\t".join([str(c) if c is not None else "" for c in row]))
                figures_texts.append("\n".join(lines))
        except Exception:
            pass
    processed_bboxes = [tuple(t.bbox) for t in tables] if tables else []
    for fb in image_bboxes:
        if fb in processed_bboxes:
            continue
        try:
            pil_img, png_bytes = crop_page_to_pil_and_bytes(page, fb, dpi=PDF_OCR_RENDER_DPI)
            if len(png_bytes) < PDF_MIN_IMG_SIZE_BYTES:
                continue
            ocr_text = run_ocr_on_pil_image(image_ocr_engine_name, image_ocr_engine_obj, pil_img)
            caption_list = caption_map.get(fb, [])
            caption_text = "\n".join(caption_list) if caption_list else ""
            combined = (caption_text + "\n" + ocr_text).strip() if caption_text else ocr_text.strip()
            if combined:
                combined = reflow_and_clean_text(combined)
                figures_texts.append(combined)
        except Exception:
            pass
    plumb.close(); doc.close()
    return clean_text, figures_texts


def download_s3_object_to_temp(s3_key: str) -> str:
    tmpdir = os.getenv("TMPDIR") or None
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=tmpdir)
    try:
        s3 = get_s3_client()
        with open(tf.name, "wb") as fh:
            s3.download_fileobj(Bucket=os.getenv("S3_BUCKET"), Key=s3_key, Fileobj=fh)
    finally:
        pass
    return tf.name


def _now_iso_z() -> str:
    sd = os.getenv("SOURCE_DATE_EPOCH")
    if sd:
        try:
            return datetime.utcfromtimestamp(int(sd)).isoformat() + "Z"
        except Exception:
            pass
    return datetime.utcnow().isoformat() + "Z"


def _derive_doc_id_from_head(s3_key: str, head_obj: dict, manifest: dict) -> str:
    if isinstance(manifest, dict) and manifest.get("file_hash"):
        return manifest.get("file_hash")
    etag = head_obj.get("ETag", "") if isinstance(head_obj, dict) else ""
    if isinstance(etag, str):
        etag = etag.strip('"')
    if etag:
        return sha256_hex_str(s3_key + str(etag))
    lm = head_obj.get("LastModified", "") if isinstance(head_obj, dict) else ""
    if lm:
        return sha256_hex_str(s3_key + str(lm))
    base = os.path.basename(s3_key)
    if base:
        return base
    return sha256_hex_str(s3_key)


def sanitize_payload_for_raw_manifest(doc_id: str, s3_raw_key: str, chunked_s3_key: str, rows: int, sha: str, size: int) -> Dict[str, Any]:
    return {"raw_key": s3_raw_key, "doc_id": doc_id, "chunked_key": chunked_s3_key, "rows": rows, "sha256": sha, "size_bytes": size, "schema_version": os.getenv("CHUNKED_SCHEMA_VERSION","chunked_v1"), "parser_version": PARSER_VERSION_PDF, "created_at": datetime.utcnow().isoformat() + "Z"}


# --- core processing ---
def process_pdf_s3_object(s3_key: str, manifest: dict) -> dict:
    start_all = time.perf_counter()
    # validate required runtime config
    if not os.getenv("S3_BUCKET"):
        total_ms = int((time.perf_counter() - start_all) * 1000)
        msg = "S3_BUCKET environment variable not set"
        logger.error(msg)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": msg}

    try:
        s3 = get_s3_client()
    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        logger.exception("Failed to create S3 client: %s", e)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

    try:
        head_obj = {}
        try:
            head_obj = s3.head_object(Bucket=os.getenv("S3_BUCKET"), Key=s3_key) or {}
        except Exception:
            head_obj = {}
        try:
            local_pdf = download_s3_object_to_temp(s3_key)
        except Exception as e:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            logger.error("Could not download S3 object %s: %s", s3_key, e)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}
        try:
            if isinstance(manifest, dict) and manifest.get("file_hash"):
                doc_id = manifest.get("file_hash")
            else:
                doc_id = local_file_sha256(local_pdf)
            out_basename = f"{doc_id}"
            raw_manifest_key = s3_key + ".manifest.json"
            if not FORCE_OVERWRITE:
                if s3_object_exists(raw_manifest_key):
                    total_ms = int((time.perf_counter() - start_all) * 1000)
                    logger.info("Skipping because raw manifest exists: %s", raw_manifest_key)
                    try:
                        os.unlink(local_pdf)
                    except Exception:
                        pass
                    return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
                if s3_object_exists(S3_CHUNKED_PREFIX + out_basename + ".parquet"):
                    total_ms = int((time.perf_counter() - start_all) * 1000)
                    logger.info("Skipping because parquet chunk exists: %s", out_basename + ".parquet")
                    try:
                        if not s3_object_exists(raw_manifest_key):
                            head = s3.head_object(Bucket=os.getenv("S3_BUCKET"), Key=S3_CHUNKED_PREFIX + out_basename + ".parquet")
                            etag = head.get("ETag", "")
                            if isinstance(etag, str):
                                etag = etag.strip('"')
                            size = head.get("ContentLength", 0)
                            raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, S3_CHUNKED_PREFIX + out_basename + ".parquet", 0, etag, size)
                            s3.put_object(Bucket=os.getenv("S3_BUCKET"), Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
                    except Exception:
                        pass
                    try:
                        os.unlink(local_pdf)
                    except Exception:
                        pass
                    return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
            img_ocr_name, img_ocr_obj = get_pdf_image_ocr_engine()
            try:
                chunker = SentenceChunker.from_env()
            except Exception as e:
                logger.exception("Failed to initialise SentenceChunker: %s", e)
                try:
                    os.unlink(local_pdf)
                except Exception:
                    pass
                total_ms = int((time.perf_counter() - start_all) * 1000)
                return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}
            fitz = import_fitz_local()
            doc = fitz.open(local_pdf)
            total_pages = len(doc)
            doc.close()

            # First pass: collect page data and total tokens
            page_data = []  # (page_num, clean_text, figures_texts, used_ocr)
            for pageno in range(total_pages):
                clean_text, figures_texts = extract_page_clean_and_figures(
                    local_pdf, pageno,
                    overlap_threshold=0.25,
                    image_ocr_engine_name=img_ocr_name,
                    image_ocr_engine_obj=img_ocr_obj
                )
                used_ocr = bool(figures_texts)
                page_data.append((pageno + 1, clean_text, figures_texts, used_ocr))

            total_tokens = sum(token_count_for(clean_text) for _, clean_text, _, _ in page_data)

            writer = S3ParquetWriter(doc_id=doc_id)
            saved = 0
            cumulative_tokens = 0

            # Second pass: chunk pages with semantic region
            for (page_num, clean_text, figures_texts, used_ocr) in page_data:
                page_ratio = page_num / total_pages if total_pages > 0 else 0.0

                if not clean_text:
                    # Empty page – produce a single empty chunk
                    chunk_id = f"{doc_id}_p{page_num}_0"
                    token_ratio = cumulative_tokens / total_tokens if total_tokens > 0 else 0.0
                    ratio = max(page_ratio, token_ratio)
                    semantic_region = _classify_semantic_region(ratio)
                    payload = {
                        "document_id": doc_id,
                        "file_name": os.path.basename(s3_key),
                        "chunk_id": chunk_id,
                        "chunk_type": "pdf_page_chunk",
                        "text": "",
                        "token_count": 0,
                        "figures": figures_texts or [],
                        "file_type": "application/pdf",
                        "source_url": f"s3://{os.getenv('S3_BUCKET')}/{s3_key}",
                        "page_number": page_num,
                        "timestamp": _now_iso_z(),
                        "parser_version": PARSER_VERSION_PDF,
                        "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                        "layout_tags": [],
                        "used_ocr": used_ocr,
                        "heading_path": [],
                        "headings": [],
                        "line_range": None,
                        "token_range": None,
                        "row_range": None,
                        "audio_range": None,
                        "slide_range": None,
                        "semantic_region": semantic_region,
                        "layout": None,
                    }
                    writer.write_payload(payload)
                    saved += 1
                    logger.info("Buffered empty page chunk %s", chunk_id)
                    continue

                for idx, chunk in enumerate(chunker.chunk_document(clean_text)):
                    chunk_tokens = int(chunk["token_count"])
                    # midpoint token position for this chunk
                    token_ratio = (cumulative_tokens + chunk_tokens / 2.0) / total_tokens if total_tokens > 0 else 0.0
                    ratio = max(page_ratio, token_ratio)
                    semantic_region = _classify_semantic_region(ratio)

                    chunk_id = f"{doc_id}_p{page_num}_{idx}"
                    payload = {
                        "document_id": doc_id,
                        "file_name": os.path.basename(s3_key),
                        "chunk_id": chunk_id,
                        "chunk_type": "pdf_page_chunk",
                        "text": chunk["text"],
                        "token_count": chunk_tokens,
                        "figures": figures_texts or [],
                        "file_type": "application/pdf",
                        "source_url": f"s3://{os.getenv('S3_BUCKET')}/{s3_key}",
                        "page_number": page_num,
                        "timestamp": _now_iso_z(),
                        "parser_version": PARSER_VERSION_PDF,
                        "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                        "layout_tags": [],
                        "used_ocr": used_ocr,
                        "heading_path": [],
                        "headings": [],
                        "line_range": None,
                        "token_range": None,
                        "row_range": None,
                        "audio_range": None,
                        "slide_range": None,
                        "semantic_region": semantic_region,
                        "layout": None,
                    }
                    writer.write_payload(payload)
                    saved += 1

                cumulative_tokens += token_count_for(clean_text)

            if saved == 0:
                try:
                    os.unlink(local_pdf)
                except Exception:
                    pass
                total_ms = int((time.perf_counter() - start_all) * 1000)
                logger.info("No chunks produced for %s", s3_key)
                return {"saved_chunks": 0, "total_parse_duration_ms": total_ms}

            count, uploaded_key, sha, size = writer.finalize_and_upload(out_basename)
            try:
                os.unlink(local_pdf)
            except Exception:
                pass
            total_ms = int((time.perf_counter() - start_all) * 1000)
            try:
                raw_manifest = sanitize_payload_for_raw_manifest(doc_id, s3_key, uploaded_key, count, sha, size)
                s3.put_object(Bucket=os.getenv("S3_BUCKET"), Key=raw_manifest_key, Body=json.dumps(raw_manifest).encode("utf-8"), ContentType="application/json")
            except Exception:
                logger.warning("Failed to write raw manifest for %s", s3_key)
            logger.info("Wrote %d chunks for %s → %s (%d ms)", count, s3_key, uploaded_key, total_ms)
            return {"saved_chunks": count, "total_parse_duration_ms": total_ms, "skipped": False}
        except Exception as e:
            try:
                os.unlink(local_pdf)
            except Exception:
                pass
            total_ms = int((time.perf_counter() - start_all) * 1000)
            logger.exception("Error while processing %s: %s", s3_key, str(e))
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}
    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        logger.exception("Unexpected error in process_pdf_s3_object: %s", e)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}


def parse_file(s3_key: str, manifest: dict) -> dict:
    start = time.perf_counter()
    try:
        # runtime validation: ensure S3_BUCKET configured
        if not os.getenv("S3_BUCKET"):
            total_ms = int((time.perf_counter() - start) * 1000)
            msg = "Missing S3_BUCKET environment variable"
            logger.error(msg)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": msg}
        return process_pdf_s3_object(s3_key, manifest or {})
    except Exception as e:
        total_ms = int((time.perf_counter() - start) * 1000)
        logger.exception("parse_file error for %s: %s", s3_key, e)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}


if __name__ == "__main__":
    # CLI: show available engine selection (executes only when run as script)
    engine_name, engine_obj = get_pdf_image_ocr_engine()
    logger.info("Engine result: %s %s", engine_name, "object_loaded" if engine_obj else "none")