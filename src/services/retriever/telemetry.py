from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

try:
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
except ImportError:  # pragma: no cover
    from opentelemetry.exporter.otlp.proto.grpc.log_exporter import OTLPLogExporter  # type: ignore

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from settings import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME

try:
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased
except ImportError:  # pragma: no cover
    from opentelemetry.sdk.trace.sampling import AlwaysOffSampler, AlwaysOnSampler, ParentBased, TraceIdRatioBased

    ALWAYS_ON = AlwaysOnSampler()
    ALWAYS_OFF = AlwaysOffSampler()

logger = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()
_HANDLE: TelemetryHandle | None = None
_STATE_KEY: tuple[Any, ...] | None = None
_ATEEXIT_REGISTERED = False


def _clean_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_setting(settings: Any, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_level_name(raw: str | None, default: str = "INFO") -> str:
    level = (_clean_str(raw) or default).upper()
    aliases = {
        "WARN": "WARNING",
        "EXCEPTION": "ERROR",
    }
    level = aliases.get(level, level)
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    return level if level in valid else default


def _level_to_int(level_name: str) -> int:
    return getattr(logging, level_name, logging.INFO)


def _utc_now_iso_z() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def _current_span_fields() -> dict[str, str]:
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return {}
        return {
            "trace_id": f"{ctx.trace_id:032x}",
            "span_id": f"{ctx.span_id:016x}",
            "trace_flags": f"{int(ctx.trace_flags):02x}",
        }
    except Exception:
        return {}


def json_log(level: str, event: str, msg: str = "", **extra: Any) -> None:
    lvl_name = _normalize_level_name(level, default="INFO")
    if _level_to_int(lvl_name) < logging.getLogger().level:
        return

    payload = {
        "component": "telemetry",
        "event": event,
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        **{k: v for k, v in extra.items() if k not in {"ts", "level", "event", "msg"}},
        **_current_span_fields(),
    }
    logger.log(_level_to_int(lvl_name), msg, extra=payload)


def _log_with_fields(level: int, event: str, message: str, **fields: Any) -> None:
    payload = {
        "component": "telemetry",
        "event": event,
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        **{k: v for k, v in fields.items() if v is not None},
        **_current_span_fields(),
    }
    logger.log(level, message, extra=payload)


def _log_info(event: str, message: str, **fields: Any) -> None:
    _log_with_fields(logging.INFO, event, message, **fields)


def _log_warn(event: str, message: str, **fields: Any) -> None:
    _log_with_fields(logging.WARNING, event, message, **fields)


def _log_exception(event: str, message: str, **fields: Any) -> None:
    payload = {
        "component": "telemetry",
        "event": event,
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        **{k: v for k, v in fields.items() if v is not None},
        **_current_span_fields(),
    }
    logger.exception(message, extra=payload)


def setup_logging(level: str | None = None) -> str:
    configured = _normalize_level_name(level, default="WARNING")
    log_level = _level_to_int(configured)

    logging.captureWarnings(True)
    root = logging.getLogger()
    root.setLevel(log_level)

    console_handler = next((handler for handler in root.handlers if getattr(handler, "_retrieval_console_handler", False)), None)
    if console_handler is None:
        root.handlers.clear()
        console_handler = logging.StreamHandler()
        console_handler._retrieval_console_handler = True  # type: ignore[attr-defined]
        root.addHandler(console_handler)

    console_handler.setLevel(log_level)
    console_handler.setFormatter(_JsonFormatter())

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


def _grpc_endpoint(endpoint: str | None) -> tuple[str | None, bool]:
    raw = _clean_str(endpoint)
    if not raw:
        return None, True

    parsed = urlparse(raw if "://" in raw else f"//{raw}", scheme="http")

    if parsed.scheme not in ("", "http", "https"):
        raise ValueError(f"unsupported otel endpoint scheme: {parsed.scheme!r}")

    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(
            "otel_endpoint looks like OTLP/HTTP; this module is configured for OTLP/gRPC only"
        )

    authority = (parsed.netloc or parsed.path).rstrip("/")
    if not authority:
        raise ValueError("otel_endpoint is invalid")

    insecure = parsed.scheme != "https"
    return authority, insecure


def _resource(settings: Any) -> Resource:
    attrs = {
        "service.name": _clean_str(_get_setting(settings, "service_name", None))
        or _clean_str(_get_setting(settings, "SERVICE_NAME", None))
        or "unknown-service",
        "service.version": _clean_str(_get_setting(settings, "service_version", None))
        or _clean_str(_get_setting(settings, "SERVICE_VERSION", None)),
        "deployment.environment": _clean_str(_get_setting(settings, "deployment_environment", None))
        or _clean_str(_get_setting(settings, "DEPLOYMENT_ENVIRONMENT", None))
        or _clean_str(_get_setting(settings, "ENV", None)),
        "k8s.cluster.name": _clean_str(_get_setting(settings, "cluster_name", None))
        or _clean_str(_get_setting(settings, "CLUSTER_NAME", None)),
        "service.instance.id": _clean_str(_get_setting(settings, "instance_id", None))
        or _clean_str(_get_setting(settings, "INSTANCE_ID", None)),
    }
    clean_attrs = {k: v for k, v in attrs.items() if v is not None}
    return Resource.create(clean_attrs)


def _require_nonnegative_number(name: str, value: object | None, default: float) -> float:
    if value is None:
        return default
    try:
        num = float(value)
    except Exception:
        return default
    return num if num >= 0 else default


def _require_positive_number(name: str, value: object | None, default: float) -> float:
    num = _require_nonnegative_number(name, value, default)
    return num if num > 0 else default


def _require_ratio(name: str, value: object | None, default: float) -> float:
    num = _require_nonnegative_number(name, value, default)
    return num if 0.0 <= num <= 1.0 else default


def _require_positive_int(name: str, value: object | None, default: int) -> int:
    if value is None:
        return default
    try:
        num = int(value)
    except Exception:
        return default
    return num if num > 0 else default


def _normalize_sampler_name(raw: str | None) -> str:
    return (_clean_str(raw) or "parentbased_traceidratio").strip().lower()


def _trace_sample_ratio(settings: Any) -> float:
    ratio = _get_setting(settings, "trace_sample_ratio", None)
    if ratio is None:
        ratio = _get_setting(settings, "TRACE_SAMPLE_RATIO", None)
    if ratio is None:
        ratio = _get_setting(settings, "otel_traces_sampler_arg", None)
    if ratio is None:
        ratio = os.getenv("OTEL_TRACES_SAMPLER_ARG")
    return _require_ratio("trace_sample_ratio", ratio, 0.1)


def _build_sampler(settings: Any):
    sampler = _normalize_sampler_name(
        _get_setting(settings, "otel_traces_sampler", None) or _get_setting(settings, "OTEL_TRACES_SAMPLER", None)
    )
    ratio = _trace_sample_ratio(settings)

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


def _config_key(
    settings: Any,
    endpoint: str | None,
    insecure: bool,
    resource_attrs: dict[str, str],
    log_level_name: str,
    traces_enabled: bool,
    metrics_enabled: bool,
    logs_enabled: bool,
) -> tuple[Any, ...]:
    return (
        endpoint,
        insecure,
        tuple(sorted(resource_attrs.items())),
        _normalize_sampler_name(
            _get_setting(settings, "otel_traces_sampler", None) or _get_setting(settings, "OTEL_TRACES_SAMPLER", None)
        ),
        _trace_sample_ratio(settings),
        _require_positive_number(
            "otel_timeout_seconds",
            _get_setting(settings, "otel_timeout_seconds", None) or _get_setting(settings, "OTEL_TIMEOUT_SECONDS", None),
            5.0,
        ),
        _require_positive_int(
            "otel_metric_export_interval_ms",
            _get_setting(settings, "otel_metric_export_interval_ms", None),
            15000,
        ),
        _require_positive_int(
            "otel_metric_export_timeout_ms",
            _get_setting(settings, "otel_metric_export_timeout_ms", None),
            10000,
        ),
        log_level_name,
        traces_enabled,
        metrics_enabled,
        logs_enabled,
    )


class _DropOpenTelemetryRecords(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry")


def _log_record_factory(
    previous_factory: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        try:
            span = trace.get_current_span()
            ctx = span.get_span_context()
            if ctx is not None and ctx.is_valid:
                record.trace_id = f"{ctx.trace_id:032x}"
                record.span_id = f"{ctx.span_id:016x}"
                record.trace_flags = f"{int(ctx.trace_flags):02x}"
        except Exception:
            pass
        return record

    return factory


def _safe_set_tracer_provider(provider: TracerProvider) -> None:
    try:
        current = trace.get_tracer_provider()
        if current is provider:
            return
        trace.set_tracer_provider(provider)
    except Exception:
        return


def _safe_set_meter_provider(provider: MeterProvider) -> None:
    try:
        metrics.set_meter_provider(provider)
    except Exception:
        return


def _safe_set_logger_provider(provider: LoggerProvider) -> None:
    try:
        set_logger_provider(provider)
    except Exception:
        return


@dataclass
class TelemetryHandle:
    tracer_provider: TracerProvider | None
    meter_provider: MeterProvider | None
    logger_provider: LoggerProvider | None
    root_handler: logging.Handler | None
    otel_log_handler: logging.Handler | None
    log_level_name: str
    endpoint: str | None
    insecure: bool
    traces_enabled: bool
    metrics_enabled: bool
    logs_enabled: bool
    previous_log_record_factory: Callable[..., logging.LogRecord]
    _closed: bool = False

    def shutdown(self) -> None:
        global _HANDLE, _STATE_KEY

        with _STATE_LOCK:
            if self._closed:
                return
            self._closed = True

            _log_info(
                event="telemetry.shutdown.start",
                message="shutting down telemetry providers",
                endpoint=self.endpoint,
                insecure=self.insecure,
            )

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
                    _log_warn(
                        event="telemetry.shutdown.flush_failed",
                        message="root handler flush failed",
                        endpoint=self.endpoint,
                        insecure=self.insecure,
                    )
                try:
                    self.root_handler.close()
                except Exception:
                    _log_warn(
                        event="telemetry.shutdown.close_failed",
                        message="root handler close failed",
                        endpoint=self.endpoint,
                        insecure=self.insecure,
                    )

            try:
                logging.setLogRecordFactory(self.previous_log_record_factory)
            except Exception:
                _log_warn(
                    event="telemetry.shutdown.factory_restore_failed",
                    message="log record factory restore failed",
                    endpoint=self.endpoint,
                    insecure=self.insecure,
                )

            for provider_name, provider in (
                ("logger_provider", self.logger_provider),
                ("meter_provider", self.meter_provider),
                ("tracer_provider", self.tracer_provider),
            ):
                if provider is None:
                    continue
                try:
                    force_flush = getattr(provider, "force_flush", None)
                    if callable(force_flush):
                        force_flush()
                    provider.shutdown()
                    _log_info(
                        event="telemetry.shutdown.provider_complete",
                        message="provider shutdown complete",
                        provider=provider_name,
                        endpoint=self.endpoint,
                        insecure=self.insecure,
                    )
                except Exception:
                    _log_warn(
                        event="telemetry.shutdown.provider_failed",
                        message="provider shutdown failed",
                        provider=provider_name,
                        endpoint=self.endpoint,
                        insecure=self.insecure,
                    )

            if _HANDLE is self:
                _HANDLE = None
                _STATE_KEY = None

            _log_info(
                event="telemetry.shutdown.complete",
                message="telemetry shutdown complete",
                endpoint=self.endpoint,
                insecure=self.insecure,
            )


def initialize_telemetry(settings: Any) -> TelemetryHandle:
    global _HANDLE, _STATE_KEY, _ATEEXIT_REGISTERED

    with _STATE_LOCK:
        log_level_name = _normalize_level_name(
            _get_setting(settings, "log_level", None)
            or _get_setting(settings, "LOG_LEVEL", None)
        )
        endpoint, insecure = _grpc_endpoint(
            _get_setting(settings, "otel_endpoint", None)
            or _get_setting(settings, "OTEL_ENDPOINT", None)
        )
        resource = _resource(settings)
        resource_attrs = {
            k: v
            for k, v in resource.attributes.items()
            if isinstance(k, str) and isinstance(v, str)
        }

        traces_enabled = _env_flag("ENABLE_OTEL_TRACES", True)
        metrics_enabled = _env_flag("ENABLE_OTEL_METRICS", True)
        logs_enabled = _env_flag("ENABLE_OTEL_LOGS", True)

        config_key = _config_key(
            settings,
            endpoint,
            insecure,
            resource_attrs,
            log_level_name,
            traces_enabled,
            metrics_enabled,
            logs_enabled,
        )

        if _HANDLE is not None:
            if _STATE_KEY == config_key:
                _log_info(
                    event="telemetry.initialize.idempotent_hit",
                    message="telemetry already initialized with identical config",
                    endpoint=endpoint,
                    insecure=insecure,
                    log_level=log_level_name,
                )
                return _HANDLE
            _log_warn(
                event="telemetry.initialize.reused",
                message="telemetry already initialized; reusing existing providers",
                endpoint=endpoint,
                insecure=insecure,
                log_level=log_level_name,
            )
            return _HANDLE

        root_logger = logging.getLogger()
        if not root_logger.handlers:
            setup_logging(log_level_name)
        else:
            root_logger.setLevel(_level_to_int(log_level_name))

        previous_factory = logging.getLogRecordFactory()
        logging.setLogRecordFactory(_log_record_factory(previous_factory))

        _log_info(
            event="telemetry.initialize.start",
            message="starting telemetry initialization",
            service_name=resource_attrs.get("service.name"),
            service_version=resource_attrs.get("service.version"),
            deployment_environment=resource_attrs.get("deployment.environment"),
            cluster_name=resource_attrs.get("k8s.cluster.name"),
            instance_id=resource_attrs.get("service.instance.id"),
            endpoint=endpoint,
            insecure=insecure,
            log_level=log_level_name,
            trace_sample_ratio=_trace_sample_ratio(settings),
            otel_timeout_seconds=_require_positive_number(
                "otel_timeout_seconds",
                _get_setting(settings, "otel_timeout_seconds", None)
                or _get_setting(settings, "OTEL_TIMEOUT_SECONDS", None),
                5.0,
            ),
            otel_metric_export_interval_ms=_require_positive_int(
                "otel_metric_export_interval_ms",
                _get_setting(settings, "otel_metric_export_interval_ms", None),
                15000,
            ),
            otel_metric_export_timeout_ms=_require_positive_int(
                "otel_metric_export_timeout_ms",
                _get_setting(settings, "otel_metric_export_timeout_ms", None),
                10000,
            ),
            traces_enabled=traces_enabled,
            metrics_enabled=metrics_enabled,
            logs_enabled=logs_enabled,
        )

        tracer_provider: TracerProvider | None = None
        meter_provider: MeterProvider | None = None
        logger_provider: LoggerProvider | None = None
        root_handler: logging.Handler | None = next(
            (handler for handler in root_logger.handlers if getattr(handler, "_retrieval_console_handler", False)),
            None,
        )
        otel_log_handler: logging.Handler | None = None

        timeout_s = _require_positive_number(
            "otel_timeout_seconds",
            _get_setting(settings, "otel_timeout_seconds", None)
            or _get_setting(settings, "OTEL_TIMEOUT_SECONDS", None),
            5.0,
        )
        metric_export_interval_ms = _require_positive_int(
            "otel_metric_export_interval_ms",
            _get_setting(settings, "otel_metric_export_interval_ms", None),
            15000,
        )
        metric_export_timeout_ms = _require_positive_int(
            "otel_metric_export_timeout_ms",
            _get_setting(settings, "otel_metric_export_timeout_ms", None),
            10000,
        )

        if traces_enabled and endpoint:
            try:
                tracer_provider = TracerProvider(resource=resource, sampler=_build_sampler(settings))
                tracer_provider.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(
                            endpoint=endpoint,
                            insecure=insecure,
                            timeout=timeout_s,
                        )
                    )
                )
                _safe_set_tracer_provider(tracer_provider)
                _log_info(
                    event="telemetry.traces.configured",
                    message="tracing configured",
                    endpoint=endpoint,
                    insecure=insecure,
                )
            except Exception as exc:
                _log_exception(
                    event="telemetry.traces.disabled",
                    message="tracing initialization failed; continuing without OTEL traces",
                    endpoint=endpoint,
                    insecure=insecure,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                tracer_provider = None

        if metrics_enabled and endpoint:
            try:
                metric_exporter = OTLPMetricExporter(
                    endpoint=endpoint,
                    insecure=insecure,
                    timeout=timeout_s,
                )
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
                _safe_set_meter_provider(meter_provider)
                _log_info(
                    event="telemetry.metrics.configured",
                    message="metrics configured",
                    endpoint=endpoint,
                    insecure=insecure,
                )
            except Exception as exc:
                _log_exception(
                    event="telemetry.metrics.disabled",
                    message="metrics initialization failed; continuing without OTEL metrics",
                    endpoint=endpoint,
                    insecure=insecure,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                meter_provider = None

        if logs_enabled and endpoint:
            try:
                logger_provider = LoggerProvider(resource=resource)
                logger_provider.add_log_record_processor(
                    BatchLogRecordProcessor(
                        OTLPLogExporter(
                            endpoint=endpoint,
                            insecure=insecure,
                            timeout=timeout_s,
                        )
                    )
                )
                _safe_set_logger_provider(logger_provider)

                otel_log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
                otel_log_handler._otel_handler = True  # type: ignore[attr-defined]
                otel_log_handler.addFilter(_DropOpenTelemetryRecords())
                root_logger.addHandler(otel_log_handler)

                _log_info(
                    event="telemetry.logs.configured",
                    message="logs configured",
                    endpoint=endpoint,
                    insecure=insecure,
                )
            except Exception as exc:
                _log_exception(
                    event="telemetry.logs.disabled",
                    message="logs initialization failed; continuing without OTEL logs",
                    endpoint=endpoint,
                    insecure=insecure,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                logger_provider = None
                otel_log_handler = None

        handle = TelemetryHandle(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            logger_provider=logger_provider,
            root_handler=root_handler,
            otel_log_handler=otel_log_handler,
            log_level_name=log_level_name,
            endpoint=endpoint,
            insecure=insecure,
            traces_enabled=traces_enabled and tracer_provider is not None,
            metrics_enabled=metrics_enabled and meter_provider is not None,
            logs_enabled=logs_enabled and logger_provider is not None,
            previous_log_record_factory=previous_factory,
        )
        _HANDLE = handle
        _STATE_KEY = config_key

        if not _ATEEXIT_REGISTERED:
            atexit.register(handle.shutdown)
            _ATEEXIT_REGISTERED = True

        _log_info(
            event="telemetry.initialize.complete",
            message="telemetry initialization complete",
            endpoint=endpoint,
            insecure=insecure,
            log_level=log_level_name,
            traces_enabled=handle.traces_enabled,
            metrics_enabled=handle.metrics_enabled,
            logs_enabled=handle.logs_enabled,
        )
        return handle


__all__ = [
    "TelemetryHandle",
    "apply_after_uvicorn",
    "initialize_telemetry",
    "json_log",
    "safe_stack",
    "setup_logging",
]
