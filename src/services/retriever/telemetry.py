from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from settings import (
    DEPLOYMENT_ENVIRONMENT,
    LOG_LEVEL,
    SERVICE_NAME,
)


def _iso_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonable(obj: Any) -> Any:
    """Recursively convert objects to JSON-safe types."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return str(obj)
    return obj


class JsonFormatter(logging.Formatter):
    """Log formatter that writes JSON lines matching the required schema."""

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
            "service": SERVICE_NAME,
            "environment": DEPLOYMENT_ENVIRONMENT,
            "instance": os.getenv("POD_NAME", os.getenv("HOSTNAME", "unknown")),
            "namespace": os.getenv("POD_NAMESPACE", "unknown"),
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
    """Convenience class for structured logging, directly writes to stdout."""

    def __init__(self) -> None:
        self._std = logging.getLogger("retrieval.json")
        self._level_map = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warn",
            logging.ERROR: "error",
        }
        self._instance = os.getenv("POD_NAME", os.getenv("HOSTNAME", "unknown"))
        self._namespace = os.getenv("POD_NAMESPACE", "unknown")

    def _emit(self, level_int: int, message: str, **fields: Any) -> None:
        record = {
            "timestamp": _iso_ts(),
            "level": self._level_map.get(level_int, "info"),
            "message": message or "",
            "service": SERVICE_NAME,
            "environment": DEPLOYMENT_ENVIRONMENT,
            "instance": self._instance,
            "namespace": self._namespace,
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


def setup_logging(level: str | None = None) -> str:
    configured = (level or LOG_LEVEL or "INFO").strip().upper()
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    aliases = {"WARN": "WARNING"}
    configured = aliases.get(configured, configured)
    if configured not in valid_levels:
        configured = "WARNING"
    log_level = getattr(logging, configured)

    root = logging.getLogger()
    root.setLevel(log_level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    for name in (
        "asyncio", "httpx", "httpcore", "urllib3", "boto3", "botocore",
        "qdrant_client", "uvicorn", "uvicorn.error", "uvicorn.access",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    return configured


def apply_after_uvicorn(level: str | None = None) -> str:
    return setup_logging(level)


def safe_stack(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    import traceback
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()


__all__ = [
    "JsonFormatter",
    "JsonLogger",
    "apply_after_uvicorn",
    "log",
    "safe_stack",
    "setup_logging",
]