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
from datetime import datetime
from typing import Any

try:
    import boto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
except Exception as e:
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore
    _BOTO3_IMPORT_ERROR = e
else:
    _BOTO3_IMPORT_ERROR = None

try:
    from markdown_it import MarkdownIt  # type: ignore
except Exception:
    MarkdownIt = None

try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None


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
        print(json.dumps(payload, ensure_ascii=False), flush=True)

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
        self._emit("info", event, msg, **kw)

    def warning(self, a, b=None, *fmt_args, **kwargs):
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "warn")
        self._emit("warn", event, msg, **kw)

    def warn(self, a, b=None, *fmt_args, **kwargs):
        self.warning(a, b, *fmt_args, **kwargs)

    def error(self, a, b=None, *fmt_args, **kwargs):
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "error")
        self._emit("error", event, msg, **kw)

    def exception(self, a, b=None, *fmt_args, **kwargs):
        tb = traceback.format_exc()
        event, msg, kw = self._unpack(a, b, fmt_args, kwargs, "exception")
        kw.update({"tb": tb})
        self._emit("error", event, msg, **kw)


log = LoggerShim("md_parser")

DATA_S3_BUCKET = (os.getenv("DATA_S3_BUCKET") or "").strip()
AWS_REGION = (os.getenv("AWS_REGION") or "").strip()

if not DATA_S3_BUCKET:
    sys.stderr.write("ERROR: DATA_S3_BUCKET environment variable must be set\n")
    sys.exit(2)

if not AWS_REGION:
    sys.stderr.write("ERROR: AWS_REGION environment variable must be set\n")
    sys.exit(2)


def _int_env(name: str, default: int) -> int:
    v = os.getenv(name, "")
    try:
        return int(v) if v != "" else default
    except Exception:
        return default


MAX_TOKENS_PER_CHUNK = _int_env("MAX_TOKENS_PER_CHUNK", 512)
MIN_TOKENS_PER_CHUNK = _int_env("MIN_TOKENS_PER_CHUNK", 100)
DEFAULT_OVERLAP = max(1, int(MAX_TOKENS_PER_CHUNK * 0.1))
OVERLAP_TOKENS = _int_env("OVERLAP_TOKENS", DEFAULT_OVERLAP)
if OVERLAP_TOKENS >= MAX_TOKENS_PER_CHUNK:
    OVERLAP_TOKENS = max(1, MAX_TOKENS_PER_CHUNK - 1)

ENC_NAME = os.getenv("TOKEN_ENCODER", "cl100k_base")
PARSER_VERSION = os.getenv("PARSER_VERSION_MD", "markdown-it-py-v1")
FORCE_OVERWRITE = os.getenv("FORCE_OVERWRITE", "false").lower() == "true"
SAVE_SNAPSHOT = os.getenv("SAVE_SNAPSHOT", "false").lower() == "true"
PUT_RETRIES = _int_env("PUT_RETRIES", 3)
PUT_BACKOFF = float(os.getenv("PUT_BACKOFF", "0.3"))
CHUNKED_SCHEMA_VERSION = os.getenv("CHUNKED_SCHEMA_VERSION", "chunked_v1")

STORAGE_RAW_PREFIX = (os.getenv("STORAGE_RAW_PREFIX") or "data/raw/").rstrip("/") + "/"
STORAGE_CHUNKED_PREFIX = (os.getenv("STORAGE_CHUNKED_PREFIX") or "data/chunked/").rstrip("/") + "/"
S3_ENDPOINT_URL = (os.getenv("AWS_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "").strip() or None


def _require_runtime_deps_or_exit() -> None:
    if boto3 is None:
        sys.stderr.write(
            "ERROR: boto3 is required but not installed.\n"
            "Install with: pip install boto3 botocore\n"
        )
        if _BOTO3_IMPORT_ERROR is not None:
            sys.stderr.write(f"DETAIL: {_BOTO3_IMPORT_ERROR}\n")
        sys.exit(2)


_require_runtime_deps_or_exit()


def full_path_from_key(key: str) -> str:
    return f"s3://{DATA_S3_BUCKET.rstrip('/')}/{key.lstrip('/')}"


def strip_root_from_path(full: str) -> str:
    root = f"s3://{DATA_S3_BUCKET.rstrip('/')}/"
    if full.startswith(root):
        return full[len(root):]
    if full.startswith(DATA_S3_BUCKET.rstrip("/") + "/"):
        return full[len(DATA_S3_BUCKET.rstrip("/")) + 1 :]
    return full


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


_tiktoken_enc = None
_md_parser = None


def get_encoder():
    global _tiktoken_enc
    if _tiktoken_enc is not None:
        return _tiktoken_enc
    try:
        if tiktoken is None:
            _tiktoken_enc = None
            return None
        try:
            _tiktoken_enc = tiktoken.get_encoding(ENC_NAME)
        except Exception:
            try:
                _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                _tiktoken_enc = None
    except Exception:
        _tiktoken_enc = None
    return _tiktoken_enc


def get_md_parser():
    global _md_parser
    if _md_parser is not None:
        return _md_parser
    try:
        if MarkdownIt is None:
            _md_parser = None
        else:
            _md_parser = MarkdownIt()
    except Exception as e:
        log.warning("md_parser_unavailable", "markdown-it-py not available", reason=str(e))
        _md_parser = None
    return _md_parser


def token_count_for(text: str) -> int:
    if not text:
        return 0
    enc = get_encoder()
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return len(text.split())


def _is_rootish(h: Any) -> bool:
    if h is None:
        return True
    try:
        return str(h).strip().lower() in ("", "root")
    except Exception:
        return False


def _new_s3_client():
    kwargs = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
    return boto3.client("s3", **kwargs)


S3_CLIENT = None
STORAGE_CLIENT_LOCK = threading.Lock()
STORAGE_ROOT = f"s3://{DATA_S3_BUCKET.rstrip('/')}/"


def _head_bucket_or_exit(client) -> None:
    try:
        client.head_bucket(Bucket=DATA_S3_BUCKET)
        log.info("aws_init", "Initialized S3 client", bucket=DATA_S3_BUCKET, region=AWS_REGION)
    except Exception as e:
        sys.stderr.write(f"ERROR: could not access S3 bucket {DATA_S3_BUCKET}: {e}\n")
        sys.exit(2)


def _init_client_singleton():
    global S3_CLIENT
    if S3_CLIENT is None:
        with STORAGE_CLIENT_LOCK:
            if S3_CLIENT is None:
                S3_CLIENT = _new_s3_client()
                _head_bucket_or_exit(S3_CLIENT)
    return S3_CLIENT


class AwsStorageClient:
    def __init__(self, s3_client):
        self.client = s3_client

    def head_object(self, Bucket, Key):
        return self.client.head_object(Bucket=Bucket, Key=Key)

    def get_object(self, Bucket, Key):
        return self.client.get_object(Bucket=Bucket, Key=Key)

    def put_object(self, Bucket, Key, Body, ContentType=None):
        kwargs: dict[str, Any] = {"Bucket": Bucket, "Key": Key, "Body": Body}
        if ContentType:
            kwargs["ContentType"] = ContentType
        return self.client.put_object(**kwargs)

    def upload_file(self, LocalFile, Bucket, Key, ExtraArgs=None):
        kwargs = {}
        if ExtraArgs:
            kwargs["ExtraArgs"] = ExtraArgs
        return self.client.upload_file(LocalFile, Bucket, Key, **kwargs)

    def copy_object(self, CopySource, Bucket, Key):
        return self.client.copy_object(CopySource=CopySource, Bucket=Bucket, Key=Key)

    def delete_object(self, Bucket, Key):
        return self.client.delete_object(Bucket=Bucket, Key=Key)

    def get_paginator(self, name):
        return self.client.get_paginator(name)


_storage_client: AwsStorageClient | None = None


def get_storage_client_singleton() -> AwsStorageClient:
    global _storage_client
    if _storage_client is None:
        with STORAGE_CLIENT_LOCK:
            if _storage_client is None:
                _storage_client = AwsStorageClient(_init_client_singleton())
    return _storage_client


def _is_not_found(err: Exception) -> bool:
    code = None
    if hasattr(err, "response"):
        try:
            code = err.response.get("Error", {}).get("Code")
        except Exception:
            code = None
    return code in ("404", "NoSuchKey", "NotFound", "NoSuchBucket")


def storage_blob_exists(key: str) -> bool:
    client = get_storage_client_singleton()
    try:
        client.head_object(Bucket=DATA_S3_BUCKET, Key=key)
        return True
    except Exception as e:
        if _is_not_found(e):
            return False
        return False


def retry(func, retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    for attempt in range(retries):
        try:
            return func()
        except Exception as e:
            if attempt == retries - 1:
                raise
            log.warning("retry_attempt", "attempt=%d error=%s", attempt + 1, str(e))
            time.sleep(delay)
            delay *= backoff


def build_header_sections(raw_text: str) -> list[dict[str, Any]]:
    lines = raw_text.splitlines(keepends=True)
    mdp = get_md_parser()
    if mdp is None:
        return [{"heading_path": [], "heading": "", "level": 0, "start_line": 0, "end_line": len(lines), "lines": lines}]
    try:
        tokens = mdp.parse(raw_text)
    except Exception:
        return [{"heading_path": [], "heading": "", "level": 0, "start_line": 0, "end_line": len(lines), "lines": lines}]
    stack = [{"heading_path": [], "heading": "", "level": 0, "start_line": None, "end_line": None}]
    sections_out: list[dict[str, Any]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        ttype = getattr(tok, "type", "")
        if ttype == "heading_open":
            tag = getattr(tok, "tag", "h1")
            try:
                level = int(tag[1])
            except Exception:
                level = 1
            map_tuple = getattr(tok, "map", None)
            heading_text = ""
            if i + 1 < len(tokens) and getattr(tokens[i + 1], "type", "") == "inline":
                heading_text = getattr(tokens[i + 1], "content", "").strip()
            while stack and stack[-1]["level"] >= level:
                completed = stack.pop()
                if completed.get("start_line") is not None:
                    sections_out.append(completed)
            parent_path = [p for p in (stack[-1]["heading_path"][:] if stack else []) if not _is_rootish(p)]
            new_path = parent_path + ([] if _is_rootish(heading_text) else [heading_text])
            sec = {
                "heading_path": new_path,
                "heading": "" if _is_rootish(heading_text) else heading_text,
                "level": level,
                "start_line": None,
                "end_line": None,
            }
            if map_tuple:
                sec["start_line"] = map_tuple[0]
                sec["end_line"] = map_tuple[1]
            stack.append(sec)
            i += 1
            continue
        map_tuple = getattr(tok, "map", None)
        if map_tuple:
            sline, eline = map_tuple[0], map_tuple[1]
            top = stack[-1]
            if top.get("start_line") is None or sline < top["start_line"]:
                top["start_line"] = sline
            if top.get("end_line") is None or eline > top["end_line"]:
                top["end_line"] = eline
        i += 1
    while stack:
        completed = stack.pop()
        if completed.get("start_line") is not None:
            sections_out.append(completed)

    total_lines = len(lines)
    normalized_sections = []
    for sec in sections_out:
        s = sec.get("start_line")
        e = sec.get("end_line")
        if s is None:
            continue
        s = max(0, s)
        e = min(total_lines, e)
        if s >= e and s < total_lines:
            e = s + 1
        heading_path = [h for h in sec.get("heading_path", []) if not _is_rootish(h)]
        heading = "" if _is_rootish(sec.get("heading", "")) else sec.get("heading", "")
        normalized_sections.append(
            {
                "heading_path": heading_path,
                "heading": heading,
                "level": sec.get("level", 0),
                "start_line": s,
                "end_line": e,
                "lines": lines[s:e],
            }
        )

    normalized_sections_sorted = sorted(normalized_sections, key=lambda x: (x["start_line"], x["end_line"]))
    merged: list[dict[str, Any]] = []
    last_end = 0
    if normalized_sections_sorted:
        first_start = normalized_sections_sorted[0]["start_line"]
        if first_start > 0:
            merged.append({"heading_path": [], "heading": "", "level": 0, "start_line": 0, "end_line": first_start, "lines": lines[0:first_start]})
    for sec in normalized_sections_sorted:
        if sec["start_line"] > last_end:
            gap_start = last_end
            gap_end = sec["start_line"]
            if gap_end > gap_start:
                merged.append({"heading_path": [], "heading": "", "level": 0, "start_line": gap_start, "end_line": gap_end, "lines": lines[gap_start:gap_end]})
        merged.append(sec)
        last_end = max(last_end, sec["end_line"])
    if last_end < total_lines:
        merged.append({"heading_path": [], "heading": "", "level": 0, "start_line": last_end, "end_line": total_lines, "lines": lines[last_end:total_lines]})
    return merged


def merge_small_sections(
    sections: list[dict[str, Any]],
    merge_threshold: int,
    max_tokens: int,
    line_token_cache: dict[int, int],
    prevent_merge_across_level: bool = False,
) -> list[dict[str, Any]]:
    merged = []
    i = 0
    n = len(sections)
    while i < n:
        sec = sections[i]
        start_line = sec["start_line"]
        end_line = sec["end_line"]
        lines_acc = list(sec.get("lines", []))
        headings_acc = [] if _is_rootish(sec.get("heading", "")) else [sec.get("heading", "")]
        heading_path = [h for h in (sec.get("heading_path", []) or []) if not _is_rootish(h)]
        level = sec.get("level", 0)
        token_sum = 0
        for idx, line in enumerate(lines_acc):
            abs_idx = start_line + idx
            if abs_idx in line_token_cache:
                cnt = line_token_cache[abs_idx]
            else:
                try:
                    enc = get_encoder()
                    cnt = len(enc.encode(line)) if enc else len(line.split())
                except Exception:
                    cnt = len(line.split())
                line_token_cache[abs_idx] = cnt
            token_sum += cnt

        if token_sum >= merge_threshold:
            merged.append(
                {
                    "heading_path": heading_path,
                    "headings": [h for h in headings_acc if not _is_rootish(h)],
                    "level": level,
                    "start_line": start_line,
                    "end_line": end_line,
                    "lines": lines_acc,
                    "token_count": token_sum,
                }
            )
            i += 1
            continue

        if merged:
            prev = merged[-1]
            if not (prevent_merge_across_level and level <= prev.get("level", 0)):
                if prev.get("token_count", 0) + token_sum <= max_tokens:
                    prev["lines"].extend(lines_acc)
                    prev["end_line"] = end_line
                    if not _is_rootish(sec.get("heading", "")):
                        prev_headings = prev.get("headings", [])
                        prev_headings.append(sec.get("heading", ""))
                        prev["headings"] = [h for h in prev_headings if not _is_rootish(h)]
                    prev["token_count"] = prev.get("token_count", 0) + token_sum
                    i += 1
                    continue

        j = i + 1
        while j < n:
            next_sec = sections[j]
            if prevent_merge_across_level and next_sec.get("level", 0) <= level:
                break
            next_start = next_sec["start_line"]
            next_lines = next_sec.get("lines", [])
            next_tokens = 0
            for idx, next_line in enumerate(next_lines):
                abs_idx = next_start + idx
                if abs_idx in line_token_cache:
                    cnt = line_token_cache[abs_idx]
                else:
                    try:
                        enc = get_encoder()
                        cnt = len(enc.encode(next_line)) if enc else len(next_line.split())
                    except Exception:
                        cnt = len(next_line.split())
                    line_token_cache[abs_idx] = cnt
                next_tokens += cnt
            if token_sum + next_tokens > max_tokens:
                break
            token_sum += next_tokens
            lines_acc = lines_acc + next_lines
            nh = next_sec.get("heading", "")
            if not _is_rootish(nh):
                headings_acc.append(nh)
            end_line = next_sec["end_line"]
            j += 1
            if token_sum >= merge_threshold:
                break

        merged.append(
            {
                "heading_path": heading_path,
                "headings": [h for h in headings_acc if not _is_rootish(h)],
                "level": level,
                "start_line": start_line,
                "end_line": end_line,
                "lines": lines_acc,
                "token_count": token_sum,
            }
        )
        i = max(j, i + 1)
    return merged


def split_long_line_into_char_windows(line: str, max_tokens: int, overlap_tokens: int) -> list[dict[str, Any]]:
    pieces = []
    line_tok = token_count_for(line)
    approx_char_per_token = max(1, len(line) // max(1, line_tok)) if line_tok > 0 else 1
    window_chars = max(200, approx_char_per_token * max_tokens)
    step_chars = max(1, window_chars - approx_char_per_token * overlap_tokens)
    start = 0
    idx = 1
    cap = 1000
    while start < len(line) and idx <= cap:
        end = min(len(line), start + window_chars)
        piece = line[start:end]
        pieces.append({"text": piece, "token_count": token_count_for(piece), "subchunk_index": idx})
        idx += 1
        if end >= len(line):
            break
        start = start + step_chars
    return pieces


def split_section_by_tokens_lines(section: dict[str, Any], overlap_tokens: int, max_tokens: int, line_token_cache: dict[int, int]) -> list[dict[str, Any]]:
    lines = section["lines"]
    base_start_line = section["start_line"]
    token_counts = []
    for idx, line in enumerate(lines):
        abs_idx = base_start_line + idx
        if abs_idx in line_token_cache:
            token_counts.append(line_token_cache[abs_idx])
        else:
            try:
                enc = get_encoder()
                cnt = len(enc.encode(line)) if enc else len(line.split())
            except Exception:
                cnt = len(line.split())
            line_token_cache[abs_idx] = cnt
            token_counts.append(cnt)
    n = len(lines)
    chunks = []
    ptr = 0
    sub_idx = 1
    while ptr < n:
        current_tokens = 0
        j = ptr
        while j < n:
            next_tokens = token_counts[j]
            if current_tokens + next_tokens > max_tokens and current_tokens > 0:
                break
            current_tokens += next_tokens
            j += 1
        if j == ptr:
            line_idx = ptr
            line_text = lines[line_idx]
            long_pieces = split_long_line_into_char_windows(line_text, max_tokens, overlap_tokens)
            for p in long_pieces:
                chunk_start_line = base_start_line + line_idx
                chunk_end_line = chunk_start_line + 1
                chunk_text = p["text"]
                chunks.append(
                    {
                        "text": canonicalize_text(chunk_text),
                        "token_count": token_count_for(chunk_text),
                        "start_line": chunk_start_line,
                        "end_line": chunk_end_line,
                        "subchunk_index": p["subchunk_index"],
                    }
                )
                sub_idx += 1
            ptr = ptr + 1
            continue
        chunk_start_line = base_start_line + ptr
        chunk_end_line = base_start_line + j
        chunk_text = "".join(lines[ptr:j]).strip()
        chunks.append(
            {
                "text": canonicalize_text(chunk_text),
                "token_count": current_tokens,
                "start_line": chunk_start_line,
                "end_line": chunk_end_line,
                "subchunk_index": sub_idx,
            }
        )
        sub_idx += 1
        if overlap_tokens <= 0:
            next_ptr = j
        else:
            back_sum = 0
            back_idx = j - 1
            min_back_idx = ptr
            while back_idx >= min_back_idx and back_sum < overlap_tokens:
                back_sum += token_counts[back_idx]
                back_idx -= 1
            overlap_start = max(ptr, back_idx + 1)
            next_ptr = overlap_start
            if next_ptr <= ptr:
                next_ptr = j
        ptr = next_ptr
    return chunks


class ParquetWriter:
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
                if v is None:
                    fields[k] = "[]"
                elif isinstance(v, (list, tuple, dict)):
                    fields[k] = json.dumps(v, ensure_ascii=False, sort_keys=True)
                else:
                    fields[k] = json.dumps([v], ensure_ascii=False)
            except Exception:
                fields[k] = "[]"
        fields["file_type"] = payload.get("file_type") or "text/markdown"
        fields["source_url"] = payload.get("source_url") or ""
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
        fields["timestamp"] = payload.get("timestamp") or ""
        fields["parser_version"] = payload.get("parser_version") or PARSER_VERSION
        fields["used_ocr"] = bool(payload.get("used_ocr", False))
        fields["semantic_region"] = payload.get("semantic_region") or ""
        return fields

    def write_payload(self, payload: dict[str, Any]) -> int:
        self._rows.append(self._normalize(payload))
        return 1

    def finalize_and_upload(self, out_basename: str) -> tuple[int, str, str, int]:
        if not self._rows:
            return 0, "", "", 0
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore
        except Exception as e:
            log.error("pyarrow_missing", "pyarrow required to write parquet", reason=str(e))
            raise

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
                pa.field("line_start", pa.int64()),
                pa.field("line_end", pa.int64()),
                pa.field("timestamp", pa.string()),
                pa.field("parser_version", pa.string()),
                pa.field("used_ocr", pa.bool_()),
                pa.field("semantic_region", pa.string()),
            ]
        )
        cols: dict[str, list[Any]] = {name: [] for name in [f.name for f in schema]}
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
                b"producer": b"md_parser",
                b"created_at": datetime.utcnow().isoformat().encode("utf-8"),
            }
        )
        table = table.replace_schema_metadata(new_md)
        tmpfile = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".parquet", dir="/tmp")
        tmpfile.close()
        pq.write_table(table, tmpfile.name, compression="zstd", flavor="spark")
        local_parquet_path = tmpfile.name
        with open(local_parquet_path, "rb") as fh:
            b = fh.read()
        sha = hashlib.sha256(b).hexdigest()
        size = os.path.getsize(local_parquet_path)
        parquet_key = out_basename + ".parquet"
        storage_upload_file_atomic(local_parquet_path, STORAGE_CHUNKED_PREFIX + parquet_key, content_type="application/octet-stream")
        try:
            os.unlink(local_parquet_path)
        except Exception:
            pass
        return len(self._rows), STORAGE_CHUNKED_PREFIX + parquet_key, sha, size


def storage_upload_file_atomic(local_path: str, key: str, content_type: str = "application/octet-stream"):
    client = get_storage_client_singleton()
    for attempt in range(1, PUT_RETRIES + 1):
        try:
            client.upload_file(
                local_path,
                DATA_S3_BUCKET,
                key,
                ExtraArgs={"ContentType": content_type} if content_type else None,
            )
            return
        except Exception as e:
            log.warning("upload_retry", "attempt=%d key=%s error=%s", attempt, key, str(e))
            time.sleep(PUT_BACKOFF * attempt)
    raise Exception(f"upload failed for {key} after {PUT_RETRIES} attempts")


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


def sanitize_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        return
    for key in ("tags", "layout_tags", "heading_path", "headings", "figures"):
        v = payload.get(key)
        if v is None:
            payload[key] = []
        elif isinstance(v, (list, tuple)):
            payload[key] = list(v)
        else:
            payload[key] = [v]
    payload["file_name"] = str(payload.get("file_name") or "")
    payload["source_url"] = str(payload.get("source_url") or "")
    try:
        payload["token_count"] = int(payload.get("token_count") or 0)
    except Exception:
        payload["token_count"] = 0
    lr = payload.get("line_range")
    if isinstance(lr, (list, tuple)) and len(lr) >= 2:
        try:
            payload["line_range"] = [int(lr[0]), int(lr[1])]
        except Exception:
            payload["line_range"] = [1, 1]
    else:
        payload["line_range"] = [1, 1]
    if not payload.get("timestamp"):
        payload["timestamp"] = datetime.utcnow().isoformat() + "Z"
    payload["parser_version"] = payload.get("parser_version") or PARSER_VERSION
    payload["used_ocr"] = bool(payload.get("used_ocr", False))


def semantic_region_for_md(line_start: int, line_end: int, total_lines: int) -> str:
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


def parse_file(s3_key: str, manifest: dict) -> dict:
    start_all = time.perf_counter()
    client = get_storage_client_singleton()
    try:
        head_obj = client.head_object(Bucket=DATA_S3_BUCKET, Key=s3_key)
    except Exception:
        head_obj = {}
    last_modified = head_obj.get("LastModified", "")
    etag = head_obj.get("ETag", "")
    if isinstance(etag, str):
        etag = etag.strip('"')
    content_len = head_obj.get("ContentLength", 0) or 0

    if isinstance(manifest, dict) and manifest.get("file_hash"):
        doc_id = manifest.get("file_hash")
    else:
        if etag:
            doc_id = sha256_hex_str(s3_key + str(etag))
        else:
            doc_id = sha256_hex_str(s3_key + str(last_modified or ""))

    out_basename = f"{doc_id}"
    raw_manifest_key = s3_key + ".manifest.json"

    try:
        if not FORCE_OVERWRITE:
            if storage_blob_exists(raw_manifest_key):
                total_ms = int((time.perf_counter() - start_all) * 1000)
                log.info("skip_manifest_exists", "raw_manifest_exists", key=raw_manifest_key)
                return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}
            if storage_blob_exists(STORAGE_CHUNKED_PREFIX + out_basename + ".parquet"):
                total_ms = int((time.perf_counter() - start_all) * 1000)
                log.info("skip_parquet_exists", "parquet_exists", key=out_basename + ".parquet")
                try:
                    if not storage_blob_exists(raw_manifest_key):
                        head = client.head_object(Bucket=DATA_S3_BUCKET, Key=STORAGE_CHUNKED_PREFIX + out_basename + ".parquet")
                        etag2 = head.get("ETag", "")
                        if isinstance(etag2, str):
                            etag2 = etag2.strip('"')
                        size = head.get("ContentLength", 0)
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

    if content_len == 0:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.info("skip_empty_object", "Skipping empty object", key=s3_key)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    try:
        obj = client.get_object(Bucket=DATA_S3_BUCKET, Key=s3_key)
    except Exception as e:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.error("read_failed", "Could not read object", key=s3_key, error=str(e))
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": str(e)}

    raw_body = obj.get("Body", b"")
    if isinstance(raw_body, (bytes, bytearray)):
        raw_text = try_decode_bytes(raw_body)
    else:
        try:
            raw_text = try_decode_bytes(raw_body.read())
        except Exception:
            raw_text = str(raw_body)

    if isinstance(manifest, dict) and manifest.get("file_hash"):
        doc_id = manifest.get("file_hash")
        out_basename = f"{doc_id}"

    source_url = f"s3://{DATA_S3_BUCKET}/{s3_key}"

    if SAVE_SNAPSHOT:
        try:
            key = f"{STORAGE_CHUNKED_PREFIX}{doc_id}.snapshot.md"
            client.put_object(
                Bucket=DATA_S3_BUCKET,
                Key=key,
                Body=raw_text.encode("utf-8"),
                ContentType="text/markdown",
            )
        except Exception:
            pass

    canonical_full = canonicalize_text(raw_text)
    sections = build_header_sections(canonical_full)
    total_lines = len(canonical_full.splitlines())
    line_token_cache: dict[int, int] = {}
    merged_sections = merge_small_sections(sections, MIN_TOKENS_PER_CHUNK, MAX_TOKENS_PER_CHUNK, line_token_cache)

    saved = 0
    chunk_index = 1

    if not FORCE_OVERWRITE and storage_blob_exists(STORAGE_CHUNKED_PREFIX + out_basename + ".parquet"):
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.info("skip_parquet_post_download", "parquet_exists", key=out_basename + ".parquet")
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True}

    writer = ParquetWriter(doc_id=doc_id)
    file_name = os.path.basename(s3_key)

    try:
        for sec in merged_sections:
            sec_lines = sec.get("lines", [])
            if not sec_lines:
                continue
            sec_text = "".join(sec_lines).strip()
            sec_token_count = sec.get("token_count", token_count_for(sec_text))
            heading_path = [h for h in (sec.get("heading_path", []) or []) if not _is_rootish(h)]
            headings_raw = sec.get("headings") or []
            headings = [h for h in headings_raw if not _is_rootish(h)]
            if not headings and heading_path:
                headings = list(heading_path)
            sec_start_line = sec.get("start_line", 0)
            sec_end_line = sec.get("end_line", sec_start_line)
            start_line_1b = sec_start_line + 1
            end_line_1b = sec_end_line
            if sec_token_count <= MAX_TOKENS_PER_CHUNK:
                chunk_id = f"{doc_id}_{chunk_index}"
                chunk_index += 1
                payload = {
                    "document_id": doc_id or "",
                    "file_name": file_name,
                    "chunk_id": chunk_id or "",
                    "chunk_type": "md_section",
                    "text": canonicalize_text(sec_text) or "",
                    "token_count": int(sec_token_count or 0),
                    "figures": [],
                    "embedding": None,
                    "file_type": "text/markdown",
                    "source_url": source_url,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "parser_version": PARSER_VERSION or "",
                    "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                    "layout_tags": [],
                    "used_ocr": False,
                    "heading_path": heading_path or [],
                    "headings": headings or [],
                    "line_range": [int(start_line_1b), int(end_line_1b)] if (start_line_1b and end_line_1b is not None) else [1, 1],
                }
                payload["semantic_region"] = semantic_region_for_md(int(payload["line_range"][0]), int(payload["line_range"][1]), total_lines)
                sanitize_payload(payload)
                writer.write_payload(payload)
                saved += 1
                log.info("buffered_chunk", "Buffered chunk", chunk_id=payload["chunk_id"])
            else:
                subchunks = split_section_by_tokens_lines(sec, OVERLAP_TOKENS, MAX_TOKENS_PER_CHUNK, line_token_cache)
                for sub in subchunks:
                    chunk_text = sub.get("text", "")
                    token_ct = int(sub.get("token_count", 0))
                    sline = sub.get("start_line", 0)
                    eline = sub.get("end_line", sline)
                    chunk_id = f"{doc_id}_{chunk_index}"
                    chunk_index += 1
                    start_line_sub = sline + 1
                    end_line_sub = eline
                    payload = {
                        "document_id": doc_id or "",
                        "file_name": file_name,
                        "chunk_id": chunk_id or "",
                        "chunk_type": "md_subchunk",
                        "text": canonicalize_text(chunk_text) or "",
                        "token_count": token_ct,
                        "figures": [],
                        "embedding": None,
                        "file_type": "text/markdown",
                        "source_url": source_url,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                        "parser_version": PARSER_VERSION or "",
                        "tags": manifest.get("tags", []) if isinstance(manifest, dict) else [],
                        "layout_tags": [],
                        "used_ocr": False,
                        "heading_path": heading_path or [],
                        "headings": headings or [],
                        "line_range": [int(start_line_sub), int(end_line_sub)] if (start_line_sub and end_line_sub is not None) else [1, 1],
                    }
                    payload["semantic_region"] = semantic_region_for_md(int(payload["line_range"][0]), int(payload["line_range"][1]), total_lines)
                    sanitize_payload(payload)
                    writer.write_payload(payload)
                    saved += 1
                    log.info("buffered_subchunk", "Buffered subchunk", chunk_id=payload["chunk_id"], lines=f"{start_line_sub}-{end_line_sub}")
    except Exception:
        total_ms = int((time.perf_counter() - start_all) * 1000)
        log.exception("buffering_failed", "Error while buffering chunks for %s", key=s3_key)
        return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": True, "error": "buffering_failed"}

    try:
        if saved == 0:
            total_ms = int((time.perf_counter() - start_all) * 1000)
            log.info("no_chunks", "No chunks produced", key=s3_key)
            return {"saved_chunks": 0, "total_parse_duration_ms": total_ms, "skipped": False}
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
    log.info("startup", "Starting markdown -> parquet parser (CLI mode)", region=AWS_REGION, bucket=DATA_S3_BUCKET)
    _ensure_cli_env_or_exit()
    client = get_storage_client_singleton()
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=DATA_S3_BUCKET, Prefix=STORAGE_RAW_PREFIX):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not (key.lower().endswith(".md") or key.lower().endswith(".markdown")):
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
