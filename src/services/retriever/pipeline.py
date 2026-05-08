#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from clients import (
    AsyncBedrockClient,
    AsyncDenseClient,
    AsyncRerankerClient,
    AsyncSparseClient,
    CircuitBreaker,
    call_with_retry,
)
from fastapi import HTTPException
from helpers import (
    build_cache_key,
    build_retrieval_metadata,
    cache_payload_to_response,
    candidate_to_public_chunk,
    canonicalize_text,
    is_payload_expired,
    normalize_query,
    rrf_fuse,
    stable_uuid_from_text,
)
from citations_helpers import (
    build_numbered_prompt_and_ui_chunks,
    deterministic_summarize,
    validate_and_filter_citations,
)
from qdrant_client import models
from settings import (
    ANSWER_PROMPT_TEMPLATE,
    BEDROCK_MODEL_ID,
    BREAKER_FAILURE_THRESHOLD,
    BREAKER_RESET_TIMEOUT,
    CACHE_CLEANUP_INTERVAL_SECONDS,
    CACHE_SCORE_THRESHOLD,
    CACHE_TTL_SECONDS,
    CORPUS_VERSION,
    DEPLOYMENT_ENVIRONMENT,
    ENV,
    LLM_TEMPERATURE,
    MAX_CHUNKS_TO_LLM,
    MAX_PROMPT_CHARS,
    PROMPT_MAX_CONTENT_CHARS,
    PROMPT_VERSION,
    QUERY_TOPK_DENSE,
    QUERY_TOPK_SPARSE,
    RERANK_ALPHA,
    RERANK_AUTO_THRESHOLD,
    RERANK_MARGIN,
    RERANKER_MODE,
    RERANKER_TOP_K,
    RETRIEVAL_VERSION,
    RRF_K,
    SERVICE_NAME,
    TENANT_ID,
)
from retriever_logging import log
from metrics import (
    pipeline_duration,
    pipeline_errors,
    qdrant_query_count,
    qdrant_query_duration,
    cache_lookup_count,
    cache_lookup_duration,
    cache_write_count,
    cache_write_duration,
    service_ready,
    _metric_labels,
)

logger = logging.getLogger("retrieval.pipeline")

SHUTDOWN = False
startup_bootstrap_error: str | None = None
background_task: asyncio.Task | None = None
cleanup_task: asyncio.Task | None = None
_background_tasks: set[asyncio.Task[Any]] = set()

_READY_VALUE = 0
_METRICS_INITIALIZED = False


def initialize_pipeline_metrics() -> None:
    global _METRICS_INITIALIZED
    _METRICS_INITIALIZED = True


def _set_ready(value: bool) -> None:
    global _READY_VALUE
    _READY_VALUE = 1 if value else 0
    service_ready.labels(environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME).set(_READY_VALUE)


@dataclass
class ServiceState:
    settings: dict[str, Any]
    store: Any
    dense: AsyncDenseClient
    sparse: AsyncSparseClient
    reranker: AsyncRerankerClient
    bedrock: AsyncBedrockClient
    breakers: dict[str, CircuitBreaker]
    health: dict[str, bool]
    semaphore: asyncio.Semaphore


@dataclass
class PipelineResult:
    answer: str | None
    chunks: list[dict[str, Any]]
    retrieval: dict[str, Any]
    cache: dict[str, Any]
    cache_hit: bool
    cache_score: float | None
    retrieval_mode: str
    hybrid_capable: bool
    prompt: str | None
    llm_lines: list[str]
    ui_chunks: list[dict[str, Any]]
    final_candidates: list[dict[str, Any]]
    dense_vector: list[float] | None = None
    cache_id: str | None = None
    query_text: str | None = None
    query_norm: str | None = None
    corpus_version: str | None = None
    prompt_version: str | None = None
    retrieval_version: str | None = None
    model_name: str | None = None
    tenant_id: str | None = None


def _new_breakers() -> dict[str, CircuitBreaker]:
    return {
        "cache": CircuitBreaker("cache", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_TIMEOUT),
        "retrieval": CircuitBreaker("retrieval", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_TIMEOUT),
        "dense": CircuitBreaker("dense", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_TIMEOUT),
        "sparse": CircuitBreaker("sparse", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_TIMEOUT),
        "reranker": CircuitBreaker("reranker", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_TIMEOUT),
        "llm": CircuitBreaker("llm", BREAKER_FAILURE_THRESHOLD, BREAKER_RESET_TIMEOUT),
    }


def _make_settings() -> dict[str, Any]:
    return {
        "corpus_version": CORPUS_VERSION,
        "prompt_version": PROMPT_VERSION,
        "retrieval_version": RETRIEVAL_VERSION,
        "llm_model": BEDROCK_MODEL_ID,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "cache_score_threshold": CACHE_SCORE_THRESHOLD,
        "max_chunks_to_llm": MAX_CHUNKS_TO_LLM,
        "reranker_model": os.getenv("RERANKER_MODEL", "cross-encoder"),
    }


def _resolve_tenant_id(tenant_id: str | None) -> str | None:
    return (tenant_id or TENANT_ID or "").strip() or None


def _hybrid_capable(state: ServiceState) -> bool:
    return bool(state.health.get("hybrid_capable", False) or getattr(state.store, "hybrid_capable", False))


def _is_ready_for_retrieval(state: ServiceState) -> bool:
    return bool(state.health.get("docs_collection_ready")) and bool(state.health.get("dense") or state.health.get("sparse"))


def _is_cache_ready(state: ServiceState) -> bool:
    return bool(state.health.get("cache_collection_ready"))


def _safe_cache_object(hit: bool, kind: str, score: float | None, cache_id: str | None) -> dict[str, Any]:
    return {"hit": bool(hit), "type": kind, "score": score, "id": cache_id}


def _semantic_cache_thresholds(state: ServiceState) -> tuple[float, float]:
    strict_default = min(0.84, float(CACHE_SCORE_THRESHOLD), float(state.store.config.cache_score_threshold))
    strict_env = os.getenv("SEMANTIC_CACHE_SCORE_THRESHOLD")
    relaxed_env = os.getenv("SEMANTIC_CACHE_RELAXED_SCORE_THRESHOLD")

    try:
        strict = float(strict_env) if strict_env is not None else strict_default
    except Exception:
        strict = strict_default
    strict = max(0.0, min(strict, 0.9999))

    relaxed_default = max(0.75, strict - 0.06)
    try:
        relaxed = float(relaxed_env) if relaxed_env is not None else relaxed_default
    except Exception:
        relaxed = relaxed_default
    relaxed = max(0.0, min(relaxed, strict))
    return strict, relaxed


def _build_exact_cache_result(
    *,
    payload: dict[str, Any],
    cache_kind: str,
    cache_score: float,
    retrieval_mode: str,
    hybrid_capable: bool,
    fetch_k: int,
    cache_id: str | None,
) -> PipelineResult:
    cache_resp = cache_payload_to_response(payload, cache_score=cache_score)
    retrieval = build_retrieval_metadata(
        mode=retrieval_mode,
        hybrid=False,
        hybrid_capable=hybrid_capable,
        dense_k=QUERY_TOPK_DENSE,
        sparse_k=QUERY_TOPK_SPARSE,
        fetch_k=fetch_k,
        dense_count=0,
        sparse_count=0,
        fused_count=0,
        rerank_enabled=False,
        rerank_applied=False,
        rerank_reason=cache_kind,
        rerank_model=None,
        rerank_count=0,
    )
    answer = cache_resp.get("answer") or ""
    chunks = cache_resp.get("chunks") if isinstance(cache_resp.get("chunks"), list) else []
    return PipelineResult(
        answer=answer,
        chunks=chunks,
        retrieval=retrieval,
        cache={"hit": True, "type": cache_kind, "score": float(cache_score), "id": cache_resp.get("cache_id") or cache_id},
        cache_hit=True,
        cache_score=float(cache_score),
        retrieval_mode=retrieval_mode,
        hybrid_capable=hybrid_capable,
        prompt=None,
        llm_lines=[],
        ui_chunks=chunks,
        final_candidates=[],
        dense_vector=None,
        cache_id=cache_resp.get("cache_id") or cache_id,
    )


def _decide_rerank(results: list[dict[str, Any]]) -> tuple[bool, str]:
    if not results:
        return False, "no_candidates"
    if RERANKER_MODE == "DISABLE":
        return False, "disabled"
    if RERANKER_MODE == "ALWAYS":
        return True, "configured_always"
    top_score = float(results[0].get("fusion_score", 0.0) or 0.0)
    second_score = float(results[1].get("fusion_score", 0.0) or 0.0) if len(results) > 1 else 0.0
    if top_score < RERANK_AUTO_THRESHOLD:
        return True, "low_fusion_confidence"
    if (top_score - second_score) < RERANK_MARGIN:
        return True, "close_fusion_scores"
    return False, "not_necessary"


def _softmax(arr: list[float]) -> list[float]:
    a = np.asarray(arr, dtype=float)
    if a.size == 0:
        return []
    a = a - np.max(a)
    e = np.exp(a)
    s = e.sum()
    if s <= 0:
        return (np.ones_like(a) / len(a)).tolist()
    return (e / s).tolist()


async def _rerank_candidates(state: ServiceState, query: str, fused: list[dict[str, Any]], fetch_k: int) -> dict[str, Any]:
    should_rerank, reason = _decide_rerank(fused)
    if not should_rerank:
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        return {"candidates": fused, "enabled": False, "applied": False, "reason": reason, "model": None, "count": 0}

    candidate_count = min(len(fused), max(1, min(fetch_k, RERANKER_TOP_K)))
    rerank_pool = fused[:candidate_count]
    docs: list[str] = []
    for c in rerank_pool:
        payload = c.get("payload") or {}
        docs.append(canonicalize_text(payload.get("content") or payload.get("text") or payload.get("html") or ""))

    async def _do():
        return await state.reranker.rerank(query=query, documents=docs)

    try:
        scores = await call_with_retry("reranker", state.breakers["reranker"], _do)
    except Exception as exc:
        log.warn("rerank skipped", error=str(exc))
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        return {"candidates": fused, "enabled": True, "applied": False, "reason": f"reranker_failed:{type(exc).__name__}", "model": state.settings.get("reranker_model"), "count": candidate_count}

    if not scores or len(scores) != len(rerank_pool):
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        return {"candidates": fused, "enabled": True, "applied": False, "reason": "invalid_reranker_output", "model": state.settings.get("reranker_model"), "count": candidate_count}

    fused_scores = [float(c.get("fusion_score", 0.0) or 0.0) for c in rerank_pool]
    fused_norm = _softmax(fused_scores)
    rerank_norm = _softmax([float(x) for x in scores])
    combined = [(RERANK_ALPHA * r) + ((1.0 - RERANK_ALPHA) * f) for r, f in zip(rerank_norm, fused_norm, strict=True)]

    order = list(np.argsort(-np.asarray(combined, dtype=float)))
    reranked_pool = [dict(rerank_pool[i]) for i in order]
    for idx, item in enumerate(reranked_pool, start=1):
        source_idx = order[idx - 1]
        item["rerank_score"] = float(scores[source_idx])
        item["post_rerank_rank"] = idx
        item["combined_score"] = float(combined[source_idx])

    remainder = fused[candidate_count:]
    for idx, item in enumerate(remainder, start=candidate_count + 1):
        item["post_rerank_rank"] = idx

    final = reranked_pool + remainder
    for idx, item in enumerate(final, start=1):
        item["post_rerank_rank"] = idx

    return {"candidates": final, "enabled": True, "applied": True, "reason": reason, "model": state.settings.get("reranker_model"), "count": candidate_count}


def _visible_chunk_list(candidates: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    return [candidate_to_public_chunk(c, rank=idx, max_content_chars=max_chars) for idx, c in enumerate(candidates, start=1)]


async def _search_docs(
    state: ServiceState,
    dense_vec: list[float] | None,
    sparse_vec: models.SparseVector | None,
    fetch_k: int,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    q_filter = None
    mode = "none"
    dense_results: list[dict[str, Any]] = []
    sparse_results: list[dict[str, Any]] = []
    start = time.perf_counter()

    if dense_vec is not None and sparse_vec is not None and _hybrid_capable(state):
        async def _dense():
            return await state.store.dense_search(query_vector=dense_vec, query_filter=q_filter, limit=fetch_k)
        async def _sparse():
            return await state.store.sparse_search(query_vector=sparse_vec, query_filter=q_filter, limit=fetch_k)

        dense_task = asyncio.create_task(call_with_retry("retrieval", state.breakers["retrieval"], _dense))
        sparse_task = asyncio.create_task(call_with_retry("retrieval", state.breakers["retrieval"], _sparse))
        dense_res, sparse_res = await asyncio.gather(dense_task, sparse_task, return_exceptions=True)
        if isinstance(dense_res, list):
            dense_results = dense_res
        if isinstance(sparse_res, list):
            sparse_results = sparse_res
        mode = "hybrid"
    elif dense_vec is not None:
        async def _dense():
            return await state.store.dense_search(query_vector=dense_vec, query_filter=q_filter, limit=fetch_k)
        try:
            dense_results = await call_with_retry("retrieval", state.breakers["retrieval"], _dense)
        except Exception as exc:
            log.warn("dense retrieval failed", error=str(exc))
        mode = "dense"
    elif sparse_vec is not None:
        async def _sparse():
            return await state.store.sparse_search(query_vector=sparse_vec, query_filter=q_filter, limit=fetch_k)
        try:
            sparse_results = await call_with_retry("retrieval", state.breakers["retrieval"], _sparse)
        except Exception as exc:
            log.warn("sparse retrieval failed", error=str(exc))
        mode = "sparse"

    fused = rrf_fuse(dense_results, sparse_results, rrf_k=RRF_K)
    debug = {
        "candidates": {"dense": len(dense_results), "sparse": len(sparse_results), "fused": len(fused)},
        "hybrid": bool(dense_results and sparse_results),
        "fusion_method": "rrf" if fused else "none",
    }

    qdrant_query_count.labels(**_metric_labels(mode=mode)).inc()
    qdrant_query_duration.labels(**_metric_labels(mode=mode)).observe(max(time.perf_counter() - start, 1e-6))

    if not fused:
        log.warn("zero search results", mode=mode, dense_count=len(dense_results), sparse_count=len(sparse_results))

    return fused, mode, debug


async def _semantic_cache_promote_exact(
    state: ServiceState,
    *,
    cache_id: str,
    dense_vec: list[float],
    query: str,
    query_norm: str,
    corpus_version: str,
    prompt_version: str,
    retrieval_version: str,
    model_name: str,
    answer: str,
    chunks: list[dict[str, Any]],
    cache_score: float,
    hit_type: str = "semantic",
) -> None:
    start = time.perf_counter()
    async def _write():
        return await state.store.semantic_cache_upsert(
            cache_id=cache_id, query_vector=dense_vec, query_text=query, query_norm=query_norm,
            corpus_version=corpus_version, prompt_version=prompt_version,
            retrieval_version=retrieval_version, model_name=model_name,
            answer=answer, ui_chunks=chunks,
            ttl_seconds=state.store.config.cache_ttl_seconds,
            hit_type=hit_type, cache_score=cache_score,
        )

    try:
        await call_with_retry("cache", state.breakers["cache"], _write)
        cache_write_count.labels(**_metric_labels(result="ok", cache_kind="promotion")).inc()
    except Exception:
        cache_write_count.labels(**_metric_labels(result="fail", cache_kind="promotion")).inc()
        raise
    finally:
        cache_write_duration.labels(**_metric_labels(cache_kind="promotion")).observe(max(time.perf_counter() - start, 1e-6))


async def write_stream_cache(
    state: ServiceState,
    *,
    pipeline: PipelineResult,
    answer: str,
    ui_chunks: list[dict[str, Any]],
    hit_type: str = "llm",
    cache_score: float = 1.0,
) -> bool:
    if not pipeline.cache_id or pipeline.dense_vector is None:
        return False
    if not answer.strip():
        return False

    start = time.perf_counter()
    try:
        ok = await state.store.semantic_cache_upsert(
            cache_id=pipeline.cache_id, query_vector=pipeline.dense_vector,
            query_text=pipeline.query_text or "", query_norm=pipeline.query_norm or "",
            corpus_version=pipeline.corpus_version or "", prompt_version=pipeline.prompt_version or "",
            retrieval_version=pipeline.retrieval_version or "",
            model_name=pipeline.model_name or BEDROCK_MODEL_ID,
            answer=answer, ui_chunks=ui_chunks,
            ttl_seconds=state.store.config.cache_ttl_seconds,
            hit_type=hit_type, cache_score=cache_score,
        )
        cache_write_count.labels(**_metric_labels(result="ok" if ok else "fail", cache_kind=hit_type)).inc()
        return ok
    except Exception:
        cache_write_count.labels(**_metric_labels(result="fail", cache_kind=hit_type)).inc()
        return False
    finally:
        cache_write_duration.labels(**_metric_labels(cache_kind=hit_type)).observe(max(time.perf_counter() - start, 1e-6))


async def _call_llm(
    state: ServiceState,
    query: str,
    docs_for_llm: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    prompt_body, llm_lines, ui_chunks = build_numbered_prompt_and_ui_chunks(
        docs_for_llm, query, max_content_chars=PROMPT_MAX_CONTENT_CHARS, prefer_snippet_len=400,
    )
    prompt = ANSWER_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)

    if not state.bedrock.health():
        return deterministic_summarize(llm_lines), llm_lines, ui_chunks

    async def _do():
        return await state.bedrock.generate(prompt=prompt, max_tokens=max_tokens, temperature=LLM_TEMPERATURE)

    try:
        answer = await call_with_retry("llm", state.breakers["llm"], _do)
    except Exception as e:
        log.warn("bedrock failed, using deterministic fallback", error=str(e))
        answer = deterministic_summarize(llm_lines)
    return answer, llm_lines, ui_chunks


async def _build_pipeline_result(
    state: ServiceState,
    *,
    query: str,
    top_k: int,
    fetch_k: int,
    corpus_version: str,
    prompt_version: str,
    retrieval_version: str,
    model_name: str,
    tenant_id: str | None = None,
    allow_semantic_cache: bool,
    allow_rerank: bool,
    include_answer: bool,
    max_tokens: int,
) -> PipelineResult:
    query = (query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    initialize_pipeline_metrics()

    start = time.perf_counter()
    outcome = "ok"

    try:
        resolved_tenant = _resolve_tenant_id(tenant_id)
        query_norm = normalize_query(query)
        cache_key = build_cache_key(
            query_norm=query_norm, corpus_version=corpus_version,
            prompt_version=prompt_version, retrieval_version=retrieval_version,
            model_name=model_name, tenant_id=resolved_tenant, top_k=top_k, fetch_k=fetch_k,
        )
        cache_id = stable_uuid_from_text(cache_key)
        query_embed_text = canonicalize_text(query)
        cache_ready = _is_cache_ready(state)

        if cache_ready and allow_semantic_cache:
            async def _exact_cache():
                return await state.store.semantic_cache_get_by_id(cache_id)

            exact = None
            exact_start = time.perf_counter()
            try:
                exact = await call_with_retry("cache", state.breakers["cache"], _exact_cache)
            except Exception:
                exact = None

            cache_lookup_count.labels(**_metric_labels(result="exact_hit" if exact else "miss")).inc()
            cache_lookup_duration.labels(**_metric_labels(result="exact")).observe(max(time.perf_counter() - exact_start, 1e-6))

            if exact and exact.get("payload") and not is_payload_expired(exact["payload"]):
                pipe = _build_exact_cache_result(
                    payload=exact["payload"], cache_kind="exact", cache_score=1.0,
                    retrieval_mode="exact_cache", hybrid_capable=_hybrid_capable(state),
                    fetch_k=fetch_k, cache_id=cache_id,
                )
                pipe.query_text = query
                pipe.query_norm = query_norm
                pipe.corpus_version = corpus_version
                pipe.prompt_version = prompt_version
                pipe.retrieval_version = retrieval_version
                pipe.model_name = model_name
                pipe.tenant_id = resolved_tenant
                pipeline_duration.labels(**_metric_labels(outcome="cache_hit")).observe(max(time.perf_counter() - start, 1e-6))
                return pipe

        if not _is_ready_for_retrieval(state):
            raise HTTPException(status_code=503, detail="no retriever backends available")

        dense_task = None
        sparse_task = None
        if state.health.get("dense"):
            async def _dense():
                return await state.dense.embed([query_embed_text])
            dense_task = asyncio.create_task(call_with_retry("dense", state.breakers["dense"], _dense))
        if state.health.get("sparse"):
            async def _sparse():
                return await state.sparse.embed_chunked([query_embed_text])
            sparse_task = asyncio.create_task(call_with_retry("sparse", state.breakers["sparse"], _sparse))

        dense_vec: list[float] | None = None
        sparse_vec: models.SparseVector | None = None

        if dense_task is not None:
            try:
                dense_res = await dense_task
                dense_vec = dense_res[0] if dense_res else None
            except Exception as e:
                log.warn("dense embed failed", error=str(e))
                dense_vec = None
        if sparse_task is not None:
            try:
                sparse_res = await sparse_task
                if sparse_res:
                    s0 = sparse_res[0]
                    sparse_vec = models.SparseVector(
                        indices=[int(x) for x in s0.get("indices", [])],
                        values=[float(x) for x in s0.get("values", [])],
                    )
            except Exception as e:
                log.warn("sparse embed failed", error=str(e))
                sparse_vec = None

        if cache_ready and allow_semantic_cache and dense_vec is not None:
            strict_threshold, relaxed_threshold = _semantic_cache_thresholds(state)

            async def _semantic_lookup(min_score: float):
                return await state.store.semantic_cache_lookup(
                    query_vector=dense_vec, corpus_version=corpus_version,
                    prompt_version=prompt_version, retrieval_version=retrieval_version,
                    model_name=model_name, min_score=min_score,
                )

            semantic_start = time.perf_counter()
            semantic_hit = None
            semantic_kind = None

            try:
                semantic_hit = await call_with_retry("cache", state.breakers["cache"], lambda: _semantic_lookup(strict_threshold))
                semantic_kind = "semantic_strict"
            except Exception:
                semantic_hit = None

            if not semantic_hit:
                try:
                    semantic_hit = await call_with_retry("cache", state.breakers["cache"], lambda: _semantic_lookup(relaxed_threshold))
                    if semantic_hit:
                        semantic_kind = "semantic_relaxed"
                except Exception:
                    semantic_hit = None

            cache_lookup_count.labels(**_metric_labels(result=semantic_kind if semantic_hit else "miss")).inc()
            cache_lookup_duration.labels(**_metric_labels(result="semantic")).observe(max(time.perf_counter() - semantic_start, 1e-6))

            if semantic_hit and semantic_hit.get("payload"):
                payload = semantic_hit["payload"]
                cache_resp = cache_payload_to_response(payload, cache_score=float(semantic_hit.get("score") or payload.get("cache_score") or 1.0))
                cache_kind = semantic_kind or "semantic"
                cache_score = float(cache_resp.get("cache_score") or payload.get("cache_score") or 1.0)
                retrieval = build_retrieval_metadata(
                    mode="semantic_cache", hybrid=False, hybrid_capable=_hybrid_capable(state),
                    dense_k=QUERY_TOPK_DENSE, sparse_k=QUERY_TOPK_SPARSE, fetch_k=fetch_k,
                    dense_count=0, sparse_count=0, fused_count=0,
                    rerank_enabled=False, rerank_applied=False, rerank_reason="semantic_cache",
                    rerank_model=None, rerank_count=0,
                )
                answer = cache_resp.get("answer") or ""
                chunks = cache_resp.get("chunks") if isinstance(cache_resp.get("chunks"), list) else []

                async def _promote() -> None:
                    try:
                        await _semantic_cache_promote_exact(
                            state, cache_id=cache_id, dense_vec=dense_vec, query=query, query_norm=query_norm,
                            corpus_version=corpus_version, prompt_version=prompt_version,
                            retrieval_version=retrieval_version, model_name=model_name,
                            answer=answer, chunks=chunks, cache_score=cache_score, hit_type="semantic",
                        )
                    except Exception as exc:
                        log.warn("semantic exact promotion failed", error=str(exc))

                task = asyncio.create_task(_promote())
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

                pipe = PipelineResult(
                    answer=answer, chunks=chunks, retrieval=retrieval,
                    cache={"hit": True, "type": cache_kind, "score": cache_score, "id": cache_resp.get("cache_id")},
                    cache_hit=True, cache_score=cache_score,
                    retrieval_mode="semantic_cache", hybrid_capable=_hybrid_capable(state),
                    prompt=None, llm_lines=[], ui_chunks=chunks, final_candidates=[],
                    dense_vector=dense_vec, cache_id=cache_id,
                    query_text=query, query_norm=query_norm,
                    corpus_version=corpus_version, prompt_version=prompt_version,
                    retrieval_version=retrieval_version, model_name=model_name, tenant_id=resolved_tenant,
                )
                pipeline_duration.labels(**_metric_labels(outcome="cache_hit")).observe(max(time.perf_counter() - start, 1e-6))
                return pipe

        async with state.semaphore:
            fused, retrieval_mode, retrieval_debug = await _search_docs(state, dense_vec, sparse_vec, fetch_k)

            if not fused:
                retrieval = build_retrieval_metadata(
                    mode=retrieval_mode, hybrid=bool(dense_vec is not None and sparse_vec is not None and _hybrid_capable(state)),
                    hybrid_capable=_hybrid_capable(state), dense_k=QUERY_TOPK_DENSE, sparse_k=QUERY_TOPK_SPARSE, fetch_k=fetch_k,
                    dense_count=retrieval_debug["candidates"]["dense"],
                    sparse_count=retrieval_debug["candidates"]["sparse"],
                    fused_count=retrieval_debug["candidates"]["fused"],
                    rerank_enabled=False, rerank_applied=False, rerank_reason="no_results",
                    rerank_model=None, rerank_count=0,
                )
                pipe = PipelineResult(
                    answer="no documents retrieved" if include_answer else None,
                    chunks=[], retrieval=retrieval,
                    cache=_safe_cache_object(False, "miss" if cache_ready else "disabled", None, None),
                    cache_hit=False, cache_score=None,
                    retrieval_mode=retrieval_mode, hybrid_capable=_hybrid_capable(state),
                    prompt=None, llm_lines=[], ui_chunks=[], final_candidates=[],
                    dense_vector=dense_vec, cache_id=cache_id,
                    query_text=query, query_norm=query_norm,
                    corpus_version=corpus_version, prompt_version=prompt_version,
                    retrieval_version=retrieval_version, model_name=model_name, tenant_id=resolved_tenant,
                )
                pipeline_duration.labels(**_metric_labels(outcome="no_results")).observe(max(time.perf_counter() - start, 1e-6))
                return pipe

            if allow_rerank:
                rerank_info = await _rerank_candidates(state, query, fused, fetch_k)
            else:
                for idx, item in enumerate(fused, start=1):
                    item["post_rerank_rank"] = idx
                rerank_info = {"candidates": fused, "enabled": False, "applied": False, "reason": "request_disabled", "model": None, "count": 0}

            final_candidates = list(rerank_info["candidates"])[: max(1, min(top_k, len(fused)))]
            for idx, item in enumerate(final_candidates, start=1):
                item["post_rerank_rank"] = idx
                if item.get("rerank_score") is None and not rerank_info["applied"]:
                    item["rerank_score"] = None

            docs_for_llm = final_candidates[: min(len(final_candidates), MAX_CHUNKS_TO_LLM)]
            retrieval = build_retrieval_metadata(
                mode=retrieval_mode, hybrid=bool(dense_vec is not None and sparse_vec is not None and _hybrid_capable(state)),
                hybrid_capable=_hybrid_capable(state), dense_k=QUERY_TOPK_DENSE, sparse_k=QUERY_TOPK_SPARSE, fetch_k=fetch_k,
                dense_count=retrieval_debug["candidates"]["dense"],
                sparse_count=retrieval_debug["candidates"]["sparse"],
                fused_count=retrieval_debug["candidates"]["fused"],
                rerank_enabled=bool(rerank_info["enabled"]),
                rerank_applied=bool(rerank_info["applied"]),
                rerank_reason=str(rerank_info["reason"]),
                rerank_model=rerank_info["model"],
                rerank_count=int(rerank_info["count"]),
            )

            if not include_answer:
                chunks = _visible_chunk_list(final_candidates, 1600)
                pipe = PipelineResult(
                    answer=None, chunks=chunks, retrieval=retrieval,
                    cache=_safe_cache_object(False, "disabled" if not cache_ready else "miss", None, None),
                    cache_hit=False, cache_score=None,
                    retrieval_mode=retrieval_mode, hybrid_capable=_hybrid_capable(state),
                    prompt=build_numbered_prompt_and_ui_chunks(docs_for_llm, query, max_content_chars=PROMPT_MAX_CONTENT_CHARS)[0],
                    llm_lines=[], ui_chunks=chunks, final_candidates=final_candidates,
                    dense_vector=dense_vec, cache_id=cache_id,
                    query_text=query, query_norm=query_norm,
                    corpus_version=corpus_version, prompt_version=prompt_version,
                    retrieval_version=retrieval_version, model_name=model_name, tenant_id=resolved_tenant,
                )
                pipeline_duration.labels(**_metric_labels(outcome="ok")).observe(max(time.perf_counter() - start, 1e-6))
                return pipe

            answer, llm_lines, ui_chunks = await _call_llm(state, query, docs_for_llm, max_tokens=max_tokens)
            valid_indexes = [c["index"] for c in ui_chunks if isinstance(c, dict) and c.get("index") is not None]
            answer = validate_and_filter_citations(answer, valid_indexes)
            if not answer.strip():
                answer = deterministic_summarize(llm_lines)
            if len(answer) > MAX_PROMPT_CHARS:
                answer = answer[:MAX_PROMPT_CHARS].rstrip()

            output_chunks = _visible_chunk_list(final_candidates, 1600)
            if cache_ready and dense_vec is not None:
                try:
                    final_chunks_for_cache = []
                    for idx, cand in enumerate(final_candidates, start=1):
                        final_chunks_for_cache.append(candidate_to_public_chunk(cand, rank=idx, max_content_chars=1600))

                    async def _cache_write():
                        return await state.store.semantic_cache_upsert(
                            cache_id=cache_id, query_vector=dense_vec,
                            query_text=query, query_norm=query_norm,
                            corpus_version=corpus_version, prompt_version=prompt_version,
                            retrieval_version=retrieval_version, model_name=model_name,
                            answer=answer, ui_chunks=final_chunks_for_cache,
                            ttl_seconds=state.store.config.cache_ttl_seconds,
                            hit_type="llm", cache_score=1.0,
                        )

                    write_start = time.perf_counter()
                    await call_with_retry("cache", state.breakers["cache"], _cache_write)
                    cache_write_count.labels(**_metric_labels(result="ok", cache_kind="llm")).inc()
                    cache_write_duration.labels(**_metric_labels(cache_kind="llm")).observe(max(time.perf_counter() - write_start, 1e-6))
                except Exception:
                    cache_write_count.labels(**_metric_labels(result="fail", cache_kind="llm")).inc()

            pipe = PipelineResult(
                answer=answer, chunks=output_chunks, retrieval=retrieval,
                cache=_safe_cache_object(False, "miss" if cache_ready else "disabled", None, None),
                cache_hit=False, cache_score=None,
                retrieval_mode=retrieval_mode, hybrid_capable=_hybrid_capable(state),
                prompt=build_numbered_prompt_and_ui_chunks(docs_for_llm, query, max_content_chars=PROMPT_MAX_CONTENT_CHARS)[0],
                llm_lines=llm_lines, ui_chunks=ui_chunks, final_candidates=final_candidates,
                dense_vector=dense_vec, cache_id=cache_id,
                query_text=query, query_norm=query_norm,
                corpus_version=corpus_version, prompt_version=prompt_version,
                retrieval_version=retrieval_version, model_name=model_name, tenant_id=resolved_tenant,
            )
            pipeline_duration.labels(**_metric_labels(outcome="ok")).observe(max(time.perf_counter() - start, 1e-6))
            return pipe

    except HTTPException:
        raise
    except Exception as exc:
        outcome = "error"
        pipeline_errors.labels(error_type=type(exc).__name__, **_metric_labels()).inc()
        pipeline_duration.labels(**_metric_labels(outcome="error")).observe(max(time.perf_counter() - start, 1e-6))
        raise


async def _cache_cleanup_loop(state: ServiceState) -> None:
    while not SHUTDOWN:
        try:
            if state.store.cache_ready:
                await state.store.cleanup_expired_cache()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warn("cache cleanup failed", error=str(e))

        for _ in range(int(CACHE_CLEANUP_INTERVAL_SECONDS * 2)):
            if SHUTDOWN:
                break
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
        if SHUTDOWN:
            break


async def _health_loop(state: ServiceState) -> None:
    last_snapshot: dict[str, bool] | None = None
    while not SHUTDOWN:
        try:
            if not state.store.docs_ready or not state.store.cache_ready:
                try:
                    docs_ready, cache_ready = await state.store.bootstrap()
                    state.store.docs_ready = docs_ready
                    state.store.cache_ready = cache_ready
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

            qdrant_ok = await state.store.ping()
            dense_ok = await state.dense.health()
            sparse_ok = await state.sparse.health()
            reranker_ok = await state.reranker.health()
            bedrock_ok = bool(state.bedrock.health())

            snapshot = {
                "qdrant": qdrant_ok,
                "docs_collection_ready": bool(state.store.docs_ready),
                "cache_collection_ready": bool(state.store.cache_ready),
                "dense": dense_ok,
                "sparse": sparse_ok,
                "reranker": reranker_ok,
                "bedrock": bedrock_ok,
                "hybrid_capable": bool(dense_ok and sparse_ok and state.store.docs_ready),
                "ready": bool(state.store.docs_ready and (dense_ok or sparse_ok) and qdrant_ok),
            }
            state.health = snapshot
            _set_ready(snapshot["ready"])
            if snapshot != last_snapshot:
                log.info("health status changed", health=snapshot)
                last_snapshot = dict(snapshot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            state.health = {
                "qdrant": False, "docs_collection_ready": False, "cache_collection_ready": False,
                "dense": False, "sparse": False, "reranker": False,
                "bedrock": bool(state.bedrock.health()),
                "hybrid_capable": False, "ready": False,
            }
            _set_ready(False)
            log.warn("health loop failed", error=str(e))

        for _ in range(20):
            if SHUTDOWN:
                break
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
        if SHUTDOWN:
            break


__all__ = [
    "SHUTDOWN",
    "PipelineResult",
    "ServiceState",
    "_build_pipeline_result",
    "_cache_cleanup_loop",
    "_health_loop",
    "_make_settings",
    "_new_breakers",
    "_safe_cache_object",
    "background_task",
    "cleanup_task",
    "initialize_pipeline_metrics",
    "startup_bootstrap_error",
    "write_stream_cache",
]