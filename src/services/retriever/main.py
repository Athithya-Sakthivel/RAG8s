#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
import settings as settings_module
import uvicorn
from clients import AsyncBedrockClient, AsyncDenseClient, AsyncRerankerClient, AsyncSparseClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from helpers import build_prompt_and_ui_chunks, deterministic_summarize, validate_and_filter_citations
from jwt import PyJWKClient
from opentelemetry import metrics, trace
from opentelemetry.context import attach, detach
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode, get_current_span
from pipeline import (
    ServiceState,
    _build_pipeline_result,
    _cache_cleanup_loop,
    _health_loop,
    _new_breakers,
    initialize_pipeline_metrics,
    write_stream_cache,
)
from settings import (
    ANSWER_PROMPT_TEMPLATE,
    AUTH_CALLBACK_PATH,
    AUTH_LOGIN_PATH,
    AUTH_LOGOUT_PATH,
    AWS_REGION,
    BEDROCK_GUARDRAIL_IDENTIFIER,
    BEDROCK_GUARDRAIL_VERSION,
    BEDROCK_MODEL_ID,
    CACHE_SCORE_THRESHOLD,
    CACHE_TTL_SECONDS,
    CLUSTER_NAME,
    COLLECTION_NAME,
    CORPUS_VERSION,
    DEFAULT_USER_RATE_LIMIT,
    DENSE_URL,
    DEPLOYMENT_ENVIRONMENT,
    ENV,
    HTTP_TIMEOUT,
    INSTANCE_ID,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LOG_LEVEL,
    MAX_CHUNKS_TO_LLM,
    MAX_CONCURRENT_REQUESTS,
    PROMPT_MAX_CONTENT_CHARS,
    PROMPT_VERSION,
    QDRANT_API_KEY,
    QDRANT_URL,
    RERANKER_URL,
    RETRIEVAL_VERSION,
    SERVICE_NAME,
    SERVICE_VERSION,
    SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    SESSION_SECRET,
    SESSION_TTL_SECONDS,
    SPARSE_URL,
    ZITADEL_ALLOWED_ALGORITHMS,
    ZITADEL_AUDIENCE,
    ZITADEL_AUTHORIZATION_ENDPOINT,
    ZITADEL_CLIENT_ID,
    ZITADEL_ISSUER,
    ZITADEL_JWKS_URI,
    ZITADEL_REDIRECT_URI,
    ZITADEL_SCOPES,
    ZITADEL_TOKEN_ENDPOINT,
    ZITADEL_USERINFO_ENDPOINT,
    GenerateRequest,
)
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask
from store import QdrantStore, QdrantStoreConfig
from telemetry import annotate_current_span, initialize_telemetry, json_log, safe_stack, setup_logging

logger = logging.getLogger("retrieval")
startup_bootstrap_error: str | None = None

_HTTP_METRICS_INITIALIZED = False
_HTTP_REQUEST_COUNT = None
_HTTP_ERROR_COUNT = None
_HTTP_DURATION = None
_HTTP_ACTIVE_REQUESTS = None
_RETRIEVED_DOCS = None

_FLOW_COOKIE_NAME = "retriever_oidc_flow"
_FLOW_COOKIE_TTL_SECONDS = 600
_DEFAULT_NEXT_PATH = "/generate/stream"


@dataclass(frozen=True)
class AuthPrincipal:
    sub: str
    email: str | None = None
    name: str | None = None
    preferred_username: str | None = None
    issuer: str | None = None
    audience: str | None = None
    scopes: tuple[str, ...] = ()
    raw_claims: dict[str, Any] | None = None


def _tracer():
    return trace.get_tracer("retriever.main")


def _meter():
    return metrics.get_meter("retriever.http")


def _http_metric_attrs(**extra: Any) -> dict[str, Any]:
    attrs = {
        "service.name": SERVICE_NAME,
        "deployment.environment": DEPLOYMENT_ENVIRONMENT,
        "env": ENV,
    }
    attrs.update({k: v for k, v in extra.items() if v is not None})
    return attrs


def initialize_http_metrics() -> None:
    global _HTTP_METRICS_INITIALIZED
    global _HTTP_REQUEST_COUNT, _HTTP_ERROR_COUNT, _HTTP_DURATION, _HTTP_ACTIVE_REQUESTS, _RETRIEVED_DOCS

    if _HTTP_METRICS_INITIALIZED:
        return

    meter = _meter()
    _HTTP_REQUEST_COUNT = meter.create_counter(
        name="http.server.request.count",
        description="Total HTTP requests",
        unit="1",
    )
    _HTTP_ERROR_COUNT = meter.create_counter(
        name="http.server.errors",
        description="Total failed HTTP requests",
        unit="1",
    )
    _HTTP_DURATION = meter.create_histogram(
        name="http.server.request.duration",
        description="HTTP request latency",
        unit="s",
        explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10],
    )
    _HTTP_ACTIVE_REQUESTS = meter.create_up_down_counter(
        name="http.server.active_requests",
        description="Number of in-flight HTTP requests",
        unit="1",
    )
    _RETRIEVED_DOCS = meter.create_histogram(
        name="retrieval.docs.returned",
        description="Returned documents per retrieval response",
        unit="1",
    )
    _HTTP_METRICS_INITIALIZED = True


def _record_docs_returned(operation: str, result: Any) -> None:
    if _RETRIEVED_DOCS is None:
        return
    count = len(result.chunks or []) if hasattr(result, "chunks") else 0
    _RETRIEVED_DOCS.record(
        count,
        attributes=_http_metric_attrs(
            operation=operation,
            retrieval_mode=getattr(result, "retrieval_mode", None),
            cache_hit=getattr(result, "cache_hit", None),
            hybrid_capable=getattr(result, "hybrid_capable", None),
        ),
    )


class RequestTelemetryMiddleware:
    def __init__(self, app: Any):
        self.app = app
        self.excluded_paths = {"/healthz", "/readyz"}

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        endpoint = scope.get("path") or "/"
        if endpoint in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        initialize_http_metrics()

        method = (scope.get("method") or "GET").upper()
        request_id = uuid.uuid4().hex
        headers = {
            (key.decode("latin-1") if isinstance(key, (bytes, bytearray)) else str(key)).lower(): (
                value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else str(value)
            )
            for key, value in (scope.get("headers") or [])
        }
        if headers.get("x-request-id"):
            request_id = headers["x-request-id"].strip() or request_id

        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id

        extracted = extract(headers)
        token = attach(extracted)
        start = time.perf_counter()
        status_code = 500
        recorded = False

        attrs = _http_metric_attrs(route=endpoint, method=method)
        if _HTTP_ACTIVE_REQUESTS is not None:
            _HTTP_ACTIVE_REQUESTS.add(1, attributes=attrs)
        if _HTTP_REQUEST_COUNT is not None:
            _HTTP_REQUEST_COUNT.add(1, attributes=attrs)

        tracer = _tracer()

        with tracer.start_as_current_span(
            f"{method} {endpoint}",
            context=extracted,
            kind=SpanKind.SERVER,
        ) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("http.route", endpoint)
            span.set_attribute("url.path", endpoint)
            span.set_attribute("http.request.id", request_id)
            span.set_attribute("service.name", SERVICE_NAME)
            span.set_attribute("service.version", SERVICE_VERSION)
            span.set_attribute("deployment.environment", DEPLOYMENT_ENVIRONMENT)
            span.set_attribute("k8s.cluster.name", CLUSTER_NAME)
            span.set_attribute("service.instance.id", INSTANCE_ID)

            async def send_wrapper(message):
                nonlocal status_code, recorded
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                    headers_list = list(message.get("headers") or [])
                    headers_list.append((b"x-request-id", request_id.encode("utf-8")))
                    message = dict(message)
                    message["headers"] = headers_list
                    span.set_attribute("http.response.status_code", status_code)
                elif message["type"] == "http.response.body" and not message.get("more_body", False) and not recorded:
                    recorded = True
                    elapsed = max(time.perf_counter() - start, 1e-6)
                    if _HTTP_DURATION is not None:
                        _HTTP_DURATION.record(
                            elapsed,
                            attributes=_http_metric_attrs(route=endpoint, method=method, status_code=status_code),
                        )
                    if status_code >= 500 and _HTTP_ERROR_COUNT is not None:
                        _HTTP_ERROR_COUNT.add(
                            1,
                            attributes=_http_metric_attrs(route=endpoint, method=method, status_code=status_code),
                        )
                    if status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except asyncio.CancelledError:
                span.set_status(Status(StatusCode.ERROR))
                raise
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                detach(token)
                if not recorded:
                    elapsed = max(time.perf_counter() - start, 1e-6)
                    if _HTTP_DURATION is not None:
                        _HTTP_DURATION.record(
                            elapsed,
                            attributes=_http_metric_attrs(route=endpoint, method=method, status_code=status_code),
                        )
                    if status_code >= 500 and _HTTP_ERROR_COUNT is not None:
                        _HTTP_ERROR_COUNT.add(
                            1,
                            attributes=_http_metric_attrs(route=endpoint, method=method, status_code=status_code),
                        )
                if _HTTP_ACTIVE_REQUESTS is not None:
                    _HTTP_ACTIVE_REQUESTS.add(-1, attributes=attrs)


def _make_settings() -> dict[str, Any]:
    return {
        "corpus_version": CORPUS_VERSION,
        "prompt_version": PROMPT_VERSION,
        "retrieval_version": RETRIEVAL_VERSION,
        "llm_model": BEDROCK_MODEL_ID,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "cache_score_threshold": CACHE_SCORE_THRESHOLD,
        "max_chunks_to_llm": MAX_CHUNKS_TO_LLM,
        "reranker_model": os.getenv("RERANKER_MODEL", "cross-encoder"),
    }


def _state() -> ServiceState:
    return app.state.state


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _build_bedrock_prompt(query: str, docs_for_llm: list[dict[str, Any]]) -> tuple[str, list[str], list[dict[str, Any]]]:
    prompt_body, llm_lines, ui_chunks = build_prompt_and_ui_chunks(
        docs_for_llm,
        query,
        max_content_chars=PROMPT_MAX_CONTENT_CHARS,
        prefer_snippet_len=400,
    )
    prompt = ANSWER_PROMPT_TEMPLATE.format(question=query, passages=prompt_body)
    return prompt, llm_lines, ui_chunks


def _session_secret() -> str:
    secret = (SESSION_SECRET or "").strip()
    if secret:
        return secret
    fallback = (ZITADEL_CLIENT_ID or ZITADEL_ISSUER or SERVICE_NAME).strip()
    return fallback or "retriever-dev-secret"


def _session_serializer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(_session_secret(), salt="retriever.session")


def _flow_serializer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(_session_secret(), salt="retriever.oidc-flow")


def _session_sign(data: dict[str, Any]) -> str:
    return _session_serializer().dumps(data)


def _session_load(token: str) -> dict[str, Any]:
    from itsdangerous import BadSignature, SignatureExpired

    try:
        return _session_serializer().loads(token, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(status_code=401, detail="invalid session") from exc


def _flow_sign(data: dict[str, Any]) -> str:
    return _flow_serializer().dumps(data)


def _flow_load(token: str) -> dict[str, Any]:
    from itsdangerous import BadSignature, SignatureExpired

    try:
        return _flow_serializer().loads(token, max_age=_FLOW_COOKIE_TTL_SECONDS)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(status_code=400, detail="invalid auth flow") from exc


def _base64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = _base64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def _clean_next_path(value: str | None) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return _DEFAULT_NEXT_PATH
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return _DEFAULT_NEXT_PATH
    if not candidate.startswith("/"):
        return _DEFAULT_NEXT_PATH
    return candidate


def _auth_redirect_url(request: Request, next_path: str | None = None) -> str:
    path = _clean_next_path(next_path)
    query = urlencode({"next": path})
    base = request.url_for("auth_login")
    return f"{base!s}?{query}"


def _get_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0].lower(), parts[1].strip()
    if scheme != "bearer" or not token:
        return None
    return token


def _jwk_client() -> PyJWKClient:
    return PyJWKClient(ZITADEL_JWKS_URI, cache_keys=True)


def _verify_jwt(token: str) -> dict[str, Any]:
    signing_key = _jwk_client().get_signing_key_from_jwt(token).key
    audience = ZITADEL_AUDIENCE.strip() or None
    claims = jwt.decode(
        token,
        signing_key,
        algorithms=list(ZITADEL_ALLOWED_ALGORITHMS),
        audience=audience,
        issuer=ZITADEL_ISSUER,
        options={
            "require": ["exp", "iat", "iss", "sub"],
            "verify_aud": bool(audience),
        },
    )
    return claims


def _claims_to_principal(claims: dict[str, Any]) -> AuthPrincipal:
    scopes = claims.get("scope") or claims.get("scopes") or ""
    scope_tuple: tuple[str, ...]
    if isinstance(scopes, str):
        scope_tuple = tuple(s for s in scopes.split() if s)
    elif isinstance(scopes, (list, tuple)):
        scope_tuple = tuple(str(s) for s in scopes if s)
    else:
        scope_tuple = ()
    return AuthPrincipal(
        sub=str(claims.get("sub") or ""),
        email=claims.get("email"),
        name=claims.get("name"),
        preferred_username=claims.get("preferred_username"),
        issuer=claims.get("iss"),
        audience=" ".join(claims.get("aud", [])) if isinstance(claims.get("aud"), list) else str(claims.get("aud") or ""),
        scopes=scope_tuple,
        raw_claims=claims,
    )


def _principal_from_session_cookie(request: Request) -> AuthPrincipal | None:
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None
    data = _session_load(raw)
    exp = int(data.get("exp") or 0)
    if exp and exp < int(time.time()):
        return None
    sub = str(data.get("sub") or "").strip()
    if not sub:
        return None
    return AuthPrincipal(
        sub=sub,
        email=data.get("email"),
        name=data.get("name"),
        preferred_username=data.get("preferred_username"),
        issuer=data.get("iss"),
        audience=data.get("aud"),
        scopes=tuple(data.get("scopes") or ()),
        raw_claims=data,
    )


def _principal_from_bearer_token(request: Request) -> AuthPrincipal | None:
    token = _get_bearer_token(request)
    if not token:
        return None
    claims = _verify_jwt(token)
    return _claims_to_principal(claims)


def _principal_from_request_quick(request: Request) -> AuthPrincipal | None:
    try:
        principal = _principal_from_session_cookie(request)
        if principal:
            return principal
    except Exception:
        pass

    try:
        principal = _principal_from_bearer_token(request)
        if principal:
            return principal
    except Exception:
        pass

    return None


def _rate_limit_key(request: Request) -> str:
    principal = _principal_from_request_quick(request)
    if principal and principal.sub:
        return f"user:{principal.sub}"
    return f"ip:{get_remote_address(request)}"



def _resolve_authenticated_principal(request: Request) -> AuthPrincipal | RedirectResponse:
    principal = _principal_from_session_cookie(request)
    if principal:
        request.state.user_sub = principal.sub
        request.state.principal = principal
        return principal

    principal = _principal_from_bearer_token(request)
    if principal:
        request.state.user_sub = principal.sub
        request.state.principal = principal
        return principal

    return RedirectResponse(_auth_redirect_url(request, next_path=_DEFAULT_NEXT_PATH), status_code=303)


async def _load_generate_request(request: Request) -> GenerateRequest:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid json body") from exc

    try:
        return GenerateRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _exchange_code_for_tokens(code: str, code_verifier: str) -> dict[str, Any]:
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ZITADEL_REDIRECT_URI,
        "client_id": ZITADEL_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    client_secret = (os.getenv("ZITADEL_CLIENT_SECRET") or os.getenv("OIDC_CLIENT_SECRET") or "").strip()
    if client_secret:
        token_data["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
        resp = await client.post(
            ZITADEL_TOKEN_ENDPOINT,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


async def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:
        resp = await client.get(
            ZITADEL_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


def _build_authorize_url(state: str, nonce: str, code_challenge: str) -> str:
    params = {
        "client_id": ZITADEL_CLIENT_ID,
        "response_type": "code",
        "scope": " ".join(ZITADEL_SCOPES),
        "redirect_uri": ZITADEL_REDIRECT_URI,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{ZITADEL_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"


def _set_session_cookie(response: RedirectResponse, principal: AuthPrincipal) -> None:
    payload = {
        "sub": principal.sub,
        "email": principal.email,
        "name": principal.name,
        "preferred_username": principal.preferred_username,
        "iss": principal.issuer,
        "aud": principal.audience,
        "scopes": list(principal.scopes),
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_session_sign(payload),
        max_age=SESSION_TTL_SECONDS,
        expires=SESSION_TTL_SECONDS,
        secure=SESSION_COOKIE_SECURE,
        httponly=SESSION_COOKIE_HTTPONLY,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(LOG_LEVEL)

    if not (ZITADEL_CLIENT_ID or "").strip():
        raise RuntimeError("ZITADEL_CLIENT_ID is required")
    if not (ZITADEL_REDIRECT_URI or "").strip():
        raise RuntimeError("ZITADEL_REDIRECT_URI is required")

    if not (SESSION_SECRET or "").strip():
        json_log(
            "warning",
            "session.secret.fallback",
            "SESSION_SECRET is not set; using a fallback secret",
            fallback_source="ZITADEL_CLIENT_ID_or_issuer",
        )

    try:
        initialize_telemetry(settings_module)
    except Exception as exc:
        json_log(
            "error",
            "telemetry.initialize_failed",
            "telemetry initialization failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    initialize_http_metrics()
    initialize_pipeline_metrics()

    settings = _make_settings()
    store = QdrantStore(
        QdrantStoreConfig(
            url=QDRANT_URL,
            api_key=QDRANT_API_KEY or "",
            docs_collection=COLLECTION_NAME,
            cache_collection=os.getenv("CACHE_COLLECTION_NAME", f"{COLLECTION_NAME}__semantic_cache"),
            dense_dim=int(os.getenv("DENSE_DIM", "384")),
        )
    )

    dense = AsyncDenseClient(DENSE_URL, timeout=HTTP_TIMEOUT)
    sparse = AsyncSparseClient(SPARSE_URL, timeout=HTTP_TIMEOUT)
    reranker = AsyncRerankerClient(RERANKER_URL, timeout=HTTP_TIMEOUT)
    bedrock = AsyncBedrockClient(
        region=AWS_REGION,
        model_id=BEDROCK_MODEL_ID,
        guardrail_identifier=BEDROCK_GUARDRAIL_IDENTIFIER,
        guardrail_version=BEDROCK_GUARDRAIL_VERSION,
        timeout=HTTP_TIMEOUT,
    )

    state = ServiceState(
        settings=settings,
        store=store,
        dense=dense,
        sparse=sparse,
        reranker=reranker,
        bedrock=bedrock,
        breakers=_new_breakers(),
        health={
            "qdrant": False,
            "docs_collection_ready": False,
            "cache_collection_ready": False,
            "dense": False,
            "sparse": False,
            "reranker": False,
            "bedrock": bool(bedrock.health()),
            "hybrid_capable": False,
            "ready": False,
        },
        semaphore=asyncio.Semaphore(MAX_CONCURRENT_REQUESTS),
    )
    app.state.state = state

    global startup_bootstrap_error
    startup_bootstrap_error = None
    try:
        docs_ready, cache_ready = await state.store.bootstrap()
        state.store.docs_ready = docs_ready
        state.store.cache_ready = cache_ready
    except Exception as e:
        startup_bootstrap_error = str(e)
        json_log("info", "bootstrap.pending", "initial qdrant bootstrap not ready yet", error=str(e))

    bg_health = asyncio.create_task(_health_loop(state))
    bg_cleanup = asyncio.create_task(_cache_cleanup_loop(state))
    app.state.background_tasks = (bg_health, bg_cleanup)

    try:
        yield
    finally:
        for task in app.state.background_tasks:
            task.cancel()

        for task in app.state.background_tasks:
            try:
                await task
            except Exception:
                pass

        for c in (dense, sparse, reranker):
            try:
                await c.close()
            except Exception:
                pass
        try:
            await store.close()
        except Exception:
            pass

        try:
            if hasattr(app.state, "state"):
                app.state.state.health["ready"] = False
        except Exception:
            pass

limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(RequestTelemetryMiddleware)


async def _generate_stream_core(request: Request) -> StreamingResponse | RedirectResponse:
    auth_result = _resolve_authenticated_principal(request)
    if isinstance(auth_result, RedirectResponse):
        return auth_result

    request.state.principal = auth_result
    request.state.user_sub = auth_result.sub
    annotate_current_span(
        user_sub=auth_result.sub,
        request_id=getattr(request.state, "request_id", None),
    )

    req = await _load_generate_request(request)

    state = _state()
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    pipeline = await _build_pipeline_result(
        state,
        query=query,
        top_k=int(req.top_k),
        fetch_k=int(req.fetch_k),
        corpus_version=req.corpus_version or CORPUS_VERSION,
        prompt_version=req.prompt_version or PROMPT_VERSION,
        retrieval_version=req.retrieval_version or RETRIEVAL_VERSION,
        model_name=req.model_name or BEDROCK_MODEL_ID,
        tenant_id=req.tenant_id,
        allow_semantic_cache=bool(req.allow_semantic_cache),
        allow_rerank=True,
        include_answer=False,
        max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
    )
    _record_docs_returned("stream", pipeline)

    cache_state: dict[str, Any] = {"answer": None, "chunks": None}

    async def finalize_cache() -> None:
        if pipeline.cache_hit:
            return
        if not pipeline.final_candidates:
            return
        answer = str(cache_state.get("answer") or "").strip()
        chunks = cache_state.get("chunks") or []
        if answer and answer not in {"no documents retrieved", "llm unavailable"}:
            await write_stream_cache(
                state,
                pipeline=pipeline,
                answer=answer,
                ui_chunks=chunks,
                hit_type="llm",
                cache_score=1.0,
            )

    async def event_gen() -> AsyncIterator[str]:
        current_span = get_current_span()
        start_event = {
            "query": query,
            "retrieval": pipeline.retrieval,
            "cache": pipeline.cache,
            "cache_hit": pipeline.cache_hit,
            "cache_score": pipeline.cache_score,
            "retrieval_mode": pipeline.retrieval_mode,
            "hybrid_capable": pipeline.hybrid_capable,
            "chunks": pipeline.chunks if req.return_chunks else None,
        }
        yield _sse("start", start_event)

        if pipeline.cache_hit and pipeline.answer is not None:
            cache_state["answer"] = pipeline.answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": pipeline.answer})
            yield _sse(
                "done",
                {
                    "answer": pipeline.answer,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
            return

        docs_for_llm = pipeline.final_candidates[: min(len(pipeline.final_candidates), MAX_CHUNKS_TO_LLM)]
        if not docs_for_llm:
            answer = "no documents retrieved"
            cache_state["answer"] = answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": answer})
            yield _sse(
                "done",
                {
                    "answer": answer,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
            return

        prompt, llm_lines, ui_chunks = _build_bedrock_prompt(query, docs_for_llm)
        answer_parts: list[str] = []

        if not state.bedrock.health():
            fallback = deterministic_summarize(llm_lines) or "llm unavailable"
            cache_state["answer"] = fallback
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("delta", {"text": fallback})
            yield _sse(
                "done",
                {
                    "answer": fallback,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
            return

        try:
            async for delta in state.bedrock.stream(
                prompt=prompt,
                max_tokens=int(req.max_tokens or LLM_MAX_TOKENS),
                temperature=LLM_TEMPERATURE,
            ):
                if await request.is_disconnected():
                    if current_span is not None:
                        current_span.add_event("request.client_disconnected")
                    return
                if delta:
                    answer_parts.append(delta)
                    yield _sse("delta", {"text": delta})

            answer = "".join(answer_parts).strip()
            if not answer:
                answer = deterministic_summarize(llm_lines)
            valid_indexes = [c["index"] for c in ui_chunks if isinstance(c, dict) and c.get("index") is not None]
            answer = validate_and_filter_citations(answer, valid_indexes)
            if not answer.strip():
                answer = deterministic_summarize(llm_lines)

            cache_state["answer"] = answer
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []

            yield _sse(
                "done",
                {
                    "answer": answer,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )
        except Exception as exc:
            fallback = deterministic_summarize(llm_lines) or f"llm call failed: {exc}"
            cache_state["answer"] = fallback
            cache_state["chunks"] = pipeline.chunks if req.return_chunks else []
            yield _sse("error", {"error": str(exc)})
            yield _sse("delta", {"text": fallback})
            yield _sse(
                "done",
                {
                    "answer": fallback,
                    "chunks": pipeline.chunks if req.return_chunks else None,
                    "retrieval": pipeline.retrieval,
                    "cache": pipeline.cache,
                    "cache_hit": pipeline.cache_hit,
                    "cache_score": pipeline.cache_score,
                    "retrieval_mode": pipeline.retrieval_mode,
                    "hybrid_capable": pipeline.hybrid_capable,
                },
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(finalize_cache),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    json_log(
        "warning",
        "request.validation_failed",
        "request validation failed",
        error=str(exc),
        endpoint=getattr(request.url, "path", str(request.url)),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    endpoint = getattr(request.url, "path", str(request.url))
    span = get_current_span()
    if span is not None:
        try:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
        except Exception:
            pass
    json_log(
        "error",
        "unhandled_exception",
        "unhandled exception",
        endpoint=endpoint,
        error=str(exc),
        stack=safe_stack(exc),
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.get(AUTH_LOGIN_PATH, name="auth_login")
async def auth_login(request: Request, next: str = _DEFAULT_NEXT_PATH):
    next_path = _clean_next_path(next)
    state_value = secrets.token_urlsafe(32)
    nonce_value = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()

    flow_cookie = _flow_sign(
        {
            "state": state_value,
            "nonce": nonce_value,
            "verifier": verifier,
            "next": next_path,
            "iat": int(time.time()),
            "exp": int(time.time()) + _FLOW_COOKIE_TTL_SECONDS,
        }
    )

    response = RedirectResponse(_build_authorize_url(state_value, nonce_value, challenge), status_code=302)
    response.set_cookie(
        key=_FLOW_COOKIE_NAME,
        value=flow_cookie,
        max_age=_FLOW_COOKIE_TTL_SECONDS,
        expires=_FLOW_COOKIE_TTL_SECONDS,
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=SESSION_COOKIE_SAMESITE,
        path="/",
    )
    return response


@app.get(AUTH_CALLBACK_PATH, name="auth_callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail=f"{error}: {error_description or 'authorization failed'}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    raw_flow = request.cookies.get(_FLOW_COOKIE_NAME)
    if not raw_flow:
        raise HTTPException(status_code=400, detail="missing auth flow")
    flow = _flow_load(raw_flow)

    if flow.get("state") != state:
        raise HTTPException(status_code=400, detail="state mismatch")

    token_data = await _exchange_code_for_tokens(code, str(flow.get("verifier") or ""))
    id_token = token_data.get("id_token")
    access_token = token_data.get("access_token")
    if not id_token or not access_token:
        raise HTTPException(status_code=400, detail="token response missing id_token or access_token")

    id_claims = _verify_jwt(str(id_token))
    if id_claims.get("nonce") != flow.get("nonce"):
        raise HTTPException(status_code=400, detail="nonce mismatch")

    userinfo: dict[str, Any] = {}
    try:
        userinfo = await _fetch_userinfo(str(access_token))
    except Exception:
        userinfo = {}

    merged_claims = dict(id_claims)
    merged_claims.update({k: v for k, v in userinfo.items() if v is not None})

    principal = _claims_to_principal(merged_claims)
    next_path = _clean_next_path(str(flow.get("next") or _DEFAULT_NEXT_PATH))

    response = RedirectResponse(next_path, status_code=303)
    _set_session_cookie(response, principal)
    response.delete_cookie(key=_FLOW_COOKIE_NAME, path="/")
    return response


@app.get(AUTH_LOGOUT_PATH, name="auth_logout")
async def auth_logout(request: Request):
    response = RedirectResponse(_DEFAULT_NEXT_PATH, status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=_FLOW_COOKIE_NAME, path="/")
    return response


@app.post("/generate/stream")
@limiter.limit(DEFAULT_USER_RATE_LIMIT)
async def api_stream(request: Request):
    result = await _generate_stream_core(request)
    return result


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    state = _state()
    ready = bool(state.health.get("ready", False))
    return {
        "status": "ready" if ready else "not_ready",
        "service_ready": ready,
        "qdrant": bool(state.health.get("qdrant", False)),
        "docs_collection_ready": bool(state.health.get("docs_collection_ready", False)),
        "cache_collection_ready": bool(state.health.get("cache_collection_ready", False)),
        "dense": bool(state.health.get("dense", False)),
        "sparse": bool(state.health.get("sparse", False)),
        "reranker": bool(state.health.get("reranker", False)),
        "bedrock": bool(state.health.get("bedrock", False)),
        "hybrid_capable": bool(state.health.get("hybrid_capable", False)),
        "bootstrap_error": startup_bootstrap_error,
    }


if __name__ == "__main__":
    try:
        setup_logging(LOG_LEVEL)
    except Exception:
        logging.getLogger("retrieval").exception("failed to apply telemetry logging before uvicorn.run")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        log_level="info",
        loop=os.getenv("UVICORN_LOOP", "uvloop"),
        http=os.getenv("UVICORN_HTTP", "httptools"),
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
        access_log=False,
    )