#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from clients import AsyncBedrockClient, AsyncDenseClient, AsyncRerankerClient, AsyncSparseClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from helpers import build_prompt_and_ui_chunks, deterministic_summarize, validate_and_filter_citations
from prometheus_client import make_asgi_app
from settings import (
    ANSWER_PROMPT_TEMPLATE,
    AWS_REGION,
    BEDROCK_GUARDRAIL_IDENTIFIER,
    BEDROCK_GUARDRAIL_VERSION,
    BEDROCK_MODEL_ID,
    CACHE_SCORE_THRESHOLD,
    CACHE_TTL_SECONDS,
    COLLECTION_NAME,
    CORPUS_VERSION,
    DENSE_URL,
    ENABLE_PROMETHEUS,
    FETCH_K,
    HTTP_TIMEOUT,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LOG_LEVEL,
    MAX_CHUNKS_TO_LLM,
    MAX_CONCURRENT_REQUESTS,
    PROMPT_MAX_CONTENT_CHARS,
    PROMPT_VERSION,
    QDRANT_API_KEY,
    QDRANT_URL,
    RERANKER_URL,
    RETRIEVAL_VERSION,
    SPARSE_URL,
    TENANT_ID,
    GenerateRequest,
)
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from store import QdrantStore, QdrantStoreConfig
from telemetry import log, safe_stack, setup_logging

logger = logging.getLogger("retrieval")
startup_bootstrap_error: str | None = None

_MAX_TOP_K = 7
_MIN_FETCH_K = 10
_MAX_FETCH_K = 50

# ---------------------------------------------------------------------------
# Rate limiting (IP‑based, auth removed)
# ---------------------------------------------------------------------------
def _rate_limit_key(request: Request) -> str:
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(LOG_LEVEL)

    # Initialize service components
    settings = {
        "corpus_version": CORPUS_VERSION,
        "prompt_version": PROMPT_VERSION,
        "retrieval_version": RETRIEVAL_VERSION,
        "llm_model": BEDROCK_MODEL_ID,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "cache_score_threshold": CACHE_SCORE_THRESHOLD,
        "max_chunks_to_llm": MAX_CHUNKS_TO_LLM,
        "reranker_model": os.getenv("RERANKER_MODEL", "cross-encoder"),
    }

    store = QdrantStore(
        QdrantStoreConfig(
            url=QDRANT_URL,
            api_key=QDRANT_API_KEY or "",
            docs_collection=COLLECTION_NAME,
            cache_collection=os.getenv("CACHE_COLLECTION_NAME", f"{COLLECTION_NAME}__semantic_cache"),
            dense_dim=int(os.getenv("DENSE_DIM", "384")),
        )
    )

    dense = AsyncDenseClient(DENSE_URL, timeout=HTTP_TIMEOUT)
    sparse = AsyncSparseClient(SPARSE_URL, timeout=HTTP_TIMEOUT)
    reranker = AsyncRerankerClient(RERANKER_URL, timeout=HTTP_TIMEOUT)
    bedrock = AsyncBedrockClient(
        region=AWS_REGION,
        model_id=BEDROCK_MODEL_ID,
        guardrail_identifier=BEDROCK_GUARDRAIL_IDENTIFIER,
        guardrail_version=BEDROCK_GUARDRAIL_VERSION,
        timeout=HTTP_TIMEOUT,
    )

    from pipeline import ServiceState, _cache_cleanup_loop, _health_loop, _new_breakers

    state = ServiceState(
        settings=settings,
        store=store,
        dense=dense,
        sparse=sparse,
        reranker=reranker,
        bedrock=bedrock,
        breakers=_new_breakers(),
        health={
            "qdrant": False,
            "docs_collection_ready": False,
            "cache_collection_ready": False,
            "dense": False,
            "sparse": False,
            "reranker": False,
            "bedrock": bool(bedrock.health()),
            "hybrid_capable": False,
            "ready": False,
        },
        semaphore=asyncio.Semaphore(MAX_CONCURRENT_REQUESTS),
    )
    app.state.state = state

    global startup_bootstrap_error
    startup_bootstrap_error = None
    try:
        docs_ready, cache_ready = await state.store.bootstrap()
        state.store.docs_ready = docs_ready
        state.store.cache_ready = cache_ready
        log.info(
            "store bootstrap complete",
            docs_ready=docs_ready,
            cache_ready=cache_ready,
        )
    except asyncio.CancelledError:
        log.info("store bootstrap cancelled during shutdown")
        # Still yield to allow cleanup
    except Exception as e:
        startup_bootstrap_error = str(e)
        log.warn("bootstrap pending", error=str(e))

    bg_health = asyncio.create_task(_health_loop(state))
    bg_cleanup = asyncio.create_task(_cache_cleanup_loop(state))
    app.state.background_tasks = (bg_health, bg_cleanup)
    
    log.info("retriever service started successfully")

    try:
        yield
    finally:
        # Graceful shutdown
        log.info("shutting down retriever service")
        
        # Mark as not ready
        try:
            app.state.state.health["ready"] = False
        except Exception:
            pass
        
        # Cancel background tasks
        for task in app.state.background_tasks:
            if not task.done():
                task.cancel()
        
        # Wait for tasks to finish (handle cancellation gracefully)
        for task in app.state.background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected during shutdown
            except Exception:
                pass
        
        # Close clients
        for client, name in [(dense, "dense"), (sparse, "sparse"), (reranker, "reranker")]:
            try:
                await client.close()
                log.debug(f"{name} client closed")
            except Exception as e:
                log.debug(f"{name} client close error: {e}")
        
        # Close store
        try:
            await store.close()
            log.debug("store closed")
        except Exception as e:
            log.debug(f"store close error: {e}")
        
        log.info("retriever service shutdown complete")

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Prometheus endpoint (replaces /metrics)
# ---------------------------------------------------------------------------
if ENABLE_PROMETHEUS:
    # Mount ASGI app for prometheus_client
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

# Add instrumentator for automatic HTTP metrics (optional)
# Instrumentator().instrument(app).expose(app)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warn(
        "request validation failed",
        error=str(exc),
        endpoint=str(request.url.path),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    log.error(
        "unhandled exception",
        endpoint=str(request.url.path),
        error=str(exc),
        stack=safe_stack(exc),
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


# ---------------------------------------------------------------------------
# Helper: resolve state & build prompt
# ---------------------------------------------------------------------------
def _state():
    return app.state.state


def _build_bedrock_prompt(query: str, docs_for_llm: list[dict[str, Any]]) -> tuple[str, list[str], list[dict[str, Any]]]:
    prompt_body, llm_lines, ui_chunks = build_prompt_and_ui_chunks(
        docs_for_llm,
        query,
        max_content_chars=PROMPT_MAX_CONTENT_CHARS,
        prefer_snippet_len=400,
    )
    prompt = ANSWER_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)
    return prompt, llm_lines, ui_chunks


def _normalize_generation_limits(req: GenerateRequest) -> tuple[int, int, int]:
    top_k = max(1, min(_MAX_TOP_K, int(req.top_k or 5)))
    fetch_k = max(_MIN_FETCH_K, min(_MAX_FETCH_K, int(req.fetch_k or FETCH_K)))
    max_tokens = max(64, min(4096, int(req.max_tokens or LLM_MAX_TOKENS)))
    return top_k, fetch_k, max_tokens


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


# ---------------------------------------------------------------------------
# Core streaming endpoint (no auth)
# ---------------------------------------------------------------------------
async def _generate_stream_core(request: Request) -> StreamingResponse:
    req = await _load_generate_request(request)
    top_k, fetch_k, max_tokens = _normalize_generation_limits(req)
    state = _state()
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    from pipeline import _build_pipeline_result  # local import to avoid circular deps

    pipeline = await _build_pipeline_result(
        state,
        query=query,
        top_k=top_k,
        fetch_k=fetch_k,
        corpus_version=req.corpus_version or CORPUS_VERSION,
        prompt_version=req.prompt_version or PROMPT_VERSION,
        retrieval_version=req.retrieval_version or RETRIEVAL_VERSION,
        model_name=req.model_name or BEDROCK_MODEL_ID,
        tenant_id=req.tenant_id or TENANT_ID,
        allow_semantic_cache=bool(req.allow_semantic_cache),
        allow_rerank=True,
        include_answer=False,
        max_tokens=max_tokens,
    )

    cache_state: dict[str, Any] = {"answer": None, "chunks": None}

    async def finalize_cache() -> None:
        if pipeline.cache_hit:
            return
        if not pipeline.final_candidates:
            return
        answer = str(cache_state.get("answer") or "").strip()
        chunks = cache_state.get("chunks") or []
        if answer and answer not in {"no documents retrieved", "llm unavailable"}:
            from pipeline import write_stream_cache

            await write_stream_cache(
                state,
                pipeline=pipeline,
                answer=answer,
                ui_chunks=chunks,
                hit_type="llm",
                cache_score=1.0,
            )

    async def event_gen() -> AsyncIterator[str]:
        start_event = {
            "query": query,
            "retrieval": pipeline.retrieval,
            "cache": pipeline.cache,
            "cache_hit": pipeline.cache_hit,
            "cache_score": pipeline.cache_score,
            "retrieval_mode": pipeline.retrieval_mode,
            "hybrid_capable": pipeline.hybrid_capable,
            "chunks": pipeline.chunks if req.return_chunks else None,
        }
        yield _sse("start", start_event)

        # Cache hit – answer already available
        if pipeline.cache_hit and pipeline.answer is not None:
            cache_state["answer"] = pipeline.answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": pipeline.answer})
            yield _sse("done", {
                "answer": pipeline.answer,
                "chunks": pipeline.chunks if req.return_chunks else None,
                "retrieval": pipeline.retrieval,
                "cache": pipeline.cache,
                "cache_hit": True,
                "cache_score": pipeline.cache_score,
                "retrieval_mode": pipeline.retrieval_mode,
                "hybrid_capable": pipeline.hybrid_capable,
            })
            return

        docs_for_llm = pipeline.final_candidates[: min(len(pipeline.final_candidates), MAX_CHUNKS_TO_LLM)]
        if not docs_for_llm:
            answer = "no documents retrieved"
            cache_state["answer"] = answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": answer})
            yield _sse("done", {
                "answer": answer,
                "chunks": pipeline.chunks if req.return_chunks else None,
                "retrieval": pipeline.retrieval,
                "cache": pipeline.cache,
                "cache_hit": False,
                "cache_score": None,
                "retrieval_mode": pipeline.retrieval_mode,
                "hybrid_capable": pipeline.hybrid_capable,
            })
            return

        prompt, llm_lines, ui_chunks = _build_bedrock_prompt(query, docs_for_llm)
        answer_parts: list[str] = []

        if not state.bedrock.health():
            fallback = deterministic_summarize(llm_lines) or "llm unavailable"
            cache_state["answer"] = fallback
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": fallback})
            yield _sse("done", {
                "answer": fallback,
                "chunks": pipeline.chunks if req.return_chunks else None,
                "retrieval": pipeline.retrieval,
                "cache": pipeline.cache,
                "cache_hit": False,
                "cache_score": None,
                "retrieval_mode": pipeline.retrieval_mode,
                "hybrid_capable": pipeline.hybrid_capable,
            })
            return

        try:
            async for delta in state.bedrock.stream(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=LLM_TEMPERATURE,
            ):
                if await request.is_disconnected():
                    return
                if delta:
                    answer_parts.append(delta)
                    yield _sse("delta", {"text": delta})

            answer = "".join(answer_parts).strip()
            if not answer:
                answer = deterministic_summarize(llm_lines)

            valid_indexes = [c["index"] for c in ui_chunks if isinstance(c, dict) and c.get("index") is not None]
            answer = validate_and_filter_citations(answer, valid_indexes)
            if not answer.strip():
                answer = deterministic_summarize(llm_lines)

            cache_state["answer"] = answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []

            yield _sse("done", {
                "answer": answer,
                "chunks": pipeline.chunks if req.return_chunks else None,
                "retrieval": pipeline.retrieval,
                "cache": pipeline.cache,
                "cache_hit": pipeline.cache_hit,
                "cache_score": pipeline.cache_score,
                "retrieval_mode": pipeline.retrieval_mode,
                "hybrid_capable": pipeline.hybrid_capable,
            })
        except Exception as exc:
            fallback = deterministic_summarize(llm_lines) or f"llm call failed: {exc}"
            cache_state["answer"] = fallback
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("error", {"error": str(exc)})
            yield _sse("delta", {"text": fallback})
            yield _sse("done", {
                "answer": fallback,
                "chunks": pipeline.chunks if req.return_chunks else None,
                "retrieval": pipeline.retrieval,
                "cache": pipeline.cache,
                "cache_hit": pipeline.cache_hit,
                "cache_score": pipeline.cache_score,
                "retrieval_mode": pipeline.retrieval_mode,
                "hybrid_capable": pipeline.hybrid_capable,
            })

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(finalize_cache),
    )


async def _load_generate_request(request: Request) -> GenerateRequest:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid json body") from exc
    try:
        return GenerateRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.post("/generate/stream")
@limiter.limit("60/minute")
async def api_stream(request: Request):
    return await _generate_stream_core(request)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    state = _state()
    ready = bool(state.health.get("ready", False))
    return {
        "status": "ready" if ready else "not_ready",
        "service_ready": ready,
        "qdrant": bool(state.health.get("qdrant", False)),
        "docs_collection_ready": bool(state.health.get("docs_collection_ready", False)),
        "cache_collection_ready": bool(state.health.get("cache_collection_ready", False)),
        "dense": bool(state.health.get("dense", False)),
        "sparse": bool(state.health.get("sparse", False)),
        "reranker": bool(state.health.get("reranker", False)),
        "bedrock": bool(state.health.get("bedrock", False)),
        "hybrid_capable": bool(state.health.get("hybrid_capable", False)),
        "bootstrap_error": startup_bootstrap_error,
    }


if __name__ == "__main__":
    try:
        setup_logging(LOG_LEVEL)
    except Exception:
        logging.getLogger("retrieval").exception("failed to apply logging before uvicorn.run")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        log_level="info",
        loop=os.getenv("UVICORN_LOOP", "uvloop"),
        http=os.getenv("UVICORN_HTTP", "httptools"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
        access_log=False,
    )