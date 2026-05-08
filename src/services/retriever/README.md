# Retriever Service

Streaming RAG backend that retrieves documents, reranks them, and generates answers via Bedrock.

---

## Quick Reference

### Request Flow

```
User → /generate/stream → Cache Check → Embed → Retrieve → Rerank → LLM → Stream SSE
                                ↓                                    ↓
                           Cache Hit? ←────────────────────────── Write Cache
```

### Service Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/generate/stream` | RAG answer generation (SSE stream) |
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/readyz` | Readiness probe |
| `GET` | `/metrics` | Prometheus metrics |

### Dependencies

| Service | Default URL | Purpose |
|---------|-------------|---------|
| Qdrant | `http://qdrant.qdrant.svc.cluster.local:6333` | Vector & sparse retrieval |
| Dense Embedder | `http://dense-svc.inference.svc.cluster.local:8200` | Dense embeddings |
| Sparse Embedder | `http://sparse-svc.inference.svc.cluster.local:8201` | Sparse embeddings |
| Reranker | `http://reranker-svc.inference.svc.cluster.local:8202` | Cross-encoder reranking |
| AWS Bedrock | External | LLM generation |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         RETRIEVER SERVICE                        │
│                                                                   │
│  ┌──────────────────────┐      ┌──────────────────────────────┐  │
│  │     HTTP Layer        │      │     Retrieval Pipeline        │  │
│  │                        │      │                              │  │
│  │  • Request validation  │      │  1. Cache Lookup              │  │
│  │  • Request ID          │ ───► │  2. Embed (dense + sparse)   │  │
│  │  • Rate limiting (IP)  │      │  3. Qdrant Search (hybrid)   │  │
│  │  • HTTP metrics        │      │  4. Rerank (auto/always)     │  │
│  │  • SSE streaming       │      │  5. LLM Generation           │  │
│  │  • Health probes       │      │  6. Citation validation      │  │
│  │                        │      │  7. Cache writeback          │  │
│  └──────────────────────┘      └──────────────────────────────┘  │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    Background Loops                           │ │
│  │  • Health loop (every 10s) → checks all dependencies         │ │
│  │  • Cache cleanup loop (every 900s) → removes expired entries │ │
│  └──────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
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
| `return_chunks` | bool | `true` | Include document metadata |
| `allow_semantic_cache` | bool | `true` | Enable cache lookup |
| `max_tokens` | int (64-4096) | `400` | Max LLM output tokens |

#### SSE Events

| Event | When | Payload |
|-------|------|---------|
| `start` | Pipeline begins | Query, retrieval config, cache info |
| `delta` | Token generated | `{"text": "token"}` |
| `done` | Stream complete | Full answer, chunks, metrics |
| `error` | Error occurred | `{"error": "message"}` |

#### Example Response (done event)

```json
{
  "event": "done",
  "data": {
    "answer": "Governance defines the rules and policies...",
    "chunks": [
      {
        "chunk_id": "abc123",
        "source_url": "s3://bucket/doc.pdf",
        "content": "Governance frameworks establish...",
        "scores": {
          "dense": 0.92,
          "sparse": 0.87,
          "fusion": 0.895,
          "rerank": 0.94
        }
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
  "timestamp": "2026-05-05T15:13:58.470Z",
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
| `level` | `debug`, `info`, `warn`, `error` |
| `message` | Human-readable event description |
| `service` | Always `retriever` |
| `environment` | Deployment environment |
| `instance` | Pod name |
| `namespace` | Kubernetes namespace |
| `fields` | Dynamic event-specific data |

---

## Configuration: Required vs Default

### Must Be Set (Non-Derivable)

| Variable | Purpose | Example |
|----------|---------|---------|
| `AWS_REGION` | Bedrock region | `ap-south-1` |
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
| `LOG_LEVEL` | `WARNING` | Set to `DEBUG` for troubleshooting |
| `LLM_TEMPERATURE` | `0.0` | Increase (0-1) for creative answers |
| `CACHE_TTL_SECONDS` | `86400` | Lower for faster cache eviction |
| `RERANKER_MODE` | `AUTO` | `ALWAYS` for stricter reranking |
| `MAX_CHUNKS_TO_LLM` | `5` | Increase for more context |

---

## Files

| File | Purpose |
|------|---------|
| `settings.py` | All configuration, env var parsing, request models |
| `telemetry.py` | JSON structured logger |
| `clients.py` | Async HTTP clients with retry, circuit breakers, metrics |
| `pipeline.py` | RAG pipeline: cache, embed, retrieve, rerank, generate |
| `main.py` | FastAPI app, SSE streaming, health probes, HTTP metrics |
| `store.py` | Qdrant vector DB and semantic cache operations |
| `helpers.py` | Text normalization, prompt building, citation filtering |
| `Dockerfile` | Multi-stage build, runs on port 8001 |

---

## Production Checklist

| Item | Status |
|------|--------|
| Auth handled externally | ✅ |
| Prometheus metrics on `/metrics` | ✅ |
| Structured JSON logs to stdout | ✅ |
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