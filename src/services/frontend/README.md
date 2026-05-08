# Frontend / Auth Service

Stateless OIDC authentication gateway and streaming RAG UI proxy. One FastAPI process serves the UI, handles OIDC login, mints signed JWTs, and proxies streaming requests to the retriever backend.

---

## Overview

This service has two responsibilities:

| Role | What it does |
|------|--------------|
| **Auth gateway** | OIDC login via Google / Microsoft / GitHub. Issues short-lived signed JWTs. Exposes JWKS endpoint so other services can verify tokens locally. |
| **UI & API proxy** | Serves the RAG chat interface. Proxies `/generate/stream` requests to the retriever. Enforces authentication and rate limiting. |

### Key design decisions

- **Stateless** — No database. Identity is carried in a signed JWT.
- **No shared secret** — The retriever verifies the JWT using the JWKS endpoint. No network call to the auth service needed at verification time.
- **Distributed rate limiting** — Valkey (Redis-compatible) stores counters. Works across multiple replicas.
- **Single binary** — Auth, UI, and proxy run in one process. No sidecars needed.

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
│  3. User clicks "Sign in"                                   │
│     → Redirected to Google / Microsoft / GitHub             │
│     → Provider authenticates the user                       │
│     → Redirected back to /auth/callback/{provider}          │
│                                                             │
│  4. Callback handler:                                       │
│     → Exchanges authorization code for tokens               │
│     → Fetches userinfo (email, name, sub)                   │
│     → Applies domain/org allowlist checks                   │
│     → Mints a signed ES256 JWT                              │
│     → Returns HTML that stores the JWT in localStorage     │
│                                                             │
│  5. User types a question and clicks "Ask"                  │
│     → POST /generate/stream with Authorization: Bearer JWT  │
│     → Frontend validates JWT, extracts sub                  │
│     → Proxies to retriever with X-Authenticated-Sub header │
│     → Retriever streams SSE events back                    │
│     → Browser renders the answer in real time               │
└─────────────────────────────────────────────────────────────┘
```

---

## How services verify identity

```
Frontend (this service)
  │
  │  Signs JWTs with a private EC key
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

### Auth (/auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | /auth/login | Login page with provider buttons |
| GET | /auth/login/start/{provider} | Start OIDC flow (google/microsoft/github) |
| GET | /auth/callback/{provider} | OIDC callback — exchanges code, mints JWT |
| GET | /auth/me | Returns authenticated user claims |
| GET | /auth/logout | Clears session and localStorage |
| GET | /auth/health | Auth health check |
| GET | /auth/.well-known/jwks.json | Public JWKS for JWT verification |
| GET | /auth/redirects | Shows configured redirect URIs |

### UI

| Method | Path | Description |
|--------|------|-------------|
| GET | / | RAG chat interface (HTML/JS) |
| GET | /health | UI health check |

### Proxy

| Method | Path | Description |
|--------|------|-------------|
| POST | /generate/stream | SSE stream proxy to retriever |
| POST | /api/generate/stream | Alias for /generate/stream |

### Observability

| Method | Path | Description |
|--------|------|-------------|
| GET | /metrics | Prometheus metrics |
| GET | /orchestrator/health | Deep health check (auth + upstream) |
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
| VALKEY_URL | Redis URL for rate limiting |
| FRONTEND_BASE | Public URL of this service |
| GENERATE_STREAM_URL | Retriever's /generate/stream endpoint |

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
| JWT_ALG | ES256 | Signing algorithm |
| JWT_TTL_SECONDS | 900 | Token lifetime (15 minutes) |
| JWT_CLOCK_SKEW_SECONDS | 90 | Allowed clock skew |

### Rate limiting

| Variable | Default | Applies to |
|----------|---------|------------|
| RATE_LIMIT_JWKS | 120/minute | JWKS endpoint |
| RATE_LIMIT_AUTH_ME | 60/minute | /auth/me |
| RATE_LIMIT_GENERATE_STREAM_AUTH | 10/minute | Authenticated streams |
| RATE_LIMIT_GENERATE_STREAM_ANON | 2/minute | Anonymous streams |
| RATE_LIMIT_GENERATE_STREAM_CONCURRENCY | 2 | Max concurrent streams |

---

## Files

| File | Purpose |
|------|---------|
| app.py | Application setup, lifespan, middleware, routes |
| config.py | Environment parsing with sensible defaults |
| stateless_openid_auth.py | OIDC flows, JWT minting, verification, JWKS |
| rate_limits.py | Distributed rate limiting (slowapi + Valkey) |
| telemetry.py | Prometheus metrics and structured JSON logging |
| frontend_ui.py | RAG chat UI served at / |
| Dockerfile | Multi-stage Python image |
| requirements.txt | Python dependencies |
| test.sh | E2E smoke test (60+ assertions) |

---

## Prometheus metrics

All metrics are prefixed with `frontend_`. Exposed at `/metrics`.

| Metric | Type | Labels |
|--------|------|--------|
| frontend_requests_total | Counter | service, route, method, status_code, environment |
| frontend_request_latency_seconds | Histogram | same |
| frontend_active_requests | Gauge | service, route, method, environment |
| frontend_auth_events_total | Counter | service, event, provider, outcome, environment |
| frontend_rate_limit_events_total | Counter | service, route_class, outcome, environment |
| frontend_upstream_stream_errors_total | Counter | service, route_class, error_type, environment |
| frontend_jwks_requests_total | Counter | service, outcome, environment |
| frontend_service_ready | Gauge | service, environment |

---

## Log format

Structured JSON to stdout. One log line per event.

```json
{
  "timestamp": "2026-05-05T23:54:47.934Z",
  "level": "info",
  "message": "service.startup",
  "service": "frontend",
  "environment": "TEST",
  "instance": "frontend-abc123",
  "namespace": "inference",
  "fields": {
    "require_auth": true,
    "jwt_material_present": true
  }
}
```
---

## Running tests

```bash
bash test.sh
```

Starts Valkey, starts a mock retriever, generates a signing key, and validates:

- Health endpoints
- JWKS endpoint and aliases
- JWT minting, verification, expiration, and rejection
- Rate limiting on JWKS, /auth/me, and stream endpoints
- SSE stream generation (authenticated and unauthenticated)
- Concurrent request handling
- Prometheus metrics presence and labels
- UI page loading
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
