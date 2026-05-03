# Retriever Service

Retriever is the streaming RAG backend for the stack. It is now centered on a single authenticated answer path:

* user request enters `/generate/stream`
* unauthenticated users are redirected into ZITADEL login
* ZITADEL issues OIDC tokens
* backend verifies the token locally with JWKS
* backend extracts `sub` which is unique per end-user
* `sub` is used for rate limiting and trace attribution
* the retrieval pipeline streams the final answer as SSE

The service still does:

* document retrieval from Qdrant
* dense and sparse embedding calls
* optional reranking
* optional Bedrock answer generation
* semantic cache lookup and writeback
* OpenTelemetry export to SigNoz via OTLP

## Runtime architecture

The service has three main layers.

1. **HTTP ingress**
   Handles request validation, auth redirect, callback handling, streaming responses, health checks, request IDs, and request-level telemetry.

2. **Auth layer**
   Uses ZITADEL OIDC. The backend does not rely on reverse-proxy forward auth. It either:

   * verifies bearer tokens on the request, or
   * completes the login redirect flow and stores a signed session cookie.

3. **Retrieval pipeline**
   Performs cache lookup, retrieval, reranking, LLM generation, and cache writes.

## Authentication model

ZITADEL is the identity provider. The retriever backend is the relying party.

Expected behavior:

* unauthenticated access to `/generate/stream` redirects to ZITADEL login
* `/auth/callback` exchanges the authorization code for tokens
* backend validates the ID/access token using ZITADEL JWKS
* backend extracts the stable user subject claim `sub`
* `sub` is used as the identity key for:

  * rate limiting
  * tracing attributes
  * session association if cookies are used

Important: the backend does not need a persistent “connection” to ZITADEL for each request. JWT verification is local after JWKS fetch and cache.

## Observability model

The service uses OpenTelemetry for all observability signals:

* **Traces** for request flow and dependency calls
* **Metrics** for request counts, latency, cache activity, retrieval activity, retries, and circuit breaker events
* **Logs** for structured events with trace correlation

Telemetry is exported to an OpenTelemetry Collector over OTLP, then forwarded to SigNoz.

### Traces

The service creates spans for:

* inbound HTTP requests
* auth redirect and callback handling
* retrieval pipeline execution
* cache lookup
* dense embedding
* sparse embedding
* Qdrant dense and sparse search
* reranking
* Bedrock generation and streaming
* cache writes
* validation and error paths

Trace context is propagated to downstream HTTP services through standard OpenTelemetry context injection.

### Metrics

Metrics are low-cardinality only. Typical attributes are:

* `service.name`
* `deployment.environment`
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

No Prometheus scrape endpoint is required for observability. Telemetry is exported through OTLP.

### Logs

Logs are structured JSON and include trace correlation fields when a span is active:

* `trace_id`
* `span_id`
* `trace_flags`

Logs are emitted through Python logging and can be exported through the OpenTelemetry logging pipeline to the collector.

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

### `GET /auth/login`

Starts the ZITADEL login flow.

### `GET /auth/callback`

Handles the ZITADEL OIDC callback, exchanges the code for tokens, verifies the token, and establishes the backend session.

### `POST /generate/stream`

Primary public API.

Server-sent event stream for answer generation. Emits:

* `start`
* `delta`
* `done`
* `error`

This is the only user-facing retrieval route that should remain enabled.

### `GET /auth/logout`

Ends the local session and optionally triggers ZITADEL end-session behavior.

## Retrieval pipeline

The retrieval pipeline is still responsible for:

* semantic cache lookup
* dense query embedding
* sparse query embedding
* Qdrant retrieval
* reranker scoring
* candidate fusion
* Bedrock answer generation
* citation validation
* cache writeback for streaming results

The answer stream should still use citation-filtered content only.

## Dependencies

The retriever talks to:

* **Qdrant** for vector and sparse retrieval
* **Dense model server** for dense embeddings
* **Sparse model server** for sparse embeddings
* **Reranker service** for reordering candidates
* **Amazon Bedrock** for LLM generation
* **ZITADEL** for OIDC login, token verification, and user identity
* **SigNoz collector** for telemetry export

For full distributed traces, downstream model services should also be instrumented with OpenTelemetry.

## Configuration

Configuration comes from environment variables at startup.

### Core service identity

* `SERVICE_NAME`
* `SERVICE_VERSION`
* `ENV`
* `DEPLOYMENT_ENVIRONMENT`
* `CLUSTER_NAME`
* `SERVICE_INSTANCE_ID`

### Retriever backends

* `QDRANT_URL`
* `QDRANT_API_KEY`
* `COLLECTION_NAME`
* `CACHE_COLLECTION_NAME`
* `DENSE_URL`
* `SPARSE_URL`
* `RERANKER_URL`
* `AWS_REGION`
* `BEDROCK_MODEL_ID`
* `AWS_BEDROCK_MODEL_ID`
* `BEDROCK_GUARDRAIL_IDENTIFIER`
* `BEDROCK_GUARDRAIL_VERSION`

### Retrieval behavior

* `CORPUS_VERSION`
* `PROMPT_VERSION`
* `RETRIEVAL_VERSION`
* `TENANT_ID`
* `DENSE_DIM`
* `MAX_CHUNKS_TO_LLM`
* `QUERY_TOPK_DENSE`
* `QUERY_TOPK_SPARSE`
* `FETCH_K`
* `RERANK_TOPK`
* `RERANKER_MODE`
* `RERANK_AUTO_THRESHOLD`
* `RERANK_MARGIN`
* `RERANK_ALPHA`
* `RRF_K`
* `CACHE_SCORE_THRESHOLD`
* `CACHE_TTL_SECONDS`
* `CACHE_CLEANUP_INTERVAL_SECONDS`
* `PROMPT_MAX_CONTENT_CHARS`
* `CHUNK_OUTPUT_MAX_CHARS`
* `MAX_PROMPT_CHARS`
* `MAX_CONCURRENT_REQUESTS`
* `HTTP_TIMEOUT`
* `HTTP_MAX_CONNECTIONS`
* `HTTP_MAX_KEEPALIVE`
* `RETRY_MAX_ATTEMPTS`
* `RETRY_BASE_DELAY`
* `RETRY_MAX_DELAY`
* `BREAKER_FAILURE_THRESHOLD`
* `BREAKER_RESET_TIMEOUT`
* `LLM_MAX_TOKENS`
* `LLM_TEMPERATURE`

### ZITADEL / auth

* `ZITADEL_ISSUER`
* `ZITADEL_DISCOVERY_URL`
* `ZITADEL_JWKS_URI`
* `ZITADEL_AUTHORIZATION_ENDPOINT`
* `ZITADEL_TOKEN_ENDPOINT`
* `ZITADEL_USERINFO_ENDPOINT`
* `ZITADEL_INTROSPECTION_ENDPOINT`
* `ZITADEL_REVOCATION_ENDPOINT`
* `ZITADEL_END_SESSION_ENDPOINT`
* `ZITADEL_CLIENT_ID`
* `ZITADEL_AUDIENCE`
* `ZITADEL_REDIRECT_URI`
* `ZITADEL_SCOPES`
* `ZITADEL_ALLOWED_ALGORITHMS`
* `ZITADEL_USER_ID_CLAIM`
* `SESSION_COOKIE_NAME`
* `SESSION_COOKIE_SECURE`
* `SESSION_COOKIE_HTTPONLY`
* `SESSION_COOKIE_SAMESITE`
* `SESSION_TTL_SECONDS`
* `SESSION_SECRET`
* `AUTH_REQUIRED_PATHS`
* `AUTH_EXEMPT_PATHS`
* `DEFAULT_ANON_RATE_LIMIT`
* `DEFAULT_USER_RATE_LIMIT`

### OpenTelemetry

* `OTEL_EXPORTER_OTLP_ENDPOINT`
* `OTEL_EXPORTER_OTLP_PROTOCOL`
* `OTEL_TIMEOUT_SECONDS`
* `OTEL_METRIC_EXPORT_INTERVAL_MS`
* `OTEL_METRIC_EXPORT_TIMEOUT_MS`
* `OTEL_TRACES_SAMPLER`
* `OTEL_TRACES_SAMPLER_ARG`
* `ENABLE_OTEL_TRACES`
* `ENABLE_OTEL_METRICS`
* `ENABLE_OTEL_LOGS`

### Logging

* `LOG_LEVEL`

## OpenTelemetry + SigNoz setup

Use OTLP directly to the collector.

Recommended defaults:

* traces enabled
* metrics enabled
* logs enabled
* sampler: `parentbased_traceidratio`
* trace sample ratio: `0.1`
* log level: `WARNING`

Protocol rules:

* gRPC OTLP uses port `4317`
* HTTP OTLP uses port `4318`

Do not mix protocol and port.

## Rate limiting model

Rate limiting should use the authenticated ZITADEL subject claim:

* authenticated user: `sub`
* anonymous fallback: client IP

This keeps one stable bucket per user, independent of whether the user signed in with local credentials or a federated provider like Google.

## CI telemetry test

The CI check should verify:

* `/healthz` responds successfully
* `/readyz` responds
* `/generate/stream` enforces auth
* a valid ZITADEL token is accepted
* `sub` is extracted and used for rate limiting
* retriever requests produce traces
* structured logs are exported
* telemetry reaches the collector

## Operational cautions

* Keep metric attributes low-cardinality.
* Do not put raw queries, document text, user IDs, or cache keys into metric labels.
* Use spans and logs for request-specific detail.
* Pin OpenTelemetry package versions together.
* Validate collector connectivity early at startup.
* Prefer OTLP export and batching over synchronous export paths.
* Cache JWKS and validate JWTs locally.
* Keep `/generate/stream` as the only public retrieval endpoint.

## Files in this service

* `settings.py` — service, auth, and telemetry configuration
* `telemetry.py` — OpenTelemetry bootstrap, logging, exporters, shutdown
* `clients.py` — outbound dependency clients with retry, metrics, and propagation
* `pipeline.py` — retrieval pipeline, cache, rerank, LLM, readiness
* `main.py` — FastAPI app, auth routes, stream route, startup/shutdown
* `store.py` — Qdrant access and cache persistence
* `helpers.py` — prompt shaping, normalization, and citation filtering

## Expected behavior

A healthy request flow should produce:

1. one inbound HTTP span
2. one auth span when login is required
3. one pipeline span
4. several child dependency spans
5. structured logs with trace correlation
6. OTLP metrics at the collector
7. SigNoz views that can pivot from a spike to the related trace and log line
