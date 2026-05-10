from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

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


def _validate_log_level(raw: str | None) -> str:
    level = (raw or "WARNING").strip().upper()
    aliases = {"WARN": "WARNING", "EXCEPTION": "ERROR"}
    level = aliases.get(level, level)
    return level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "WARNING"


# ---------------------------------------------------------------------------
# Service identity
# ---------------------------------------------------------------------------
SERVICE_NAME = _env_first("SERVICE_NAME", default="retriever") or "retriever"
SERVICE_VERSION = _env_first("SERVICE_VERSION", default="unknown") or "unknown"
ENV = (_env_first("ENV", "DEPLOYMENT_ENVIRONMENT", default="PROD") or "PROD").strip().upper() or "PROD"
DEPLOYMENT_ENVIRONMENT = (_env_str("DEPLOYMENT_ENVIRONMENT", ENV) or ENV).strip().upper() or ENV
CLUSTER_NAME = _env_first("K8S_CLUSTER_NAME", "CLUSTER_NAME", default="") or ""
INSTANCE_ID = _env_first("SERVICE_INSTANCE_ID", "INSTANCE_ID", "HOSTNAME", default="") or ""

# ---------------------------------------------------------------------------
# AWS / Bedrock
# ---------------------------------------------------------------------------
AWS_REGION = (_env_str("AWS_REGION", None) or _env_str("AWS_DEFAULT_REGION", None) or "ap-south-1").strip()
BEDROCK_MODEL_ID = (
    _env_str("BEDROCK_MODEL_ID", None)
    or _env_str("AWS_BEDROCK_MODEL_ID", None)
    or "meta.llama3-8b-instruct-v1:0"
)

# ---------------------------------------------------------------------------
# Vector store (Qdrant)
# ---------------------------------------------------------------------------
QDRANT_URL = (_env_str("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333") or "").strip()
QDRANT_API_KEY = _env_str("QDRANT_API_KEY", "") or None
COLLECTION_NAME = (_env_str("COLLECTION_NAME", "default_rag_collection1") or "").strip()

# ---------------------------------------------------------------------------
# Inference services – defaults follow standard Kubernetes DNS conventions.
# Override only if your service names or namespace differ.
# ---------------------------------------------------------------------------
DENSE_URL = (_env_str("DENSE_URL", "http://dense-svc.inference.svc.cluster.local:8200") or "").strip()
SPARSE_URL = (_env_str("SPARSE_URL", "http://sparse-svc.inference.svc.cluster.local:8201") or "").strip()
RERANKER_URL = (_env_str("RERANKER_URL", "http://reranker-svc.inference.svc.cluster.local:8202") or "").strip()

# ---------------------------------------------------------------------------
# LLM / prompt settings
# ---------------------------------------------------------------------------
ANSWER_PROMPT_TEMPLATE = _env_str(
    "LLM_PROMPT_TEMPLATE",
    (
        "You are a knowledge assistant who must explain concretely to an end-user by referring ONLY to the provided passages below.\n"
        "You MUST end every passage with a citation in the exact format [n], where n is one of the numbered passage blocks.\n"
        "Use ONLY the provided passage numbers. Do NOT output filenames, secrets, URLs, page numbers, or any other metadata.\n"
        "Do NOT invent citations.\n"
        "PASSAGES:\n{passages}\n\n"
        "QUESTION: {question}\n\n"
        "Answer:"
    ),
)

# ---------------------------------------------------------------------------
# Presigned URL generation (for citation source retrieval)
# ---------------------------------------------------------------------------
ENABLE_PRESIGNED_URLS = _env_bool("ENABLE_PRESIGNED_URLS", True)
PRESIGNED_URL_TTL_SECONDS = _env_int("PRESIGNED_URL_TTL_SECONDS", 1800)

LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 250)
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.0)

BEDROCK_GUARDRAIL_IDENTIFIER = (_env_str("BEDROCK_GUARDRAIL_IDENTIFIER", "") or "").strip()
BEDROCK_GUARDRAIL_VERSION = (_env_str("BEDROCK_GUARDRAIL_VERSION", "") or "").strip()

# ---------------------------------------------------------------------------
# Retrieval versions
# ---------------------------------------------------------------------------
CORPUS_VERSION = _env_str("CORPUS_VERSION", "v1") or "v1"
PROMPT_VERSION = _env_str("PROMPT_VERSION", "v1") or "v1"
RETRIEVAL_VERSION = _env_str("RETRIEVAL_VERSION", "retrieval-v1") or "retrieval-v1"
TENANT_ID = _env_str("TENANT_ID", "") or None

# ---------------------------------------------------------------------------
# Retrieval parameters
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# HTTP & client tuning
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 10.0)
HTTP_MAX_CONNECTIONS = _env_int("HTTP_MAX_CONNECTIONS", 100)
HTTP_MAX_KEEPALIVE = _env_int("HTTP_MAX_KEEPALIVE", 20)
RETRY_MAX_ATTEMPTS = _env_int("RETRY_MAX_ATTEMPTS", 3)
RETRY_BASE_DELAY = _env_float("RETRY_BASE_DELAY", 0.08)
RETRY_MAX_DELAY = _env_float("RETRY_MAX_DELAY", 0.8)
BREAKER_FAILURE_THRESHOLD = _env_int("BREAKER_FAILURE_THRESHOLD", 3)
BREAKER_RESET_TIMEOUT = _env_float("BREAKER_RESET_TIMEOUT", 20.0)

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
SHUTDOWN_TIMEOUT = _env_int("SHUTDOWN_TIMEOUT", 30)

# ---------------------------------------------------------------------------
# Prometheus – served on the main HTTP port, no separate port needed.
# PROMETHEUS_PORT is kept for environments that require a dedicated port.
# ---------------------------------------------------------------------------
ENABLE_PROMETHEUS = _env_bool("ENABLE_PROMETHEUS", True)
PROMETHEUS_PATH = _env_str("PROMETHEUS_PATH", "/metrics")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = _validate_log_level(_env_str("LOG_LEVEL", "WARNING"))


# ---------------------------------------------------------------------------
# Runtime settings dataclass
# ---------------------------------------------------------------------------
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
    s = RuntimeSettings()
    return {
        "corpus_version": s.corpus_version,
        "prompt_version": s.prompt_version,
        "retrieval_version": s.retrieval_version,
        "llm_model": s.llm_model,
        "cache_ttl_seconds": s.cache_ttl_seconds,
        "cache_score_threshold": s.cache_score_threshold,
        "max_chunks_to_llm": s.max_chunks_to_llm,
        "reranker_model": s.reranker_model,
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
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
    "DENSE_DIM",
    "DENSE_URL",
    "DEPLOYMENT_ENVIRONMENT",
    "ENABLE_PROMETHEUS",
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
    "PROMETHEUS_PATH",
    "PROMPT_MAX_CONTENT_CHARS",
    "PROMPT_VERSION",
    "QDRANT_API_KEY",
    "QDRANT_URL",
    "QUERY_TOPK_DENSE",
    "QUERY_TOPK_SPARSE",
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
    "SHUTDOWN_TIMEOUT",
    "SPARSE_URL",
    "TENANT_ID",
    "GenerateRequest",
    "GenerateResponse",
    "RetrieveRequest",
    "RetrieveResponse",
    "RuntimeSettings",
    "make_settings",
]