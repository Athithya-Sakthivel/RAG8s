from __future__ import annotations

import json
import logging
import os
import sys
import time as _time
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

DEPLOYMENT_ENVIRONMENT = (os.getenv("ENV") or "STAGING").strip().upper()
INSTANCE = os.getenv("POD_NAME", os.getenv("HOSTNAME", "unknown"))
NAMESPACE = os.getenv("POD_NAMESPACE", "unknown")

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
_ALLOWED_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}
if LOG_LEVEL not in _ALLOWED_LEVELS:
    sys.stderr.write(f"invalid LOG_LEVEL '{LOG_LEVEL}', defaulting to INFO\n")
    LOG_LEVEL = "INFO"

logging.basicConfig(stream=sys.stderr, level=getattr(logging, LOG_LEVEL, logging.INFO), force=True)

# Prometheus metrics
REQUEST_LATENCY = Histogram(
    "frontend_request_latency_seconds",
    "Request latency in seconds",
    ["service", "route", "method", "status_code", "environment"],
)
REQUESTS_TOTAL = Counter(
    "frontend_requests_total",
    "Total application requests",
    ["service", "route", "method", "status_code", "environment"],
)
ACTIVE_REQUESTS = Gauge(
    "frontend_active_requests",
    "Active in-flight requests",
    ["service", "route", "method", "environment"],
)
AUTH_EVENTS_TOTAL = Counter(
    "frontend_auth_events_total",
    "Authentication events",
    ["service", "event", "provider", "outcome", "environment"],
)
RATE_LIMIT_EVENTS_TOTAL = Counter(
    "frontend_rate_limit_events_total",
    "Rate limit decisions",
    ["service", "route_class", "outcome", "environment"],
)
UPSTREAM_STREAM_ERRORS_TOTAL = Counter(
    "frontend_upstream_stream_errors_total",
    "Upstream stream errors",
    ["service", "route_class", "error_type", "environment"],
)
JWKS_REQUESTS_TOTAL = Counter(
    "frontend_jwks_requests_total",
    "JWKS requests",
    ["service", "outcome", "environment"],
)
SERVICE_READY = Gauge(
    "frontend_service_ready",
    "Service readiness gauge",
    ["service", "environment"],
)
SERVICE_READY.labels(service=SERVICE, environment=DEPLOYMENT_ENVIRONMENT).set(1)


def _iso_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
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


class JsonFormatter(logging.Formatter):
    _STANDARD_ATTRS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _iso_ts(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "service": SERVICE,
            "environment": DEPLOYMENT_ENVIRONMENT,
            "instance": INSTANCE,
            "namespace": NAMESPACE,
        }
        extra_fields = {
            key: value
            for key, value in record.__dict__.items()
            if key not in self._STANDARD_ATTRS and not key.startswith("_") and value is not None
        }
        if record.exc_info and record.exc_text is None:
            extra_fields["exception"] = self.formatException(record.exc_info)
        if extra_fields:
            payload["fields"] = _jsonable(extra_fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


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
            "environment": DEPLOYMENT_ENVIRONMENT,
            "instance": INSTANCE,
            "namespace": NAMESPACE,
        }
        if fields:
            record["fields"] = _jsonable(fields)
        try:
            sys.stdout.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
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
    with ACTIVE_REQUESTS.labels(
        service=SERVICE, route=route, method=method, environment=DEPLOYMENT_ENVIRONMENT
    ).track_inprogress():
        start = _now()
        try:
            yield
            elapsed = _now() - start
            REQUEST_LATENCY.labels(
                service=SERVICE, route=route, method=method,
                status_class="2xx", environment=DEPLOYMENT_ENVIRONMENT,
            ).observe(elapsed)
            REQUESTS_TOTAL.labels(
                service=SERVICE, route=route, method=method,
                status_class="2xx", environment=DEPLOYMENT_ENVIRONMENT,
            ).inc()
        except Exception:
            elapsed = _now() - start
            REQUEST_LATENCY.labels(
                service=SERVICE, route=route, method=method,
                status_class="5xx", environment=DEPLOYMENT_ENVIRONMENT,
            ).observe(elapsed)
            REQUESTS_TOTAL.labels(
                service=SERVICE, route=route, method=method,
                status_class="5xx", environment=DEPLOYMENT_ENVIRONMENT,
            ).inc()
            raise


def observe_request(route: str, method: str, status_code: int, elapsed_seconds: float) -> None:
    REQUEST_LATENCY.labels(
        service=SERVICE, route=route, method=method,
        status_code=str(status_code), environment=DEPLOYMENT_ENVIRONMENT,
    ).observe(float(elapsed_seconds))
    REQUESTS_TOTAL.labels(
        service=SERVICE, route=route, method=method,
        status_code=str(status_code), environment=DEPLOYMENT_ENVIRONMENT,
    ).inc()


def track_active(route: str, method: str) -> None:
    ACTIVE_REQUESTS.labels(
        service=SERVICE, route=route, method=method,
        environment=DEPLOYMENT_ENVIRONMENT,
    ).inc()


def untrack_active(route: str, method: str) -> None:
    ACTIVE_REQUESTS.labels(
        service=SERVICE, route=route, method=method,
        environment=DEPLOYMENT_ENVIRONMENT,
    ).dec()


def record_auth_event(event: str, provider: str = "unknown", outcome: str = "ok") -> None:
    AUTH_EVENTS_TOTAL.labels(
        service=SERVICE, event=event, provider=provider,
        outcome=outcome, environment=DEPLOYMENT_ENVIRONMENT,
    ).inc()


def record_rate_limit(route_class: str, outcome: str) -> None:
    RATE_LIMIT_EVENTS_TOTAL.labels(
        service=SERVICE, route_class=route_class,
        outcome=outcome, environment=DEPLOYMENT_ENVIRONMENT,
    ).inc()


def record_upstream_stream_error(route_class: str, error_type: str) -> None:
    UPSTREAM_STREAM_ERRORS_TOTAL.labels(
        service=SERVICE, route_class=route_class,
        error_type=error_type, environment=DEPLOYMENT_ENVIRONMENT,
    ).inc()


def record_jwks_request(outcome: str) -> None:
    JWKS_REQUESTS_TOTAL.labels(
        service=SERVICE, outcome=outcome, environment=DEPLOYMENT_ENVIRONMENT,
    ).inc()


def set_ready(is_ready: bool, env: str | None = None) -> None:
    environment = env or DEPLOYMENT_ENVIRONMENT
    SERVICE_READY.labels(service=SERVICE, environment=environment).set(1 if is_ready else 0)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install_metrics(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    def _metrics():
        return metrics_response()