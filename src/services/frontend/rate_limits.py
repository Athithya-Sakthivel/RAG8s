# rate_limits.py
from __future__ import annotations

from typing import Any

from config import (
    RATE_LIMIT_AUTH_CALLBACK,
    RATE_LIMIT_AUTH_LOGIN,
    RATE_LIMIT_AUTH_LOGOUT,
    RATE_LIMIT_AUTH_ME,
    RATE_LIMIT_AUTH_START,
    RATE_LIMIT_GENERATE_STREAM_ANON,
    RATE_LIMIT_GENERATE_STREAM_AUTH,
    RATE_LIMIT_GENERATE_STREAM_CONCURRENCY,
    RATE_LIMIT_JWKS,
    VALKEY_URL,
)
from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_ipaddr

if not VALKEY_URL:
    raise RuntimeError("VALKEY_URL is required for rate limiting storage.")


def _path(request: Request) -> str:
    return (request.url.path or "").rstrip("/")


def _state_value(request: Request, *names: str) -> Any:
    state = getattr(request, "state", None)
    if state is None:
        return None
    for name in names:
        if hasattr(state, name):
            value = getattr(state, name)
            if value is not None:
                return value
    return None


def _claim_subject(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("sub", "user_id", "uid", "id"):
            candidate = value.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        return None
    for key in ("sub", "user_id", "uid", "id"):
        candidate = getattr(value, key, None)
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def verified_subject(request: Request) -> str | None:
    return _claim_subject(
        _state_value(request, "auth_claims", "jwt_claims", "user_claims", "claims", "user")
    )


def _global_key_func(request: Request) -> str:
    path = _path(request)
    if path.startswith("/generate/stream") or path.startswith("/api/generate/stream"):
        sub = verified_subject(request)
        if sub:
            return f"stream:sub:{sub}"
        return f"stream:ip:{get_ipaddr(request)}"

    if path.startswith("/auth") or path.startswith("/.well-known"):
        return f"auth:ip:{get_ipaddr(request)}"

    return f"default:ip:{get_ipaddr(request)}"


limiter = Limiter(
    key_func=_global_key_func,
    storage_uri=VALKEY_URL,
    headers_enabled=True,
    in_memory_fallback_enabled=True,
)


def install_rate_limits(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class limits:
    auth_login   = RATE_LIMIT_AUTH_LOGIN
    auth_start   = RATE_LIMIT_AUTH_START
    auth_callback = RATE_LIMIT_AUTH_CALLBACK
    auth_me      = RATE_LIMIT_AUTH_ME
    auth_logout  = RATE_LIMIT_AUTH_LOGOUT
    jwks         = RATE_LIMIT_JWKS
    stream_auth  = RATE_LIMIT_GENERATE_STREAM_AUTH
    stream_anon  = RATE_LIMIT_GENERATE_STREAM_ANON
    stream_concurrency = RATE_LIMIT_GENERATE_STREAM_CONCURRENCY


def generate_stream_concurrency_limit() -> int:
    return RATE_LIMIT_GENERATE_STREAM_CONCURRENCY