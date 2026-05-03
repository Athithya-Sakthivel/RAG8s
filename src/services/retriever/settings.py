from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, conint


def _env_str(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip()
    return text if text else default


def _env_first(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = _env_str(name, None)
        if value is not None:
            return value
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int_any(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            return int(raw)
        except Exception:
            continue
    return default


def _parse_duration_seconds(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None

    multiplier = 1.0
    for suffix, factor in (("ms", 0.001), ("s", 1.0), ("m", 60.0)):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            multiplier = factor
            break

    try:
        return float(text) * multiplier
    except Exception:
        return None


def _env_otlp_timeout_seconds(default: float = 5.0) -> float:
    for name in (
        "OTEL_TIMEOUT_SECONDS",
        "OTEL_EXPORTER_OTLP_TIMEOUT",
        "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
        "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
        "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
    ):
        raw = os.getenv(name)
        value = _parse_duration_seconds(raw)
        if value is not None and value > 0:
            return value
    return default


def _normalize_sampler_name(raw: str | None) -> str:
    value = (raw or "parentbased_traceidratio").strip().lower()
    return {
        "always_on": "always_on",
        "always_off": "always_off",
        "traceidratio": "traceidratio",
        "parentbased_always_on": "parentbased_always_on",
        "parentbased_always_off": "parentbased_always_off",
        "parentbased_traceidratio": "parentbased_traceidratio",
    }.get(value, "parentbased_traceidratio")


def _sampler_ratio(sampler_name: str, raw_value: str | None) -> float:
    default = 0.1
    if sampler_name in {"always_on", "always_off"}:
        return default
    if raw_value is None or not str(raw_value).strip():
        return default
    try:
        value = float(raw_value)
    except Exception:
        return default
    return value if 0.0 <= value <= 1.0 else default


def _validate_log_level(raw: str | None) -> str:
    level = (raw or "WARNING").strip().upper()
    aliases = {"WARN": "WARNING", "EXCEPTION": "ERROR"}
    level = aliases.get(level, level)
    return level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "WARNING"


def _split_csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    text = (raw or "").strip()
    if not text:
        return default
    values = tuple(part.strip() for part in text.split(",") if part.strip())
    return values or default


def _clean_url(value: str | None, default: str) -> str:
    text = (value or default).strip()
    if not text:
        text = default
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid url: {text!r}")
    return text.rstrip("/")


SERVICE_NAME = _env_first("SERVICE_NAME", "OTEL_SERVICE_NAME", default="retrieval") or "retrieval"
SERVICE_VERSION = _env_first("SERVICE_VERSION", "OTEL_SERVICE_VERSION", default="unknown") or "unknown"
ENV = (_env_first("ENV", "DEPLOYMENT_ENVIRONMENT", default="PROD") or "PROD").strip().upper() or "PROD"
DEPLOYMENT_ENVIRONMENT = (_env_str("DEPLOYMENT_ENVIRONMENT", ENV) or ENV).strip().upper() or ENV

CLUSTER_NAME = _env_first("K8S_CLUSTER_NAME", "CLUSTER_NAME", default="") or ""
INSTANCE_ID = _env_first("SERVICE_INSTANCE_ID", "INSTANCE_ID", "HOSTNAME", default="") or ""

AWS_REGION = (_env_str("AWS_REGION", None) or _env_str("AWS_DEFAULT_REGION", None) or "ap-south-1").strip()
QDRANT_URL = (_env_str("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333") or "").strip()
QDRANT_API_KEY = _env_str("QDRANT_API_KEY", "") or None
COLLECTION_NAME = (_env_str("COLLECTION_NAME", "default_rag_collection1") or "").strip()

DENSE_URL = (_env_str("DENSE_URL", "http://dense-svc.inference.svc.cluster.local:8200") or "").strip()
SPARSE_URL = (_env_str("SPARSE_URL", "http://sparse-svc.inference.svc.cluster.local:8201") or "").strip()
RERANKER_URL = (_env_str("RERANKER_URL", "http://reranker-svc.inference.svc.cluster.local:8202") or "").strip()

BEDROCK_MODEL_ID = (
    _env_str("BEDROCK_MODEL_ID", None)
    or _env_str("AWS_BEDROCK_MODEL_ID", None)
    or "meta.llama3-8b-instruct-v1:0"
)

ANSWER_PROMPT_TEMPLATE = _env_str(
    "LLM_PROMPT_TEMPLATE",
    (
        "You are a knowledge assistant who must explain explicitly to an end-user by referring ONLY to the provided passages below.\n"
        "You MUST end every passage with a citation in the exact format [n], where n is one of the numbered passage blocks.\n"
        "Use ONLY the provided passage numbers. Do NOT output filenames, secrets, URLs, page numbers, or any other metadata.\n"
        "Do NOT invent citations.\n"
        "PASSAGES:\n{passages}\n\n"
        "QUESTION: {question}\n\n"
        "Answer:"
    ),
)

LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 400)
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.0)

BEDROCK_GUARDRAIL_IDENTIFIER = (_env_str("BEDROCK_GUARDRAIL_IDENTIFIER", "") or "").strip()
BEDROCK_GUARDRAIL_VERSION = (_env_str("BEDROCK_GUARDRAIL_VERSION", "") or "").strip()

CORPUS_VERSION = _env_str("CORPUS_VERSION", "v1") or "v1"
PROMPT_VERSION = _env_str("PROMPT_VERSION", "v1") or "v1"
RETRIEVAL_VERSION = _env_str("RETRIEVAL_VERSION", "retrieval-v1") or "retrieval-v1"
TENANT_ID = _env_str("TENANT_ID", "") or None

DENSE_DIM = _env_int("DENSE_DIM", 384)
MAX_CHUNKS_TO_LLM = _env_int("MAX_CHUNKS_TO_LLM", 5)
QUERY_TOPK_DENSE = _env_int("QUERY_TOPK_DENSE", 50)
QUERY_TOPK_SPARSE = _env_int("QUERY_TOPK_SPARSE", 50)
FETCH_K = _env_int("FETCH_K", 20)
RERANKER_TOP_K = _env_int("RERANK_TOPK", 10)
RERANKER_MODE = (_env_str("RERANKER_MODE", "AUTO") or "AUTO").upper()
RERANK_AUTO_THRESHOLD = _env_float("RERANK_AUTO_THRESHOLD", 0.75)
RERANK_MARGIN = _env_float("RERANK_MARGIN", 0.08)
RERANK_ALPHA = _env_float("RERANK_ALPHA", 0.6)
RRF_K = _env_int("RRF_K", 60)
CACHE_SCORE_THRESHOLD = _env_float("CACHE_SCORE_THRESHOLD", 0.72)
CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 86400)
CACHE_CLEANUP_INTERVAL_SECONDS = _env_int("CACHE_CLEANUP_INTERVAL_SECONDS", 900)
PROMPT_MAX_CONTENT_CHARS = _env_int("PROMPT_MAX_CONTENT_CHARS", 2500)
CHUNK_OUTPUT_MAX_CHARS = _env_int("CHUNK_OUTPUT_MAX_CHARS", 1600)
MAX_PROMPT_CHARS = _env_int("MAX_PROMPT_CHARS", 40000)
MAX_CONCURRENT_REQUESTS = _env_int("MAX_CONCURRENT_REQUESTS", 64)
HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 10.0)
HTTP_MAX_CONNECTIONS = _env_int("HTTP_MAX_CONNECTIONS", 100)
HTTP_MAX_KEEPALIVE = _env_int("HTTP_MAX_KEEPALIVE", 20)
RETRY_MAX_ATTEMPTS = _env_int("RETRY_MAX_ATTEMPTS", 3)
RETRY_BASE_DELAY = _env_float("RETRY_BASE_DELAY", 0.08)
RETRY_MAX_DELAY = _env_float("RETRY_MAX_DELAY", 0.8)
BREAKER_FAILURE_THRESHOLD = _env_int("BREAKER_FAILURE_THRESHOLD", 3)
BREAKER_RESET_TIMEOUT = _env_float("BREAKER_RESET_TIMEOUT", 20.0)

AUTH_ISSUER_URL = _clean_url(
    _env_first("AUTH_ISSUER_URL", "ZITADEL_ISSUER", default="https://auth.athithya.site"),
    "https://auth.athithya.site",
)
AUTH_DISCOVERY_URL = _clean_url(
    _env_first(
        "AUTH_DISCOVERY_URL",
        "ZITADEL_DISCOVERY_URL",
        default=f"{AUTH_ISSUER_URL}/.well-known/openid-configuration",
    ),
    f"{AUTH_ISSUER_URL}/.well-known/openid-configuration",
)
AUTH_JWKS_URI = _clean_url(
    _env_first("AUTH_JWKS_URI", "ZITADEL_JWKS_URI", default=f"{AUTH_ISSUER_URL}/oauth/v2/keys"),
    f"{AUTH_ISSUER_URL}/oauth/v2/keys",
)
AUTH_AUTHORIZATION_ENDPOINT = _clean_url(
    _env_first(
        "AUTH_AUTHORIZATION_ENDPOINT",
        "ZITADEL_AUTHORIZATION_ENDPOINT",
        default=f"{AUTH_ISSUER_URL}/oauth/v2/authorize",
    ),
    f"{AUTH_ISSUER_URL}/oauth/v2/authorize",
)
AUTH_TOKEN_ENDPOINT = _clean_url(
    _env_first("AUTH_TOKEN_ENDPOINT", "ZITADEL_TOKEN_ENDPOINT", default=f"{AUTH_ISSUER_URL}/oauth/v2/token"),
    f"{AUTH_ISSUER_URL}/oauth/v2/token",
)
AUTH_USERINFO_ENDPOINT = _clean_url(
    _env_first("AUTH_USERINFO_ENDPOINT", "ZITADEL_USERINFO_ENDPOINT", default=f"{AUTH_ISSUER_URL}/oidc/v1/userinfo"),
    f"{AUTH_ISSUER_URL}/oidc/v1/userinfo",
)
AUTH_INTROSPECTION_ENDPOINT = _clean_url(
    _env_first(
        "AUTH_INTROSPECTION_ENDPOINT",
        "ZITADEL_INTROSPECTION_ENDPOINT",
        default=f"{AUTH_ISSUER_URL}/oauth/v2/introspect",
    ),
    f"{AUTH_ISSUER_URL}/oauth/v2/introspect",
)
AUTH_REVOCATION_ENDPOINT = _clean_url(
    _env_first("AUTH_REVOCATION_ENDPOINT", "ZITADEL_REVOCATION_ENDPOINT", default=f"{AUTH_ISSUER_URL}/oauth/v2/revoke"),
    f"{AUTH_ISSUER_URL}/oauth/v2/revoke",
)
AUTH_END_SESSION_ENDPOINT = _clean_url(
    _env_first("AUTH_END_SESSION_ENDPOINT", "ZITADEL_END_SESSION_ENDPOINT", default=f"{AUTH_ISSUER_URL}/oidc/v1/end_session"),
    f"{AUTH_ISSUER_URL}/oidc/v1/end_session",
)
AUTH_CLIENT_ID = _env_first("AUTH_CLIENT_ID", "ZITADEL_CLIENT_ID", default="") or ""
AUTH_CLIENT_SECRET = _env_first("AUTH_CLIENT_SECRET", "ZITADEL_CLIENT_SECRET", default="") or ""
AUTH_FLOW = (_env_str("AUTH_FLOW", "pkce") or "pkce").strip().lower()
AUTH_REDIRECT_URI = _clean_url(
    _env_first("AUTH_REDIRECT_URI", "ZITADEL_REDIRECT_URI", default="https://api.athithya.site/auth/callback"),
    "https://api.athithya.site/auth/callback",
)
AUTH_LOGOUT_REDIRECT_URI = _env_first("AUTH_LOGOUT_REDIRECT_URI", "ZITADEL_LOGOUT_REDIRECT_URI", default="") or None
AUTH_SCOPES = _split_csv(_env_str("AUTH_SCOPES", "openid,profile,email"), ("openid", "profile", "email"))
AUTH_ALLOWED_ALGORITHMS = _split_csv(_env_str("AUTH_ALLOWED_ALGORITHMS", "RS256,EdDSA"), ("RS256", "EdDSA"))
AUTH_USER_ID_CLAIM = _env_str("AUTH_USER_ID_CLAIM", "sub") or "sub"
ENABLE_AUTH = _env_bool("ENABLE_AUTH", True)
RATE_LIMIT_KEY_MODE = (_env_str("RATE_LIMIT_KEY_MODE", "sub") or "sub").strip().lower()

SESSION_COOKIE_NAME = _env_str("SESSION_COOKIE_NAME", "retriever_session") or "retriever_session"
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
SESSION_COOKIE_HTTPONLY = _env_bool("SESSION_COOKIE_HTTPONLY", True)
SESSION_COOKIE_SAMESITE = (_env_str("SESSION_COOKIE_SAMESITE", "Lax") or "Lax").strip().capitalize()
SESSION_TTL_SECONDS = _env_int("SESSION_TTL_SECONDS", 86400)
SESSION_SECRET = _env_str("SESSION_SECRET", "") or ""

AUTH_REQUIRED_PATHS = _split_csv(_env_str("AUTH_REQUIRED_PATHS", "/generate/stream"), ("/generate/stream",))
AUTH_EXEMPT_PATHS = _split_csv(
    _env_str("AUTH_EXEMPT_PATHS", "/healthz,/readyz,/auth/login,/auth/callback,/auth/logout"),
    ("/healthz", "/readyz", "/auth/login", "/auth/callback", "/auth/logout"),
)
AUTH_LOGIN_PATH = _env_str("AUTH_LOGIN_PATH", "/auth/login") or "/auth/login"
AUTH_CALLBACK_PATH = _env_str("AUTH_CALLBACK_PATH", "/auth/callback") or "/auth/callback"
AUTH_LOGOUT_PATH = _env_str("AUTH_LOGOUT_PATH", "/auth/logout") or "/auth/logout"

DEFAULT_ANON_RATE_LIMIT = _env_str("DEFAULT_ANON_RATE_LIMIT", "10/minute") or "10/minute"
DEFAULT_USER_RATE_LIMIT = _env_str("DEFAULT_USER_RATE_LIMIT", "60/minute") or "60/minute"

OTEL_EXPORTER_OTLP_ENDPOINT = (
    _env_first(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_ENDPOINT",
        default="http://signoz-otel-collector.signoz.svc.cluster.local:4317",
    )
    or "http://signoz-otel-collector.signoz.svc.cluster.local:4317"
).strip()
OTEL_EXPORTER_OTLP_PROTOCOL = (
    _env_first("OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_PROTOCOL", default="grpc") or "grpc"
).strip().lower()
if OTEL_EXPORTER_OTLP_PROTOCOL not in {"grpc", "http/protobuf"}:
    OTEL_EXPORTER_OTLP_PROTOCOL = "grpc"
OTEL_TIMEOUT_SECONDS = _env_otlp_timeout_seconds(5.0)
OTEL_METRIC_EXPORT_INTERVAL_MS = _env_int_any(("OTEL_METRIC_EXPORT_INTERVAL_MS",), 15000)
OTEL_METRIC_EXPORT_TIMEOUT_MS = _env_int_any(("OTEL_METRIC_EXPORT_TIMEOUT_MS",), 10000)
OTEL_TRACES_SAMPLER = _normalize_sampler_name(_env_str("OTEL_TRACES_SAMPLER", "parentbased_traceidratio"))
OTEL_TRACES_SAMPLER_ARG = _env_str("OTEL_TRACES_SAMPLER_ARG", "0.1") or "0.1"
TRACE_SAMPLE_RATIO = _sampler_ratio(OTEL_TRACES_SAMPLER, OTEL_TRACES_SAMPLER_ARG)
LOG_LEVEL = _validate_log_level(_env_str("LOG_LEVEL", "WARNING"))
ENABLE_OTEL_TRACES = _env_bool("ENABLE_OTEL_TRACES", True)
ENABLE_OTEL_METRICS = _env_bool("ENABLE_OTEL_METRICS", True)
ENABLE_OTEL_LOGS = _env_bool("ENABLE_OTEL_LOGS", True)

ZITADEL_ISSUER = AUTH_ISSUER_URL
ZITADEL_DISCOVERY_URL = AUTH_DISCOVERY_URL
ZITADEL_JWKS_URI = AUTH_JWKS_URI
ZITADEL_AUTHORIZATION_ENDPOINT = AUTH_AUTHORIZATION_ENDPOINT
ZITADEL_TOKEN_ENDPOINT = AUTH_TOKEN_ENDPOINT
ZITADEL_USERINFO_ENDPOINT = AUTH_USERINFO_ENDPOINT
ZITADEL_INTROSPECTION_ENDPOINT = AUTH_INTROSPECTION_ENDPOINT
ZITADEL_REVOCATION_ENDPOINT = AUTH_REVOCATION_ENDPOINT
ZITADEL_END_SESSION_ENDPOINT = AUTH_END_SESSION_ENDPOINT
ZITADEL_CLIENT_ID = AUTH_CLIENT_ID
ZITADEL_CLIENT_SECRET = AUTH_CLIENT_SECRET
ZITADEL_REDIRECT_URI = AUTH_REDIRECT_URI
ZITADEL_LOGOUT_REDIRECT_URI = AUTH_LOGOUT_REDIRECT_URI
ZITADEL_SCOPES = AUTH_SCOPES
ZITADEL_ALLOWED_ALGORITHMS = AUTH_ALLOWED_ALGORITHMS
ZITADEL_USER_ID_CLAIM = AUTH_USER_ID_CLAIM


@dataclass(frozen=True)
class RuntimeSettings:
    corpus_version: str = CORPUS_VERSION
    prompt_version: str = PROMPT_VERSION
    retrieval_version: str = RETRIEVAL_VERSION
    llm_model: str = BEDROCK_MODEL_ID
    cache_ttl_seconds: int = CACHE_TTL_SECONDS
    cache_score_threshold: float = CACHE_SCORE_THRESHOLD
    max_chunks_to_llm: int = MAX_CHUNKS_TO_LLM
    reranker_model: str = _env_str("RERANKER_MODEL", "cross-encoder") or "cross-encoder"


def make_settings() -> dict[str, Any]:
    settings = RuntimeSettings()
    return {
        "corpus_version": settings.corpus_version,
        "prompt_version": settings.prompt_version,
        "retrieval_version": settings.retrieval_version,
        "llm_model": settings.llm_model,
        "cache_ttl_seconds": settings.cache_ttl_seconds,
        "cache_score_threshold": settings.cache_score_threshold,
        "max_chunks_to_llm": settings.max_chunks_to_llm,
        "reranker_model": settings.reranker_model,
    }


def validate_runtime_contract() -> None:
    if not re.match(r"^https?://", AUTH_ISSUER_URL):
        raise ValueError("AUTH_ISSUER_URL must be a valid http(s) URL")
    if not re.match(r"^https?://", AUTH_REDIRECT_URI):
        raise ValueError("AUTH_REDIRECT_URI must be a valid http(s) URL")
    if AUTH_LOGOUT_REDIRECT_URI and not re.match(r"^https?://", AUTH_LOGOUT_REDIRECT_URI):
        raise ValueError("AUTH_LOGOUT_REDIRECT_URI must be a valid http(s) URL")
    if ENABLE_AUTH:
        required = {
            "AUTH_ISSUER_URL": AUTH_ISSUER_URL,
            "AUTH_CLIENT_ID": AUTH_CLIENT_ID,
            "AUTH_REDIRECT_URI": AUTH_REDIRECT_URI,
            "SESSION_SECRET": SESSION_SECRET,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"auth is enabled but missing required settings: {', '.join(missing)}")
        if AUTH_FLOW not in {"pkce", "confidential"}:
            raise ValueError("AUTH_FLOW must be pkce or confidential")
        if AUTH_FLOW == "confidential" and not AUTH_CLIENT_SECRET:
            raise ValueError("AUTH_CLIENT_SECRET is required when AUTH_FLOW=confidential")
        if RATE_LIMIT_KEY_MODE not in {"sub", "session", "ip"}:
            raise ValueError("RATE_LIMIT_KEY_MODE must be sub, session, or ip")
    if OTEL_EXPORTER_OTLP_PROTOCOL not in {"grpc", "http/protobuf"}:
        raise ValueError("OTEL_EXPORTER_OTLP_PROTOCOL must be grpc or http/protobuf")


class GenerateRequest(BaseModel):
    query: str = Field(..., min_length=1)
    tenant_id: str | None = None
    corpus_version: str | None = Field(default=CORPUS_VERSION)
    prompt_version: str | None = Field(default=PROMPT_VERSION)
    retrieval_version: str | None = Field(default=RETRIEVAL_VERSION)
    model_name: str | None = Field(default=BEDROCK_MODEL_ID)
    debug: bool | None = False
    enable_tracing: bool | None = False
    top_k: conint(ge=1, le=50) = 5
    fetch_k: conint(ge=1, le=200) = FETCH_K
    return_chunks: bool | None = True
    max_tokens: conint(ge=64, le=4096) | None = LLM_MAX_TOKENS
    allow_semantic_cache: bool | None = True


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    tenant_id: str | None = None
    corpus_version: str | None = Field(default=CORPUS_VERSION)
    retrieval_version: str | None = Field(default=RETRIEVAL_VERSION)
    top_k: conint(ge=1, le=50) = 5
    fetch_k: conint(ge=1, le=200) = FETCH_K
    rerank: bool | None = True
    include_cache: bool | None = False


class GenerateResponse(BaseModel):
    answer: str
    chunks: list[dict[str, Any]] | None = None
    retrieval: dict[str, Any]
    cache: dict[str, Any]
    cache_hit: bool = False
    cache_score: float | None = None
    retrieval_mode: str | None = None
    hybrid_capable: bool = False


class RetrieveResponse(BaseModel):
    query: str
    chunks: list[dict[str, Any]] | None = None
    retrieval: dict[str, Any]
    cache: dict[str, Any]
    cache_hit: bool = False
    cache_score: float | None = None
    retrieval_mode: str | None = None
    hybrid_capable: bool = False


__all__ = [
    "ANSWER_PROMPT_TEMPLATE",
    "AUTH_ALLOWED_ALGORITHMS",
    "AUTH_AUTHORIZATION_ENDPOINT",
    "AUTH_CALLBACK_PATH",
    "AUTH_CLIENT_ID",
    "AUTH_CLIENT_SECRET",
    "AUTH_DISCOVERY_URL",
    "AUTH_END_SESSION_ENDPOINT",
    "AUTH_EXEMPT_PATHS",
    "AUTH_FLOW",
    "AUTH_ISSUER_URL",
    "AUTH_JWKS_URI",
    "AUTH_LOGIN_PATH",
    "AUTH_LOGOUT_PATH",
    "AUTH_LOGOUT_REDIRECT_URI",
    "AUTH_REDIRECT_URI",
    "AUTH_REQUIRED_PATHS",
    "AUTH_REVOCATION_ENDPOINT",
    "AUTH_SCOPES",
    "AUTH_TOKEN_ENDPOINT",
    "AUTH_USERINFO_ENDPOINT",
    "AUTH_USER_ID_CLAIM",
    "AWS_REGION",
    "BEDROCK_GUARDRAIL_IDENTIFIER",
    "BEDROCK_GUARDRAIL_VERSION",
    "BEDROCK_MODEL_ID",
    "BREAKER_FAILURE_THRESHOLD",
    "BREAKER_RESET_TIMEOUT",
    "CACHE_CLEANUP_INTERVAL_SECONDS",
    "CACHE_SCORE_THRESHOLD",
    "CACHE_TTL_SECONDS",
    "CHUNK_OUTPUT_MAX_CHARS",
    "CLUSTER_NAME",
    "COLLECTION_NAME",
    "CORPUS_VERSION",
    "DEFAULT_ANON_RATE_LIMIT",
    "DEFAULT_USER_RATE_LIMIT",
    "DENSE_DIM",
    "DENSE_URL",
    "DEPLOYMENT_ENVIRONMENT",
    "ENABLE_AUTH",
    "ENABLE_OTEL_LOGS",
    "ENABLE_OTEL_METRICS",
    "ENABLE_OTEL_TRACES",
    "ENV",
    "FETCH_K",
    "HTTP_MAX_CONNECTIONS",
    "HTTP_MAX_KEEPALIVE",
    "HTTP_TIMEOUT",
    "INSTANCE_ID",
    "LLM_MAX_TOKENS",
    "LLM_TEMPERATURE",
    "LOG_LEVEL",
    "MAX_CHUNKS_TO_LLM",
    "MAX_CONCURRENT_REQUESTS",
    "MAX_PROMPT_CHARS",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_METRIC_EXPORT_INTERVAL_MS",
    "OTEL_METRIC_EXPORT_TIMEOUT_MS",
    "OTEL_TIMEOUT_SECONDS",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "PROMPT_MAX_CONTENT_CHARS",
    "PROMPT_VERSION",
    "QDRANT_API_KEY",
    "QDRANT_URL",
    "QUERY_TOPK_DENSE",
    "QUERY_TOPK_SPARSE",
    "RATE_LIMIT_KEY_MODE",
    "RERANKER_MODE",
    "RERANKER_TOP_K",
    "RERANKER_URL",
    "RERANK_ALPHA",
    "RERANK_AUTO_THRESHOLD",
    "RERANK_MARGIN",
    "RETRIEVAL_VERSION",
    "RETRY_BASE_DELAY",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_MAX_DELAY",
    "RRF_K",
    "SERVICE_NAME",
    "SERVICE_VERSION",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_NAME",
    "SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_SECURE",
    "SESSION_SECRET",
    "SESSION_TTL_SECONDS",
    "SPARSE_URL",
    "TENANT_ID",
    "TRACE_SAMPLE_RATIO",
    "ZITADEL_ALLOWED_ALGORITHMS",
    "ZITADEL_AUTHORIZATION_ENDPOINT",
    "ZITADEL_CLIENT_ID",
    "ZITADEL_CLIENT_SECRET",
    "ZITADEL_DISCOVERY_URL",
    "ZITADEL_END_SESSION_ENDPOINT",
    "ZITADEL_INTROSPECTION_ENDPOINT",
    "ZITADEL_ISSUER",
    "ZITADEL_JWKS_URI",
    "ZITADEL_LOGOUT_REDIRECT_URI",
    "ZITADEL_REDIRECT_URI",
    "ZITADEL_REVOCATION_ENDPOINT",
    "ZITADEL_SCOPES",
    "ZITADEL_TOKEN_ENDPOINT",
    "ZITADEL_USERINFO_ENDPOINT",
    "ZITADEL_USER_ID_CLAIM",
    "GenerateRequest",
    "GenerateResponse",
    "RetrieveRequest",
    "RetrieveResponse",
    "RuntimeSettings",
    "make_settings",
    "validate_runtime_contract",
]