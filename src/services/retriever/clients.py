#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import random
import re
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

import boto3
import httpx
import numpy as np
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, ReadTimeoutError
from settings import (
    DENSE_DIM,
    ENV,
    HTTP_MAX_CONNECTIONS,
    HTTP_MAX_KEEPALIVE,
    HTTP_TIMEOUT,
    RETRY_BASE_DELAY,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY,
    SERVICE_NAME,
)
from telemetry import (
    BREAKER_OPEN_COUNT,
    DENSE_EMBED_COUNT,
    DENSE_EMBED_LATENCY,
    LLM_CALL_COUNT,
    LLM_CALL_LATENCY,
    RERANK_COUNT,
    RERANK_LATENCY,
    RETRY_COUNT,
    SPARSE_EMBED_COUNT,
    SPARSE_EMBED_LATENCY,
)

T = TypeVar("T")


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


async def call_with_retry(dep: str, breaker: CircuitBreaker, fn: Callable[[], Any]):
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
