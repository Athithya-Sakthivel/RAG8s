from __future__ import annotations

import atexit
import json
import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse, urlunparse

try:
    from opentelemetry import metrics, trace
except Exception:  # pragma: no cover
    metrics = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter as GrpcOTLPLogExporter
except Exception:  # pragma: no cover
    try:
        from opentelemetry.exporter.otlp.proto.grpc.log_exporter import (
            OTLPLogExporter as GrpcOTLPLogExporter,  # type: ignore
        )
    except Exception:  # pragma: no cover
        GrpcOTLPLogExporter = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter as GrpcOTLPMetricExporter
except Exception:  # pragma: no cover
    GrpcOTLPMetricExporter = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcOTLPSpanExporter
except Exception:  # pragma: no cover
    GrpcOTLPSpanExporter = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter as HttpOTLPLogExporter
except Exception:  # pragma: no cover
    try:
        from opentelemetry.exporter.otlp.proto.http.log_exporter import (
            OTLPLogExporter as HttpOTLPLogExporter,  # type: ignore
        )
    except Exception:  # pragma: no cover
        HttpOTLPLogExporter = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter as HttpOTLPMetricExporter
except Exception:  # pragma: no cover
    HttpOTLPMetricExporter = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpOTLPSpanExporter
except Exception:  # pragma: no cover
    HttpOTLPSpanExporter = None  # type: ignore[assignment]

try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
except Exception:  # pragma: no cover
    set_logger_provider = None  # type: ignore[assignment]
    LoggerProvider = None  # type: ignore[assignment]
    LoggingHandler = None  # type: ignore[assignment]
    BatchLogRecordProcessor = None  # type: ignore[assignment]
    MeterProvider = None  # type: ignore[assignment]
    PeriodicExportingMetricReader = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]

try:
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased
except Exception:  # pragma: no cover
    from opentelemetry.sdk.trace.sampling import AlwaysOffSampler, AlwaysOnSampler, ParentBased, TraceIdRatioBased

    ALWAYS_ON = AlwaysOnSampler()
    ALWAYS_OFF = AlwaysOffSampler()

try:
    from .settings import (
        CLUSTER_NAME,
        DEPLOYMENT_ENVIRONMENT,
        ENABLE_OTEL_LOGS,
        ENABLE_OTEL_METRICS,
        ENABLE_OTEL_TRACES,
        INSTANCE_ID,
        LOG_LEVEL,
        OTEL_EXPORTER_OTLP_ENDPOINT,
        OTEL_EXPORTER_OTLP_PROTOCOL,
        OTEL_METRIC_EXPORT_INTERVAL_MS,
        OTEL_METRIC_EXPORT_TIMEOUT_MS,
        OTEL_TIMEOUT_SECONDS,
        SERVICE_NAME,
        SERVICE_VERSION,
    )
except Exception:  # pragma: no cover
    from settings import (  # type: ignore
        CLUSTER_NAME,
        DEPLOYMENT_ENVIRONMENT,
        ENABLE_OTEL_LOGS,
        ENABLE_OTEL_METRICS,
        ENABLE_OTEL_TRACES,
        INSTANCE_ID,
        LOG_LEVEL,
        OTEL_EXPORTER_OTLP_ENDPOINT,
        OTEL_EXPORTER_OTLP_PROTOCOL,
        OTEL_METRIC_EXPORT_INTERVAL_MS,
        OTEL_METRIC_EXPORT_TIMEOUT_MS,
        OTEL_TIMEOUT_SECONDS,
        SERVICE_NAME,
        SERVICE_VERSION,
    )

logger = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()
_HANDLE: TelemetryHandle | None = None
_ATEEXIT_REGISTERED = False


def _clean_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now_iso_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_level_name(raw: str | None, default: str = "INFO") -> str:
    level = (_clean_str(raw) or default).upper()
    aliases = {"WARN": "WARNING", "EXCEPTION": "ERROR"}
    level = aliases.get(level, level)
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    return level if level in valid else default


def _level_to_int(level_name: str) -> int:
    return getattr(logging, level_name, logging.INFO)


class _JsonFormatter(logging.Formatter):
    _standard_attrs: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_now_iso_z(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._standard_attrs or key.startswith("_") or value is None:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def json_log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    lvl_name = _normalize_level_name(level, default="INFO")
    payload = {
        "component": "telemetry",
        "event": event,
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        **{k: v for k, v in extra.items() if v is not None},
    }
    logger.log(_level_to_int(lvl_name), msg, extra=payload)


def _log(level: int, event: str, message: str, **fields: Any) -> None:
    payload = {
        "component": "telemetry",
        "event": event,
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        **{k: v for k, v in fields.items() if v is not None},
    }
    logger.log(level, message, extra=payload)


def _log_exception(event: str, message: str, **fields: Any) -> None:
    payload = {
        "component": "telemetry",
        "event": event,
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        **{k: v for k, v in fields.items() if v is not None},
    }
    logger.exception(message, extra=payload)


def setup_logging(level: str | None = None) -> str:
    configured = _normalize_level_name(level, default="WARNING")
    log_level = _level_to_int(configured)

    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(log_level)

    handler = next((h for h in root.handlers if getattr(h, "_retrieval_console_handler", False)), None)
    if handler is None:
        root.handlers.clear()
        handler = logging.StreamHandler()
        handler._retrieval_console_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    handler.setLevel(log_level)
    handler.setFormatter(_JsonFormatter())

    for name in (
        "asyncio",
        "httpx",
        "httpcore",
        "urllib3",
        "boto3",
        "botocore",
        "qdrant_client",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "opentelemetry",
    ):
        logging.getLogger(name).setLevel(log_level)

    return configured


def apply_after_uvicorn(level: str | None = None) -> str:
    return setup_logging(level)


def safe_stack(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()


def _normalize_protocol(raw: str | None) -> str:
    value = (_clean_str(raw) or "grpc").strip().lower()
    if value in {"grpc", "gprc"}:
        return "grpc"
    if value in {"http", "http/protobuf", "http-protobuf"}:
        return "http/protobuf"
    return "grpc"


def _build_http_endpoint(endpoint: str, signal: str) -> str:
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported OTLP HTTP scheme: {parsed.scheme!r}")
    base_path = parsed.path.rstrip("/")
    signal_path = f"/v1/{signal}"
    if not base_path:
        path = signal_path
    elif base_path.endswith(signal_path):
        path = base_path
    else:
        path = f"{base_path}{signal_path}"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, parsed.fragment))


def _grpc_endpoint(endpoint: str) -> tuple[str, bool]:
    parsed = urlparse(endpoint if "://" in endpoint else f"//{endpoint}", scheme="http")
    if parsed.scheme not in {"", "http", "https"}:
        raise ValueError(f"unsupported OTLP gRPC scheme: {parsed.scheme!r}")
    authority = (parsed.netloc or parsed.path).rstrip("/")
    if not authority:
        raise ValueError("OTLP gRPC endpoint is invalid")
    insecure = parsed.scheme != "https"
    return authority, insecure


def _resource(settings: Any):
    if Resource is None:
        return None
    attrs = {
        "service.name": _clean_str(getattr(settings, "service_name", None))
        or _clean_str(getattr(settings, "SERVICE_NAME", None))
        or SERVICE_NAME,
        "service.version": _clean_str(getattr(settings, "service_version", None))
        or _clean_str(getattr(settings, "SERVICE_VERSION", None))
        or SERVICE_VERSION,
        "deployment.environment": _clean_str(getattr(settings, "deployment_environment", None))
        or _clean_str(getattr(settings, "DEPLOYMENT_ENVIRONMENT", None))
        or DEPLOYMENT_ENVIRONMENT,
        "k8s.cluster.name": _clean_str(getattr(settings, "cluster_name", None))
        or _clean_str(getattr(settings, "CLUSTER_NAME", None))
        or CLUSTER_NAME,
        "service.instance.id": _clean_str(getattr(settings, "instance_id", None))
        or _clean_str(getattr(settings, "INSTANCE_ID", None))
        or INSTANCE_ID,
    }
    return Resource.create({k: v for k, v in attrs.items() if v})


def _sampler_ratio(settings: Any) -> float:
    raw = getattr(settings, "trace_sample_ratio", None)
    if raw is None:
        raw = getattr(settings, "TRACE_SAMPLE_RATIO", None)
    if raw is None:
        raw = getattr(settings, "otel_traces_sampler_arg", None)
    if raw is None:
        raw = getattr(settings, "OTEL_TRACES_SAMPLER_ARG", None)
    try:
        value = float(raw)
    except Exception:
        value = 0.1
    return value if 0.0 <= value <= 1.0 else 0.1


def _normalize_sampler_name(raw: str | None) -> str:
    value = (_clean_str(raw) or "parentbased_traceidratio").strip().lower()
    return {
        "always_on": "always_on",
        "always_off": "always_off",
        "traceidratio": "traceidratio",
        "parentbased_always_on": "parentbased_always_on",
        "parentbased_always_off": "parentbased_always_off",
        "parentbased_traceidratio": "parentbased_traceidratio",
    }.get(value, "parentbased_traceidratio")


def _build_sampler(settings: Any):
    sampler = _normalize_sampler_name(getattr(settings, "otel_traces_sampler", None) or getattr(settings, "OTEL_TRACES_SAMPLER", None))
    ratio = _sampler_ratio(settings)
    if sampler == "always_on":
        return ALWAYS_ON
    if sampler == "always_off":
        return ALWAYS_OFF
    if sampler == "traceidratio":
        return TraceIdRatioBased(ratio)
    if sampler == "parentbased_always_on":
        return ParentBased(root=ALWAYS_ON)
    if sampler == "parentbased_always_off":
        return ParentBased(root=ALWAYS_OFF)
    return ParentBased(root=TraceIdRatioBased(ratio))


@dataclass
class TelemetryHandle:
    tracer_provider: Any = None
    meter_provider: Any = None
    logger_provider: Any = None
    root_handler: logging.Handler | None = None
    otel_log_handler: logging.Handler | None = None
    log_level_name: str = "WARNING"
    protocol: str = "grpc"
    endpoint: str | None = None
    traces_enabled: bool = False
    metrics_enabled: bool = False
    logs_enabled: bool = False
    previous_log_record_factory: Callable[..., logging.LogRecord] | None = None
    _closed: bool = False

    def shutdown(self) -> None:
        global _HANDLE
        with _STATE_LOCK:
            if self._closed:
                return
            self._closed = True

            root_logger = logging.getLogger()

            if self.otel_log_handler is not None and self.otel_log_handler in root_logger.handlers:
                root_logger.removeHandler(self.otel_log_handler)
                try:
                    self.otel_log_handler.flush()
                except Exception:
                    pass
                try:
                    self.otel_log_handler.close()
                except Exception:
                    pass

            if self.root_handler is not None and self.root_handler in root_logger.handlers:
                root_logger.removeHandler(self.root_handler)
                try:
                    self.root_handler.flush()
                except Exception:
                    pass
                try:
                    self.root_handler.close()
                except Exception:
                    pass

            if self.previous_log_record_factory is not None:
                try:
                    logging.setLogRecordFactory(self.previous_log_record_factory)
                except Exception:
                    pass

            for provider in (self.logger_provider, self.meter_provider, self.tracer_provider):
                if provider is None:
                    continue
                try:
                    force_flush = getattr(provider, "force_flush", None)
                    if callable(force_flush):
                        force_flush()
                except Exception:
                    pass
                try:
                    provider.shutdown()
                except Exception:
                    pass

            if _HANDLE is self:
                _HANDLE = None


def annotate_current_span(*, user_sub: str | None = None, tenant_id: str | None = None, request_id: str | None = None) -> None:
    if trace is None:
        return
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return
        if user_sub:
            span.set_attribute("enduser.id", user_sub)
        if tenant_id:
            span.set_attribute("tenant.id", tenant_id)
        if request_id:
            span.set_attribute("http.request.id", request_id)
    except Exception:
        return


def initialize_telemetry(settings: Any) -> TelemetryHandle:
    global _HANDLE, _ATEEXIT_REGISTERED

    with _STATE_LOCK:
        level_name = _normalize_level_name(
            getattr(settings, "log_level", None) or getattr(settings, "LOG_LEVEL", None) or LOG_LEVEL
        )
        protocol = _normalize_protocol(
            getattr(settings, "otel_protocol", None)
            or getattr(settings, "OTEL_PROTOCOL", None)
            or getattr(settings, "OTEL_EXPORTER_OTLP_PROTOCOL", None)
            or OTEL_EXPORTER_OTLP_PROTOCOL
        )
        endpoint = _clean_str(
            getattr(settings, "otel_endpoint", None)
            or getattr(settings, "OTEL_ENDPOINT", None)
            or getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", None)
            or OTEL_EXPORTER_OTLP_ENDPOINT
        )

        root = logging.getLogger()
        if not root.handlers:
            setup_logging(level_name)
        else:
            root.setLevel(_level_to_int(level_name))
            for handler in root.handlers:
                try:
                    handler.setFormatter(_JsonFormatter())
                except Exception:
                    pass

        previous_factory = logging.getLogRecordFactory()

        resource = _resource(settings)
        traces_enabled = bool(
            getattr(settings, "enable_otel_traces", None)
            if getattr(settings, "enable_otel_traces", None) is not None
            else ENABLE_OTEL_TRACES
        )
        metrics_enabled = bool(
            getattr(settings, "enable_otel_metrics", None)
            if getattr(settings, "enable_otel_metrics", None) is not None
            else ENABLE_OTEL_METRICS
        )
        logs_enabled = bool(
            getattr(settings, "enable_otel_logs", None)
            if getattr(settings, "enable_otel_logs", None) is not None
            else ENABLE_OTEL_LOGS
        )

        _log(
            logging.INFO,
            "telemetry.initialize.start",
            "starting telemetry initialization",
            service_name=SERVICE_NAME,
            service_version=SERVICE_VERSION,
            deployment_environment=DEPLOYMENT_ENVIRONMENT,
            cluster_name=CLUSTER_NAME,
            instance_id=INSTANCE_ID,
            endpoint=endpoint,
            protocol=protocol,
            traces_enabled=traces_enabled,
            metrics_enabled=metrics_enabled,
            logs_enabled=logs_enabled,
        )

        tracer_provider = None
        meter_provider = None
        logger_provider = None
        otel_log_handler = None
        timeout_s = float(
            getattr(settings, "otel_timeout_seconds", None)
            or getattr(settings, "OTEL_TIMEOUT_SECONDS", None)
            or OTEL_TIMEOUT_SECONDS
        )
        metric_export_interval_ms = int(
            getattr(settings, "otel_metric_export_interval_ms", None)
            or getattr(settings, "OTEL_METRIC_EXPORT_INTERVAL_MS", None)
            or OTEL_METRIC_EXPORT_INTERVAL_MS
        )
        metric_export_timeout_ms = int(
            getattr(settings, "otel_metric_export_timeout_ms", None)
            or getattr(settings, "OTEL_METRIC_EXPORT_TIMEOUT_MS", None)
            or OTEL_METRIC_EXPORT_TIMEOUT_MS
        )

        try:
            if traces_enabled and endpoint and TracerProvider is not None and BatchSpanProcessor is not None:
                tracer_provider = TracerProvider(resource=resource, sampler=_build_sampler(settings))
                if protocol == "grpc":
                    if GrpcOTLPSpanExporter is None:
                        raise RuntimeError("grpc OTLP span exporter unavailable")
                    grpc_endpoint, insecure = _grpc_endpoint(endpoint)
                    exporter = GrpcOTLPSpanExporter(endpoint=grpc_endpoint, insecure=insecure, timeout=timeout_s)
                else:
                    if HttpOTLPSpanExporter is None:
                        raise RuntimeError("http OTLP span exporter unavailable")
                    exporter = HttpOTLPSpanExporter(endpoint=_build_http_endpoint(endpoint, "traces"), timeout=timeout_s)
                tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
                if trace is not None:
                    try:
                        current = trace.get_tracer_provider()
                        if current is not tracer_provider:
                            trace.set_tracer_provider(tracer_provider)
                    except Exception:
                        pass
        except Exception as exc:
            _log_exception(
                "telemetry.traces.disabled",
                "tracing initialization failed; continuing without OTEL traces",
                endpoint=endpoint,
                protocol=protocol,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            tracer_provider = None

        try:
            if metrics_enabled and endpoint and MeterProvider is not None and PeriodicExportingMetricReader is not None:
                if protocol == "grpc":
                    if GrpcOTLPMetricExporter is None:
                        raise RuntimeError("grpc OTLP metric exporter unavailable")
                    grpc_endpoint, insecure = _grpc_endpoint(endpoint)
                    metric_exporter = GrpcOTLPMetricExporter(endpoint=grpc_endpoint, insecure=insecure, timeout=timeout_s)
                else:
                    if HttpOTLPMetricExporter is None:
                        raise RuntimeError("http OTLP metric exporter unavailable")
                    metric_exporter = HttpOTLPMetricExporter(endpoint=_build_http_endpoint(endpoint, "metrics"), timeout=timeout_s)
                meter_provider = MeterProvider(
                    resource=resource,
                    metric_readers=[
                        PeriodicExportingMetricReader(
                            metric_exporter,
                            export_interval_millis=metric_export_interval_ms,
                            export_timeout_millis=metric_export_timeout_ms,
                        )
                    ],
                )
                if metrics is not None:
                    try:
                        metrics.set_meter_provider(meter_provider)
                    except Exception:
                        pass
        except Exception as exc:
            _log_exception(
                "telemetry.metrics.disabled",
                "metrics initialization failed; continuing without OTEL metrics",
                endpoint=endpoint,
                protocol=protocol,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            meter_provider = None

        try:
            if logs_enabled and endpoint and LoggerProvider is not None and BatchLogRecordProcessor is not None and LoggingHandler is not None:
                if protocol == "grpc":
                    if GrpcOTLPLogExporter is None:
                        raise RuntimeError("grpc OTLP log exporter unavailable")
                    grpc_endpoint, insecure = _grpc_endpoint(endpoint)
                    log_exporter = GrpcOTLPLogExporter(endpoint=grpc_endpoint, insecure=insecure, timeout=timeout_s)
                else:
                    if HttpOTLPLogExporter is None:
                        raise RuntimeError("http OTLP log exporter unavailable")
                    log_exporter = HttpOTLPLogExporter(endpoint=_build_http_endpoint(endpoint, "logs"), timeout=timeout_s)
                logger_provider = LoggerProvider(resource=resource)
                logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
                if set_logger_provider is not None:
                    try:
                        set_logger_provider(logger_provider)
                    except Exception:
                        pass
                otel_log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
                root.addHandler(otel_log_handler)
        except Exception as exc:
            _log_exception(
                "telemetry.logs.disabled",
                "logs initialization failed; continuing without OTEL logs",
                endpoint=endpoint,
                protocol=protocol,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            logger_provider = None
            otel_log_handler = None

        handle = TelemetryHandle(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            logger_provider=logger_provider,
            root_handler=next((handler for handler in root.handlers if getattr(handler, "_retrieval_console_handler", False)), None),
            otel_log_handler=otel_log_handler,
            log_level_name=level_name,
            protocol=protocol,
            endpoint=endpoint,
            traces_enabled=bool(traces_enabled and tracer_provider is not None),
            metrics_enabled=bool(metrics_enabled and meter_provider is not None),
            logs_enabled=bool(logs_enabled and logger_provider is not None),
            previous_log_record_factory=previous_factory,
        )
        _HANDLE = handle

        if not _ATEEXIT_REGISTERED:
            atexit.register(handle.shutdown)
            _ATEEXIT_REGISTERED = True

        _log(
            logging.INFO,
            "telemetry.initialize.complete",
            "telemetry initialization complete",
            endpoint=endpoint,
            protocol=protocol,
            traces_enabled=handle.traces_enabled,
            metrics_enabled=handle.metrics_enabled,
            logs_enabled=handle.logs_enabled,
        )
        return handle



__all__ = [
    "TelemetryHandle",
    "annotate_current_span",
    "apply_after_uvicorn",
    "initialize_telemetry",
    "json_log",
    "safe_stack",
    "setup_logging",
]