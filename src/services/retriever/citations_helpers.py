# src/services/retriever/citations_helpers.py
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config

from settings import AWS_REGION, ENABLE_PRESIGNED_URLS, PRESIGNED_URL_TTL_SECONDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  File type detection – only PDF, HTML, MD (others become "txt"/"unknown")
# ---------------------------------------------------------------------------
def _detect_type(
    file_type: Optional[str],
    source_url: Optional[str],
    file_name: Optional[str],
    chunk_type: Optional[str],
) -> str:
    if file_type:
        ft = file_type.lower()
        if "pdf" in ft:
            return "pdf"
        if "html" in ft or "xml" in ft:
            return "html"
        if "markdown" in ft:
            return "md"
        if "text" in ft:
            return "txt"

    ext = (_ext_from_url_or_name(source_url) or _ext_from_url_or_name(file_name)).lower()
    if ext == "pdf":
        return "pdf"
    if ext in ("html", "htm", "xhtml"):
        return "html"
    if ext in ("md", "markdown"):
        return "md"
    if ext in ("txt", "text"):
        return "txt"

    chunk_lower = (chunk_type or "").lower()
    if "pdf" in chunk_lower:
        return "pdf"
    if "html" in chunk_lower:
        return "html"
    if "markdown" in chunk_lower:
        return "md"
    return "txt"  # safe default


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
#  UI metadata fields builder
# ---------------------------------------------------------------------------
def ui_fields_from_payload(
    payload: Dict[str, Any],
    prefer_snippet_len: Optional[int] = None,
) -> List[Tuple[str, Any]]:
    p = payload or {}
    file_name = p.get("file_name") or (p.get("source_url") or "").split("/")[-1] or None
    source_url = p.get("source_url") or p.get("s3_path") or p.get("raw_key") or None
    file_type = p.get("file_type") or None
    chunk_type = p.get("chunk_type") or None
    detected = _detect_type(file_type, source_url, file_name, chunk_type)

    ordered: List[Tuple[str, Any]] = []
    if source_url:
        ordered.append(("source_url", source_url))
    if file_name:
        ordered.append(("file_name", file_name))
    if p.get("chunk_id"):
        ordered.append(("chunk_id", p["chunk_id"]))
    if p.get("chunk_index") is not None:
        ordered.append(("chunk_index", p["chunk_index"]))

    # PDF-specific fields
    if detected == "pdf":
        if p.get("page_number") is not None:
            ordered.append(("page_number", int(p["page_number"])))
        if p.get("line_start") is not None or p.get("line_end") is not None:
            ls = int(p.get("line_start") or 0)
            le = int(p.get("line_end") or 0)
            ordered.append(("line_range", [ls, le]))

    # HTML / MD / TXT
    elif detected in ("html", "md", "txt"):
        if p.get("headings"):
            ordered.append(("headings", p["headings"]))
        if p.get("line_range"):
            ordered.append(("line_range", p["line_range"]))

    # Common
    if p.get("tags"):
        ordered.append(("tags", p["tags"]))

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

        # Build UI chunk
        ui_chunk = dict(fields)
        ui_chunk["index"] = idx
        ui_chunk["meta_items"] = [{"k": k, "v": v} for k, v in fields]
        ui_chunks.append(ui_chunk)

        # Build LLM passage block
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
    # Remove any citation-like tokens that reference metadata
    answer = re.sub(
        r"\[.*?(source_url|page_number|file_name|row_range|token_range|audio_range|headings|chunk_id).*?\]",
        " ",
        answer,
        flags=re.IGNORECASE,
    )
    # Only keep [n] if n is in the valid list
    def repl(match):
        num = int(match.group(1))
        return f"[{num}]" if num in valid_indexes else ""
    answer = re.sub(r"\[(\d+)\]", repl, answer)
    # Remove raw URLs
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
#  Presigned URL generation (synchronous, non-blocking)
# ---------------------------------------------------------------------------
def parse_s3_path(path: str) -> Tuple[str, str]:
    if not path.startswith("s3://"):
        raise ValueError("s3_path must start with s3://")
    path = path[5:]  # remove s3://
    bucket, key = path.split("/", 1)
    # Strip fragment or query
    key = key.split("#")[0].split("?")[0]
    return bucket, key


def generate_presigned_url_sync(bucket: str, key: str, ttl_seconds: int = 3600, region: str = "us-east-1") -> str:
    if not ENABLE_PRESIGNED_URLS:
        raise RuntimeError("Presigned URLs are disabled")
    s3_client = boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4"),
    )
    return s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl_seconds,
        HttpMethod="GET",
    )