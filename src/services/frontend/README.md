# Frontend / Auth Service

Stateless OIDC authentication gateway and streaming RAG UI proxy with citations and presigned URL support. One FastAPI process serves the UI, handles OIDC login, mints signed JWTs, proxies streaming requests to the retriever backend, and generates read-only presigned S3 URLs for source documents.

---

## Overview

This service has three responsibilities:

| Role | What it does |
|------|--------------|
| **Auth gateway** | OIDC login via Google / Microsoft / GitHub. Issues short-lived signed ES256 JWTs. Exposes JWKS endpoint so other services can verify tokens locally. |
| **UI & API proxy** | Serves the RAG chat interface with citation blocks. Proxies `/generate/stream` and `/presign` requests to the retriever. Enforces authentication and rate limiting. |
| **Citation sources** | Each retrieved chunk includes a `source_url` (S3 path). The UI provides one-click presigned URL generation to view source documents. |

### Key design decisions

- **Stateless** — No database. Identity is carried in a signed JWT.
- **Fail-fast on missing secrets** — If `REQUIRE_AUTH=true` and critical secrets are missing, the service refuses to start.
- **Asymmetric keys** — The retriever verifies the JWT using the JWKS endpoint. No shared secret needed.
- **Distributed rate limiting** — Valkey (Redis-compatible) stores counters. Works across multiple replicas.
- **Single binary** — Auth, UI, and proxy run in one process. No sidecars needed.
- **Split observability** — `frontend_logger.py` for structured JSON logs, `metrics.py` for Prometheus metrics (same ClickHouse-compatible schema as the retriever).

---

## Request flow

```
User opens the RAG UI in a browser.
      │
      ▼
┌─────────────────────────────────────────────────────────────┐
│  1. Browser loads / (HTML + JS)                             │
│  2. JS checks localStorage for an existing JWT              │
│     ├─ Has token? → Calls /auth/me to validate              │
│     └─ No token?  → Shows "Sign in" button                 │
│                                                             │
│  3. User clicks "Sign in" (Tailwind-styled login page)     │
│     → Redirected to Google / Microsoft / GitHub             │
│     → Provider authenticates the user                       │
│     → Redirected back to /auth/callback/{provider}          │
│                                                             │
│  4. Callback handler:                                       │
│     → Exchanges authorization code for tokens               │
│     → Manual token exchange fallback if OAuthError occurs   │
│     → Fetches userinfo (email, name, sub)                   │
│     → Applies domain/org allowlist checks                   │
│     → Mints a signed ES256 JWT                              │
│     → Returns HTML that stores the JWT in localStorage     │
│                                                             │
│  5. User types a question and clicks "Ask"                  │
│     → POST /generate/stream with Authorization: Bearer JWT  │
│     → Frontend validates JWT, extracts claims               │
│     → Proxies to retriever with X-Authenticated-* headers  │
│     → Retriever streams SSE events (delta + done)          │
│     → Browser renders answer word-by-word (typewriter)      │
│     → On "done" event, citations block is rendered          │
│                                                             │
│  6. User clicks "open" next to a source_url                 │
│     → POST /presign with s3_path                            │
│     → Frontend proxies to retriever's /presign              │
│     → Retriever generates read-only presigned S3 URL        │
│     → Browser opens the document in a new tab               │
└─────────────────────────────────────────────────────────────┘
```

---

## How services verify identity

```
Frontend (this service)
  │
  │  Signs JWTs with a private EC P-256 key
  │  Publishes the public key at /.well-known/jwks.json
  │
  ▼
Retriever (or any other service)
  │
  │  Fetches /.well-known/jwks.json (cached)
  │  Verifies the JWT signature locally
  │  Extracts sub, email, provider from claims
  │
  ▼
No runtime dependency between services for auth verification
```

---

## Endpoints

### Auth (mounted at /auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | /auth/login | Tailwind-styled login page with provider SVG icons |
| GET | /auth/login/start/{provider} | Start OIDC flow (google/microsoft/github) |
| GET | /auth/callback/{provider} | OIDC callback — exchanges code, mints JWT, manual fallback |
| GET | /auth/me | Returns authenticated user claims |
| GET | /auth/logout | Clears session and localStorage |
| GET | /auth/health | Auth health check |
| GET | /auth/.well-known/jwks.json | Public JWKS for JWT verification |
| GET | /auth/redirects | Shows configured redirect URIs |

### UI

| Method | Path | Description |
|--------|------|-------------|
| GET | / | RAG chat interface with streaming answer and citation blocks |

### Proxy

| Method | Path | Description |
|--------|------|-------------|
| POST | /generate/stream | SSE stream proxy to retriever |
| POST | /presign | Presigned URL proxy to retriever |

### Observability

| Method | Path | Description |
|--------|------|-------------|
| GET | /metrics | Prometheus metrics |
| GET | /orchestrator/health | Deep health check (auth + upstream + enabled providers) |
| GET | /.well-known/jwks.json | JWKS alias |
| GET | /jwks.json | JWKS alias |

---

## Configuration reference

### Required

| Variable | Purpose |
|----------|---------|
| SESSION_SECRET | Encrypts the OIDC session cookie |
| JWT_PRIVATE_KEY_PEM or JWT_PRIVATE_KEY_PATH | EC P-256 private key for signing JWTs |
| JWT_KID | Stable key identifier (must survive restarts) |
| VALKEY_URL | Redis URL for rate limiting (`redis://:password@host:6379`) |
| FRONTEND_HOSTNAME | Public hostname for OAuth redirects (e.g., `athithya.site`) |
| GENERATE_STREAM_URL | Retriever's `/generate/stream` endpoint |

### OIDC providers

**Google**

| Variable | Purpose |
|----------|---------|
| ENABLE_GOOGLE_AUTH | Enable Google login |
| GOOGLE_CLIENT_ID | OAuth client ID |
| GOOGLE_CLIENT_SECRET | OAuth client secret |
| GOOGLE_ALLOWED_DOMAINS | Restrict to email domains (comma-separated) |

**Microsoft**

| Variable | Purpose |
|----------|---------|
| ENABLE_MICROSOFT_AUTH | Enable Microsoft login |
| MS_CLIENT_ID | OAuth client ID |
| MS_CLIENT_SECRET | OAuth client secret |
| MS_TENANT_ID | Tenant ID (or "common") |
| MICROSOFT_ALLOWED_TENANT_IDS | Restrict to specific tenants |
| MICROSOFT_ALLOWED_DOMAINS | Restrict to email domains |

**GitHub**

| Variable | Purpose |
|----------|---------|
| ENABLE_GITHUB_AUTH | Enable GitHub login |
| GITHUB_CLIENT_ID | OAuth client ID |
| GITHUB_CLIENT_SECRET | OAuth client secret |
| GITHUB_ALLOWED_ORGS | Restrict to members of these orgs |

### JWT

| Variable | Default | Purpose |
|----------|---------|---------|
| JWT_ISS | stateless-openid-auth | Token issuer claim |
| JWT_AUD | rag-ui | Token audience claim |
| JWT_ALG | ES256 | Signing algorithm (pinned) |
| JWT_TTL_SECONDS | 900 | Token lifetime (15 minutes) |
| JWT_CLOCK_SKEW_SECONDS | 90 | Allowed clock skew |

### Rate limiting

| Variable | Default | Applies to |
|----------|---------|------------|
| RATE_LIMIT_AUTH_ME | 30/minute | /auth/me |
| RATE_LIMIT_GENERATE_STREAM | 10/minute | Authenticated streams |
| RATE_LIMIT_STREAM_CONCURRENCY | 10 | Max concurrent streams |

### Presigned URLs

| Variable | Default | Purpose |
|----------|---------|---------|
| ENABLE_PRESIGNED_URLS | true | Enable `/presign` proxy |
| PRESIGNED_URL_TTL_SECONDS | 3600 | Presigned URL lifetime |

### Other

| Variable | Default | Purpose |
|----------|---------|---------|
| REQUIRE_AUTH | true | Require authentication for proxy endpoints |
| DISPLAY_SOURCES_IN_UI | true | Show citation blocks |
| DISPLAY_TOPK_IN_UI | true | Show Top-K selector |
| LOG_LEVEL | INFO | Log level (INFO/WARNING/ERROR) |
| UPSTREAM_TIMEOUT_SECONDS | 60 | Timeout for retriever connections |

---

## Files

| File | Purpose |
|------|---------|
| app.py | Application setup, lifespan, middleware, routes, presign proxy |
| config.py | Environment parsing with fail-fast validation |
| stateless_openid_auth.py | OIDC flows, JWT minting, verification, JWKS, Tailwind login page |
| rate_limits.py | Distributed rate limiting (slowapi + Valkey) |
| frontend_logger.py | Structured JSON logger (info/warn/error) |
| metrics.py | Prometheus metrics (same label conventions as retriever) |
| frontend_ui.py | Streaming RAG chat UI with citations and presigned URL support |
| Dockerfile | Multi-stage Python image |
| requirements.txt | Python dependencies (includes redis client) |
| test.sh | E2E smoke test |

---

## Prometheus metrics

All metrics are prefixed with `frontend_`. Exposed at `/metrics`.

| Metric | Type | Labels |
|--------|------|--------|
| frontend_request_latency_seconds | Histogram | route, method, status_code, environment, service |
| frontend_requests_total | Counter | route, method, status_code, environment, service |
| frontend_active_requests | Gauge | route, method, environment, service |
| frontend_auth_events_total | Counter | event, provider, outcome, environment, service |
| frontend_rate_limit_events_total | Counter | route_class, outcome, environment, service |
| frontend_upstream_stream_errors_total | Counter | route_class, error_type, environment, service |
| frontend_jwks_requests_total | Counter | outcome, environment, service |
| frontend_service_ready | Gauge | environment, service |

---

## Log format

Structured JSON to stdout. One log line per event. Same schema as retriever for unified ClickHouse ingestion.

```json
{
  "timestamp": "2026-05-08T12:00:00.000Z",
  "level": "info",
  "message": "service.startup",
  "service": "frontend",
  "environment": "PROD",
  "instance": "frontend-abc123",
  "namespace": "inference",
  "fields": {
    "require_auth": true,
    "enabled_providers": ["google"],
    "external_base": "https://athithya.site"
  }
}
```

Log levels: `info` (dev only), `warn` (operational issues), `error` (failures). No `debug` level.

---

## Running tests

```bash
bash test.sh
```

Starts Valkey, starts a mock retriever, generates a signing key, and validates:

- Health endpoints
- JWKS endpoint and aliases
- JWT minting, verification, expiration, and rejection
- Rate limiting on /auth/me and stream endpoints
- SSE stream generation (authenticated and unauthenticated)
- Concurrent request handling
- Prometheus metrics presence and labels
- UI page loading with citations block
- Presigned URL proxy
- Edge cases (404, XSS, large payloads)

---

## Security

| Concern | How it's handled |
|---------|-----------------|
| OAuth PKCE | S256 code challenge for all providers |
| CSRF | Random state parameter validated on callback |
| Token signing | ES256 asymmetric keys (private key never leaves the service) |
| Token lifetime | 15 minutes by default |
| JWKS rotation | Change JWT_KID and update the key simultaneously |
| Rate limiting | Distributed via Valkey; per-subject for authenticated users |
| Session cookies | SameSite=Lax, Secure in production, HttpOnly |
| Secrets in browser | JWT only; no refresh tokens or secrets stored |
| Presigned URLs | Read-only (GET only), short-lived, generated server-side |
| Fail-fast startup | Refuses to start if REQUIRE_AUTH=true and secrets are missing |
<<<<<<< Updated upstream
=======
-
>>>>>>> Stashed changes
