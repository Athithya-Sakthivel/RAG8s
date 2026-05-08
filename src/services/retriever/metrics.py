from __future__ import annotations

from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from settings import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME

_ALLOWED_LABELS = {"mode", "result", "cache_kind", "outcome", "attempt", "status_code", "dependency", "error_type"}


def _metric_labels(**extra: Any) -> dict[str, str]:
    base = {
        "environment": DEPLOYMENT_ENVIRONMENT,
        "service": SERVICE_NAME,
    }
    for k, v in extra.items():
        if k not in _ALLOWED_LABELS:
            continue
        if v is not None:
            base[k] = str(v)
    return base


http_request_count = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ("method", "route", "status_code", "environment", "service"),
)
http_request_duration = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ("method", "route", "status_code", "environment", "service"),
)
http_active_requests = Gauge(
    "http_active_requests",
    "In-flight HTTP requests",
    ("method", "route", "environment", "service"),
)
http_error_count = Counter(
    "http_errors_total",
    "Total HTTP errors",
    ("method", "route", "status_code", "environment", "service"),
)

pipeline_duration = Histogram(
    "pipeline_duration_seconds",
    "Total pipeline duration",
    ("outcome", "environment", "service"),
)
pipeline_errors = Counter(
    "pipeline_errors_total",
    "Pipeline errors by type",
    ("error_type", "environment", "service"),
)

qdrant_query_count = Counter(
    "qdrant_query_total",
    "Qdrant query count",
    ("mode", "environment", "service"),
)
qdrant_query_duration = Histogram(
    "qdrant_query_duration_seconds",
    "Qdrant query latency",
    ("mode", "environment", "service"),
)

cache_lookup_count = Counter(
    "cache_lookup_total",
    "Cache lookup count",
    ("result", "environment", "service"),
)
cache_lookup_duration = Histogram(
    "cache_lookup_duration_seconds",
    "Cache lookup latency",
    ("result", "environment", "service"),
)
cache_write_count = Counter(
    "cache_write_total",
    "Cache write count",
    ("result", "cache_kind", "environment", "service"),
)
cache_write_duration = Histogram(
    "cache_write_duration_seconds",
    "Cache write latency",
    ("cache_kind", "environment", "service"),
)

circuit_breaker_open = Counter(
    "circuit_breaker_open_total",
    "Circuit breaker open events",
    ("dependency", "environment", "service"),
)
retry_attempts = Counter(
    "retry_attempts_total",
    "Retry attempts by dependency",
    ("dependency", "attempt", "environment", "service"),
)
dependency_errors = Counter(
    "dependency_errors_total",
    "Dependency error count",
    ("dependency", "error_type", "environment", "service"),
)

dense_embed_requests = Counter(
    "dense_embed_requests_total",
    "Dense embedding requests",
    ("environment", "service"),
)
dense_embed_duration = Histogram(
    "dense_embed_duration_seconds",
    "Dense embedding latency",
    ("environment", "service"),
)

sparse_embed_requests = Counter(
    "sparse_embed_requests_total",
    "Sparse embedding requests",
    ("environment", "service"),
)
sparse_embed_duration = Histogram(
    "sparse_embed_duration_seconds",
    "Sparse embedding latency",
    ("environment", "service"),
)

rerank_requests = Counter(
    "rerank_requests_total",
    "Reranker requests",
    ("environment", "service"),
)
rerank_duration = Histogram(
    "rerank_duration_seconds",
    "Reranker latency",
    ("environment", "service"),
)

llm_requests = Counter(
    "llm_requests_total",
    "LLM requests",
    ("mode", "environment", "service"),
)
llm_duration = Histogram(
    "llm_duration_seconds",
    "LLM call latency",
    ("mode", "environment", "service"),
)

service_ready = Gauge(
    "service_ready",
    "Service readiness (1=ready, 0=not_ready)",
    ("environment", "service"),
)

__all__ = [
    "_metric_labels",
    "http_request_count",
    "http_request_duration",
    "http_active_requests",
    "http_error_count",
    "pipeline_duration",
    "pipeline_errors",
    "qdrant_query_count",
    "qdrant_query_duration",
    "cache_lookup_count",
    "cache_lookup_duration",
    "cache_write_count",
    "cache_write_duration",
    "circuit_breaker_open",
    "retry_attempts",
    "dependency_errors",
    "dense_embed_requests",
    "dense_embed_duration",
    "sparse_embed_requests",
    "sparse_embed_duration",
    "rerank_requests",
    "rerank_duration",
    "llm_requests",
    "llm_duration",
    "service_ready",
]