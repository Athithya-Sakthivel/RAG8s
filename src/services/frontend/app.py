from __future__ import annotations

import asyncio
import os
import sys
import time as _time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import (
    EXTERNAL_BASE,
    GENERATE_STREAM_URL,
    REQUIRE_AUTH,
    SESSION_SECRET,
    STATIC_URL_PREFIX,
    UPSTREAM_TIMEOUT_SECONDS,
    USE_UVLOOP,
    has_jwt_signing_material,
)
from rate_limits import (
    generate_stream_concurrency_limit,
    install_rate_limits,
    limiter,
    limits,
    verified_subject,
)
from telemetry import (
    install_metrics,
    log,
    observe_request,
    record_upstream_stream_error,
    set_ready,
    track_active,
    untrack_active,
)

SERVICE_NAME = (os.getenv("SERVICE_NAME") or "frontend").strip()
ENV = (os.getenv("ENV") or "STAGING").strip().upper()

STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

try:
    import stateless_openid_auth as auth_mod
except Exception as exc:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("import_failed", module="stateless_openid_auth", stack=tb)
    raise

try:
    import frontend_ui as frontend_mod
except Exception as exc:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("import_failed", module="frontend_ui", stack=tb)
    raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS, connect=10.0, write=10.0, pool=5.0)
    limits_cfg = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0)
    app.state.http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits_cfg,
        follow_redirects=False,
        http2=False,
        headers={"User-Agent": f"{SERVICE_NAME}/{ENV}"},
    )
    app.state.stream_semaphore = asyncio.Semaphore(max(1, generate_stream_concurrency_limit()))

    if not has_jwt_signing_material():
        log.warn("JWT signing material missing; auth will be unavailable")
    if not SESSION_SECRET:
        log.warn("SESSION_SECRET missing; OAuth flow will fail")

    set_ready(True, ENV)
    log.info(
        "service.startup",
        service=SERVICE_NAME,
        env=ENV,
        external_base=EXTERNAL_BASE,
        generate_stream_url=GENERATE_STREAM_URL,
        require_auth=REQUIRE_AUTH,
        jwt_material_present=has_jwt_signing_material(),
    )
    try:
        yield
    finally:
        client = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()
        set_ready(False, ENV)
        log.info("service.shutdown", service=SERVICE_NAME, env=ENV)


app = FastAPI(title="frontend", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.mount(STATIC_URL_PREFIX, StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/auth", auth_mod.app)
app.include_router(frontend_mod.app.router)

install_rate_limits(app)
install_metrics(app)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    route = request.url.path.rstrip("/") or "/"
    method = request.method

    track_active(route, method)
    start = _time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        elapsed = _time.perf_counter() - start
        observe_request(route=route, method=method, status_code=status_code, elapsed_seconds=elapsed)
        untrack_active(route, method)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if path.startswith("/generate/stream") or path.startswith("/api/generate/stream"):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token:
                try:
                    request.state.auth_claims = auth_mod.verify_access_token(token)
                except Exception:
                    return JSONResponse({"detail": "Invalid token"}, status_code=401)
            else:
                request.state.auth_claims = None
        else:
            request.state.auth_claims = None
    return await call_next(request)


def _request_id(request: Request) -> str | None:
    rid = request.headers.get("x-request-id")
    return rid or None


def _client_ip(request: Request) -> str | None:
    if request.client and request.client.host:
        return request.client.host
    return None


def _upstream_headers(request: Request, claims: dict[str, Any] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        headers["Authorization"] = auth

    if claims:
        if claims.get("sub"):
            headers["X-Authenticated-Sub"] = str(claims["sub"])
        if claims.get("email"):
            headers["X-Authenticated-Email"] = str(claims["email"])
        if claims.get("provider"):
            headers["X-Authenticated-Provider"] = str(claims["provider"])

    rid = _request_id(request)
    if rid:
        headers["X-Request-ID"] = rid
    client_ip = _client_ip(request)
    if client_ip:
        headers["X-Forwarded-For"] = client_ip

    return headers


def _stream_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    }


async def _proxy_generate_stream(request: Request) -> StreamingResponse:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    claims = getattr(request.state, "auth_claims", None)
    if REQUIRE_AUTH and claims is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    client: httpx.AsyncClient = request.app.state.http_client
    headers = _upstream_headers(request, claims)

    upstream_request = client.build_request(
        "POST", GENERATE_STREAM_URL, json=body, headers=headers, timeout=None,
    )

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        log.error(
            "upstream.stream.connect_failed",
            path=request.url.path,
            error=str(exc),
            request_id=_request_id(request),
        )
        record_upstream_stream_error("generate_stream", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Upstream stream unavailable")

    if upstream.status_code >= 400:
        err_body = await upstream.aread()
        await upstream.aclose()
        detail = err_body.decode("utf-8", errors="replace")
        log.error(
            "upstream.stream.error",
            path=request.url.path,
            status_code=upstream.status_code,
            detail=detail[:2000],
            request_id=_request_id(request),
        )
        record_upstream_stream_error("generate_stream", f"http_{upstream.status_code}")
        raise HTTPException(status_code=502, detail=f"Upstream error: {upstream.status_code}")

    media_type = upstream.headers.get("content-type") or "text/event-stream"

    async def iterator():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    claims_sub = claims.get("sub") if claims else None
    log.info(
        "upstream.stream.start",
        path=request.url.path,
        status_code=upstream.status_code,
        content_type=media_type,
        request_id=_request_id(request),
        claims_sub=claims_sub,
        verified_subject=verified_subject(request),
    )
    return StreamingResponse(
        iterator(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=_stream_response_headers(),
    )


@app.post("/generate/stream", include_in_schema=False)
@limiter.limit(limits.stream_auth)
async def generate_stream(request: Request):
    sem: asyncio.Semaphore = request.app.state.stream_semaphore
    async with sem:
        return await _proxy_generate_stream(request)


@app.get("/.well-known/jwks.json", include_in_schema=False)
@limiter.limit(limits.jwks)
async def jwks_root(request: Request):
    return JSONResponse(auth_mod.PUBLIC_JWKS)


@app.get("/jwks.json", include_in_schema=False)
@limiter.limit(limits.jwks)
async def jwks_alias(request: Request):
    return JSONResponse(auth_mod.PUBLIC_JWKS)


@app.get("/login", include_in_schema=False)
async def login_redirect():
    return RedirectResponse(url="/auth/login", status_code=302)


@app.get("/orchestrator/health", include_in_schema=False)
async def orchestrator_health(request: Request):
    client_ready = bool(getattr(request.app.state, "http_client", None))
    auth_ready = bool(has_jwt_signing_material())
    return JSONResponse({
        "status": "ok" if client_ready and auth_ready else "degraded",
        "service": SERVICE_NAME,
        "env": ENV,
        "frontend_base": EXTERNAL_BASE,
        "generate_stream_url": GENERATE_STREAM_URL,
        "require_auth": REQUIRE_AUTH,
        "static_prefix": STATIC_URL_PREFIX,
        "auth_ready": auth_ready,
        "upstream_client_ready": client_ready,
    })


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        loop="uvloop" if USE_UVLOOP else "auto",
        http="httptools",
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )