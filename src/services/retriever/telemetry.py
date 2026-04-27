#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE_NAME = os.getenv("SERVICE_NAME", "retrieval").strip() or "retrieval"
ENV = os.getenv("ENV", "STAGING").strip().upper() or "STAGING"

_CONFIGURED_LEVEL = "WARNING"
_CONFIGURED_LEVEL_ORDER = 30


def _canonical_level(level: str | None) -> str:
    raw = (level or os.getenv("LOG_LEVEL") or os.getenv("LOGLEVEL") or "WARNING") or "WARNING"
    raw = raw.strip().upper()
    return {
        "WARN": "WARNING",
        "WARNING": "WARNING",
        "INFO": "INFO",
        "DEBUG": "DEBUG",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }.get(raw, "WARNING")


def _level_order(level: str) -> int:
    return {
        "DEBUG": 10,
        "INFO": 20,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }.get(level, 20)


def _utc_now_iso_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def setup_logging(level: str | None = None) -> str:
    global _CONFIGURED_LEVEL, _CONFIGURED_LEVEL_ORDER
    configured = _canonical_level(level)
    log_level = getattr(logging, configured, logging.WARNING)
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    for name in (
        "httpx",
        "httpcore",
        "urllib3",
        "boto3",
        "botocore",
        "qdrant_client",
        "asyncio",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ):
        logging.getLogger(name).setLevel(log_level)
    _CONFIGURED_LEVEL = configured
    _CONFIGURED_LEVEL_ORDER = _level_order(configured)
    return configured


def apply_after_uvicorn(level: str | None = None) -> str:
    return setup_logging(level)


def json_log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    lvl = _canonical_level(level)
    if _level_order(lvl) < _CONFIGURED_LEVEL_ORDER:
        return
    record: dict[str, Any] = {
        "ts": _utc_now_iso_z(),
        "level": lvl.lower() if lvl != "WARNING" else "warn",
        "event": event,
        "msg": msg,
        "service": SERVICE_NAME,
        "env": ENV,
    }
    for key, value in extra.items():
        if key in {"ts", "level", "event", "msg", "service", "env"}:
            continue
        record[key] = value
    try:
        sys.stdout.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def safe_stack(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()


REQUEST_COUNT = Counter(
    "retrieval_requests_total",
    "Total requests served by the retriever service",
    ["service", "env", "endpoint", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "retrieval_request_duration_seconds",
    "Request latency in seconds",
    ["service", "env", "endpoint", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0),
)

ERROR_COUNT = Counter(
    "retrieval_errors_total",
    "Request errors by endpoint and status",
    ["service", "env", "endpoint", "status_code"],
)

RETRY_COUNT = Counter(
    "retrieval_retries_total",
    "Retry attempts by dependency",
    ["service", "env", "dependency"],
)

BREAKER_OPEN_COUNT = Counter(
    "circuit_breaker_open_total",
    "Circuit breaker open events",
    ["service", "env", "dependency"],
)

SERVICE_READY = Gauge(
    "service_ready",
    "Service readiness gauge (1 ready, 0 not ready)",
    ["service", "env"],
)

INFLIGHT_REQUESTS = Gauge(
    "retrieval_inflight_requests",
    "Current in-flight requests",
    ["service", "env", "endpoint"],
)

CACHE_LOOKUP_COUNT = Counter(
    "semantic_cache_lookups_total",
    "Semantic cache lookups",
    ["service", "env", "result"],
)

CACHE_LOOKUP_LATENCY = Histogram(
    "semantic_cache_lookup_duration_seconds",
    "Semantic cache lookup latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

CACHE_WRITE_COUNT = Counter(
    "semantic_cache_writes_total",
    "Semantic cache write attempts",
    ["service", "env", "result"],
)

CACHE_WRITE_LATENCY = Histogram(
    "semantic_cache_write_duration_seconds",
    "Semantic cache write latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

DENSE_EMBED_COUNT = Counter(
    "dense_embed_requests_total",
    "Dense embedding requests",
    ["service", "env"],
)

DENSE_EMBED_LATENCY = Histogram(
    "dense_embed_duration_seconds",
    "Dense embedding latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

SPARSE_EMBED_COUNT = Counter(
    "sparse_embed_requests_total",
    "Sparse embedding requests",
    ["service", "env"],
)

SPARSE_EMBED_LATENCY = Histogram(
    "sparse_embed_duration_seconds",
    "Sparse embedding latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

QDRANT_QUERY_COUNT = Counter(
    "qdrant_query_total",
    "Qdrant query count by mode",
    ["service", "env", "mode"],
)

QDRANT_QUERY_LATENCY = Histogram(
    "qdrant_query_duration_seconds",
    "Qdrant query latency by mode",
    ["service", "env", "mode"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

RERANK_COUNT = Counter(
    "rerank_requests_total",
    "Reranker requests",
    ["service", "env"],
)

RERANK_LATENCY = Histogram(
    "rerank_duration_seconds",
    "Reranker latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

LLM_CALL_COUNT = Counter(
    "llm_calls_total",
    "LLM calls",
    ["service", "env", "mode"],
)

LLM_CALL_LATENCY = Histogram(
    "llm_call_duration_seconds",
    "LLM latency",
    ["service", "env", "mode"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0),
)

RETRIEVED_DOCS = Histogram(
    "retrieved_docs_count",
    "Number of retrieved docs per request",
    ["service", "env"],
    buckets=(0, 1, 2, 3, 5, 10, 20, 50),
)


def metrics_response() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def set_service_ready(is_ready: bool) -> None:
    SERVICE_READY.labels(service=SERVICE_NAME, env=ENV).set(1 if is_ready else 0)
