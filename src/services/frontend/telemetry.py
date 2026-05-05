# telemetry.py
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = (os.getenv("SERVICE_NAME") or "frontend").strip()
if not SERVICE:
    raise RuntimeError("SERVICE_NAME must be set and non-empty")

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
_ALLOWED_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}
if LOG_LEVEL not in _ALLOWED_LEVELS:
    sys.stderr.write(f"invalid LOG_LEVEL '{LOG_LEVEL}', defaulting to INFO\n")
    LOG_LEVEL = "INFO"

logging.basicConfig(stream=sys.stderr, level=getattr(logging, LOG_LEVEL, logging.INFO), force=True)

_REQUEST_LATENCY = Histogram(
    "frontend_request_latency_seconds",
    "Request latency in seconds",
    ["service", "route", "method", "status_class"],
)

_REQUESTS_TOTAL = Counter(
    "frontend_requests_total",
    "Total application requests",
    ["service", "route", "method", "status_class"],
)

_ACTIVE_REQUESTS = Gauge(
    "frontend_active_requests",
    "Active in-flight requests",
    ["service", "route", "method"],
)

_AUTH_EVENTS_TOTAL = Counter(
    "frontend_auth_events_total",
    "Authentication events",
    ["service", "event", "provider", "outcome"],
)

_RATE_LIMIT_EVENTS_TOTAL = Counter(
    "frontend_rate_limit_events_total",
    "Rate limit decisions",
    ["service", "route_class", "outcome"],
)

_UPSTREAM_STREAM_ERRORS_TOTAL = Counter(
    "frontend_upstream_stream_errors_total",
    "Upstream stream errors",
    ["service", "route_class", "error_type"],
)

_JWKS_REQUESTS_TOTAL = Counter(
    "frontend_jwks_requests_total",
    "JWKS requests",
    ["service", "outcome"],
)

_SERVICE_READY = Gauge(
    "frontend_service_ready",
    "Service readiness gauge",
    ["service", "env"],
)
_SERVICE_READY.labels(service=SERVICE, env=(os.getenv("ENV") or "STAGING").strip().upper()).set(1)


def _iso_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


class JsonLogger:
    def __init__(self) -> None:
        self._std = logging.getLogger("frontend-json")
        self._level_map = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warn",
            logging.ERROR: "error",
        }

    def _emit(self, level_int: int, message: str, **fields: Any) -> None:
        record = {
            "timestamp": _iso_ts(),
            "level": self._level_map.get(level_int, "info"),
            "message": message or "",
            "service": SERVICE,
        }
        if fields:
            record["fields"] = _jsonable(fields)
        try:
            sys.stdout.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            try:
                sys.stderr.write(f"logger failed for message={message}\n")
            except Exception:
                pass

    def debug(self, msg: str, **kw: Any) -> None:
        if self._std.isEnabledFor(logging.DEBUG):
            self._emit(logging.DEBUG, msg, **kw)

    def info(self, msg: str, **kw: Any) -> None:
        if self._std.isEnabledFor(logging.INFO):
            self._emit(logging.INFO, msg, **kw)

    def warn(self, msg: str, **kw: Any) -> None:
        if self._std.isEnabledFor(logging.WARNING):
            self._emit(logging.WARNING, msg, **kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._emit(logging.ERROR, msg, **kw)

    def exception(self, msg: str, **kw: Any) -> None:
        kw.setdefault("exc_info", True)
        self._emit(logging.ERROR, msg, **kw)


log = JsonLogger()


def status_class(status_code: int) -> str:
    return f"{int(status_code) // 100}xx"


def _now() -> float:
    from time import monotonic
    return monotonic()


@contextmanager
def request_timer(route: str, method: str) -> Iterator[None]:
    with _ACTIVE_REQUESTS.labels(service=SERVICE, route=route, method=method).track_inprogress():
        start = _now()
        try:
            yield
            elapsed = _now() - start
            _REQUEST_LATENCY.labels(
                service=SERVICE,
                route=route,
                method=method,
                status_class="2xx",
            ).observe(elapsed)
            _REQUESTS_TOTAL.labels(
                service=SERVICE,
                route=route,
                method=method,
                status_class="2xx",
            ).inc()
        except Exception:
            elapsed = _now() - start
            _REQUEST_LATENCY.labels(
                service=SERVICE,
                route=route,
                method=method,
                status_class="5xx",
            ).observe(elapsed)
            _REQUESTS_TOTAL.labels(
                service=SERVICE,
                route=route,
                method=method,
                status_class="5xx",
            ).inc()
            raise


def observe_request(route: str, method: str, status_code: int, elapsed_seconds: float) -> None:
    cls = status_class(status_code)
    _REQUEST_LATENCY.labels(
        service=SERVICE,
        route=route,
        method=method,
        status_class=cls,
    ).observe(float(elapsed_seconds))
    _REQUESTS_TOTAL.labels(
        service=SERVICE,
        route=route,
        method=method,
        status_class=cls,
    ).inc()


def record_auth_event(event: str, provider: str = "unknown", outcome: str = "ok") -> None:
    _AUTH_EVENTS_TOTAL.labels(
        service=SERVICE,
        event=event,
        provider=provider,
        outcome=outcome,
    ).inc()


def record_rate_limit(route_class: str, outcome: str) -> None:
    _RATE_LIMIT_EVENTS_TOTAL.labels(
        service=SERVICE,
        route_class=route_class,
        outcome=outcome,
    ).inc()


def record_upstream_stream_error(route_class: str, error_type: str) -> None:
    _UPSTREAM_STREAM_ERRORS_TOTAL.labels(
        service=SERVICE,
        route_class=route_class,
        error_type=error_type,
    ).inc()


def record_jwks_request(outcome: str) -> None:
    _JWKS_REQUESTS_TOTAL.labels(service=SERVICE, outcome=outcome).inc()


def set_ready(is_ready: bool, env: str | None = None) -> None:
    environment = (env or os.getenv("ENV") or "STAGING").strip().upper()
    _SERVICE_READY.labels(service=SERVICE, env=environment).set(1 if is_ready else 0)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install_metrics(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    def _metrics():
        return metrics_response()