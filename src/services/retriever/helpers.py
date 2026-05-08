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


def iso_now_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _truncate_value(v: Any, limit: int | None) -> Any:
    if not limit or limit <= 0:
        return v
    if isinstance(v, str) and len(v) > limit:
        return v[:limit].rstrip() + "…"
    if isinstance(v, list) and len(v) > limit:
        return v[:limit]
    return v


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


def _join_heading_like(v: Any) -> str:
    if v is None or v == "":
        return ""
    v = _maybe_json(v)
    if isinstance(v, list):
        vals = [canonicalize_text(x) for x in v if canonicalize_text(x)]
        return " - ".join(vals)
    return canonicalize_text(v)


def _content_from_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("content"):
        return canonicalize_text(payload.get("content"))
    if payload.get("text"):
        return canonicalize_text(payload.get("text"))
    if payload.get("html"):
        content = payload["html"]
        t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", content)
        t = re.sub(r"(?is)<[^>]+>", " ", t)
        t = html_lib.unescape(t)
        t = re.sub(r"\s+", " ", t).strip()
        return canonicalize_text(t)
    return ""


def build_cache_key(
    query_norm: str,
    corpus_version: str,
    prompt_version: str,
    retrieval_version: str,
    model_name: str,
    tenant_id: str | None = None,
    top_k: int | None = None,
    fetch_k: int | None = None,
) -> str:
    raw = "|".join(
        [
            normalize_query(query_norm),
            tenant_id or "",
            corpus_version or "",
            prompt_version or "",
            retrieval_version or "",
            model_name or "",
            str(top_k if top_k is not None else ""),
            str(fetch_k if fetch_k is not None else ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    top_k: int | None = None,
    fetch_k: int | None = None,
    retrieval_mode: str | None = None,
    rerank_applied: bool | None = None,
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
        "top_k": int(top_k) if top_k is not None else None,
        "fetch_k": int(fetch_k) if fetch_k is not None else None,
        "retrieval_mode": retrieval_mode or "",
        "rerank_applied": bool(rerank_applied) if rerank_applied is not None else None,
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


def rrf_fuse(
    dense_results: list[dict[str, Any]],
    sparse_results: list[dict[str, Any]],
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    dense_map: dict[str, dict[str, Any]] = {}
    sparse_map: dict[str, dict[str, Any]] = {}

    def _key(item: dict[str, Any]) -> str:
        payload = item.get("payload") or {}
        return str(payload.get("chunk_id") or item.get("id") or "")

    for rank, item in enumerate(dense_results, start=1):
        key = _key(item)
        if not key:
            continue
        dense_map[key] = {
            "rank": rank,
            "score": float(item.get("score", 0.0) or 0.0),
            "payload": item.get("payload") or {},
            "id": item.get("id"),
        }

    for rank, item in enumerate(sparse_results, start=1):
        key = _key(item)
        if not key:
            continue
        sparse_map[key] = {
            "rank": rank,
            "score": float(item.get("score", 0.0) or 0.0),
            "payload": item.get("payload") or {},
            "id": item.get("id"),
        }

    fused: list[dict[str, Any]] = []
    for key in sorted(set(dense_map) | set(sparse_map)):
        d = dense_map.get(key)
        s = sparse_map.get(key)
        dense_rank = d["rank"] if d else None
        sparse_rank = s["rank"] if s else None
        dense_score = d["score"] if d else None
        sparse_score = s["score"] if s else None
        payload = (d or s or {}).get("payload") or {}
        item_id = (d or s or {}).get("id")
        fusion_score = 0.0
        if dense_rank is not None:
            fusion_score += 1.0 / float(rrf_k + dense_rank)
        if sparse_rank is not None:
            fusion_score += 1.0 / float(rrf_k + sparse_rank)
        fused.append(
            {
                "id": item_id,
                "payload": payload,
                "dense_rank": dense_rank,
                "sparse_rank": sparse_rank,
                "dense_score": dense_score,
                "sparse_score": sparse_score,
                "fusion_score": fusion_score,
                "pre_rerank_rank": None,
                "post_rerank_rank": None,
                "rerank_score": None,
            }
        )

    fused.sort(key=lambda x: (x["fusion_score"], x["dense_score"] or 0.0, x["sparse_score"] or 0.0), reverse=True)
    for idx, item in enumerate(fused, start=1):
        item["pre_rerank_rank"] = idx
    return fused


def build_retrieval_metadata(
    *,
    mode: str,
    hybrid: bool,
    hybrid_capable: bool,
    dense_k: int,
    sparse_k: int,
    fetch_k: int,
    dense_count: int,
    sparse_count: int,
    fused_count: int,
    rerank_enabled: bool,
    rerank_applied: bool,
    rerank_reason: str,
    rerank_model: str | None,
    rerank_count: int,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "hybrid": hybrid,
        "hybrid_capable": hybrid_capable,
        "dense_k": dense_k,
        "sparse_k": sparse_k,
        "fetch_k": fetch_k,
        "fusion_method": "rrf" if hybrid or mode in {"dense", "sparse"} else "none",
        "candidates": {
            "dense": dense_count,
            "sparse": sparse_count,
            "fused": fused_count,
        },
        "rerank": {
            "enabled": rerank_enabled,
            "applied": rerank_applied,
            "reason": rerank_reason,
            "model": rerank_model,
            "count": rerank_count,
        },
    }


def candidate_to_public_chunk(
    candidate: dict[str, Any],
    rank: int,
    max_content_chars: int = 1600,
) -> dict[str, Any]:
    payload = candidate.get("payload") or {}
    content = _content_from_payload(payload)
    if max_content_chars and len(content) > max_content_chars:
        content = content[:max_content_chars].rstrip() + "…"

    out: dict[str, Any] = {
        "chunk_id": payload.get("chunk_id") or str(candidate.get("id") or ""),
        "source_url": payload.get("source_url") or "",
        "scores": {
            "dense": candidate.get("dense_score"),
            "sparse": candidate.get("sparse_score"),
            "fusion": candidate.get("fusion_score"),
            "rerank": candidate.get("rerank_score"),
        },
        "rank": {
            "pre_rerank": candidate.get("pre_rerank_rank", rank),
            "post_rerank": candidate.get("post_rerank_rank", rank),
        },
        "content": content,
    }

    if payload.get("page_number") is not None:
        out["page_number"] = payload.get("page_number")
    if payload.get("semantic_region"):
        out["semantic_region"] = payload.get("semantic_region")
    if payload.get("title"):
        out["title"] = payload.get("title")
    if payload.get("headings"):
        out["headings"] = _as_list(payload.get("headings"))
    if payload.get("heading_path"):
        out["heading_path"] = _as_list(payload.get("heading_path"))
    if payload.get("line_range"):
        out["line_range"] = _as_list(payload.get("line_range"))
    elif payload.get("line_start") is not None or payload.get("line_end") is not None:
        out["line_range"] = [int(payload.get("line_start") or 0), int(payload.get("line_end") or 0)]

    return out


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "build_cache_key",
    "build_retrieval_metadata",
    "build_semantic_cache_payload",
    "cache_payload_to_response",
    "candidate_to_public_chunk",
    "canonicalize_text",
    "decode_cached_chunks",
    "is_payload_expired",
    "iso_now_z",
    "normalize_query",
    "rrf_fuse",
    "sha256_hex_str",
    "stable_uuid_from_text",
]