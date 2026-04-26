#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, conint

SERVICE_NAME = os.getenv("SERVICE_NAME", "retrieval").strip() or "retrieval"
ENV = os.getenv("ENV", "STAGING").strip().upper() or "STAGING"

AWS_REGION = (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1").strip()
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "default_rag_collection1").strip()

DENSE_URL = os.getenv("DENSE_URL", "http://dense-svc.models.svc.cluster.local:8200").strip()
SPARSE_URL = os.getenv("SPARSE_URL", "http://sparse-svc.models.svc.cluster.local:8201").strip()
RERANKER_URL = os.getenv("RERANKER_URL", "http://reranker-svc.models.svc.cluster.local:8202").strip()

BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID") or os.getenv("AWS_BEDROCK_MODEL_ID") or "meta.llama3-8b-instruct-v1:0"

ANSWER_PROMPT_TEMPLATE = os.getenv(
    "LLM_PROMPT_TEMPLATE",
    (
        "You are a knowledge assistant who must answer unambigiously referring ONLY to the provided passages below."
        "Each factual sentence MUST end with a citation in the exact format [n], where n is one of the numbered passage blocks. "
        "Use ONLY the provided passage numbers. Do NOT output filenames, URLs, page numbers, or any other metadata. Do NOT invent citations."
        "PASSAGES:\n{passages}\n\n"
        "QUESTION: {question}\n\n"
        "Answer:"
    ),
)

LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "512"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))

BEDROCK_GUARDRAIL_IDENTIFIER = os.getenv("BEDROCK_GUARDRAIL_IDENTIFIER", "").strip()
BEDROCK_GUARDRAIL_VERSION = os.getenv("BEDROCK_GUARDRAIL_VERSION", "").strip()

CORPUS_VERSION = os.getenv("CORPUS_VERSION", "v1")
PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v1")
RETRIEVAL_VERSION = os.getenv("RETRIEVAL_VERSION", "retrieval-v1")
TENANT_ID = os.getenv("TENANT_ID", "").strip() or None

DENSE_DIM = int(os.getenv("DENSE_DIM", "384"))
MAX_CHUNKS_TO_LLM = int(os.getenv("MAX_CHUNKS_TO_LLM", "6"))
QUERY_TOPK_DENSE = int(os.getenv("QUERY_TOPK_DENSE", "50"))
QUERY_TOPK_SPARSE = int(os.getenv("QUERY_TOPK_SPARSE", "50"))
FETCH_K = int(os.getenv("FETCH_K", "20"))
RERANKER_TOP_K = int(os.getenv("RERANK_TOPK", "15"))
RERANKER_MODE = os.getenv("RERANKER_MODE", "AUTO").upper()
RERANK_AUTO_THRESHOLD = float(os.getenv("RERANK_AUTO_THRESHOLD", "0.75"))
RERANK_MARGIN = float(os.getenv("RERANK_MARGIN", "0.08"))
RERANK_ALPHA = float(os.getenv("RERANK_ALPHA", "0.6"))
RRF_K = int(os.getenv("RRF_K", "60"))
CACHE_SCORE_THRESHOLD = float(os.getenv("CACHE_SCORE_THRESHOLD", "0.72"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_CLEANUP_INTERVAL_SECONDS = int(os.getenv("CACHE_CLEANUP_INTERVAL_SECONDS", "900"))
PROMPT_MAX_CONTENT_CHARS = int(os.getenv("PROMPT_MAX_CONTENT_CHARS", "2500"))
CHUNK_OUTPUT_MAX_CHARS = int(os.getenv("CHUNK_OUTPUT_MAX_CHARS", "1600"))
MAX_PROMPT_CHARS = int(os.getenv("MAX_PROMPT_CHARS", "40000"))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "64"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10.0"))
HTTP_MAX_CONNECTIONS = int(os.getenv("HTTP_MAX_CONNECTIONS", "100"))
HTTP_MAX_KEEPALIVE = int(os.getenv("HTTP_MAX_KEEPALIVE", "20"))
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "0.08"))
RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "0.8"))
BREAKER_FAILURE_THRESHOLD = int(os.getenv("BREAKER_FAILURE_THRESHOLD", "3"))
BREAKER_RESET_TIMEOUT = float(os.getenv("BREAKER_RESET_TIMEOUT", "20.0"))


@dataclass(frozen=True)
class RuntimeSettings:
    corpus_version: str = CORPUS_VERSION
    prompt_version: str = PROMPT_VERSION
    retrieval_version: str = RETRIEVAL_VERSION
    llm_model: str = BEDROCK_MODEL_ID
    cache_ttl_seconds: int = CACHE_TTL_SECONDS
    cache_score_threshold: float = CACHE_SCORE_THRESHOLD
    max_chunks_to_llm: int = MAX_CHUNKS_TO_LLM
    reranker_model: str = os.getenv("RERANKER_MODEL", "cross-encoder")


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
    "COLLECTION_NAME",
    "CORPUS_VERSION",
    "DENSE_DIM",
    "DENSE_URL",
    "ENV",
    "FETCH_K",
    "HTTP_MAX_CONNECTIONS",
    "HTTP_MAX_KEEPALIVE",
    "HTTP_TIMEOUT",
    "LLM_MAX_TOKENS",
    "LLM_TEMPERATURE",
    "MAX_CHUNKS_TO_LLM",
    "MAX_CONCURRENT_REQUESTS",
    "MAX_PROMPT_CHARS",
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
    "SPARSE_URL",
    "TENANT_ID",
    "GenerateRequest",
    "GenerateResponse",
    "RetrieveRequest",
    "RetrieveResponse",
    "RuntimeSettings",
    "make_settings",
]
