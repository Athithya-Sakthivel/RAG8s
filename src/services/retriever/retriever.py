#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, conint
from qdrant_client import models
from qdrant_store import QdrantStore, QdrantStoreConfig
from query_helpers import (
    build_cache_key,
    build_numbered_prompt_and_ui_chunks,
    cache_payload_to_response,
    canonicalize_text,
    deterministic_summarize,
    is_payload_expired,
    normalize_query,
    stable_uuid_from_text,
    ui_fields_from_payload,
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
    RETRY_COUNT,
    SERVICE_NAME,
    SERVICE_READY,
    SPARSE_EMBED_COUNT,
    SPARSE_EMBED_LATENCY,
    json_log,
    safe_stack,
)

logger = logging.getLogger("retrieval")

AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

DENSE_URL = os.getenv("DENSE_URL", "http://dense-svc.models.svc.cluster.local:8200")
SPARSE_URL = os.getenv("SPARSE_URL", "http://sparse-svc.models.svc.cluster.local:8201")
RERANKER_URL = os.getenv("RERANKER_URL", "http://reranker-svc.models.svc.cluster.local:8202")

BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID") or os.getenv("AWS_BEDROCK_MODEL_ID") or "meta.llama3-8b-instruct-v1:0"
BEDROCK_GUARDRAIL_IDENTIFIER = os.getenv("BEDROCK_GUARDRAIL_IDENTIFIER", "").strip()
BEDROCK_GUARDRAIL_VERSION = os.getenv("BEDROCK_GUARDRAIL_VERSION", "").strip()

LLM_PROMPT_TEMPLATE = os.getenv(
    "LLM_PROMPT_TEMPLATE",
    (
        "You are a knowledge assistant who must answer unambiguously ONLY from the provided passages below "
        "Each factual sentence MUST end with a citation in the exact format [n], where n is one of the numbered passage blocks. "
        "Use ONLY the provided passage numbers. Do NOT output filenames, URLs, page numbers, or any other metadata. "
        "Do NOT invent citations.\n\n"
        "PASSAGES:\n{passages}\n\n"
        "QUESTION: {question}\n\n"
        "Answer:"
    ),
)

LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
MAX_ANSWER_CHARS = int(os.getenv("MAX_ANSWER_CHARS", "40000"))
PROMPT_MAX_CONTENT_CHARS = int(os.getenv("PROMPT_MAX_CONTENT_CHARS", "2500"))
CHUNK_OUTPUT_MAX_CHARS = int(os.getenv("CHUNK_OUTPUT_MAX_CHARS", "1600"))

CORPUS_VERSION = os.getenv("CORPUS_VERSION", "v1")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")
RETRIEVAL_VERSION = os.getenv("RETRIEVAL_VERSION", "retrieval-v1")

DENSE_DIM = int(os.getenv("DENSE_DIM", "384"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "6"))
QUERY_TOPK_DENSE = int(os.getenv("QUERY_TOPK_DENSE", "200"))
QUERY_TOPK_SPARSE = int(os.getenv("QUERY_TOPK_SPARSE", "200"))
RERANKER_TOP_K = int(os.getenv("RERANK_TOPK", "20"))
RERANKER_MODE = os.getenv("RERANKER_MODE", "AUTO").upper()
RERANK_AUTO_THRESHOLD = float(os.getenv("RERANK_AUTO_THRESHOLD", "0.75"))
RERANK_MARGIN = float(os.getenv("RERANK_MARGIN", "0.08"))
RERANK_ALPHA = float(os.getenv("RERANK_ALPHA", "0.6"))
RRF_K = int(os.getenv("RRF_K", "60"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10.0"))
HTTP_MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
HTTP_MAX_KEEPALIVE = int(os.getenv("HTTP_MAX_KEEPALIVE", "20"))

RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "0.08"))
RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "0.8"))

BREAKER_FAILURE_THRESHOLD = int(os.getenv("BREAKER_FAILURE_THRESHOLD", "3"))
BREAKER_RESET_TIMEOUT = float(os.getenv("BREAKER_RESET_TIMEOUT", "20.0"))

CACHE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("CACHE_CLEANUP_INTERVAL_SECONDS", "900"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "64"))
CACHE_SCORE_THRESHOLD = float(os.getenv("CACHE_SCORE_THRESHOLD", "0.92"))

SHUTDOWN = False
background_task: asyncio.Task | None = None
cleanup_task: asyncio.Task | None = None
startup_bootstrap_error: str | None = None


def l2_normalize(v: list[float]) -> list[float]:
    a = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(a))
    if n > 0:
        a = a / n
    return a.astype(float).tolist()


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
            res = await fn()
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
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{dep} failed without exception")


class AsyncJSONServiceClient:
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=HTTP_MAX_CONNECTIONS,
                max_keepalive_connections=HTTP_MAX_KEEPALIVE,
            )
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
                out.append(l2_normalize(vv))
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
                    left = await _do(batch[:mid])
                    right = await _do(batch[mid:])
                    return left + right

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
        cfg: dict[str, Any] = {
            "guardrailIdentifier": self.guardrail_identifier,
            "trace": "enabled",
        }
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

    async def generate(self, user_prompt: str, max_tokens: int, temperature: float) -> str:
        LLM_CALL_COUNT.labels(service=SERVICE_NAME, env=ENV).inc()
        start = time.perf_counter()

        def _call() -> str:
            payload: dict[str, Any] = {
                "modelId": self.model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": user_prompt}],
                    }
                ],
                "inferenceConfig": {
                    "maxTokens": int(max_tokens),
                    "temperature": float(temperature),
                },
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
            LLM_CALL_LATENCY.labels(service=SERVICE_NAME, env=ENV).observe(max(time.perf_counter() - start, 1e-6))


@dataclass
class ServiceState:
    settings: dict[str, Any]
    store: QdrantStore
    dense: AsyncDenseClient
    sparse: AsyncSparseClient
    reranker: AsyncRerankerClient
    bedrock: AsyncBedrockClient
    breakers: dict[str, CircuitBreaker]
    semaphore: asyncio.Semaphore
    health: dict[str, bool]


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
        "cache_ttl_seconds": int(os.getenv("CACHE_TTL_SECONDS", "86400")),
        "cache_score_threshold": CACHE_SCORE_THRESHOLD,
        "max_chunks_to_llm": MAX_CHUNKS_TO_LLM,
    }


def _content_from_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("content"):
        return str(payload.get("content") or "")
    if payload.get("text"):
        return str(payload.get("text") or "")
    if payload.get("html"):
        return str(payload.get("html") or "")
    return ""


def _truncate_text(text: str, max_chars: int) -> str:
    txt = canonicalize_text(text or "")
    if max_chars and len(txt) > max_chars:
        return txt[:max_chars].rstrip() + "…"
    return txt


def _visible_fields_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    fields = ui_fields_from_payload(payload, prefer_snippet_len=None, verbose=False)
    out = dict(fields)
    for key in ("page_number", "semantic_region", "title", "headings", "heading_path", "line_range"):
        if key in out and out[key] in (None, "", [], {}):
            out.pop(key, None)
    return out


def _chunk_output_from_candidate(candidate: dict[str, Any], rank: int) -> dict[str, Any]:
    payload = candidate.get("payload") or {}
    visible = _visible_fields_from_payload(payload)
    chunk_id = visible.get("chunk_id") or str(candidate.get("id") or "")
    source_url = visible.get("source_url") or ""
    content = _truncate_text(_content_from_payload(payload), CHUNK_OUTPUT_MAX_CHARS)

    out: dict[str, Any] = {
        "chunk_id": chunk_id,
        "source_url": source_url,
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

    for key in ("page_number", "semantic_region", "title", "headings", "heading_path", "line_range"):
        if key in visible and visible[key] not in (None, "", [], {}):
            out[key] = visible[key]
    return out


def _sanitize_cached_chunk(chunk: dict[str, Any], rank: int) -> dict[str, Any]:
    if not isinstance(chunk, dict):
        return {}
    out: dict[str, Any] = {
        "chunk_id": str(chunk.get("chunk_id") or chunk.get("id") or ""),
        "source_url": str(chunk.get("source_url") or ""),
        "scores": chunk.get("scores") if isinstance(chunk.get("scores"), dict) else {"dense": None, "sparse": None, "fusion": None, "rerank": None},
        "rank": chunk.get("rank") if isinstance(chunk.get("rank"), dict) else {"pre_rerank": rank, "post_rerank": rank},
        "content": _truncate_text(str(chunk.get("content") or ""), CHUNK_OUTPUT_MAX_CHARS),
    }

    for key in ("page_number", "semantic_region", "title", "headings", "heading_path", "line_range"):
        if key in chunk and chunk[key] not in (None, "", [], {}):
            out[key] = chunk[key]

    return out


def _build_cache_object(hit: bool, cache_type: str, score: float | None, cache_id: str | None) -> dict[str, Any]:
    return {
        "hit": bool(hit),
        "type": cache_type,
        "score": float(score) if score is not None else None,
        "id": cache_id,
    }


def _validate_and_filter_citations(ans: str, valid_indexes: list[int]) -> str:
    if not ans:
        return ans

    ans = re.sub(
        r"\[.*?(source_url|page_number|file_name|row_range|token_range|audio_range|headings|heading_path|chunk_id).*?\]",
        " ",
        ans,
        flags=re.IGNORECASE,
    )

    def repl(match):
        num = int(match.group(1))
        return f"[{num}]" if num in valid_indexes else ""

    ans = re.sub(r"\[(\d+)\]", repl, ans)
    ans = re.sub(r"https?://\S+", "", ans)
    ans = re.sub(r"\s+", " ", ans).strip()
    return ans


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


def _make_cache_id(cache_key: str) -> str:
    return stable_uuid_from_text(cache_key)


def _build_llm_prompt(query: str, candidates: list[dict[str, Any]]) -> tuple[str, list[str], list[dict[str, Any]]]:
    prompt_body, llm_lines, ui_chunks = build_numbered_prompt_and_ui_chunks(
        candidates,
        query,
        max_content_chars=PROMPT_MAX_CONTENT_CHARS,
        prefer_snippet_len=None,
    )
    user_prompt = LLM_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)
    return user_prompt, llm_lines, ui_chunks


def _reciprocal_rank_fusion(
    dense_results: list[dict[str, Any]],
    sparse_results: list[dict[str, Any]],
    rrf_k: int = RRF_K,
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

    keys = list(dict.fromkeys([*dense_map.keys(), *sparse_map.keys()]))
    fused: list[dict[str, Any]] = []
    for key in keys:
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
                "combined_score": None,
            }
        )

    fused.sort(key=lambda x: (x["fusion_score"], x["dense_score"] or 0.0, x["sparse_score"] or 0.0), reverse=True)
    for idx, item in enumerate(fused, start=1):
        item["pre_rerank_rank"] = idx
    return fused


def _rationale_for_rerank(results: list[dict[str, Any]]) -> tuple[bool, str]:
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
    should_rerank, reason = _rationale_for_rerank(fused)
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
        text = _content_from_payload(payload)
        docs.append(_truncate_text(text, PROMPT_MAX_CONTENT_CHARS))

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

    fused_scores = _softmax([float(c.get("fusion_score", 0.0) or 0.0) for c in rerank_pool])
    rerank_norm = _softmax([float(x) for x in scores])
    combined = [(RERANK_ALPHA * r) + ((1.0 - RERANK_ALPHA) * f) for r, f in zip(rerank_norm, fused_scores, strict=True)]

    order = list(np.argsort(-np.asarray(combined, dtype=float)))
    reranked_pool: list[dict[str, Any]] = []
    for idx in order:
        item = dict(rerank_pool[idx])
        item["rerank_score"] = float(scores[idx])
        item["combined_score"] = float(combined[idx])
        reranked_pool.append(item)

    remainder = [dict(item) for item in fused[candidate_count:]]
    ordered = reranked_pool + remainder

    for idx, item in enumerate(ordered, start=1):
        item["post_rerank_rank"] = idx

    return {
        "candidates": ordered,
        "enabled": True,
        "applied": True,
        "reason": reason,
        "model": state.settings.get("reranker_model"),
        "count": candidate_count,
    }


async def _search_docs(
    state: ServiceState,
    query: str,
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

        dense_results, sparse_results = await asyncio.gather(
            call_with_retry("retrieval", state.breakers["retrieval"], _dense),
            call_with_retry("retrieval", state.breakers["retrieval"], _sparse),
        )
        mode = "hybrid"
    elif dense_vec is not None:
        async def _dense():
            return await state.store.dense_search(query_vector=dense_vec, query_filter=q_filter, limit=fetch_k)

        dense_results = await call_with_retry("retrieval", state.breakers["retrieval"], _dense)
        mode = "dense"
    elif sparse_vec is not None:
        async def _sparse():
            return await state.store.sparse_search(query_vector=sparse_vec, query_filter=q_filter, limit=fetch_k)

        sparse_results = await call_with_retry("retrieval", state.breakers["retrieval"], _sparse)
        mode = "sparse"

    if dense_results and sparse_results:
        fused = _reciprocal_rank_fusion(dense_results, sparse_results)
    elif dense_results:
        fused = _reciprocal_rank_fusion(dense_results, [])
    elif sparse_results:
        fused = _reciprocal_rank_fusion([], sparse_results)
    else:
        fused = []

    debug = {
        "candidates": {
            "dense": len(dense_results),
            "sparse": len(sparse_results),
            "fused": len(fused),
        },
        "hybrid": bool(dense_results and sparse_results),
        "fusion_method": "rrf",
    }
    QDRANT_QUERY_LATENCY.labels(service=SERVICE_NAME, env=ENV, mode=mode).observe(max(time.perf_counter() - start, 1e-6))
    return fused, mode, debug


def _build_cache_response(payload: dict[str, Any], cache_type: str, cache_score: float | None) -> dict[str, Any]:
    base = cache_payload_to_response(payload, cache_score=cache_score)
    chunks = base.get("chunks") or []
    sanitized_chunks = []
    for idx, chunk in enumerate(chunks, start=1):
        sanitized_chunks.append(_sanitize_cached_chunk(chunk if isinstance(chunk, dict) else {}, idx))
    return {
        "answer": str(base.get("answer") or payload.get("answer") or ""),
        "chunks": sanitized_chunks,
        "cache": _build_cache_object(
            True,
            cache_type,
            float(base.get("cache_score") if base.get("cache_score") is not None else (cache_score if cache_score is not None else 1.0)),
            str(base.get("cache_id") or payload.get("cache_id") or ""),
        ),
    }


class GenerateRequest(BaseModel):
    query: str = Field(..., min_length=1)
    corpus_version: str | None = Field(default=CORPUS_VERSION)
    prompt_version: str | None = Field(default=PROMPT_VERSION)
    retrieval_version: str | None = Field(default=RETRIEVAL_VERSION)
    model_name: str | None = Field(default=BEDROCK_MODEL_ID)
    debug: bool | None = False
    enable_tracing: bool | None = False
    top_k: conint(ge=1, le=50) = 5
    fetch_k: conint(ge=1, le=200) = 20
    return_chunks: bool | None = True
    max_tokens: conint(ge=64, le=4096) | None = LLM_MAX_TOKENS
    allow_semantic_cache: bool | None = True


class GenerateResponse(BaseModel):
    answer: str
    chunks: list[dict[str, Any]] | None = None
    retrieval: dict[str, Any]
    cache: dict[str, Any]
    cache_hit: bool = False
    cache_score: float | None = None
    retrieval_mode: str | None = None
    hybrid_capable: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = _make_settings()
    settings["reranker_model"] = os.getenv("RERANKER_MODEL", "cross-encoder")

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
        semaphore=asyncio.Semaphore(MAX_CONCURRENT_REQUESTS),
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


def _is_cache_ready(state: ServiceState) -> bool:
    return bool(state.health.get("cache_collection_ready"))


def _is_ready_for_retrieval(state: ServiceState) -> bool:
    return bool(state.health.get("docs_collection_ready")) and bool(state.health.get("dense") or state.health.get("sparse"))


def _refresh_health_snapshot(state: ServiceState) -> None:
    state.health["hybrid_capable"] = bool(state.store.hybrid_capable and state.health.get("dense") and state.health.get("sparse"))
    state.health["ready"] = bool(state.health.get("docs_collection_ready") and (state.health.get("dense") or state.health.get("sparse")))


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

            state.health = {
                "qdrant": qdrant_ok,
                "docs_collection_ready": bool(state.store.docs_ready),
                "cache_collection_ready": bool(state.store.cache_ready),
                "dense": dense_ok,
                "sparse": sparse_ok,
                "reranker": reranker_ok,
                "bedrock": bedrock_ok,
                "hybrid_capable": bool(state.store.hybrid_capable and dense_ok and sparse_ok),
                "ready": bool(state.store.docs_ready and (dense_ok or sparse_ok)),
            }
            SERVICE_READY.labels(service=SERVICE_NAME, env=ENV).set(1 if state.health["ready"] else 0)

            json_log(
                "info",
                "health",
                "dependency health",
                qdrant=qdrant_ok,
                docs_ready=state.store.docs_ready,
                cache_ready=state.store.cache_ready,
                dense=dense_ok,
                sparse=sparse_ok,
                reranker=reranker_ok,
                bedrock=bedrock_ok,
                hybrid_capable=state.health["hybrid_capable"],
                ready=state.health["ready"],
            )
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


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        endpoint = getattr(request.url, "path", str(request.url))
        status_code = 422
        REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        endpoint = getattr(request.url, "path", str(request.url))
        status_code = int(exc.status_code)
        REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        if status_code >= 400:
            ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        return JSONResponse(status_code=status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        endpoint = getattr(request.url, "path", str(request.url))
        status_code = 500
        REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
        json_log("error", "unhandled_exception", "unhandled exception", endpoint=endpoint, error=str(exc), stack=safe_stack(exc))
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    async def _maybe_raise_if_disconnected(request: Request) -> None:
        try:
            if await request.is_disconnected():
                raise HTTPException(status_code=499, detail="client disconnected")
        except HTTPException:
            raise
        except Exception:
            return

    async def _handle_generate(req: GenerateRequest, request: Request) -> GenerateResponse:
        endpoint = "/generate"
        start = time.perf_counter()
        status_code = 200
        completed = False
        state: ServiceState = app.state.state

        try:
            async with state.semaphore:
                await _maybe_raise_if_disconnected(request)

                query = (req.query or "").strip()
                if not query:
                    status_code = 400
                    raise HTTPException(status_code=400, detail="query required")

                corpus_version = req.corpus_version or CORPUS_VERSION
                prompt_version = req.prompt_version or PROMPT_VERSION
                retrieval_version = req.retrieval_version or RETRIEVAL_VERSION
                model_name = req.model_name or BEDROCK_MODEL_ID

                query_norm = normalize_query(query)
                cache_key = build_cache_key(
                    query_norm=query_norm,
                    corpus_version=corpus_version,
                    prompt_version=prompt_version,
                    retrieval_version=retrieval_version,
                    model_name=model_name,
                )
                cache_id = _make_cache_id(cache_key)
                query_embed_text = canonicalize_text(query)

                if _is_cache_ready(state) and req.allow_semantic_cache:
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
                        retrieval_obj = {
                            "mode": "exact_cache",
                            "hybrid": False,
                            "hybrid_capable": bool(state.health.get("hybrid_capable", False)),
                            "dense_k": QUERY_TOPK_DENSE,
                            "sparse_k": QUERY_TOPK_SPARSE,
                            "fetch_k": req.fetch_k,
                            "fusion_method": "none",
                            "candidates": {"dense": 0, "sparse": 0, "fused": 0},
                            "rerank": {
                                "enabled": False,
                                "applied": False,
                                "reason": "exact_cache",
                                "model": None,
                                "count": 0,
                            },
                        }
                        completed = True
                        return GenerateResponse(
                            answer=cache_resp["answer"],
                            chunks=cache_resp["chunks"] if req.return_chunks else None,
                            retrieval=retrieval_obj,
                            cache=cache_resp["cache"],
                            cache_hit=True,
                            cache_score=1.0,
                            retrieval_mode="exact_cache",
                            hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                        )

                if not state.health.get("dense") and not state.health.get("sparse"):
                    status_code = 503
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
                            sparse_vec = models.SparseVector(
                                indices=[int(x) for x in s0.get("indices", [])],
                                values=[float(x) for x in s0.get("values", [])],
                            )
                    except Exception as e:
                        json_log("debug", "sparse_embed_failed", "sparse embed failed", error=str(e))
                        sparse_vec = None

                await _maybe_raise_if_disconnected(request)

                if _is_cache_ready(state) and req.allow_semantic_cache and dense_vec is not None:
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
                        cache_resp = _build_cache_response(
                            payload,
                            "semantic",
                            float(semantic_hit.get("score") or payload.get("cache_score") or 1.0),
                        )
                        if not req.return_chunks:
                            cache_resp["chunks"] = None

                        retrieval_obj = {
                            "mode": "semantic_cache",
                            "hybrid": False,
                            "hybrid_capable": bool(state.health.get("hybrid_capable", False)),
                            "dense_k": QUERY_TOPK_DENSE,
                            "sparse_k": QUERY_TOPK_SPARSE,
                            "fetch_k": req.fetch_k,
                            "fusion_method": "none",
                            "candidates": {"dense": 0, "sparse": 0, "fused": 0},
                            "rerank": {
                                "enabled": False,
                                "applied": False,
                                "reason": "semantic_cache",
                                "model": None,
                                "count": 0,
                            },
                        }
                        completed = True
                        return GenerateResponse(
                            answer=cache_resp["answer"],
                            chunks=cache_resp["chunks"] if req.return_chunks else None,
                            retrieval=retrieval_obj,
                            cache=cache_resp["cache"],
                            cache_hit=True,
                            cache_score=float(cache_resp["cache"]["score"] or 1.0),
                            retrieval_mode="semantic_cache",
                            hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                        )

                await _maybe_raise_if_disconnected(request)

                fused, retrieval_mode, retrieval_debug = await _search_docs(state, query, dense_vec, sparse_vec, req.fetch_k)
                if not fused:
                    answer = "no documents retrieved"
                    retrieval_obj = {
                        "mode": retrieval_mode,
                        "hybrid": bool(dense_vec is not None and sparse_vec is not None and state.health.get("hybrid_capable", False)),
                        "hybrid_capable": bool(state.health.get("hybrid_capable", False)),
                        "dense_k": QUERY_TOPK_DENSE,
                        "sparse_k": QUERY_TOPK_SPARSE,
                        "fetch_k": req.fetch_k,
                        "fusion_method": retrieval_debug["fusion_method"],
                        "candidates": retrieval_debug["candidates"],
                        "rerank": {
                            "enabled": False,
                            "applied": False,
                            "reason": "no_results",
                            "model": None,
                            "count": 0,
                        },
                    }
                    completed = True
                    return GenerateResponse(
                        answer=answer,
                        chunks=[] if req.return_chunks else None,
                        retrieval=retrieval_obj,
                        cache=_build_cache_object(False, "miss", None, None),
                        cache_hit=False,
                        cache_score=None,
                        retrieval_mode=retrieval_mode,
                        hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                    )

                QDRANT_QUERY_COUNT.labels(service=SERVICE_NAME, env=ENV, mode=retrieval_mode).inc()

                rerank_info = await _rerank_candidates(state, query, fused, req.fetch_k)
                final_candidates = rerank_info["candidates"]

                final_k = min(len(final_candidates), int(req.top_k))
                final_candidates = final_candidates[:final_k]
                for idx, item in enumerate(final_candidates, start=1):
                    item["post_rerank_rank"] = idx
                    if item.get("rerank_score") is None and rerank_info["applied"] is False:
                        item["rerank_score"] = None

                docs_for_llm = final_candidates[: min(len(final_candidates), MAX_CHUNKS_TO_LLM)]

                user_prompt, llm_lines, _ = _build_llm_prompt(query, docs_for_llm)
                answer = ""
                try:
                    if state.bedrock.health():
                        async def _do_llm():
                            return await state.bedrock.generate(
                                user_prompt=user_prompt,
                                max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
                                temperature=LLM_TEMPERATURE,
                            )

                        answer = await call_with_retry("llm", state.breakers["llm"], _do_llm)
                        if isinstance(answer, str) and len(answer.strip()) < 3:
                            json_log("warning", "llm.too_short", "llm output too short; using deterministic fallback", length=len(answer))
                            answer = deterministic_summarize(llm_lines)
                    else:
                        answer = deterministic_summarize(llm_lines)
                except Exception as e:
                    json_log("warning", "llm.call.failed", "bedrock failed, using deterministic fallback", error=str(e))
                    answer = deterministic_summarize(llm_lines)

                valid_indexes = [i for i in range(1, len(docs_for_llm) + 1)]
                try:
                    answer = _validate_and_filter_citations(answer, valid_indexes)
                except Exception as e:
                    json_log("warning", "citation.filter.failed", "citation filter failed", error=str(e))

                if not answer.strip():
                    answer = deterministic_summarize(llm_lines)

                if len(answer) > MAX_ANSWER_CHARS:
                    answer = answer[:MAX_ANSWER_CHARS].rstrip()

                final_chunks_for_cache = [_chunk_output_from_candidate(cand, idx) for idx, cand in enumerate(final_candidates, start=1)]

                if _is_cache_ready(state) and dense_vec is not None:
                    try:
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

                output_chunks = []
                if req.return_chunks:
                    for idx, cand in enumerate(final_candidates, start=1):
                        output_chunks.append(_chunk_output_from_candidate(cand, idx))

                retrieval_obj = {
                    "mode": retrieval_mode,
                    "hybrid": bool(dense_vec is not None and sparse_vec is not None and state.health.get("hybrid_capable", False)),
                    "hybrid_capable": bool(state.health.get("hybrid_capable", False)),
                    "dense_k": QUERY_TOPK_DENSE,
                    "sparse_k": QUERY_TOPK_SPARSE,
                    "fetch_k": req.fetch_k,
                    "fusion_method": retrieval_debug["fusion_method"],
                    "candidates": retrieval_debug["candidates"],
                    "rerank": {
                        "enabled": rerank_info["enabled"],
                        "applied": rerank_info["applied"],
                        "reason": rerank_info["reason"],
                        "model": rerank_info["model"],
                        "count": rerank_info["count"],
                    },
                }

                completed = True
                return GenerateResponse(
                    answer=answer,
                    chunks=output_chunks if req.return_chunks else None,
                    retrieval=retrieval_obj,
                    cache=_build_cache_object(False, "miss", None, None),
                    cache_hit=False,
                    cache_score=None,
                    retrieval_mode=retrieval_mode,
                    hybrid_capable=bool(state.health.get("hybrid_capable", False)),
                )

        finally:
            if completed:
                elapsed = max(time.perf_counter() - start, 1e-6)
                REQUEST_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()
                REQUEST_LATENCY.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).observe(elapsed)
                if status_code >= 400:
                    ERROR_COUNT.labels(service=SERVICE_NAME, env=ENV, endpoint=endpoint, status_code=str(status_code)).inc()

    @app.post("/generate", response_model=GenerateResponse)
    async def api_generate(req: GenerateRequest, request: Request):
        return await _handle_generate(req, request)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        state: ServiceState = app.state.state
        ready = bool(state.health.get("ready", False))
        status = "ready" if ready else ("degraded" if state.health.get("docs_collection_ready") else "not_ready")
        return {
            "status": status,
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
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        loop=os.getenv("UVICORN_LOOP", "uvloop"),
        http=os.getenv("UVICORN_HTTP", "httptools"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
