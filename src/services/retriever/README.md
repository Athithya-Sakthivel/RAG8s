# Retriever Service

Streaming RAG backend that retrieves documents, reranks them, generates answers via AWS Bedrock, and provides presigned S3 URLs for source retrieval.

---

## Quick Reference

### Request Flow

```
User → /generate/stream → Cache Check → Embed → Retrieve → Rerank → LLM → Stream SSE
                                ↓                                    ↓
                           Cache Hit? ←────────────────────────── Write Cache

User → /presign → Parse S3 Path → Generate Presigned URL → Return URL
```

### Service Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/generate/stream` | RAG answer generation (SSE stream with citations) |
| `POST` | `/presign` | Generate read-only presigned S3 URL for source documents |
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/readyz` | Readiness probe |
| `GET` | `/metrics` | Prometheus metrics |

### Dependencies

| Service | Default URL | Purpose |
|---------|-------------|---------|
| Qdrant | `http://qdrant.qdrant.svc.cluster.local:6333` | Vector and sparse retrieval |
| Dense Embedder | `http://dense-svc.inference.svc.cluster.local:8200` | Dense embeddings |
| Sparse Embedder | `http://sparse-svc.inference.svc.cluster.local:8201` | Sparse embeddings |
| Reranker | `http://reranker-svc.inference.svc.cluster.local:8202` | Cross-encoder reranking |
| AWS Bedrock | External | LLM generation |
| AWS S3 | External | Presigned URL generation |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        RETRIEVER SERVICE                         │
│                                                                  │
│  ┌──────────────────────┐      ┌───────────────────────────────┐ │
│  │     HTTP Layer        │      │     Retrieval Pipeline         │ │
│  │                       │      │                               │ │
│  │  • Request validation │      │  1. Cache Lookup               │ │
│  │  • Request ID         │ ───► │  2. Embed (dense + sparse)    │ │
│  │  • Rate limiting (IP) │      │  3. Qdrant Search (hybrid)    │ │
│  │  • HTTP metrics       │      │  4. Rerank (auto/always)      │ │
│  │  • SSE streaming      │      │  5. LLM Generation (Bedrock)  │ │
│  │  • Presign proxy      │      │  6. Citation validation       │ │
│  │  • Health probes      │      │  7. Cache writeback           │ │
│  └──────────────────────┘      └───────────────────────────────┘ │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │                    Background Loops                           ││
│  │  • Health loop (every 10s) → checks all dependencies         ││
│  │  • Cache cleanup loop (every 900s) → removes expired entries ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

---

## Request/Response Specification

### `POST /generate/stream`

#### Request Body

```json
{
  "query": "how does governance differ from guardrails?",
  "top_k": 5,
  "fetch_k": 20,
  "return_chunks": true,
  "allow_semantic_cache": true,
  "max_tokens": 400
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | required | User question |
| `top_k` | int (1-50) | `5` | Final results to return |
| `fetch_k` | int (1-200) | `20` | Candidates to fetch per index |
| `return_chunks` | bool | `true` | Include document metadata with citations |
| `allow_semantic_cache` | bool | `true` | Enable cache lookup |
| `max_tokens` | int (64-4096) | `400` | Max LLM output tokens |

#### SSE Events

| Event | When | Payload |
|-------|------|---------|
| `start` | Pipeline begins | Query, retrieval config, cache info, chunks (if return_chunks) |
| `delta` | Token generated | `{"text": "token"}` |
| `done` | Stream complete | Full answer with citations, chunks, retrieval metadata |
| `error` | Error occurred | `{"error": "message"}` (followed by fallback answer via delta+done) |

#### Example Response (done event)

```json
{
  "event": "done",
  "data": {
    "answer": "Governance defines the rules and policies [1], while guardrails enforce specific constraints [2].",
    "chunks": [
      {
        "index": 1,
        "chunk_id": "abc123",
        "source_url": "s3://bucket/path/to/doc.pdf",
        "meta_items": [
          {"k": "source_url", "v": "s3://bucket/path/to/doc.pdf"},
          {"k": "file_name", "v": "doc.pdf"},
          {"k": "page_number", "v": 3},
          {"k": "line_range", "v": [10, 25]}
        ]
      }
    ],
    "retrieval": {
      "mode": "hybrid",
      "candidates": {"dense": 50, "sparse": 50, "fused": 35},
      "rerank": {"enabled": true, "applied": true, "count": 10}
    },
    "cache_hit": false,
    "retrieval_mode": "hybrid"
  }
}
```

### `POST /presign`

#### Request Body

```json
{
  "s3_path": "s3://bucket/path/to/doc.pdf"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `s3_path` | string | Full S3 path (must start with `s3://`) |
| `path` | string | Alias for `s3_path` |

#### Response (200)

```json
{
  "url": "https://s3.ap-south-1.amazonaws.com/bucket/path/to/doc.pdf?X-Amz-Algorithm=...",
  "expires_in": 3600
}
```

#### Error Responses

| Status | Condition |
|--------|-----------|
| `400` | Missing or invalid `s3_path` |
| `403` | `ENABLE_PRESIGNED_URLS=false` |
| `500` | S3 presigning failed |

---

## Retrieval Pipeline (Step by Step)

```
                    ┌──────────────┐
                    │  User Query  │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ Cache Lookup │
                    │  (Exact ID)  │
                    └──┬────────┬──┘
               Hit │         │ Miss
                   │         │
      ┌────────────▼─┐   ┌──▼──────────┐
      │ Return Cached │   │ Embed Query │
      │   Answer      │   │ (Dense +    │
      └───────────────┘   │  Sparse)    │
                          └──┬──────────┘
                             │
                    ┌────────▼─────────┐
                    │ Semantic Cache   │
                    │ Lookup (Cosine)  │
                    └──┬──────────┬────┘
               Hit │         │ Miss
                   │         │
      ┌────────────▼─┐   ┌──▼──────────────┐
      │ Return +      │   │ Qdrant Search   │
      │ Promote Exact │   │ (Dense/Sparse/  │
      │ Cache Entry   │   │  Hybrid)        │
      └───────────────┘   └──┬──────────────┘
                             │
                    ┌────────▼─────────┐
                    │ RRF Fusion       │
                    │ (Dense + Sparse  │
                    │  → Fused List)   │
                    └──┬───────────────┘
                       │
              ┌────────▼──────────┐
              │ Rerank Decision   │
              │ (Auto/Always/     │
              │  Disable)         │
              └──┬────────────┬───┘
         Rerank │            │ Skip
                │            │
     ┌──────────▼──┐    ┌───▼──────────┐
     │ Cross-Encoder│    │ Keep Fused   │
     │ Reranking    │    │ Scores       │
     └──────────┬───┘    └───┬──────────┘
                │            │
           ┌────▼────────────▼────┐
           │ Select Top K Results │
           └──────────┬───────────┘
                      │
           ┌──────────▼───────────┐
           │ Build Prompt +       │
           │ Call Bedrock LLM     │
           └──────────┬───────────┘
                      │
           ┌──────────▼───────────┐
           │ Validate Citations   │
           │ + Write Cache Entry  │
           └──────────┬───────────┘
                      │
                ┌─────▼─────┐
                │ SSE Stream │
                └───────────┘
```

---

## Citations

### Prompt Format

The LLM prompt uses numbered passage blocks:

```
[1]
Heading: Governance Overview
Content: Governance frameworks define organizational policies...

[2]
Heading: Guardrail Mechanisms
Content: Guardrails are specific constraints that enforce governance...

Q: how does governance differ from guardrails?
A:
```

### Citation Validation

After LLM generation, citations are validated against actual chunk indexes:

| Before | After | Reason |
|--------|-------|--------|
| `Governance sets rules [1] and guardrails enforce them [2].` | `Governance sets rules [1] and guardrails enforce them [2].` | Both valid |
| `The system uses RAG [1] as described in [99].` | `The system uses RAG [1] as described in .` | `[99]` hallucinated |
| `See [source_url] for details.` | `See  for details.` | Metadata citation stripped |

---

## Presigned URLs

### Flow

```
1. Chunk returned by /generate/stream contains source_url: "s3://bucket/doc.pdf"
2. Frontend sends POST /presign with {"s3_path": "s3://bucket/doc.pdf"}
3. Retriever parses bucket and key, generates read-only S3 presigned URL
4. URL returned with configurable TTL (default 3600s)
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_PRESIGNED_URLS` | `true` | Enable/disable presign endpoint |
| `PRESIGNED_URL_TTL_SECONDS` | `3600` | URL expiration in seconds |

### Security

The presigned URL is **read-only** (`GET` method) and contains an embedded signature. It does not expose AWS credentials. The URL expires after the configured TTL.

---

## Caching Strategy

| Cache Type | Lookup Method | Match Criteria | Score Threshold |
|------------|---------------|----------------|-----------------|
| **Exact** | SHA256 hash of (query + corpus + model + tenant) | ID match | ≥ 0.72 |
| **Semantic Strict** | Cosine similarity on query embedding | Same params as query | ≥ 0.84 |
| **Semantic Relaxed** | Cosine similarity (fallback) | Same params as query | ≥ 0.75 |

**Cache Promotion**: When a semantic cache hit occurs, an exact cache entry is created for faster future lookups.

---

## Reranking Modes

| Mode | Behavior |
|------|----------|
| `ALWAYS` | Always rerank fused results |
| `AUTO` | Rerank if fusion confidence is low (top score < 0.75 or margin < 0.08) |
| `DISABLE` | Never rerank |

---

## Metrics Reference

### HTTP Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `http_requests_total` | Counter | `method`, `route`, `status_code` |
| `http_request_duration_seconds` | Histogram | `method`, `route`, `status_code` |
| `http_active_requests` | Gauge | `method`, `route` |
| `http_errors_total` | Counter | `method`, `route`, `status_code` |

### Pipeline Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `pipeline_duration_seconds` | Histogram | `outcome` (`ok`, `cache_hit`, `error`) |
| `qdrant_query_total` | Counter | `mode` (`dense`, `sparse`, `hybrid`) |
| `qdrant_query_duration_seconds` | Histogram | `mode` |
| `cache_lookup_total` | Counter | `result` (`exact_hit`, `semantic_strict`, `miss`) |
| `cache_write_total` | Counter | `result` (`ok`, `fail`), `cache_kind` (`llm`, `promotion`) |
| `pipeline_errors_total` | Counter | `error_type` |

### Dependency Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `dense_embed_requests_total` | Counter | — |
| `dense_embed_duration_seconds` | Histogram | — |
| `sparse_embed_requests_total` | Counter | — |
| `sparse_embed_duration_seconds` | Histogram | — |
| `rerank_requests_total` | Counter | — |
| `rerank_duration_seconds` | Histogram | — |
| `llm_requests_total` | Counter | `mode` (`generate`, `stream`) |
| `llm_duration_seconds` | Histogram | `mode` |
| `circuit_breaker_open_total` | Counter | `dependency` |
| `retry_attempts_total` | Counter | `dependency`, `attempt` |
| `dependency_errors_total` | Counter | `dependency`, `error_type` |

### Service Health

| Metric | Type | Values |
|--------|------|--------|
| `service_ready` | Gauge | `1` = ready, `0` = not ready |

---

## Log Format

```json
{
  "timestamp": "2026-05-08T07:47:50.000Z",
  "level": "info",
  "message": "store bootstrap complete",
  "service": "retriever",
  "environment": "PROD",
  "instance": "retriever-647dd747c4-6pprj",
  "namespace": "inference",
  "fields": {
    "docs_ready": true,
    "cache_ready": true
  }
}
```

| Field | Description |
|-------|-------------|
| `timestamp` | ISO 8601 with milliseconds, UTC |
| `level` | `info`, `warn`, `error` |
| `message` | Human-readable event description |
| `service` | Always `retriever` |
| `environment` | Deployment environment |
| `instance` | Pod name |
| `namespace` | Kubernetes namespace |
| `fields` | Dynamic event-specific data |

Note: The `debug` level has been removed. Use `info` for development diagnostics and `warn`/`error` for production issues. Set `LOG_LEVEL=WARNING` in production to suppress `info` logs.

---

## Configuration: Required vs Default

### Must Be Set (Non-Derivable)

| Variable | Purpose | Example |
|----------|---------|---------|
| `AWS_REGION` | Bedrock and S3 region | `ap-south-1` |
| `BEDROCK_MODEL_ID` | LLM model | `meta.llama3-8b-instruct-v1:0` |
| `COLLECTION_NAME` | Qdrant collection | `default_rag_collection1` |

### Use Defaults (Derivable from Kubernetes conventions)

| Variable | Default |
|----------|---------|
| `DENSE_URL` | `http://dense-svc.inference.svc.cluster.local:8200` |
| `SPARSE_URL` | `http://sparse-svc.inference.svc.cluster.local:8201` |
| `RERANKER_URL` | `http://reranker-svc.inference.svc.cluster.local:8202` |
| `QDRANT_URL` | `http://qdrant.qdrant.svc.cluster.local:6333` |

### Common Tuning Parameters

| Variable | Default | When to Change |
|----------|---------|----------------|
| `LOG_LEVEL` | `INFO` | Set to `WARNING` for production |
| `LLM_TEMPERATURE` | `0.0` | Increase (0-1) for creative answers |
| `CACHE_TTL_SECONDS` | `86400` | Lower for faster cache eviction |
| `RERANKER_MODE` | `AUTO` | `ALWAYS` for stricter reranking |
| `MAX_CHUNKS_TO_LLM` | `5` | Increase for more context |
| `ENABLE_PRESIGNED_URLS` | `true` | Set to `false` to disable presigning |
| `PRESIGNED_URL_TTL_SECONDS` | `3600` | URL expiration duration |

---

## Files

| File | Purpose |
|------|---------|
| `settings.py` | Configuration, env var parsing, request/response models |
| `retriever_logging.py` | JSON structured logger (info/warn/error only) |
| `metrics.py` | Prometheus metric definitions |
| `clients.py` | Async HTTP clients with retry, circuit breakers, metrics |
| `pipeline.py` | RAG pipeline: cache, embed, retrieve, rerank, generate |
| `main.py` | FastAPI app, SSE streaming, presign endpoint, health probes |
| `store.py` | Qdrant vector DB and semantic cache operations |
| `helpers.py` | Text normalization, prompt building, cache payload construction |
| `citations_helpers.py` | Citation validation, UI chunk building, presigned URL generation |
| `Dockerfile` | Multi-stage build, runs on port 8001 |

---

## Production Checklist

| Item | Status |
|------|--------|
| Auth handled externally | ✅ |
| Prometheus metrics on `/metrics` | ✅ |
| Structured JSON logs to stdout (no debug level) | ✅ |
| Request ID propagation to downstream | ✅ |
| Circuit breakers on all dependencies | ✅ |
| Retry with exponential backoff | ✅ |
| Graceful shutdown (30s timeout) | ✅ |
| Fast health loop shutdown (0.5s intervals) | ✅ |
| Metric label cardinality validated | ✅ |
| Cache TTL with background cleanup | ✅ |
| Streaming SSE with disconnect detection | ✅ |
| IP-based rate limiting (60 req/min) | ✅ |
| Startup/liveness/readiness probes | ✅ |
| Citation validation (anti-hallucination) | ✅ |
| Presigned S3 URLs (read-only, configurable TTL) | ✅ |
| Headless service for direct pod access | ✅ |
<<<<<<< Updated upstream
=======
---
>>>>>>> Stashed changes
