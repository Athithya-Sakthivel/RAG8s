# rate_limits.py
from __future__ import annotations

from typing import Any

from config import (
    RATE_LIMIT_AUTH_CALLBACK,
    RATE_LIMIT_AUTH_LOGIN,
    RATE_LIMIT_AUTH_LOGOUT,
    RATE_LIMIT_AUTH_ME,
    RATE_LIMIT_AUTH_START,
    RATE_LIMIT_GENERATE_STREAM,
    RATE_LIMIT_STREAM_CONCURRENCY,
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


# ── Key functions for different route categories ─────────────

def _user_key_func(request: Request) -> str:
    """Global default key function: authenticated user sub, else empty -> no limit.
    This ensures that only authenticated routes get rate limited via user identity."""
    sub = verified_subject(request)
    if sub:
        return f"user:{sub}"
    return ""   # anonymous requests are not rate limited by default


def _auth_ip_key(request: Request) -> str:
    """IP‑based key for unauthenticated auth routes (login, start, callback, logout)."""
    return f"auth:ip:{get_ipaddr(request)}"


# ── Limiter singleton ────────────────────────────────────────
limiter = Limiter(
    key_func=_user_key_func,          # safe default for authenticated routes
    storage_uri=VALKEY_URL,
    headers_enabled=True,
    in_memory_fallback_enabled=True,
)


def install_rate_limits(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Rate limit strings ───────────────────────────────────────
class limits:
    generate_stream = RATE_LIMIT_GENERATE_STREAM
    auth_me         = RATE_LIMIT_AUTH_ME
    auth_login      = RATE_LIMIT_AUTH_LOGIN
    auth_start      = RATE_LIMIT_AUTH_START
    auth_callback   = RATE_LIMIT_AUTH_CALLBACK
    auth_logout     = RATE_LIMIT_AUTH_LOGOUT
    stream_concurrency = RATE_LIMIT_STREAM_CONCURRENCY


def generate_stream_concurrency_limit() -> int:
    return RATE_LIMIT_STREAM_CONCURRENCY