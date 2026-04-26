#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import boto3
import httpx
import numpy as np
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, ReadTimeoutError
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, conint
from qdrant_client import models
from qdrant_store import QdrantStore, QdrantStoreConfig
from retriever_helpers import (
    build_cache_key,
    build_prompt_and_ui_chunks,
    build_retrieval_metadata,
    cache_payload_to_response,
    candidate_to_public_chunk,
    canonicalize_text,
    deterministic_summarize,
    is_payload_expired,
    normalize_query,
    rrf_fuse,
    stable_uuid_from_text,
    validate_and_filter_citations,
)
from telemetry import (
    BREAKER_OPEN_COUNT,
    CACHE_LOOKUP_COUNT,
    CACHE_LOOKUP_LATENCY,
    CACHE_WRITE_COUNT,
    CACHE_WRITE_LATENCY,
    DENSE_EMBED_COUNT,
    DENSE_EMBED_LATENCY,
    ENV,
    ERROR_COUNT,
    LLM_CALL_COUNT,
    LLM_CALL_LATENCY,
    QDRANT_QUERY_COUNT,
    QDRANT_QUERY_LATENCY,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    RERANK_COUNT,
    RERANK_LATENCY,
    RETRIEVED_DOCS,
    RETRY_COUNT,
    SERVICE_NAME,
    SERVICE_READY,
    SPARSE_EMBED_COUNT,
    SPARSE_EMBED_LATENCY,
    json_log,
    safe_stack,
    setup_logging,
)

setup_logging()
logger = logging.getLogger("retrieval")

AWS_REGION = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1").strip()
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "default_rag_collection1").strip()

DENSE_URL = os.getenv("DENSE_URL", "http://dense-svc.models.svc.cluster.local:8200").strip()
SPARSE_URL = os.getenv("SPARSE_URL", "http://sparse-svc.models.svc.cluster.local:8201").strip()
RERANKER_URL = os.getenv("RERANKER_URL", "http://reranker-svc.models.svc.cluster.local:8202").strip()

BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID") or os.getenv("AWS_BEDROCK_MODEL_ID") or "meta.llama3-8b-instruct-v1:0"

ANSWER_PROMPT_TEMPLATE = os.getenv(
    "LLM_PROMPT_TEMPLATE",
    (
        "You are a knowledge assistant who must answer in an understandable way, but referring ONLY to the provided passages below."
        "Each factual sentence MUST end with a citation in the exact format [n], where n is one of the numbered passage blocks. "
        "Use ONLY the provided passage numbers. Do NOT output filenames, URLs, page numbers, or any other metadata. Do NOT invent citations."
        "PASSAGES:\n{passages}\n\n"
        "QUESTION: {question}\n\n"
        "Answer:"
    ),
)

LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))

BEDROCK_GUARDRAIL_IDENTIFIER = os.getenv("BEDROCK_GUARDRAIL_IDENTIFIER", "").strip()
BEDROCK_GUARDRAIL_VERSION = os.getenv("BEDROCK_GUARDRAIL_VERSION", "").strip()

CORPUS_VERSION = os.getenv("CORPUS_VERSION", "v1")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")
RETRIEVAL_VERSION = os.getenv("RETRIEVAL_VERSION", "retrieval-v1")
TENANT_ID = os.getenv("TENANT_ID", "").strip() or None

DENSE_DIM = int(os.getenv("DENSE_DIM", "384"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "6"))
QUERY_TOPK_DENSE = int(os.getenv("QUERY_TOPK_DENSE", "50"))
QUERY_TOPK_SPARSE = int(os.getenv("QUERY_TOPK_SPARSE", "50"))
FETCH_K = int(os.getenv("FETCH_K", "20"))
RERANKER_TOP_K = int(os.getenv("RERANK_TOPK", "15"))
RERANKER_MODE = os.getenv("RERANKER_MODE", "AUTO").upper()
RERANK_AUTO_THRESHOLD = float(os.getenv("RERANK_AUTO_THRESHOLD", "0.75"))
RERANK_MARGIN = float(os.getenv("RERANK_MARGIN", "0.08"))
RERANK_ALPHA = float(os.getenv("RERANK_ALPHA", "0.6"))
RRF_K = int(os.getenv("RRF_K", "60"))
CACHE_SCORE_THRESHOLD = float(os.getenv("CACHE_SCORE_THRESHOLD", "0.92"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("CACHE_CLEANUP_INTERVAL_SECONDS", "900"))
PROMPT_MAX_CONTENT_CHARS = int(os.getenv("PROMPT_MAX_CONTENT_CHARS", "2500"))
CHUNK_OUTPUT_MAX_CHARS = int(os.getenv("CHUNK_OUTPUT_MAX_CHARS", "1600"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "40000"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "64"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10.0"))
HTTP_MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
HTTP_MAX_KEEPALIVE = int(os.getenv("HTTP_MAX_KEEPALIVE", "20"))
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "0.08"))
RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "0.8"))
BREAKER_FAILURE_THRESHOLD = int(os.getenv("BREAKER_FAILURE_THRESHOLD", "3"))
BREAKER_RESET_TIMEOUT = float(os.getenv("BREAKER_RESET_TIMEOUT", "20.0"))

SHUTDOWN = False
startup_bootstrap_error: str | None = None
background_task: asyncio.Task | None = None
cleanup_task: asyncio.Task | None = None


class OpenCircuitError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int, reset_timeout: float):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.state = "closed"
        self.opened_at = 0.0
        self._lock = asyncio.Lock()

    async def allow(self) -> None:
        async with self._lock:
            if self.state != "open":
                return
            now = time.monotonic()
            if (now - self.opened_at) >= self.reset_timeout:
                self.state = "half_open"
                return
            raise OpenCircuitError(f"{self.name} breaker is open")

    async def record_success(self) -> None:
        async with self._lock:
            self.failures = 0
            self.state = "closed"
            self.opened_at = 0.0

    async def record_failure(self) -> None:
        async with self._lock:
            self.failures += 1
            if self.state == "half_open" or self.failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = time.monotonic()
                BREAKER_OPEN_COUNT.labels(service=SERVICE_NAME, env=ENV, dependency=self.name).inc()


def is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, OpenCircuitError):
        return False
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "")).lower()
        if code in {"validationexception", "accessdeniedexception", "resourcenotfoundexception", "modelnotfoundexception"}:
            return False
        return code in {"throttlingexception", "toomanyrequestsexception", "serviceunavailableexception", "internalserverexception"}
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    if isinstance(exc, (BotoCoreError, EndpointConnectionError, ReadTimeoutError)):
        return True
    msg = str(exc).lower()
    if "validationexception" in msg or "access denied" in msg or "model identifier is invalid" in msg:
        return False
    return any(token in msg for token in ("timeout", "temporarily", "connection reset", "broken pipe", "unavailable", "429", "502", "503", "504"))


async def call_with_retry(dep: str, breaker: CircuitBreaker, fn):
    await breaker.allow()
    last_exc: BaseException | None = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                res = await res
            await breaker.record_success()
            return res
        except BaseException as exc:
            last_exc = exc
            if isinstance(exc, asyncio.CancelledError) or not is_retryable_exception(exc):
                raise
            if attempt >= RETRY_MAX_ATTEMPTS:
                await breaker.record_failure()
                raise
            delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            jitter = random.uniform(0.0, delay * 0.2)
            RETRY_COUNT.labels(service=SERVICE_NAME, env=ENV, dependency=dep).inc()
            await asyncio.sleep(delay + jitter)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{dep} failed without exception")


class AsyncJSONServiceClient:
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(max_connections=HTTP_MAX_CONNECTIONS, max_keepalive_connections=HTTP_MAX_KEEPALIVE)
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                limits=limits,
                trust_env=False,
                headers={"accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class AsyncDenseClient(AsyncJSONServiceClient):
    async def health(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        DENSE_EMBED_COUNT.labels(service=SERVICE_NAME, env=ENV).inc()
        start = time.perf_counter()

        async def _do():
            c = await self.client()
            r = await c.post(f"{self.base_url}/embed", json={"texts": texts})
            if r.status_code != 200:
                r.raise_for_status()
            j = r.json()
            vecs = j.get("vectors")
            if not isinstance(vecs, list) or len(vecs) != len(texts):
                raise RuntimeError("dense embed invalid response shape")
            out = []
            for v in vecs:
                vv = [float(x) for x in v]
                if len(vv) != DENSE_DIM:
                    raise RuntimeError(f"dense dim mismatch expected={DENSE_DIM} got={len(vv)}")
                arr = np.asarray(vv, dtype=np.float32)
                n = float(np.linalg.norm(arr))
                if n > 0:
                    arr = arr / n
                out.append(arr.astype(float).tolist())
            return out

        try:
            return await _do()
        finally:
            DENSE_EMBED_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - start, 1e-6))


class AsyncSparseClient(AsyncJSONServiceClient):
    async def health(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def embed_chunked(self, texts: list[str]) -> list[dict[str, Any]]:
        if not texts:
            return []
        SPARSE_EMBED_COUNT.labels(service=SERVICE_NAME, env=ENV).inc()
        start = time.perf_counter()

        async def _do(batch: list[str]) -> list[dict[str, Any]]:
            c = await self.client()
            r = await c.post(f"{self.base_url}/embed", json={"texts": batch})
            if r.status_code == 200:
                j = r.json()
                vecs = j.get("vectors")
                if not isinstance(vecs, list) or len(vecs) != len(batch):
                    raise RuntimeError("sparse embed invalid response shape")
                out = []
                for s in vecs:
                    if not isinstance(s, dict) or "indices" not in s or "values" not in s:
                        raise RuntimeError("sparse embed invalid item")
                    out.append({"indices": [int(x) for x in s["indices"]], "values": [float(x) for x in s["values"]]})
                return out

            if r.status_code in (400, 422):
                detail = ""
                try:
                    detail = str(r.json().get("detail", ""))
                except Exception:
                    detail = r.text or ""
                m = re.search(r"max=(\d+)", detail)
                if m:
                    max_batch = max(1, int(m.group(1)))
                    if len(batch) > max_batch:
                        out: list[dict[str, Any]] = []
                        for i in range(0, len(batch), max_batch):
                            out.extend(await _do(batch[i : i + max_batch]))
                        return out
                if r.status_code == 422 and len(batch) > 1:
                    mid = max(1, len(batch) // 2)
                    return (await _do(batch[:mid])) + (await _do(batch[mid:]))

            r.raise_for_status()
            return []

        try:
            return await _do(texts)
        finally:
            SPARSE_EMBED_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - start, 1e-6))


class AsyncRerankerClient(AsyncJSONServiceClient):
    async def health(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        RERANK_COUNT.labels(service=SERVICE_NAME, env=ENV).inc()
        start = time.perf_counter()

        async def _do():
            c = await self.client()
            r = await c.post(f"{self.base_url}/rerank", json={"query": query, "documents": documents})
            if r.status_code != 200:
                r.raise_for_status()
            j = r.json()
            scores = j.get("scores")
            if not isinstance(scores, list) or len(scores) != len(documents):
                raise RuntimeError("reranker invalid response shape")
            return [float(x) for x in scores]

        try:
            return await _do()
        finally:
            RERANK_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - start, 1e-6))


class AsyncBedrockClient:
    def __init__(
        self,
        region: str,
        model_id: str,
        guardrail_identifier: str = "",
        guardrail_version: str = "",
        timeout: float = HTTP_TIMEOUT,
    ):
        self.region = region
        self.model_id = model_id
        self.guardrail_identifier = guardrail_identifier.strip()
        self.guardrail_version = guardrail_version.strip()
        self.timeout = timeout
        session = boto3.session.Session(region_name=region)
        self._client = session.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"max_attempts": RETRY_MAX_ATTEMPTS, "mode": "standard"},
            ),
        )

    def health(self) -> bool:
        return bool(self.region and self.model_id)

    def _guardrail_config(self) -> dict[str, Any] | None:
        if not self.guardrail_identifier:
            return None
        cfg: dict[str, Any] = {"guardrailIdentifier": self.guardrail_identifier, "trace": "enabled"}
        if self.guardrail_version:
            cfg["guardrailVersion"] = self.guardrail_version
        return cfg

    @staticmethod
    def _extract_text(resp: dict[str, Any]) -> str:
        output = resp.get("output") or {}
        message = output.get("message") or {}
        content = message.get("content") or []
        pieces: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    txt = block.get("text")
                    if txt:
                        pieces.append(str(txt))
                elif isinstance(block, str):
                    pieces.append(block)
        if pieces:
            return "".join(pieces).strip()
        for path in (("outputText",), ("completion",)):
            cur: Any = resp
            for p in path:
                if isinstance(cur, dict):
                    cur = cur.get(p)
            if isinstance(cur, str) and cur.strip():
                return cur.strip()
        return ""

    async def generate(self, prompt: str, max_tokens: int, temperature: float) -> str:
        LLM_CALL_COUNT.labels(service=SERVICE_NAME, env=ENV, mode="generate").inc()
        start = time.perf_counter()

        def _call() -> str:
            payload: dict[str, Any] = {
                "modelId": self.model_id,
                "messages": [{"role": "user", "content": [{"text": prompt}]}],
                "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": float(temperature)},
            }
            guardrail_cfg = self._guardrail_config()
            if guardrail_cfg:
                payload["guardrailConfig"] = guardrail_cfg
            resp = self._client.converse(**payload)
            text = self._extract_text(resp if isinstance(resp, dict) else {})
            if not text:
                raise RuntimeError("bedrock returned empty content")
            return text

        try:
            return await asyncio.to_thread(_call)
        finally:
            LLM_CALL_LATENCY.labels(service=SERVICE_NAME, env=ENV, mode="generate").observe(max(time.perf_counter() - start, 1e-6))

    async def stream(self, prompt: str, max_tokens: int, temperature: float) -> AsyncIterator[str]:
        LLM_CALL_COUNT.labels(service=SERVICE_NAME, env=ENV, mode="stream").inc()
        start = time.perf_counter()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        def _worker() -> None:
            try:
                payload: dict[str, Any] = {
                    "modelId": self.model_id,
                    "messages": [{"role": "user", "content": [{"text": prompt}]}],
                    "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": float(temperature)},
                }
                guardrail_cfg = self._guardrail_config()
                if guardrail_cfg:
                    payload["guardrailConfig"] = guardrail_cfg
                resp = self._client.converse_stream(**payload)
                stream = resp.get("stream") if isinstance(resp, dict) else None
                if stream is None:
                    raise RuntimeError("bedrock stream missing stream field")
                for event in stream:
                    block = event.get("contentBlockDelta") if isinstance(event, dict) else None
                    if block:
                        delta = block.get("delta") or {}
                        text = delta.get("text")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, str(text))
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        threading.Thread(target=_worker, daemon=True).start()
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                yield str(item)
        finally:
            LLM_CALL_LATENCY.labels(service=SERVICE_NAME, env=ENV, mode="stream").observe(max(time.perf_counter() - start, 1e-6))


@dataclass
class ServiceState:
    settings: dict[str, Any]
    store: QdrantStore
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


def _is_ready_for_retrieval(state: ServiceState) -> bool:
    return bool(state.health.get("docs_collection_ready")) and bool(state.health.get("dense") or state.health.get("sparse"))


def _is_cache_ready(state: ServiceState) -> bool:
    return bool(state.health.get("cache_collection_ready"))


def _safe_cache_object(hit: bool, kind: str, score: float | None, cache_id: str | None) -> dict[str, Any]:
    return {"hit": bool(hit), "type": kind, "score": score, "id": cache_id}


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


async def _rerank_candidates(state: ServiceState, query: str, fused: list[dict[str, Any]], fetch_k: int) -> dict[str, Any]:
    should_rerank, reason = _decide_rerank(fused)
    if not should_rerank:
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        return {
            "candidates": fused,
            "enabled": False,
            "applied": False,
            "reason": reason,
            "model": None,
            "count": 0,
        }

    candidate_count = min(len(fused), max(1, min(fetch_k, RERANKER_TOP_K)))
    rerank_pool = fused[:candidate_count]
    docs = []
    for c in rerank_pool:
        payload = c.get("payload") or {}
        docs.append(canonicalize_text(payload.get("content") or payload.get("text") or payload.get("html") or ""))

    async def _do():
        return await state.reranker.rerank(query=query, documents=docs)

    try:
        scores = await call_with_retry("reranker", state.breakers["reranker"], _do)
    except Exception as exc:
        logger.debug("rerank skipped: %s", exc)
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        return {
            "candidates": fused,
            "enabled": True,
            "applied": False,
            "reason": f"reranker_failed:{type(exc).__name__}",
            "model": state.settings.get("reranker_model"),
            "count": candidate_count,
        }

    if not scores or len(scores) != len(rerank_pool):
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        return {
            "candidates": fused,
            "enabled": True,
            "applied": False,
            "reason": "invalid_reranker_output",
            "model": state.settings.get("reranker_model"),
            "count": candidate_count,
        }

    fused_scores = [float(c.get("fusion_score", 0.0) or 0.0) for c in rerank_pool]
    fused_norm = _softmax(fused_scores)
    rerank_norm = _softmax([float(x) for x in scores])
    combined = [(RERANK_ALPHA * r) + ((1.0 - RERANK_ALPHA) * f) for r, f in zip(rerank_norm, fused_norm, strict=True)]

    order = list(np.argsort(-np.asarray(combined, dtype=float)))
    reranked_pool = [dict(rerank_pool[i]) for i in order]
    for idx, item in enumerate(reranked_pool, start=1):
        item["rerank_score"] = float(scores[order[idx - 1]])
        item["post_rerank_rank"] = idx
        item["combined_score"] = float(combined[order[idx - 1]])

    remainder = fused[candidate_count:]
    for idx, item in enumerate(remainder, start=candidate_count + 1):
        item["post_rerank_rank"] = idx
    for idx, item in enumerate(reranked_pool + remainder, start=1):
        item["post_rerank_rank"] = idx

    return {
        "candidates": reranked_pool + remainder,
        "enabled": True,
        "applied": True,
        "reason": reason,
        "model": state.settings.get("reranker_model"),
        "count": candidate_count,
    }


def _build_cache_response(payload: dict[str, Any], cache_type: str, cache_score: float | None) -> dict[str, Any]:
    resp = cache_payload_to_response(payload, cache_score=cache_score)
    return {
        "answer": resp.get("answer") or "",
        "chunks": resp.get("chunks") or [],
        "cache": {
            "hit": True,
            "type": cache_type,
            "score": float(resp.get("cache_score") or 1.0),
            "id": resp.get("cache_id"),
        },
    }


def _visible_chunk_list(candidates: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    return [candidate_to_public_chunk(c, rank=idx, max_content_chars=max_chars) for idx, c in enumerate(candidates, start=1)]


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

    if dense_vec is not None and sparse_vec is not None and state.health.get("hybrid_capable", False):
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
            logger.debug("dense retrieval failed: %s", exc)
        mode = "dense"
    elif sparse_vec is not None:
        async def _sparse():
            return await state.store.sparse_search(query_vector=sparse_vec, query_filter=q_filter, limit=fetch_k)

        try:
            sparse_results = await call_with_retry("retrieval", state.breakers["retrieval"], _sparse)
        except Exception as exc:
            logger.debug("sparse retrieval failed: %s", exc)
        mode = "sparse"

    fused = rrf_fuse(dense_results, sparse_results, rrf_k=RRF_K)
    debug = {
        "candidates": {"dense": len(dense_results), "sparse": len(sparse_results), "fused": len(fused)},
        "hybrid": bool(dense_results and sparse_results),
        "fusion_method": "rrf" if fused else "none",
    }
    QDRANT_QUERY_LATENCY.labels(service=SERVICE_NAME, env=ENV, mode=mode).observe(max(time.perf_counter() - start, 1e-6))
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
    top_k: int,
    fetch_k: int,
    retrieval_mode: str,
    rerank_applied: bool,
    cache_score: float,
) -> None:
    async def _write():
        return await state.store.semantic_cache_upsert(
            cache_id=cache_id,
            query_vector=dense_vec,
            query_text=query,
            query_norm=query_norm,
            corpus_version=corpus_version,
            prompt_version=prompt_version,
            retrieval_version=retrieval_version,
            model_name=model_name,
            answer=answer,
            ui_chunks=chunks,
            ttl_seconds=state.store.config.cache_ttl_seconds,
            hit_type="semantic" if cache_score < 1.0 else "llm",
            cache_score=cache_score,
        )

    try:
        await call_with_retry("cache", state.breakers["cache"], _write)
        CACHE_WRITE_COUNT.labels(service=SERVICE_NAME, env=ENV, result="ok").inc()
    except Exception:
        CACHE_WRITE_COUNT.labels(service=SERVICE_NAME, env=ENV, result="fail").inc()


async def _call_llm(state: ServiceState, query: str, docs_for_llm: list[dict[str, Any]], max_tokens: int) -> tuple[str, list[str], list[dict[str, Any]]]:
    prompt_body, llm_lines, ui_chunks = build_prompt_and_ui_chunks(
        docs_for_llm,
        query,
        max_content_chars=PROMPT_MAX_CONTENT_CHARS,
        prefer_snippet_len=400,
    )
    prompt = ANSWER_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)
    if not state.bedrock.health():
        return deterministic_summarize(llm_lines), llm_lines, ui_chunks

    async def _do():
        return await state.bedrock.generate(prompt=prompt, max_tokens=max_tokens, temperature=LLM_TEMPERATURE)

    try:
        answer = await call_with_retry("llm", state.breakers["llm"], _do)
    except Exception as e:
        logger.warning("bedrock failed, using deterministic fallback: %s", e)
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
    allow_semantic_cache: bool,
    allow_rerank: bool,
    include_answer: bool,
    max_tokens: int,
) -> PipelineResult:
    query_norm = normalize_query(query)
    cache_key = build_cache_key(
        query_norm=query_norm,
        corpus_version=corpus_version,
        prompt_version=prompt_version,
        retrieval_version=retrieval_version,
        model_name=model_name,
        tenant_id=TENANT_ID,
        top_k=top_k,
        fetch_k=fetch_k,
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
        CACHE_LOOKUP_COUNT.labels(service=SERVICE_NAME, env=ENV, result="exact_hit" if exact else "miss").inc()
        CACHE_LOOKUP_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - exact_start, 1e-6))
        if exact and exact.get("payload") and not is_payload_expired(exact["payload"]):
            cache_resp = _build_cache_response(exact["payload"], "exact", 1.0)
            retrieval = build_retrieval_metadata(
                mode="exact_cache",
                hybrid=False,
                hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                dense_k=QUERY_TOPK_DENSE,
                sparse_k=QUERY_TOPK_SPARSE,
                fetch_k=fetch_k,
                dense_count=0,
                sparse_count=0,
                fused_count=0,
                rerank_enabled=False,
                rerank_applied=False,
                rerank_reason="exact_cache",
                rerank_model=None,
                rerank_count=0,
            )
            return PipelineResult(
                answer=cache_resp["answer"],
                chunks=cache_resp["chunks"] if cache_resp["chunks"] is not None else [],
                retrieval=retrieval,
                cache=cache_resp["cache"],
                cache_hit=True,
                cache_score=1.0,
                retrieval_mode="exact_cache",
                hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                prompt=None,
                llm_lines=[],
                ui_chunks=cache_resp["chunks"] if isinstance(cache_resp["chunks"], list) else [],
                final_candidates=[],
            )

    if not state.health.get("dense") and not state.health.get("sparse"):
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
            json_log("debug", "dense_embed_failed", "dense embed failed", error=str(e))
            dense_vec = None

    if sparse_task is not None:
        try:
            sparse_res = await sparse_task
            if sparse_res:
                s0 = sparse_res[0]
                sparse_vec = models.SparseVector(indices=[int(x) for x in s0.get("indices", [])], values=[float(x) for x in s0.get("values", [])])
        except Exception as e:
            json_log("debug", "sparse_embed_failed", "sparse embed failed", error=str(e))
            sparse_vec = None

    if cache_ready and allow_semantic_cache and dense_vec is not None:
        async def _semantic_cache():
            return await state.store.semantic_cache_lookup(
                query_vector=dense_vec,
                corpus_version=corpus_version,
                prompt_version=prompt_version,
                retrieval_version=retrieval_version,
                model_name=model_name,
                min_score=min(CACHE_SCORE_THRESHOLD, float(state.store.config.cache_score_threshold)),
            )

        semantic_start = time.perf_counter()
        semantic_hit = None
        try:
            semantic_hit = await call_with_retry("cache", state.breakers["cache"], _semantic_cache)
        except Exception:
            semantic_hit = None
        CACHE_LOOKUP_COUNT.labels(service=SERVICE_NAME, env=ENV, result="semantic_hit" if semantic_hit else "miss").inc()
        CACHE_LOOKUP_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - semantic_start, 1e-6))
        if semantic_hit and semantic_hit.get("payload"):
            payload = semantic_hit["payload"]
            cache_resp = _build_cache_response(payload, "semantic", float(semantic_hit.get("score") or payload.get("cache_score") or 1.0))
            if not include_answer:
                retrieval = build_retrieval_metadata(
                    mode="semantic_cache",
                    hybrid=False,
                    hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                    dense_k=QUERY_TOPK_DENSE,
                    sparse_k=QUERY_TOPK_SPARSE,
                    fetch_k=fetch_k,
                    dense_count=0,
                    sparse_count=0,
                    fused_count=0,
                    rerank_enabled=False,
                    rerank_applied=False,
                    rerank_reason="semantic_cache",
                    rerank_model=None,
                    rerank_count=0,
                )
                return PipelineResult(
                    answer=cache_resp["answer"],
                    chunks=cache_resp["chunks"] if isinstance(cache_resp["chunks"], list) else [],
                    retrieval=retrieval,
                    cache=cache_resp["cache"],
                    cache_hit=True,
                    cache_score=float(cache_resp["cache"]["score"] or 1.0),
                    retrieval_mode="semantic_cache",
                    hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                    prompt=None,
                    llm_lines=[],
                    ui_chunks=cache_resp["chunks"] if isinstance(cache_resp["chunks"], list) else [],
                    final_candidates=[],
                )

            final_chunks = cache_resp["chunks"] if isinstance(cache_resp["chunks"], list) else []
            await _semantic_cache_promote_exact(
                state,
                cache_id=cache_id,
                dense_vec=dense_vec,
                query=query,
                query_norm=query_norm,
                corpus_version=corpus_version,
                prompt_version=prompt_version,
                retrieval_version=retrieval_version,
                model_name=model_name,
                answer=cache_resp["answer"],
                chunks=final_chunks,
                top_k=top_k,
                fetch_k=fetch_k,
                retrieval_mode="semantic_cache",
                rerank_applied=False,
                cache_score=float(cache_resp["cache"]["score"] or 1.0),
            )
            retrieval = build_retrieval_metadata(
                mode="semantic_cache",
                hybrid=False,
                hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                dense_k=QUERY_TOPK_DENSE,
                sparse_k=QUERY_TOPK_SPARSE,
                fetch_k=fetch_k,
                dense_count=0,
                sparse_count=0,
                fused_count=0,
                rerank_enabled=False,
                rerank_applied=False,
                rerank_reason="semantic_cache",
                rerank_model=None,
                rerank_count=0,
            )
            return PipelineResult(
                answer=cache_resp["answer"],
                chunks=final_chunks,
                retrieval=retrieval,
                cache=cache_resp["cache"],
                cache_hit=True,
                cache_score=float(cache_resp["cache"]["score"] or 1.0),
                retrieval_mode="semantic_cache",
                hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                prompt=None,
                llm_lines=[],
                ui_chunks=final_chunks,
                final_candidates=[],
            )

    fused, retrieval_mode, retrieval_debug = await _search_docs(state, dense_vec, sparse_vec, fetch_k)
    if not fused:
        retrieval = build_retrieval_metadata(
            mode=retrieval_mode,
            hybrid=bool(dense_vec is not None and sparse_vec is not None and state.health.get("hybrid_capable", False)),
            hybrid_capable=bool(state.health.get("hybrid_capable", False)),
            dense_k=QUERY_TOPK_DENSE,
            sparse_k=QUERY_TOPK_SPARSE,
            fetch_k=fetch_k,
            dense_count=retrieval_debug["candidates"]["dense"],
            sparse_count=retrieval_debug["candidates"]["sparse"],
            fused_count=retrieval_debug["candidates"]["fused"],
            rerank_enabled=False,
            rerank_applied=False,
            rerank_reason="no_results",
            rerank_model=None,
            rerank_count=0,
        )
        return PipelineResult(
            answer="no documents retrieved" if include_answer else None,
            chunks=[],
            retrieval=retrieval,
            cache=_safe_cache_object(False, "miss", None, None),
            cache_hit=False,
            cache_score=None,
            retrieval_mode=retrieval_mode,
            hybrid_capable=bool(state.health.get("hybrid_capable", False)),
            prompt=None,
            llm_lines=[],
            ui_chunks=[],
            final_candidates=[],
        )

    QDRANT_QUERY_COUNT.labels(service=SERVICE_NAME, env=ENV, mode=retrieval_mode).inc()

    if allow_rerank:
        rerank_info = await _rerank_candidates(state, query, fused, fetch_k)
    else:
        for idx, item in enumerate(fused, start=1):
            item["post_rerank_rank"] = idx
        rerank_info = {
            "candidates": fused,
            "enabled": False,
            "applied": False,
            "reason": "request_disabled",
            "model": None,
            "count": 0,
        }
    final_candidates = list(rerank_info["candidates"])
    final_candidates = final_candidates[: max(1, min(top_k, len(final_candidates)))]
    for idx, item in enumerate(final_candidates, start=1):
        item["post_rerank_rank"] = idx
        if item.get("rerank_score") is None and not rerank_info["applied"]:
            item["rerank_score"] = None

    docs_for_llm = final_candidates[: min(len(final_candidates), MAX_CHUNKS_TO_LLM)]
    retrieval = build_retrieval_metadata(
        mode=retrieval_mode,
        hybrid=bool(dense_vec is not None and sparse_vec is not None and state.health.get("hybrid_capable", False)),
        hybrid_capable=bool(state.health.get("hybrid_capable", False)),
        dense_k=QUERY_TOPK_DENSE,
        sparse_k=QUERY_TOPK_SPARSE,
        fetch_k=fetch_k,
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
        chunks = _visible_chunk_list(final_candidates, CHUNK_OUTPUT_MAX_CHARS)
        return PipelineResult(
            answer=None,
            chunks=chunks,
            retrieval=retrieval,
            cache=_safe_cache_object(False, "disabled", None, None) if not cache_ready else _safe_cache_object(False, "miss", None, None),
            cache_hit=False,
            cache_score=None,
            retrieval_mode=retrieval_mode,
            hybrid_capable=bool(state.health.get("hybrid_capable", False)),
            prompt=build_prompt_and_ui_chunks(docs_for_llm, query, max_content_chars=PROMPT_MAX_CONTENT_CHARS)[0],
            llm_lines=[],
            ui_chunks=chunks,
            final_candidates=final_candidates,
        )

    answer, llm_lines, ui_chunks = await _call_llm(state, query, docs_for_llm, max_tokens=max_tokens)
    valid_indexes = [c["index"] for c in ui_chunks if isinstance(c, dict) and c.get("index") is not None]
    answer = validate_and_filter_citations(answer, valid_indexes)
    if not answer.strip():
        answer = deterministic_summarize(llm_lines)
    if len(answer) > MAX_PROMPT_CHARS:
        answer = answer[:MAX_PROMPT_CHARS].rstrip()

    output_chunks = _visible_chunk_list(final_candidates, CHUNK_OUTPUT_MAX_CHARS)
    if cache_ready and dense_vec is not None:
        try:
            final_chunks_for_cache = []
            for idx, cand in enumerate(final_candidates, start=1):
                final_chunks_for_cache.append(candidate_to_public_chunk(cand, rank=idx, max_content_chars=CHUNK_OUTPUT_MAX_CHARS))
            async def _cache_write():
                return await state.store.semantic_cache_upsert(
                    cache_id=cache_id,
                    query_vector=dense_vec,
                    query_text=query,
                    query_norm=query_norm,
                    corpus_version=corpus_version,
                    prompt_version=prompt_version,
                    retrieval_version=retrieval_version,
                    model_name=model_name,
                    answer=answer,
                    ui_chunks=final_chunks_for_cache,
                    ttl_seconds=state.store.config.cache_ttl_seconds,
                    hit_type="llm",
                    cache_score=1.0,
                )

            write_start = time.perf_counter()
            await call_with_retry("cache", state.breakers["cache"], _cache_write)
            CACHE_WRITE_COUNT.labels(service=SERVICE_NAME, env=ENV, result="ok").inc()
            CACHE_WRITE_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - write_start, 1e-6))
        except Exception:
            CACHE_WRITE_COUNT.labels(service=SERVICE_NAME, env=ENV, result="fail").inc()

    return PipelineResult(
        answer=answer,
        chunks=output_chunks,
        retrieval=retrieval,
        cache=_safe_cache_object(False, "miss", None, None),
        cache_hit=False,
        cache_score=None,
        retrieval_mode=retrieval_mode,
        hybrid_capable=bool(state.health.get("hybrid_capable", False)),
        prompt=build_prompt_and_ui_chunks(docs_for_llm, query, max_content_chars=PROMPT_MAX_CONTENT_CHARS)[0],
        llm_lines=llm_lines,
        ui_chunks=ui_chunks,
        final_candidates=final_candidates,
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _cache_cleanup_loop(state: ServiceState) -> None:
    while not SHUTDOWN:
        try:
            if state.store.cache_ready:
                await state.store.cleanup_expired_cache()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("cache cleanup failed: %s", e)
        await asyncio.sleep(CACHE_CLEANUP_INTERVAL_SECONDS)


async def _health_loop(state: ServiceState) -> None:
    last_snapshot: dict[str, bool] | None = None
    while not SHUTDOWN:
        try:
            if not state.store.docs_ready or not state.store.cache_ready:
                try:
                    docs_ready, cache_ready = await state.store.bootstrap()
                    state.store.docs_ready = docs_ready
                    state.store.cache_ready = cache_ready
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
            SERVICE_READY.labels(service=SERVICE_NAME, env=ENV).set(1 if snapshot["ready"] else 0)
            if snapshot != last_snapshot:
                json_log("debug", "health", "dependency health", **snapshot)
                last_snapshot = dict(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            state.health = {
                "qdrant": False,
                "docs_collection_ready": False,
                "cache_collection_ready": False,
                "dense": False,
                "sparse": False,
                "reranker": False,
                "bedrock": bool(state.bedrock.health()),
                "hybrid_capable": False,
                "ready": False,
            }
            SERVICE_READY.labels(service=SERVICE_NAME, env=ENV).set(0)
            logger.warning("health loop failed: %s", e)
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _make_settings()
    store = QdrantStore(QdrantStoreConfig.from_env())
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

    global startup_bootstrap_error, background_task, cleanup_task
    try:
        docs_ready, cache_ready = await state.store.bootstrap()
        state.store.docs_ready = docs_ready
        state.store.cache_ready = cache_ready
    except Exception as e:
        startup_bootstrap_error = str(e)
        json_log("info", "bootstrap.pending", "initial qdrant bootstrap not ready yet", error=str(e))

    background_task = asyncio.create_task(_health_loop(state))
    cleanup_task = asyncio.create_task(_cache_cleanup_loop(state))

    try:
        yield
    finally:
        global SHUTDOWN
        SHUTDOWN = True
        for task in (background_task, cleanup_task):
            if task is not None:
                task.cancel()
        for task in (background_task, cleanup_task):
            if task is not None:
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


class GenerateRequest(BaseModel):
    query: str = Field(..., min_length=1)
    tenant_id: str | None = None
    corpus_version: str | None = Field(default=CORPUS_VERSION)
    prompt_version: str | None = Field(default=PROMPT_VERSION)
    retrieval_version: str | None = Field(default=RETRIEVAL_VERSION)
    model_name: str | None = Field(default=BEDROCK_MODEL_ID)
    debug: bool | None = False
    enable_tracing: bool | None = False
    top_k: conint(ge=1, le=50) = 5
    fetch_k: conint(ge=1, le=200) = FETCH_K
    return_chunks: bool | None = True
    max_tokens: conint(ge=64, le=4096) | None = LLM_MAX_TOKENS
    allow_semantic_cache: bool | None = True


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    tenant_id: str | None = None
    corpus_version: str | None = Field(default=CORPUS_VERSION)
    retrieval_version: str | None = Field(default=RETRIEVAL_VERSION)
    top_k: conint(ge=1, le=50) = 5
    fetch_k: conint(ge=1, le=200) = FETCH_K
    rerank: bool | None = True
    include_cache: bool | None = False


class GenerateResponse(BaseModel):
    answer: str
    chunks: list[dict[str, Any]] | None = None
    retrieval: dict[str, Any]
    cache: dict[str, Any]
    cache_hit: bool = False
    cache_score: float | None = None
    retrieval_mode: str | None = None
    hybrid_capable: bool = False


class RetrieveResponse(BaseModel):
    query: str
    chunks: list[dict[str, Any]] | None = None
    retrieval: dict[str, Any]
    cache: dict[str, Any]
    cache_hit: bool = False
    cache_score: float | None = None
    retrieval_mode: str | None = None
    hybrid_capable: bool = False


def _state() -> ServiceState:
    return app.state.state


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
        allow_semantic_cache=bool(req.include_cache),
        allow_rerank=bool(req.rerank),
        include_answer=False,
        max_tokens=LLM_MAX_TOKENS,
    )
    RETRIEVED_DOCS.labels(service=SERVICE_NAME, env=ENV).observe(len(result.chunks or []))
    cache = result.cache if req.include_cache else _safe_cache_object(False, "disabled", None, None)
    return RetrieveResponse(
        query=query,
        chunks=result.chunks,
        retrieval=result.retrieval,
        cache=cache,
        cache_hit=cache.get("hit", False),
        cache_score=cache.get("score"),
        retrieval_mode=result.retrieval_mode,
        hybrid_capable=result.hybrid_capable,
    )


async def _stream_core(req: GenerateRequest) -> StreamingResponse:
    state = _state()
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    async def event_gen() -> AsyncIterator[str]:
        async with state.semaphore:
            pipeline = await _build_pipeline_result(
                state,
                query=query,
                top_k=int(req.top_k),
                fetch_k=int(req.fetch_k),
                corpus_version=req.corpus_version or CORPUS_VERSION,
                prompt_version=req.prompt_version or PROMPT_VERSION,
                retrieval_version=req.retrieval_version or RETRIEVAL_VERSION,
                model_name=req.model_name or BEDROCK_MODEL_ID,
                allow_semantic_cache=bool(req.allow_semantic_cache),
                allow_rerank=True,
                include_answer=False,
                max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
            )
            RETRIEVED_DOCS.labels(service=SERVICE_NAME, env=ENV).observe(len(pipeline.chunks or []))
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
                return

            docs_for_llm = pipeline.final_candidates[: min(len(pipeline.final_candidates), MAX_CHUNKS_TO_LLM)]
            if not docs_for_llm:
                answer = "no documents retrieved"
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

            prompt_body, llm_lines, ui_chunks = build_prompt_and_ui_chunks(
                docs_for_llm,
                query,
                max_content_chars=PROMPT_MAX_CONTENT_CHARS,
                prefer_snippet_len=400,
            )
            prompt = ANSWER_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)
            answer_parts: list[str] = []
            try:
                async for delta in state.bedrock.stream(prompt=prompt, max_tokens=int(req.max_tokens or LLM_MAX_TOKENS), temperature=LLM_TEMPERATURE):
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
    state = _state()
    async with state.semaphore:
        start = time.perf_counter()
        endpoint = "/generate"
        status_code = 200
        try:
            resp = await _generate_core(req)
            return resp
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
    state = _state()
    async with state.semaphore:
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


@app.post("/stream")
async def api_stream(req: GenerateRequest):
    state = _state()
    async with state.semaphore:
        start = time.perf_counter()
        endpoint = "/stream"
        status_code = 200
        try:
            return await _stream_core(req)
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
    from telemetry import metrics_response

    body, content_type = metrics_response()
    return Response(body, media_type=content_type)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        log_level={"WARN": "warning", "WARNING": "warning", "INFO": "info", "DEBUG": "debug", "ERROR": "error"}.get((os.getenv("LOG_LEVEL") or os.getenv("LOGLEVEL") or "warning").upper(), "warning"),
        loop=os.getenv("UVICORN_LOOP", "uvloop"),
        http=os.getenv("UVICORN_HTTP", "httptools"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
