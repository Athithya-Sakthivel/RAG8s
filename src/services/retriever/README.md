# Retriever Service

Retriever is the streaming RAG backend for the stack. It provides a single answer generation endpoint:

* user request enters `/generate/stream`
* authentication is handled externally (no built-in auth)
* the retrieval pipeline streams the final answer as SSE

The service does:

* document retrieval from Qdrant
* dense and sparse embedding calls
* optional reranking
* optional Bedrock answer generation
* semantic cache lookup and writeback
* Prometheus metrics export

## Runtime architecture

The service has two main layers.

1. **HTTP ingress**
   Handles request validation, streaming responses, health checks, request IDs, and request-level metrics.

2. **Retrieval pipeline**
   Performs cache lookup, retrieval, reranking, LLM generation, and cache writes.

## Authentication model

Authentication is handled externally (e.g., by a reverse proxy or API gateway). The retriever service does not perform any authentication or authorization. All endpoints are publicly accessible within the cluster.

Rate limiting is IP-based.

## Observability model

The service uses Prometheus for metrics and structured JSON logging.

* **Metrics** for request counts, latency, cache activity, retrieval activity, retries, and circuit breaker events
* **Logs** for structured JSON events

Metrics are exposed on the main service port at `/metrics` for Prometheus scraping.

### Metrics

Metrics are low-cardinality only. Typical attributes are:

* `service_name`
* `deployment_environment`
* `env`
* `route`
* `method`
* `status_code`
* `dependency`
* `mode`
* `result`

Exported metrics include:

* HTTP request count
* HTTP request duration
* HTTP in-flight request count
* HTTP error count
* retrieval pipeline duration
* Qdrant query count and latency
* cache lookup count and latency
* cache write count and latency
* dense embedding count and latency
* sparse embedding count and latency
* rerank request count and latency
* LLM request count and latency
* circuit breaker open events
* retry attempts
* readiness state
* retrieved document count

A Prometheus scrape endpoint is required at `/metrics` on the main service port.

### Logs

Logs are structured JSON written to stdout with the following schema:

```json
{
  "timestamp": "2026-05-05T15:13:58.470Z",
  "level": "info",
  "message": "store bootstrap complete",
  "service": "retriever",
  "deployment.environment": "PROD",
  "env": "PROD",
  "fields": {
    "docs_ready": true,
    "cache_ready": true
  }
}
```

## HTTP routes

### `GET /healthz`

Liveness check.

Response:

```json
{"status":"ok"}
```

### `GET /readyz`

Readiness check.

Returns dependency readiness, cache readiness, retriever readiness, and bootstrap error state.

### `GET /metrics`

Prometheus metrics endpoint. Exposes all service metrics for scraping.

### `POST /generate/stream`

Primary public API.

Server-sent event stream for answer generation. Emits:

* `start`
* `delta`
* `done`
* `error`

Request body:

```json
{
  "query": "how governance differs from guardrails?",
  "top_k": 5,
  "fetch_k": 20,
  "return_chunks": true
}
```

## Retrieval pipeline

The retrieval pipeline is responsible for:

* semantic cache lookup (exact and semantic similarity)
* dense query embedding
* sparse query embedding
* Qdrant retrieval (dense, sparse, or hybrid)
* reranker scoring (auto/always/disable modes)
* candidate fusion (RRF + softmax combination)
* Bedrock answer generation (streaming with fallback)
* citation validation and filtering
* cache writeback for streaming results

## Dependencies

The retriever talks to:

* **Qdrant** for vector and sparse retrieval
* **Dense model server** for dense embeddings
* **Sparse model server** for sparse embeddings
* **Reranker service** for reordering candidates
* **Amazon Bedrock** for LLM generation

## Configuration

Configuration comes from environment variables at startup.

### Core service identity

* `SERVICE_NAME` — default: `retrieval`
* `SERVICE_VERSION` — default: `unknown`
* `ENV` — default: `PROD`
* `DEPLOYMENT_ENVIRONMENT` — default: `PROD`
* `CLUSTER_NAME` — Kubernetes cluster name
* `SERVICE_INSTANCE_ID` — pod instance identifier

### Retriever backends

* `QDRANT_URL` — default: `http://qdrant.qdrant.svc.cluster.local:6333`
* `QDRANT_API_KEY` — Qdrant API key (optional)
* `COLLECTION_NAME` — default: `default_rag_collection1`
* `CACHE_COLLECTION_NAME` — default: `{COLLECTION_NAME}__semantic_cache`
* `DENSE_URL` — default: `http://dense-svc.inference.svc.cluster.local:8200`
* `SPARSE_URL` — default: `http://sparse-svc.inference.svc.cluster.local:8201`
* `RERANKER_URL` — default: `http://reranker-svc.inference.svc.cluster.local:8202`
* `AWS_REGION` — default: `ap-south-1`
* `BEDROCK_MODEL_ID` — default: `meta.llama3-8b-instruct-v1:0`
* `BEDROCK_GUARDRAIL_IDENTIFIER` — Bedrock guardrail ID (optional)
* `BEDROCK_GUARDRAIL_VERSION` — Bedrock guardrail version (optional)

### Retrieval behavior

* `CORPUS_VERSION` — default: `v1`
* `PROMPT_VERSION` — default: `v1`
* `RETRIEVAL_VERSION` — default: `retrieval-v1`
* `TENANT_ID` — tenant identifier (optional)
* `DENSE_DIM` — default: `384`
* `MAX_CHUNKS_TO_LLM` — default: `5`
* `QUERY_TOPK_DENSE` — default: `50`
* `QUERY_TOPK_SPARSE` — default: `50`
* `FETCH_K` — default: `20`
* `RERANK_TOPK` — default: `10`
* `RERANKER_MODE` — `AUTO`, `ALWAYS`, or `DISABLE` (default: `AUTO`)
* `RERANK_AUTO_THRESHOLD` — default: `0.75`
* `RERANK_MARGIN` — default: `0.08`
* `RERANK_ALPHA` — default: `0.6`
* `RRF_K` — default: `60`
* `CACHE_SCORE_THRESHOLD` — default: `0.72`
* `CACHE_TTL_SECONDS` — default: `86400`
* `CACHE_CLEANUP_INTERVAL_SECONDS` — default: `900`
* `PROMPT_MAX_CONTENT_CHARS` — default: `2500`
* `CHUNK_OUTPUT_MAX_CHARS` — default: `1600`
* `MAX_PROMPT_CHARS` — default: `40000`
* `MAX_CONCURRENT_REQUESTS` — default: `64`
* `HTTP_TIMEOUT` — default: `10.0`
* `HTTP_MAX_CONNECTIONS` — default: `100`
* `HTTP_MAX_KEEPALIVE` — default: `20`
* `RETRY_MAX_ATTEMPTS` — default: `3`
* `RETRY_BASE_DELAY` — default: `0.08`
* `RETRY_MAX_DELAY` — default: `0.8`
* `BREAKER_FAILURE_THRESHOLD` — default: `3`
* `BREAKER_RESET_TIMEOUT` — default: `20.0`
* `LLM_MAX_TOKENS` — default: `400`
* `LLM_TEMPERATURE` — default: `0.0`

### Prometheus

* `ENABLE_PROMETHEUS` — default: `true`
* `PROMETHEUS_PORT` — default: `9090`
* `PROMETHEUS_PATH` — default: `/metrics`

### Logging

* `LOG_LEVEL` — default: `WARNING` (options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)

## Monitoring setup

Metrics are exposed at `/metrics` on the main service port (default: 8001) for Prometheus scraping.

Recommended defaults:

* Prometheus metrics enabled
* Log level: `INFO` for production, `DEBUG` for troubleshooting
* Scrape interval: 15-30 seconds

## Rate limiting model

Rate limiting uses client IP address:

* All requests: IP-based rate limiting
* Default limit: 60 requests per minute

## Operational cautions

* Keep metric labels low-cardinality.
* Do not put raw queries, document text, or cache keys into metric labels.
* Use structured logs for request-specific detail.
* Validate dependency connectivity early at startup.
* Cache health states and update asynchronously.
* Health checks run in background loops with graceful cancellation.

## Files in this service

* `settings.py` — service and retrieval configuration
* `telemetry.py` — JSON structured logging
* `clients.py` — outbound dependency clients with retry, metrics, and circuit breakers
* `pipeline.py` — retrieval pipeline, cache, rerank, LLM, readiness
* `main.py` — FastAPI app, stream route, startup/shutdown
* `store.py` — Qdrant access and cache persistence
* `helpers.py` — prompt shaping, normalization, and citation filtering

## Expected behavior

A healthy request flow should produce:

1. structured JSON logs for startup and health checks
2. one pipeline execution with Prometheus metrics
3. several dependency calls with individual metrics
4. streaming SSE response with citations
5. cache hit/miss metrics
6. Prometheus metrics at `/metrics`
7. all dependencies showing healthy in `/readyz`