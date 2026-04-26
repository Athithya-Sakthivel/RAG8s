#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from clients import AsyncBedrockClient, AsyncDenseClient, AsyncRerankerClient, AsyncSparseClient
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from helpers import build_prompt_and_ui_chunks, deterministic_summarize, validate_and_filter_citations
from pipeline import (
    ServiceState,
    _build_pipeline_result,
    _cache_cleanup_loop,
    _health_loop,
    _new_breakers,
    write_stream_cache,
)
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
    ENV,
    HTTP_TIMEOUT,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    MAX_CHUNKS_TO_LLM,
    MAX_CONCURRENT_REQUESTS,
    PROMPT_MAX_CONTENT_CHARS,
    PROMPT_VERSION,
    QDRANT_API_KEY,
    QDRANT_URL,
    RERANKER_URL,
    RETRIEVAL_VERSION,
    SERVICE_NAME,
    SPARSE_URL,
    GenerateRequest,
    GenerateResponse,
    RetrieveRequest,
    RetrieveResponse,
)
from starlette.background import BackgroundTask
from store import QdrantStore, QdrantStoreConfig
from telemetry import (
    ERROR_COUNT,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    RETRIEVED_DOCS,
    SERVICE_READY,
    json_log,
    metrics_response,
    safe_stack,
    setup_logging,
)

# Do NOT call setup_logging() at import time when running under uvicorn CLI.
# Reapply logging after uvicorn config in startup (see on_startup below).

logger = logging.getLogger("retrieval")


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


def _state() -> ServiceState:
    return app.state.state


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _build_bedrock_prompt(query: str, docs_for_llm: list[dict[str, Any]]) -> tuple[str, list[str], list[dict[str, Any]]]:
    prompt_body, llm_lines, ui_chunks = build_prompt_and_ui_chunks(
        docs_for_llm,
        query,
        max_content_chars=PROMPT_MAX_CONTENT_CHARS,
        prefer_snippet_len=400,
    )
    prompt = ANSWER_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)
    return prompt, llm_lines, ui_chunks


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _make_settings()

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
    except Exception as e:
        startup_bootstrap_error = str(e)
        json_log("info", "bootstrap.pending", "initial qdrant bootstrap not ready yet", error=str(e))

    bg_health = asyncio.create_task(_health_loop(state))
    bg_cleanup = asyncio.create_task(_cache_cleanup_loop(state))
    app.state.background_tasks = (bg_health, bg_cleanup)

    try:
        yield
    finally:
        global SHUTDOWN
        SHUTDOWN = True

        for task in app.state.background_tasks:
            task.cancel()

        for task in app.state.background_tasks:
            try:
                await task
            except Exception:
                pass

        for c in (dense, sparse, reranker):
            try:
                await c.close()
            except Exception:
                pass
        try:
            await store.close()
        except Exception:
            pass

        SERVICE_READY.labels(service=SERVICE_NAME, env=ENV).set(0)


app = FastAPI(lifespan=lifespan)


# Reapply telemetry logging after uvicorn configures logging (important when uvicorn is started via CLI).
@app.on_event("startup")
async def _reapply_logging_after_uvicorn():
    try:
        setup_logging()
        # refresh local logger reference level/handlers if needed
        global logger
        logger = logging.getLogger("retrieval")
        logger.debug("telemetry logging reapplied on startup")
    except Exception:
        # best-effort: log to the current logger if possible
        try:
            logging.getLogger("retrieval").exception("failed to reapply telemetry logging")
        except Exception:
            pass


async def _generate_core(req: GenerateRequest) -> GenerateResponse:
    state = _state()
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    result = await _build_pipeline_result(
        state,
        query=query,
        top_k=int(req.top_k),
        fetch_k=int(req.fetch_k),
        corpus_version=req.corpus_version or CORPUS_VERSION,
        prompt_version=req.prompt_version or PROMPT_VERSION,
        retrieval_version=req.retrieval_version or RETRIEVAL_VERSION,
        model_name=req.model_name or BEDROCK_MODEL_ID,
        tenant_id=req.tenant_id,
        allow_semantic_cache=bool(req.allow_semantic_cache),
        allow_rerank=True,
        include_answer=True,
        max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
    )
    RETRIEVED_DOCS.labels(service=SERVICE_NAME, env=ENV).observe(len(result.chunks or []))
    chunks = result.chunks if req.return_chunks else None
    return GenerateResponse(
        answer=result.answer or "",
        chunks=chunks,
        retrieval=result.retrieval,
        cache=result.cache,
        cache_hit=result.cache_hit,
        cache_score=result.cache_score,
        retrieval_mode=result.retrieval_mode,
        hybrid_capable=result.hybrid_capable,
    )


async def _retrieve_core(req: RetrieveRequest) -> RetrieveResponse:
    state = _state()
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    result = await _build_pipeline_result(
        state,
        query=query,
        top_k=int(req.top_k),
        fetch_k=int(req.fetch_k),
        corpus_version=req.corpus_version or CORPUS_VERSION,
        prompt_version=PROMPT_VERSION,
        retrieval_version=req.retrieval_version or RETRIEVAL_VERSION,
        model_name=BEDROCK_MODEL_ID,
        tenant_id=req.tenant_id,
        allow_semantic_cache=True,
        allow_rerank=bool(req.rerank),
        include_answer=False,
        max_tokens=LLM_MAX_TOKENS,
    )
    RETRIEVED_DOCS.labels(service=SERVICE_NAME, env=ENV).observe(len(result.chunks or []))
    return RetrieveResponse(
        query=query,
        chunks=result.chunks,
        retrieval=result.retrieval,
        cache=result.cache,
        cache_hit=result.cache_hit,
        cache_score=result.cache_score,
        retrieval_mode=result.retrieval_mode,
        hybrid_capable=result.hybrid_capable,
    )


async def _stream_core(req: GenerateRequest, request: Request) -> StreamingResponse:
    state = _state()
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    pipeline = await _build_pipeline_result(
        state,
        query=query,
        top_k=int(req.top_k),
        fetch_k=int(req.fetch_k),
        corpus_version=req.corpus_version or CORPUS_VERSION,
        prompt_version=req.prompt_version or PROMPT_VERSION,
        retrieval_version=req.retrieval_version or RETRIEVAL_VERSION,
        model_name=req.model_name or BEDROCK_MODEL_ID,
        tenant_id=req.tenant_id,
        allow_semantic_cache=bool(req.allow_semantic_cache),
        allow_rerank=True,
        include_answer=False,
        max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
    )
    RETRIEVED_DOCS.labels(service=SERVICE_NAME, env=ENV).observe(len(pipeline.chunks or []))

    cache_state: dict[str, Any] = {"answer": None, "chunks": None}

    async def finalize_cache() -> None:
        if pipeline.cache_hit:
            return
        answer = str(cache_state.get("answer") or "").strip()
        chunks = cache_state.get("chunks") or []
        if answer:
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

        if pipeline.cache_hit and pipeline.answer is not None:
            yield _sse("delta", {"text": pipeline.answer})
            yield _sse(
                "done",
                {
                    "answer": pipeline.answer,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
            cache_state["answer"] = pipeline.answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            return

        docs_for_llm = pipeline.final_candidates[: min(len(pipeline.final_candidates), MAX_CHUNKS_TO_LLM)]
        if not docs_for_llm:
            answer = "no documents retrieved"
            cache_state["answer"] = answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": answer})
            yield _sse(
                "done",
                {
                    "answer": answer,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
            return

        prompt, llm_lines, ui_chunks = _build_bedrock_prompt(query, docs_for_llm)
        answer_parts: list[str] = []

        if not state.bedrock.health():
            fallback = deterministic_summarize(llm_lines) or "llm unavailable"
            cache_state["answer"] = fallback
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": fallback})
            yield _sse(
                "done",
                {
                    "answer": fallback,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
            return

        try:
            async for delta in state.bedrock.stream(
                prompt=prompt,
                max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
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

            yield _sse(
                "done",
                {
                    "answer": answer,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
        except Exception as exc:
            fallback = deterministic_summarize(llm_lines) or f"llm call failed: {exc}"
            cache_state["answer"] = fallback
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("error", {"error": str(exc)})
            yield _sse("delta", {"text": fallback})
            yield _sse(
                "done",
                {
                    "answer": fallback,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(finalize_cache),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    endpoint = getattr(request.url, "path", str(request.url))
    status_code = 422
    REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
    ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    endpoint = getattr(request.url, "path", str(request.url))
    status_code = 500
    REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
    ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
    json_log("error", "unhandled_exception", "unhandled exception", endpoint=endpoint, error=str(exc), stack=safe_stack(exc))
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.post("/generate", response_model=GenerateResponse)
async def api_generate(req: GenerateRequest):
    start = time.perf_counter()
    endpoint = "/generate"
    status_code = 200
    try:
        return await _generate_core(req)
    except HTTPException as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    finally:
        elapsed = max(time.perf_counter() - start, 1e-6)
        REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        REQUEST_LATENCY.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).observe(elapsed)
        if status_code >= 400:
            ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()


@app.post("/retrieve", response_model=RetrieveResponse)
async def api_retrieve(req: RetrieveRequest):
    start = time.perf_counter()
    endpoint = "/retrieve"
    status_code = 200
    try:
        return await _retrieve_core(req)
    except HTTPException as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    finally:
        elapsed = max(time.perf_counter() - start, 1e-6)
        REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        REQUEST_LATENCY.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).observe(elapsed)
        if status_code >= 400:
            ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()


@app.post("/generate/stream")
@app.post("/stream")
async def api_stream(req: GenerateRequest, request: Request):
    start = time.perf_counter()
    endpoint = "/generate/stream"
    status_code = 200
    try:
        return await _stream_core(req, request)
    except HTTPException as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    finally:
        elapsed = max(time.perf_counter() - start, 1e-6)
        REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        REQUEST_LATENCY.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).observe(elapsed)
        if status_code >= 400:
            ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()


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


@app.get("/metrics")
def metrics():
    body, content_type = metrics_response()
    return Response(body, media_type=content_type)


if __name__ == "__main__":
    # When running programmatically, apply logging before starting uvicorn so our
    # desired LOG_LEVEL is applied consistently.
    try:
        setup_logging()
    except Exception:
        logging.getLogger("retrieval").exception("failed to apply telemetry logging before uvicorn.run")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        log_level="info",
        loop=os.getenv("UVICORN_LOOP", "uvloop"),
        http=os.getenv("UVICORN_HTTP", "httptools"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
        access_log=False,  # disable uvicorn access log lines (e.g., 404 access entries)
    )
