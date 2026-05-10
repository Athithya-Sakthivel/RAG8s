from __future__ import annotations

import asyncio
from typing import Any

from config import (
    RATE_LIMIT_GENERATE_STREAM,
    RATE_LIMIT_AUTH_ME,
    RATE_LIMIT_STREAM_CONCURRENCY,
    VALKEY_URL,
)
from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_ipaddr

from frontend_logger import log


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


def _user_key_func(request: Request) -> str:
    sub = verified_subject(request)
    if sub:
        return f"user:{sub}"
    return ""


def _auth_ip_key(request: Request) -> str:
    return f"auth:ip:{get_ipaddr(request)}"


limiter = Limiter(
    key_func=_user_key_func,
    storage_uri=VALKEY_URL if VALKEY_URL else "memory://",
    headers_enabled=True,
    in_memory_fallback_enabled=True,
)


def install_rate_limits(app: FastAPI) -> None:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class limits:
    generate_stream   = RATE_LIMIT_GENERATE_STREAM
    auth_me           = RATE_LIMIT_AUTH_ME
    stream_concurrency = RATE_LIMIT_STREAM_CONCURRENCY

    auth_login    = "10/minute"
    auth_start    = "5/minute"
    auth_callback = "20/minute"
    auth_logout   = "20/minute"


def generate_stream_concurrency_limit() -> int:
    return RATE_LIMIT_STREAM_CONCURRENCY


async def is_valkey_available() -> bool:
    """Return True if Valkey is reachable and responds to PING."""
    if not VALKEY_URL:
        return False
    try:
        import redis.asyncio as redis_asyncio
        r = redis_asyncio.from_url(VALKEY_URL, socket_connect_timeout=3)
        # brief timeout, we are just checking connectivity
        await r.ping()
        await r.aclose()
        return True
    except Exception:
        return False