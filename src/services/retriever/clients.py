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
from opentelemetry import metrics, trace
from opentelemetry.propagate import inject
from opentelemetry.trace import Status, StatusCode
from settings import (
    DENSE_DIM,
    DEPLOYMENT_ENVIRONMENT,
    ENV,
    HTTP_MAX_CONNECTIONS,
    HTTP_MAX_KEEPALIVE,
    HTTP_TIMEOUT,
    RETRY_BASE_DELAY,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY,
    SERVICE_NAME,
)

T = TypeVar("T")


def _base_attrs(**extra: Any) -> dict[str, Any]:
    return {
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        "env": ENV,
        **{k: v for k, v in extra.items() if v is not None},
    }


def _current_span() -> trace.Span | None:
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return None
        return span
    except Exception:
        return None


def _span_event(event: str, **attrs: Any) -> None:
    span = _current_span()
    if span is not None:
        span.add_event(event, attributes={k: v for k, v in attrs.items() if v is not None})


def _span_error(exc: BaseException, **attrs: Any) -> None:
    span = _current_span()
    if span is not None:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR))


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
        meter = metrics.get_meter("retriever.breakers")
        self._open_counter = meter.create_counter(
            name="retrieval.circuit_breaker.open",
            description="Circuit breaker open events",
            unit="1",
        )

    async def allow(self) -> None:
        async with self._lock:
            if self.state != "open":
                return
            now = time.monotonic()
            if (now - self.opened_at) >= self.reset_timeout:
                self.state = "half_open"
                _span_event("circuit_breaker.half_open", dependency=self.name)
                return
            _span_event("circuit_breaker.open", dependency=self.name)
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
                self._open_counter.add(1, attributes=_base_attrs(dependency=self.name))
                _span_event("circuit_breaker.opened", dependency=self.name, failures=self.failures)


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
            retry_counter.add(1, attributes=_base_attrs(dependency=dep, attempt=attempt))
            _span_event(
                "retry_attempt",
                dependency=dep,
                attempt=attempt,
                max_attempts=RETRY_MAX_ATTEMPTS,
                sleep_s=round(delay + jitter, 6),
            )
            await asyncio.sleep(delay + jitter)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{dep} failed without exception")


_retry_meter = metrics.get_meter("retriever.retry")
retry_counter = _retry_meter.create_counter(
    name="retrieval.retry.attempts",
    description="Retry attempts by dependency",
    unit="1",
)


class AsyncJSONServiceClient:
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._meter = metrics.get_meter("retriever.clients")
        self._tracer = trace.get_tracer("retriever.clients")

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

    def _request_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"accept": "application/json"}
        inject(headers)
        return headers

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class AsyncDenseClient(AsyncJSONServiceClient):
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        super().__init__(base_url, timeout)
        self._count = self._meter.create_counter(
            name="retrieval.dense.embed.requests",
            description="Dense embedding requests",
            unit="1",
        )
        self._latency = self._meter.create_histogram(
            name="retrieval.dense.embed.duration",
            description="Dense embedding latency",
            unit="s",
        )

    async def health(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.base_url}/health", headers=self._request_headers())
            return r.status_code == 200
        except Exception:
            return False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        with self._tracer.start_as_current_span("dense.embed") as span:
            span.set_attribute("dependency", "dense")
            span.set_attribute("request.count", len(texts))
            start = time.perf_counter()
            self._count.add(1, attributes=_base_attrs(dependency="dense"))

            try:
                c = await self.client()
                r = await c.post(f"{self.base_url}/embed", json={"texts": texts}, headers=self._request_headers())
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
                span.set_status(Status(StatusCode.OK))
                return out
            except Exception as exc:
                _span_error(exc, dependency="dense", error_type=exc.__class__.__name__)
                raise
            finally:
                self._latency.record(max(time.perf_counter() - start, 1e-6), attributes=_base_attrs(dependency="dense"))


class AsyncSparseClient(AsyncJSONServiceClient):
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        super().__init__(base_url, timeout)
        self._count = self._meter.create_counter(
            name="retrieval.sparse.embed.requests",
            description="Sparse embedding requests",
            unit="1",
        )
        self._latency = self._meter.create_histogram(
            name="retrieval.sparse.embed.duration",
            description="Sparse embedding latency",
            unit="s",
        )

    async def health(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.base_url}/health", headers=self._request_headers())
            return r.status_code == 200
        except Exception:
            return False

    async def embed_chunked(self, texts: list[str]) -> list[dict[str, Any]]:
        if not texts:
            return []

        with self._tracer.start_as_current_span("sparse.embed") as span:
            span.set_attribute("dependency", "sparse")
            span.set_attribute("request.count", len(texts))
            start = time.perf_counter()
            self._count.add(1, attributes=_base_attrs(dependency="sparse"))

            async def _do(batch: list[str]) -> list[dict[str, Any]]:
                c = await self.client()
                r = await c.post(f"{self.base_url}/embed", json={"texts": batch}, headers=self._request_headers())
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
            except Exception as exc:
                _span_error(exc, dependency="sparse", error_type=exc.__class__.__name__)
                raise
            finally:
                self._latency.record(max(time.perf_counter() - start, 1e-6), attributes=_base_attrs(dependency="sparse"))


class AsyncRerankerClient(AsyncJSONServiceClient):
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        super().__init__(base_url, timeout)
        self._count = self._meter.create_counter(
            name="retrieval.rerank.requests",
            description="Reranker requests",
            unit="1",
        )
        self._latency = self._meter.create_histogram(
            name="retrieval.rerank.duration",
            description="Reranker latency",
            unit="s",
        )

    async def health(self) -> bool:
        try:
            c = await self.client()
            r = await c.get(f"{self.base_url}/health", headers=self._request_headers())
            return r.status_code == 200
        except Exception:
            return False

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []

        with self._tracer.start_as_current_span("rerank") as span:
            span.set_attribute("dependency", "reranker")
            span.set_attribute("request.count", len(documents))
            start = time.perf_counter()
            self._count.add(1, attributes=_base_attrs(dependency="reranker"))

            try:
                c = await self.client()
                r = await c.post(f"{self.base_url}/rerank", json={"query": query, "documents": documents}, headers=self._request_headers())
                if r.status_code != 200:
                    r.raise_for_status()
                j = r.json()
                scores = j.get("scores")
                if not isinstance(scores, list) or len(scores) != len(documents):
                    raise RuntimeError("reranker invalid response shape")
                span.set_status(Status(StatusCode.OK))
                return [float(x) for x in scores]
            except Exception as exc:
                _span_error(exc, dependency="reranker", error_type=exc.__class__.__name__)
                raise
            finally:
                self._latency.record(max(time.perf_counter() - start, 1e-6), attributes=_base_attrs(dependency="reranker"))


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
        self._meter = metrics.get_meter("retriever.clients")
        self._tracer = trace.get_tracer("retriever.clients")
        self._count = self._meter.create_counter(
            name="retrieval.llm.requests",
            description="LLM requests",
            unit="1",
        )
        self._latency = self._meter.create_histogram(
            name="retrieval.llm.duration",
            description="LLM call latency",
            unit="s",
        )
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
        with self._tracer.start_as_current_span("llm.generate") as span:
            span.set_attribute("dependency", "bedrock")
            span.set_attribute("mode", "generate")
            span.set_attribute("model_id", self.model_id)
            start = time.perf_counter()
            self._count.add(1, attributes=_base_attrs(dependency="bedrock", mode="generate"))

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
                answer = await asyncio.to_thread(_call)
                span.set_status(Status(StatusCode.OK))
                return answer
            except Exception as exc:
                _span_error(exc, dependency="bedrock", mode="generate", error_type=exc.__class__.__name__)
                raise
            finally:
                self._latency.record(max(time.perf_counter() - start, 1e-6), attributes=_base_attrs(dependency="bedrock", mode="generate"))

    async def stream(self, prompt: str, max_tokens: int, temperature: float) -> AsyncIterator[str]:
        with self._tracer.start_as_current_span("llm.stream") as span:
            span.set_attribute("dependency", "bedrock")
            span.set_attribute("mode", "stream")
            span.set_attribute("model_id", self.model_id)
            start = time.perf_counter()
            self._count.add(1, attributes=_base_attrs(dependency="bedrock", mode="stream"))
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
                        _span_error(item, dependency="bedrock", mode="stream", error_type=item.__class__.__name__)
                        raise item
                    yield str(item)
                span.set_status(Status(StatusCode.OK))
            finally:
                self._latency.record(max(time.perf_counter() - start, 1e-6), attributes=_base_attrs(dependency="bedrock", mode="stream"))


__all__ = [
    "AsyncBedrockClient",
    "AsyncDenseClient",
    "AsyncJSONServiceClient",
    "AsyncRerankerClient",
    "AsyncSparseClient",
    "CircuitBreaker",
    "OpenCircuitError",
    "call_with_retry",
    "is_retryable_exception",
]
