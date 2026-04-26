#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

SERVICE_NAME = os.getenv("SERVICE_NAME", "retrieval").strip() or "retrieval"
ENV = os.getenv("ENV", "STAGING").upper()

REQUEST_COUNT = Counter(
    "retrieval_requests_total",
    "HTTP requests served",
    ["service", "env", "endpoint", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "retrieval_request_duration_seconds",
    "Request latency",
    ["service", "env", "endpoint", "status_code"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
ERROR_COUNT = Counter(
    "retrieval_errors_total",
    "Retrieval errors",
    ["service", "env", "endpoint", "status_code"],
)

CACHE_LOOKUP_COUNT = Counter(
    "semantic_cache_lookups_total",
    "Semantic cache lookups",
    ["service", "env", "result"],
)
CACHE_WRITE_COUNT = Counter(
    "semantic_cache_writes_total",
    "Semantic cache writes",
    ["service", "env", "result"],
)
CACHE_LOOKUP_LATENCY = Histogram(
    "semantic_cache_lookup_duration_seconds",
    "Semantic cache lookup latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
CACHE_WRITE_LATENCY = Histogram(
    "semantic_cache_write_duration_seconds",
    "Semantic cache write latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

DENSE_EMBED_COUNT = Counter(
    "dense_embed_requests_total",
    "Dense embed requests",
    ["service", "env"],
)
DENSE_EMBED_LATENCY = Histogram(
    "dense_embed_duration_seconds",
    "Dense embed latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
SPARSE_EMBED_COUNT = Counter(
    "sparse_embed_requests_total",
    "Sparse embed requests",
    ["service", "env"],
)
SPARSE_EMBED_LATENCY = Histogram(
    "sparse_embed_duration_seconds",
    "Sparse embed latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

QDRANT_QUERY_COUNT = Counter(
    "qdrant_query_total",
    "Qdrant retrieval queries",
    ["service", "env", "mode"],
)
QDRANT_QUERY_LATENCY = Histogram(
    "qdrant_query_duration_seconds",
    "Qdrant query latency",
    ["service", "env", "mode"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

LLM_CALL_COUNT = Counter(
    "llm_calls_total",
    "LLM calls",
    ["service", "env"],
)
LLM_CALL_LATENCY = Histogram(
    "llm_call_duration_seconds",
    "LLM call latency",
    ["service", "env"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

RERANK_COUNT = Counter(
    "rerank_requests_total",
    "Rerank requests",
    ["service", "env"],
)
RERANK_LATENCY = Histogram(
    "rerank_duration_seconds",
    "Reranker latency",
    ["service", "env"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

RETRY_COUNT = Counter(
    "retrieval_retries_total",
    "Retry attempts for dependencies",
    ["service", "env", "dependency"],
)
BREAKER_OPEN_COUNT = Counter(
    "circuit_breaker_open_total",
    "Circuit breaker opens",
    ["service", "env", "dependency"],
)

SERVICE_READY = Gauge(
    "service_ready",
    "Service readiness",
    ["service", "env"],
)


def setup_logging(level: str | None = None) -> None:
    configured = (level or os.getenv("LOG_LEVEL", "WARN")).upper()
    root = logging.getLogger()
    if getattr(setup_logging, "_configured", False):
        root.setLevel(getattr(logging, configured, logging.INFO))
        return

    root.handlers.clear()
    root.setLevel(getattr(logging, configured, logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)

    for name in ("httpx", "httpcore", "urllib3", "boto3", "botocore", "qdrant_client", "asyncio"):
        lg = logging.getLogger(name)
        lg.propagate = False

    setup_logging._configured = True  # type: ignore[attr-defined]


def iso_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_stack(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()


def json_log(level: str, event: str, message: str = "", **extra: Any) -> None:
    lvl = (level or "info").strip().lower()
    if lvl in ("warn", "warning"):
        lvl = "warning"
    elif lvl in ("err", "error", "fatal", "critical"):
        lvl = "error"
    elif lvl not in ("debug", "info", "warning", "error"):
        lvl = "info"

    payload: dict[str, Any] = {
        "ts": iso_ts(),
        "level": lvl,
        "event": str(event or ""),
        "msg": str(message or ""),
        "service": SERVICE_NAME,
        "env": ENV,
    }
    for k, v in extra.items():
        if k in ("ts", "level", "event", "msg", "service", "env"):
            continue
        payload[k] = v

    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except Exception:
        try:
            logging.getLogger("retrieval.telemetry").exception("failed_to_emit_json_log")
        except Exception:
            pass
