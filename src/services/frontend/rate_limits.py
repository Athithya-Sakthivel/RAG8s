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
        _state_value(
            request,
            "auth_claims",
            "jwt_claims",
            "user_claims",
            "claims",
            "user",
        )
    )


def _limit_namespace(request: Request) -> str:
    path = _path(request)
    if path.startswith("/auth"):
        return "auth"
    if path.startswith("/generate/stream") or path.startswith("/api/generate/stream"):
        return "stream"
    return "default"


def rate_limit_key_func(request: Request) -> str:
    namespace = _limit_namespace(request)
    ip = get_ipaddr(request)

    if namespace == "auth":
        return f"auth:ip:{ip}"

    if namespace == "stream":
        subject = verified_subject(request)
        if subject:
            return f"stream:sub:{subject}"
        return f"stream:ip:{ip}"

    return f"default:ip:{ip}"


limiter = Limiter(
    key_func=rate_limit_key_func,
    storage_uri=VALKEY_URL,
    headers_enabled=True,
)


def install_rate_limits(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def auth_login_limit():
    return limiter.limit(
        RATE_LIMIT_AUTH_LOGIN,
        key_func=lambda request: f"auth:ip:{get_ipaddr(request)}",
    )


def auth_start_limit():
    return limiter.limit(
        RATE_LIMIT_AUTH_START,
        key_func=lambda request: f"auth:ip:{get_ipaddr(request)}",
    )


def auth_callback_limit():
    return limiter.limit(
        RATE_LIMIT_AUTH_CALLBACK,
        key_func=lambda request: f"auth:ip:{get_ipaddr(request)}",
    )


def auth_me_limit():
    return limiter.limit(
        RATE_LIMIT_AUTH_ME,
        key_func=lambda request: f"auth:ip:{get_ipaddr(request)}",
    )


def auth_logout_limit():
    return limiter.limit(
        RATE_LIMIT_AUTH_LOGOUT,
        key_func=lambda request: f"auth:ip:{get_ipaddr(request)}",
    )


def jwks_limit():
    return limiter.limit(
        RATE_LIMIT_JWKS,
        key_func=lambda request: f"auth:ip:{get_ipaddr(request)}",
    )


def generate_stream_limit(authenticated: bool = True):
    limit_value = RATE_LIMIT_GENERATE_STREAM_AUTH if authenticated else RATE_LIMIT_GENERATE_STREAM_ANON
    return limiter.limit(
        limit_value,
        key_func=lambda request: (
            f"stream:sub:{verified_subject(request)}"
            if verified_subject(request)
            else f"stream:ip:{get_ipaddr(request)}"
        ),
    )


def generate_stream_concurrency_limit() -> int:
    return RATE_LIMIT_GENERATE_STREAM_CONCURRENCY