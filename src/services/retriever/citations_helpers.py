# src/services/retriever/citations_helpers.py
from __future__ import annotations

import json
import logging
import mimetypes
import re
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config

from settings import AWS_REGION, ENABLE_PRESIGNED_URLS, PRESIGNED_URL_TTL_SECONDS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  File type detection – recognises all MIME types in the collection
# ---------------------------------------------------------------------------
def _detect_type(
    file_type: Optional[str],
    source_url: Optional[str],
    file_name: Optional[str],
    chunk_type: Optional[str],
) -> str:
    ft = (file_type or "").lower()
    if ft:
        if "pdf" in ft:
            return "pdf"
        if "presentation" in ft or "powerpoint" in ft or "pptx" in ft or "ppt" in ft:
            return "pptx"
        if ft.startswith("audio/"):
            return "audio"
        if ft.startswith("image/"):
            return "image"
        if "csv" in ft:
            return "csv"
        if "json" in ft and "jsonl" in ft:
            return "jsonl"
        if "markdown" in ft:
            return "md"
        if "html" in ft or "xml" in ft:
            return "html"
        if "text" in ft:
            return "txt"

    ext = _ext_from_url_or_name(source_url) or _ext_from_url_or_name(file_name)
    if ext in ("pdf",):
        return "pdf"
    if ext in ("ppt", "pptx", "pptm", "odp"):
        return "pptx"
    if ext in ("mp3", "wav", "m4a", "flac", "aac", "ogg"):
        return "audio"
    if ext in ("jpg", "jpeg", "png", "webp", "tiff", "tif", "gif", "bmp"):
        return "image"
    if ext in ("csv", "tsv"):
        return "csv"
    if ext in ("json", "jsonl", "ndjson"):
        return "jsonl"
    if ext in ("md", "markdown"):
        return "md"
    if ext in ("html", "htm", "xhtml"):
        return "html"
    if ext in ("txt", "text"):
        return "txt"

    ct = (chunk_type or "").lower()
    if "audio" in ct:
        return "audio"
    if "slide" in ct or "slides" in ct:
        return "pptx"
    if "row" in ct or "csv" in ct:
        return "csv"
    if "image" in ct or "frame" in ct:
        return "image"

    return "unknown"


def _ext_from_url_or_name(val: Optional[str]) -> str:
    if not val:
        return ""
    base = val.split("?")[0].split("#")[0]
    _, ext = base.rsplit(".", 1) if "." in base else ("", "")
    return ext.strip().lower()


# ---------------------------------------------------------------------------
#  Content extraction from Qdrant payload
# ---------------------------------------------------------------------------
def _strip_html(content: str) -> str:
    try:
        t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
        t = re.sub(r"(?is)<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t
    except Exception:
        return re.sub(r"\s+", " ", content or "").strip()


def _full_text_from_payload(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("content"):
        return str(payload["content"])
    if payload.get("text"):
        return str(payload["text"])
    if payload.get("html"):
        return _strip_html(str(payload["html"]))
    headings = payload.get("headings") or payload.get("heading_path") or payload.get("title") or ""
    if isinstance(headings, (list, tuple)):
        return " - ".join(str(x) for x in headings)
    return str(headings or "")


# ---------------------------------------------------------------------------
#  UI metadata fields builder – clean, user-facing only
# ---------------------------------------------------------------------------
def ui_fields_from_payload(
    payload: Dict[str, Any],
    prefer_snippet_len: Optional[int] = None,
) -> List[Tuple[str, Any]]:
    p = payload or {}

    # --- source_url and corrected file_name ---
    source_url = p.get("source_url") or p.get("s3_path") or p.get("raw_key") or None
    raw_file_name = p.get("file_name") or ""
    if source_url:
        url_path = source_url.split("?")[0].split("#")[0]
        extracted = url_path.rstrip("/").split("/")[-1]
        # Use extracted name only if it looks like a real filename (has an extension)
        if extracted and "." in extracted:
            file_name = extracted
        else:
            file_name = raw_file_name or extracted or None
    else:
        file_name = raw_file_name or None

    file_type = p.get("file_type") or None
    chunk_type = p.get("chunk_type") or None
    detected = _detect_type(file_type, source_url, file_name, chunk_type)

    ordered: List[Tuple[str, Any]] = []
    if source_url:
        ordered.append(("source_url", source_url))
    if file_name:
        ordered.append(("file_name", file_name))

    # --- only user-meaningful fields per type ---
    if detected == "pdf":
        if p.get("page_number") is not None:
            ordered.append(("page_number", int(p["page_number"])))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "html":
        if p.get("headings"):
            ordered.append(("headings", p["headings"]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "txt":
        if p.get("headings"):
            ordered.append(("headings", p["headings"]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "md":
        if p.get("headings"):
            ordered.append(("headings", p["headings"]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "pptx":
        if p.get("slide_range"):
            ordered.append(("slide_range", p["slide_range"]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "csv":
        if p.get("row_range"):
            ordered.append(("row_range", p["row_range"]))
        if p.get("headings"):
            ordered.append(("headings", p["headings"]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "image":
        if p.get("used_ocr") is not None:
            ordered.append(("used_ocr", bool(p["used_ocr"])))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    elif detected == "audio":
        if p.get("audio_range"):
            ordered.append(("audio_range", p["audio_range"]))
        if p.get("duration"):
            ordered.append(("duration", p["duration"]))

    elif detected == "jsonl":
        if p.get("line_range"):
            ordered.append(("line_range", p["line_range"]))

    else:
        # unknown – show what we have
        if p.get("headings"):
            ordered.append(("headings", p["headings"]))
        if p.get("line_range"):
            ordered.append(("line_range", p["line_range"]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p["semantic_region"]))

    return [(k, v) for k, v in ordered if v is not None and v != ""]


# ---------------------------------------------------------------------------
#  Numbered prompt & UI chunks builder
# ---------------------------------------------------------------------------
def build_numbered_prompt_and_ui_chunks(
    results: List[Dict[str, Any]],
    query: str,
    max_content_chars: Optional[int] = None,
    prefer_snippet_len: int = 400,
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    llm_blocks: List[str] = []
    llm_lines: List[str] = []
    ui_chunks: List[Dict[str, Any]] = []

    for idx, r in enumerate(results, start=1):
        payload = r.get("payload") or {}
        fields = ui_fields_from_payload(payload, prefer_snippet_len=prefer_snippet_len)
        full_text = _full_text_from_payload(payload)

        ui_chunk = dict(fields)
        ui_chunk["index"] = idx
        ui_chunk["meta_items"] = [{"k": k, "v": v} for k, v in fields]
        ui_chunks.append(ui_chunk)

        heading = None
        for k, v in fields:
            if k == "headings":
                if isinstance(v, list) and v:
                    heading = v[0]
                elif isinstance(v, str) and v:
                    heading = v
                break

        content = full_text or ""
        if max_content_chars and len(content) > max_content_chars:
            content = content[:max_content_chars] + "..."

        block_lines = [f"[{idx}]"]
        if heading:
            block_lines.append(f"Heading: {heading}")
        if content:
            block_lines.append(f"Content: {content}")
        llm_blocks.append("\n".join(block_lines))
        llm_lines.append(json.dumps({"index": idx, "heading": heading, "content": content}, ensure_ascii=False))

    prompt_body = "\n\n".join(llm_blocks) + f"\n\nQ: {query}\nA:"
    return prompt_body, llm_lines, ui_chunks


# ---------------------------------------------------------------------------
#  Citation validation & filtering
# ---------------------------------------------------------------------------
def validate_and_filter_citations(answer: str, valid_indexes: List[int]) -> str:
    if not answer:
        return answer
    answer = re.sub(
        r"\[.*?(source_url|page_number|file_name|row_range|token_range|audio_range|headings|chunk_id).*?\]",
        " ",
        answer,
        flags=re.IGNORECASE,
    )
    def repl(match):
        num = int(match.group(1))
        return f"[{num}]" if num in valid_indexes else ""
    answer = re.sub(r"\[(\d+)\]", repl, answer)
    answer = re.sub(r"https?://\S+", "", answer)
    answer = re.sub(r"\s+", " ", answer).strip()
    return answer


# ---------------------------------------------------------------------------
#  Deterministic fallback summarization
# ---------------------------------------------------------------------------
def deterministic_summarize(
    llm_lines: List[str],
    query: str = "",
    max_chars: int = 800,
) -> str:
    texts = []
    for ln in llm_lines:
        try:
            obj = json.loads(ln)
            c = obj.get("content", "")
        except Exception:
            c = str(ln)
        if c:
            texts.append(c)
    joined = " ".join(texts).strip()
    if not joined:
        return "no documents retrieved"
    sentences = re.split(r"(?<=[.!?])\s+", joined)
    out = []
    for s in sentences:
        s = s.strip()
        if s:
            out.append(s)
            if len(out) >= 2 or sum(len(x) for x in out) >= max_chars:
                break
    if not out:
        return joined[:max_chars]
    return " ".join(out)[:max_chars]


# ---------------------------------------------------------------------------
#  Presigned URL generation (inline browser viewing)
# ---------------------------------------------------------------------------
def parse_s3_path(path: str) -> Tuple[str, str]:
    if not path.startswith("s3://"):
        raise ValueError("s3_path must start with s3://")
    path = path[5:]
    bucket, key = path.split("/", 1)
    key = key.split("#")[0].split("?")[0]
    return bucket, key


def _guess_content_type(key: str) -> str:
    ctype, _ = mimetypes.guess_type(key)
    return ctype or "application/octet-stream"

def generate_presigned_url_sync(
    bucket: str, key: str, ttl_seconds: int = 3600, region: str = "us-east-1"
) -> str:
    if not ENABLE_PRESIGNED_URLS:
        raise RuntimeError("Presigned URLs are disabled")
    
    s3_client = boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4"),
    )
    content_type = _guess_content_type(key)
    
    params = {
        "Bucket": bucket,
        "Key": key,
        "ResponseContentDisposition": "inline",
    }
    
    # For HTML: strip scripts but keep structure by serving as text/html 
    # with sandbox attributes handled on frontend.
    if content_type.startswith("text/html"):
        params["ResponseContentType"] = "text/html; charset=utf-8"
    else:
        params["ResponseContentType"] = content_type
    
    return s3_client.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ttl_seconds,
        HttpMethod="GET",
    )