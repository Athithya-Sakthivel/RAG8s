# Retriever Service

Retriever is the RAG retrieval and answer-generation service for the stack. It exposes a FastAPI application that:

- validates user requests,
- retrieves documents from Qdrant,
- performs dense and sparse embedding,
- optionally reranks results,
- optionally calls Bedrock for answer generation,
- maintains semantic cache entries,
- exports telemetry through OpenTelemetry to an OTLP collector for SigNoz.

## Runtime architecture

The service has two main layers:

1. **HTTP ingress**
   Handles inbound requests, request IDs, validation, streaming responses, health checks, and request-level telemetry.

2. **Retrieval pipeline**
   Performs cache lookup, embedding, Qdrant search, reranking, LLM generation, and cache writes.

The service uses OpenTelemetry for all observability signals:

- **Traces** for request flow and dependency calls
- **Metrics** for request counts, latency, cache activity, retrieval activity, readiness, retries, and circuit breaker events
- **Logs** for structured events with trace correlation

Telemetry is exported to an OpenTelemetry Collector over OTLP. The Collector is expected to forward data to SigNoz.

## Observability model

### Traces

The service creates spans for:

- inbound HTTP requests
- pipeline execution
- exact cache lookup
- semantic cache lookup
- dense embedding
- sparse embedding
- Qdrant dense and sparse search
- reranking
- Bedrock generation and streaming
- cache writes
- validation and error paths

Trace context is propagated across outbound HTTP calls by injecting request headers before the request leaves the retriever service. Downstream services must also extract incoming context to continue the same trace end to end.

### Metrics

The service exports low-cardinality metrics only. Typical attributes are:

- `service.name`
- `deployment.environment`
- `route`
- `method`
- `status_code`
- `dependency`
- `mode`
- `result`
- `cache_kind`

Exported metrics include:

- HTTP request count
- HTTP request duration
- HTTP in-flight request count
- HTTP error count
- retrieval pipeline duration
- Qdrant query count and latency
- cache lookup count and latency
- cache write count and latency
- dense embedding count and latency
- sparse embedding count and latency
- rerank request count and latency
- LLM request count and latency
- circuit breaker open events
- retry attempts
- readiness gauge
- retrieved document count

No Prometheus scrape endpoint is required for observability. Telemetry is exported through OTLP to the collector.

### Logs

Logs are structured JSON and include trace correlation fields when a span is active:

- `trace_id`
- `span_id`
- `trace_flags`

Logs are emitted through Python logging and exported through the OpenTelemetry logging pipeline to the collector.

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

### `POST /generate`

Generates an answer and returns retrieval metadata plus cached or generated answer content.

Request body fields:

- `query` required
- `tenant_id` optional
- `corpus_version` optional
- `prompt_version` optional
- `retrieval_version` optional
- `model_name` optional
- `debug` optional
- `enable_tracing` optional
- `top_k` optional
- `fetch_k` optional
- `return_chunks` optional
- `max_tokens` optional
- `allow_semantic_cache` optional

### `POST /retrieve`

Returns retrieval results without generating an answer.

Request body fields:

- `query` required
- `tenant_id` optional
- `corpus_version` optional
- `retrieval_version` optional
- `top_k` optional
- `fetch_k` optional
- `rerank` optional
- `include_cache` optional

### `POST /generate/stream`

### `POST /stream`

Server-sent event stream for answer generation. Emits:

- `start`
- `delta`
- `done`
- `error`

## Dependencies

The retriever talks to:

- **Qdrant** for vector and sparse retrieval
- **Dense model server** for dense embeddings
- **Sparse model server** for sparse embeddings
- **Reranker service** for reordering candidates
- **Amazon Bedrock** for LLM generation

For full distributed traces, the downstream model servers should also be instrumented with OpenTelemetry so they extract incoming context and continue the trace.

## Configuration

Configuration comes from environment variables. The service reads these values at startup.

### Core service identity

- `SERVICE_NAME`
- `SERVICE_VERSION`
- `ENV`
- `DEPLOYMENT_ENVIRONMENT`
- `CLUSTER_NAME`
- `SERVICE_INSTANCE_ID`

### Retriever backends

- `QDRANT_URL`
- `QDRANT_API_KEY`
- `COLLECTION_NAME`
- `CACHE_COLLECTION_NAME`
- `DENSE_URL`
- `SPARSE_URL`
- `RERANKER_URL`
- `AWS_REGION`
- `BEDROCK_MODEL_ID`
- `AWS_BEDROCK_MODEL_ID`
- `BEDROCK_GUARDRAIL_IDENTIFIER`
- `BEDROCK_GUARDRAIL_VERSION`

### Retrieval behavior

- `CORPUS_VERSION`
- `PROMPT_VERSION`
- `RETRIEVAL_VERSION`
- `TENANT_ID`
- `DENSE_DIM`
- `MAX_CHUNKS_TO_LLM`
- `QUERY_TOPK_DENSE`
- `QUERY_TOPK_SPARSE`
- `FETCH_K`
- `RERANK_TOPK`
- `RERANKER_MODE`
- `RERANK_AUTO_THRESHOLD`
- `RERANK_MARGIN`
- `RERANK_ALPHA`
- `RRF_K`
- `CACHE_SCORE_THRESHOLD`
- `CACHE_TTL_SECONDS`
- `CACHE_CLEANUP_INTERVAL_SECONDS`
- `PROMPT_MAX_CONTENT_CHARS`
- `CHUNK_OUTPUT_MAX_CHARS`
- `MAX_PROMPT_CHARS`
- `MAX_CONCURRENT_REQUESTS`
- `HTTP_TIMEOUT`
- `HTTP_MAX_CONNECTIONS`
- `HTTP_MAX_KEEPALIVE`
- `RETRY_MAX_ATTEMPTS`
- `RETRY_BASE_DELAY`
- `RETRY_MAX_DELAY`
- `BREAKER_FAILURE_THRESHOLD`
- `BREAKER_RESET_TIMEOUT`
- `LLM_MAX_TOKENS`
- `LLM_TEMPERATURE`

### OpenTelemetry

- `OTEL_EXPORTER_OTLP_ENDPOINT`
- `OTEL_TIMEOUT_SECONDS`
- `OTEL_METRIC_EXPORT_INTERVAL_MS`
- `OTEL_METRIC_EXPORT_TIMEOUT_MS`
- `OTEL_TRACES_SAMPLER`
- `OTEL_TRACES_SAMPLER_ARG`
- `ENABLE_OTEL_TRACES`
- `ENABLE_OTEL_METRICS`
- `ENABLE_OTEL_LOGS`

### Logging

- `LOG_LEVEL`

## OpenTelemetry + SigNoz setup

The service should send OTLP to your collector, not to Prometheus.

Typical collector target:

```text
http://signoz-otel-collector.signoz.svc.cluster.local:4317
```

Recommended defaults for production-like environments:

- traces enabled
- metrics enabled
- logs enabled
- sampler: `parentbased_traceidratio`
- trace sample ratio: `0.1`
- log level: `WARNING`

SigNoz receives the three signals from the collector and correlates them through the shared OTEL resource attributes and trace context.

## CI telemetry test

The CI check should verify:

- `/healthz` responds successfully
- `/readyz` responds
- retriever requests produce traces
- retriever requests produce metrics
- structured logs are exported
- trace correlation fields appear in logs

The CI script should validate the collector output, not scrape Prometheus.

## Notes on downstream services

To keep the trace continuous across the whole RAG path:

- retriever must inject outbound context
- downstream model services must extract inbound context
- downstream services should emit their own spans, logs, and metrics

If a downstream service is not instrumented, retriever telemetry still works, but the trace will stop at that boundary.

## Operational cautions

- Keep metric attributes low-cardinality.
- Do not put raw queries, document text, user IDs, or cache keys into metric labels.
- Use spans and logs for request-specific detail.
- Keep OTel package versions pinned together.
- Validate collector connectivity early at startup.
- Prefer OTLP export and batching over synchronous export paths.

## Files in this service

- `settings.py` — service and telemetry configuration
- `telemetry.py` — OTel bootstrap, logging, exporters, shutdown
- `clients.py` — outbound dependency clients with retry, metrics, and propagation
- `pipeline.py` — retrieval pipeline, cache, rerank, LLM, readiness
- `main.py` — FastAPI app, HTTP telemetry, routes, startup/shutdown
- `store.py` — Qdrant access and cache persistence
- `helpers.py` — prompt shaping, normalization, and citation filtering

## Expected behavior

A healthy request flow should produce:

1. one inbound HTTP span
2. one pipeline span
3. several child dependency spans
4. structured logs with trace correlation
5. OTLP metrics at the collector
6. SigNoz views that can pivot from a spike to the related trace and log line
