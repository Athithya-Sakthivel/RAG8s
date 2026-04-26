#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

SUPPORTED_EXTENSIONS = {"pdf", "html", "md"}


def canonicalize_text(s: Any) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+$", "", ln) for ln in s.split("\n")]
    return "\n".join(lines).strip()


def normalize_query(s: Any) -> str:
    return re.sub(r"\s+", " ", canonicalize_text(s)).strip().lower()


def sha256_hex_str(s: Any) -> str:
    return hashlib.sha256(canonicalize_text(s).encode("utf-8")).hexdigest()


def stable_uuid_from_text(text: Any) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, canonicalize_text(text)))


def _truncate_text(text: str, max_chars: int | None) -> str:
    if not text:
        return ""
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if boundary >= int(max_chars * 0.6):
        cut = cut[: boundary + 1]
    return cut.rstrip() + "…"


def _maybe_json(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    s = v.strip()
    if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
        try:
            return json.loads(s)
        except Exception:
            return v
    return v


def _as_list(v: Any) -> list[Any]:
    if v is None or v == "":
        return []
    v = _maybe_json(v)
    if isinstance(v, list):
        return list(v)
    if isinstance(v, tuple):
        return list(v)
    return [v]


def _strip_html(content: str) -> str:
    if not content:
        return ""
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
    t = re.sub(r"(?is)<[^>]+>", " ", t)
    t = html_lib.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _detect_doc_kind(payload: dict[str, Any]) -> str:
    p = payload or {}
    ft = str(p.get("file_type") or "").lower()
    if "pdf" in ft:
        return "pdf"
    if "html" in ft or "xml" in ft:
        return "html"
    if "markdown" in ft or ft == "md" or "md" in ft:
        return "md"

    src = str(p.get("source_url") or p.get("file_name") or "").lower()
    if src.endswith(".pdf"):
        return "pdf"
    if src.endswith(".html") or src.endswith(".htm"):
        return "html"
    if src.endswith(".md") or src.endswith(".markdown"):
        return "md"

    ct = str(p.get("chunk_type") or "").lower()
    if "pdf" in ct:
        return "pdf"
    if "html" in ct:
        return "html"
    if "md" in ct or "markdown" in ct:
        return "md"
    return "unknown"


def _join_heading_like(v: Any) -> str:
    if v is None or v == "":
        return ""
    v = _maybe_json(v)
    if isinstance(v, list):
        vals = [canonicalize_text(x) for x in v if canonicalize_text(x)]
        return " - ".join(vals)
    return canonicalize_text(v)


def _full_text_from_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    p = payload

    if p.get("content"):
        return canonicalize_text(p.get("content"))
    if p.get("text"):
        return canonicalize_text(p.get("text"))
    if p.get("html"):
        return canonicalize_text(_strip_html(str(p.get("html"))))

    headings = p.get("headings") or p.get("heading_path") or p.get("title") or ""
    heading_text = _join_heading_like(headings)
    if heading_text:
        return heading_text

    return ""


def ui_fields_from_payload(
    payload: dict[str, Any],
    prefer_snippet_len: int | None = None,
    verbose: bool = False,
) -> list[tuple[str, Any]]:
    p = payload or {}
    kind = _detect_doc_kind(p)

    source_url = p.get("source_url") or p.get("s3_path") or p.get("raw_key") or None
    ordered: list[tuple[str, Any]] = []

    if source_url:
        ordered.append(("source_url", source_url))
    if p.get("chunk_id"):
        ordered.append(("chunk_id", p.get("chunk_id")))
    if p.get("chunk_index") is not None:
        ordered.append(("chunk_index", p.get("chunk_index")))

    if kind == "pdf":
        if p.get("page_number") is not None:
            try:
                ordered.append(("page_number", int(p.get("page_number"))))
            except Exception:
                ordered.append(("page_number", p.get("page_number")))
        if p.get("line_range"):
            lr = _as_list(p.get("line_range"))
            if len(lr) >= 2:
                ordered.append(("line_range", [lr[0], lr[1]]))
        else:
            ls = p.get("line_start")
            le = p.get("line_end")
            if ls is not None or le is not None:
                ordered.append(("line_range", [int(ls or 0), int(le or 0)]))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p.get("semantic_region")))
        if p.get("layout_tags"):
            ordered.append(("layout_tags", _as_list(p.get("layout_tags"))))
        if p.get("headings"):
            ordered.append(("headings", _as_list(p.get("headings"))))
        if p.get("heading_path"):
            ordered.append(("heading_path", _as_list(p.get("heading_path"))))

    elif kind == "html":
        title = p.get("title")
        if title:
            ordered.append(("title", title))
        if p.get("headings"):
            ordered.append(("headings", _as_list(p.get("headings"))))
        if p.get("heading_path"):
            ordered.append(("heading_path", _as_list(p.get("heading_path"))))
        if p.get("line_range"):
            ordered.append(("line_range", _as_list(p.get("line_range"))))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p.get("semantic_region")))

    elif kind == "md":
        title = p.get("title")
        if title:
            ordered.append(("title", title))
        if p.get("headings"):
            ordered.append(("headings", _as_list(p.get("headings"))))
        if p.get("heading_path"):
            ordered.append(("heading_path", _as_list(p.get("heading_path"))))
        if p.get("line_range"):
            ordered.append(("line_range", _as_list(p.get("line_range"))))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p.get("semantic_region")))

    else:
        if p.get("headings"):
            ordered.append(("headings", _as_list(p.get("headings"))))
        if p.get("heading_path"):
            ordered.append(("heading_path", _as_list(p.get("heading_path"))))
        if p.get("line_range"):
            ordered.append(("line_range", _as_list(p.get("line_range"))))
        if p.get("semantic_region"):
            ordered.append(("semantic_region", p.get("semantic_region")))

    if verbose:
        for k in ("parser_version", "timestamp", "file_type", "document_id"):
            if p.get(k) is not None and p.get(k) != "":
                ordered.append((k, p.get(k)))

    out: list[tuple[str, Any]] = []
    for k, v in ordered:
        if v is None or v == "":
            continue
        if prefer_snippet_len and isinstance(v, str):
            v = _truncate_text(v, prefer_snippet_len)
        elif prefer_snippet_len and isinstance(v, list):
            v = [_truncate_text(str(x), prefer_snippet_len) for x in v]
        out.append((k, v))
    return out


def _display_heading_from_payload(payload: dict[str, Any]) -> str:
    p = payload or {}
    for key in ("title", "heading_path", "headings"):
        value = p.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            text = " - ".join([canonicalize_text(x) for x in value if canonicalize_text(x)])
        else:
            text = canonicalize_text(value)
        if text:
            return text
    return ""


def build_numbered_prompt_and_ui_chunks(
    results: list[dict[str, Any]],
    query: str,
    max_content_chars: int = 2500,
    prefer_snippet_len: int | None = None,
):
    llm_blocks: list[str] = []
    llm_lines: list[str] = []
    ui_chunks: list[dict[str, Any]] = []

    for idx, r in enumerate(results, start=1):
        payload = r.get("payload") or {}
        fields = ui_fields_from_payload(payload, prefer_snippet_len=prefer_snippet_len, verbose=False)
        fields_map = dict(fields)

        full_text = _truncate_text(_full_text_from_payload(payload), max_content_chars)
        heading_text = _display_heading_from_payload(payload)

        meta_items = [{"k": k, "v": v} for k, v in fields]

        ui_chunk = {
            "index": idx,
            "chunk_id": fields_map.get("chunk_id") or str(r.get("id") or ""),
            "source_url": fields_map.get("source_url") or "",
            "meta_items": meta_items,
        }
        ui_chunks.append(ui_chunk)

        block_lines = [f"[{idx}]"]
        if heading_text:
            block_lines.append(f"Heading: {heading_text}")
        if full_text:
            block_lines.append(f"Content: {full_text}")

        llm_blocks.append("\n".join(block_lines))
        llm_lines.append(
            json.dumps(
                {
                    "index": idx,
                    "heading": heading_text or None,
                    "content": full_text,
                    "chunk_id": ui_chunk["chunk_id"],
                },
                ensure_ascii=False,
            )
        )

    prompt_body = "\n\n".join(llm_blocks) + f"\n\nQ: {query}\nA:"
    return prompt_body, llm_lines, ui_chunks


def build_cache_key(
    query_norm: str,
    corpus_version: str,
    prompt_version: str,
    retrieval_version: str,
    model_name: str,
) -> str:
    raw = "|".join(
        [
            normalize_query(query_norm),
            canonicalize_text(corpus_version),
            canonicalize_text(prompt_version),
            canonicalize_text(retrieval_version),
            canonicalize_text(model_name),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def iso_now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_semantic_cache_payload(
    *,
    cache_id: str,
    query_text: str,
    query_norm: str,
    corpus_version: str,
    prompt_version: str,
    retrieval_version: str,
    model_name: str,
    answer: str,
    ui_chunks: list[dict[str, Any]],
    ttl_seconds: int,
    cache_group: str = "semantic_rag_v1",
    hit_type: str = "llm",
    cache_score: float = 1.0,
) -> dict[str, Any]:
    now_epoch = int(datetime.now(UTC).timestamp())
    expires_at_epoch = now_epoch + max(1, int(ttl_seconds))

    chunk_ids: list[str] = []
    for c in ui_chunks or []:
        if isinstance(c, dict) and c.get("chunk_id"):
            chunk_ids.append(str(c.get("chunk_id")))

    return {
        "cache_id": cache_id,
        "cache_group": cache_group,
        "query_text": query_text or "",
        "query_norm": query_norm or "",
        "query_norm_hash": sha256_hex_str(query_norm or ""),
        "corpus_version": corpus_version or "",
        "prompt_version": prompt_version or "",
        "retrieval_version": retrieval_version or "",
        "model_name": model_name or "",
        "answer": answer or "",
        "answer_hash": sha256_hex_str(answer or ""),
        "cached_chunks_json": json.dumps(ui_chunks or [], ensure_ascii=False),
        "retrieved_chunk_ids_json": json.dumps(chunk_ids, ensure_ascii=False),
        "cache_score": float(cache_score),
        "hit_type": hit_type or "llm",
        "created_at": iso_now_z(),
        "created_at_epoch": now_epoch,
        "expires_at_epoch": expires_at_epoch,
    }


def decode_cached_chunks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("cached_chunks_json") or payload.get("chunks_json") or payload.get("chunks")
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return decoded
        except Exception:
            return []
    return []


def cache_payload_to_response(payload: dict[str, Any], cache_score: float | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    return {
        "answer": payload.get("answer") or "",
        "chunks": decode_cached_chunks(payload),
        "cache_hit": True,
        "cache_score": float(cache_score if cache_score is not None else payload.get("cache_score") or 1.0),
        "cache_id": payload.get("cache_id"),
        "hit_type": payload.get("hit_type") or "cache",
    }


def is_payload_expired(payload: dict[str, Any], now_epoch: int | None = None) -> bool:
    if not isinstance(payload, dict):
        return True
    now_epoch = int(now_epoch or datetime.now(UTC).timestamp())
    exp = payload.get("expires_at_epoch")
    if exp is None:
        return False
    try:
        return int(exp) < now_epoch
    except Exception:
        return True


def deterministic_summarize(lines: list[str], max_chars: int = 800) -> str:
    texts: list[str] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
            c = obj.get("content", "")
        except Exception:
            c = str(ln)
        if c:
            texts.append(c)

    joined = " ".join(texts).strip()
    if not joined:
        return ""

    sents = re.split(r"(?<=[.!?])\s+", joined)
    out = []
    total = 0
    for s in sents:
        s = s.strip()
        if not s:
            continue
        out.append(s)
        total += len(s)
        if len(out) >= 2 or total >= max_chars:
            break

    if not out:
        return joined[:max_chars]
    return " ".join(out)[:max_chars]
