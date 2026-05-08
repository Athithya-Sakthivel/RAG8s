# metrics.py
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from config import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME

_ALLOWED_LABELS = {"route", "method", "status_code", "environment", "service", "event", "provider", "outcome", "route_class", "error_type"}


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


request_latency = Histogram(
    "frontend_request_latency_seconds",
    "Request latency in seconds",
    ("route", "method", "status_code", "environment", "service"),
)
requests_total = Counter(
    "frontend_requests_total",
    "Total application requests",
    ("route", "method", "status_code", "environment", "service"),
)
active_requests = Gauge(
    "frontend_active_requests",
    "Active in-flight requests",
    ("route", "method", "environment", "service"),
)

auth_events_total = Counter(
    "frontend_auth_events_total",
    "Authentication events",
    ("event", "provider", "outcome", "environment", "service"),
)

rate_limit_events_total = Counter(
    "frontend_rate_limit_events_total",
    "Rate limit decisions",
    ("route_class", "outcome", "environment", "service"),
)

upstream_stream_errors_total = Counter(
    "frontend_upstream_stream_errors_total",
    "Upstream stream errors",
    ("route_class", "error_type", "environment", "service"),
)

jwks_requests_total = Counter(
    "frontend_jwks_requests_total",
    "JWKS requests",
    ("outcome", "environment", "service"),
)

service_ready = Gauge(
    "frontend_service_ready",
    "Service readiness gauge",
    ("environment", "service"),
)
service_ready.labels(environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME).set(0)


def observe_request(route: str, method: str, status_code: int, elapsed_seconds: float) -> None:
    request_latency.labels(
        route=route, method=method, status_code=str(status_code),
        environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).observe(float(elapsed_seconds))
    requests_total.labels(
        route=route, method=method, status_code=str(status_code),
        environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).inc()


def track_active(route: str, method: str) -> None:
    active_requests.labels(
        route=route, method=method, environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).inc()


def untrack_active(route: str, method: str) -> None:
    active_requests.labels(
        route=route, method=method, environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).dec()


def record_auth_event(event: str, provider: str = "unknown", outcome: str = "ok") -> None:
    auth_events_total.labels(
        event=event, provider=provider, outcome=outcome,
        environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).inc()


def record_rate_limit(route_class: str, outcome: str) -> None:
    rate_limit_events_total.labels(
        route_class=route_class, outcome=outcome,
        environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).inc()


def record_upstream_stream_error(route_class: str, error_type: str) -> None:
    upstream_stream_errors_total.labels(
        route_class=route_class, error_type=error_type,
        environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).inc()


def record_jwks_request(outcome: str) -> None:
    jwks_requests_total.labels(
        outcome=outcome, environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME,
    ).inc()


def set_ready(is_ready: bool) -> None:
    service_ready.labels(environment=DEPLOYMENT_ENVIRONMENT, service=SERVICE_NAME).set(1 if is_ready else 0)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install_metrics(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    def _metrics():
        return metrics_response()


__all__ = [
    "_metric_labels",
    "request_latency",
    "requests_total",
    "active_requests",
    "auth_events_total",
    "rate_limit_events_total",
    "upstream_stream_errors_total",
    "jwks_requests_total",
    "service_ready",
    "observe_request",
    "track_active",
    "untrack_active",
    "record_auth_event",
    "record_rate_limit",
    "record_upstream_stream_error",
    "record_jwks_request",
    "set_ready",
    "metrics_response",
    "install_metrics",
]