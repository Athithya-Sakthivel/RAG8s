#!/usr/bin/env bash
set -euo pipefail

# Auto-detect script directory — works in CI and locally
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")" && pwd)}"
HOST="127.0.0.1"
APP_PORT="8000"
RETRIEVER_PORT="8001"
VALKEY_PORT="6379"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS=0
FAIL=0

tmpdir="$(mktemp -d)"
cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "${RET_PID:-}" ] && kill "$RET_PID" 2>/dev/null || true
  docker rm -f valkey 2>/dev/null || true
  rm -rf "$tmpdir"
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

mkdir -p "$tmpdir"

# ── Move to APP_DIR FIRST, then set up venv ───────────────────
cd "$APP_DIR"

# Create/activate venv if not already in one
if [ -z "${VIRTUAL_ENV:-}" ]; then
  if [ ! -d .venv ]; then
    python3 -m venv .venv
  fi
  source .venv/bin/activate
  pip install -r requirements.txt --quiet
fi

log_section() { echo -e "\n${BLUE}═══ $1 ═══${NC}"; }
log_pass()   { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS+1)); }
log_fail()   { echo -e "  ${RED}✗${NC} $1"; FAIL=$((FAIL+1)); }
log_info()   { echo -e "  ${YELLOW}→${NC} $1"; }

assert_status() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then
    log_pass "$desc (HTTP $actual)"
  else
    log_fail "$desc (expected HTTP $expected, got HTTP $actual)"
  fi
}

assert_contains() {
  local desc="$1" haystack="$2" needle="$3"
  if echo "$haystack" | grep -q "$needle"; then
    log_pass "$desc"
  else
    log_fail "$desc"
  fi
}

assert_json_field() {
  local desc="$1" json="$2" field="$3" expected="$4"
  local actual
  actual=$(echo "$json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
v = d.get('$field', '')
if isinstance(v, bool):
    print(str(v).lower())
else:
    print(str(v))
" 2>/dev/null || echo "")
  if [ "$actual" = "$expected" ]; then
    log_pass "$desc"
  else
    log_fail "$desc (expected '$expected', got '$actual')"
  fi
}

# ═══════════════════════════════════════════════════════════════
log_section "SETUP"
# ═══════════════════════════════════════════════════════════════

openssl ecparam -name prime256v1 -genkey -noout -out "$tmpdir/jwt.key.pem" 2>/dev/null
log_pass "EC key generated"

docker rm -f valkey 2>/dev/null || true
docker run --rm -d --name valkey -p "${VALKEY_PORT}:6379" valkey/valkey:latest </dev/null

VALKEY_READY=0
for _ in $(seq 1 40); do
  if docker exec valkey valkey-cli ping >/dev/null 2>&1; then
    VALKEY_READY=1
    log_pass "Valkey ready"
    break
  fi
  sleep 0.25
done
if [ "$VALKEY_READY" = "0" ]; then
  log_fail "Valkey failed to start"
  exit 1
fi
docker exec valkey valkey-cli FLUSHDB >/dev/null 2>&1 || true

cat > "$tmpdir/mock_retriever.py" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path.rstrip("/") != "/generate/stream":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length) if length else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'event: start\ndata: {"query":"test","retrieval_mode":"hybrid","cache_hit":false}\n\n')
        self.wfile.flush(); time.sleep(0.02)
        self.wfile.write(b'event: delta\ndata: {"text":"Hello"}\n\n')
        self.wfile.flush(); time.sleep(0.02)
        self.wfile.write(b'event: delta\ndata: {"text":" from mock"}\n\n')
        self.wfile.flush(); time.sleep(0.02)
        self.wfile.write(b'event: delta\ndata: {"text":" retriever!"}\n\n')
        self.wfile.flush(); time.sleep(0.02)
        self.wfile.write(b'event: done\ndata: {"answer":"Hello from mock retriever!","chunks":[],"cache_hit":false,"retrieval_mode":"hybrid"}\n\n')
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        elif self.path == "/readyz":
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"status":"ready","service_ready":true}')
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b'# HELP mock_metric Mock\n# TYPE mock_metric counter\nmock_metric 1\n')
        else:
            self.send_response(404); self.end_headers()

    def log_message(self, *args): pass

ThreadingHTTPServer(("127.0.0.1", 8001), H).serve_forever()
PY

python3 "$tmpdir/mock_retriever.py" </dev/null &
RET_PID=$!
sleep 0.5
log_pass "Mock retriever started"

# ═══════════════════════════════════════════════════════════════
log_section "CONFIGURE ENVIRONMENT"
# ═══════════════════════════════════════════════════════════════

export SERVICE_NAME="frontend"
export ENV="TEST"
export HOST="$HOST"
export PORT="$APP_PORT"
export FRONTEND_BASE="http://127.0.0.1:$APP_PORT"
export GENERATE_STREAM_URL="http://127.0.0.1:$RETRIEVER_PORT/generate/stream"
export VALKEY_URL="redis://127.0.0.1:$VALKEY_PORT/0"

export SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

export JWT_ALG="ES256"
export JWT_ISS="stateless-openid-auth"
export JWT_AUD="rag-ui"
export JWT_TTL_SECONDS="900"
export JWT_CLOCK_SKEW_SECONDS="90"
export JWT_KID="smoke-test"
export JWT_PRIVATE_KEY_PATH="$tmpdir/jwt.key.pem"

export REQUIRE_AUTH="true"
export DISPLAY_SOURCES_IN_UI="true"
export DISPLAY_TOPK_IN_UI="true"

# ── Use config keys that match rate_limits.py ─────────────────
export RATE_LIMIT_GENERATE_STREAM="4/minute"
export RATE_LIMIT_AUTH_ME="4/minute"
export RATE_LIMIT_AUTH_LOGIN="10/minute"
export RATE_LIMIT_AUTH_START="5/minute"
export RATE_LIMIT_AUTH_CALLBACK="10/minute"
export RATE_LIMIT_AUTH_LOGOUT="10/minute"
export RATE_LIMIT_STREAM_CONCURRENCY="3"

# ═══════════════════════════════════════════════════════════════
log_section "START FRONTEND"
# ═══════════════════════════════════════════════════════════════

uvicorn app:app --host "$HOST" --port "$APP_PORT" </dev/null &
APP_PID=$!

APP_READY=0
for i in $(seq 1 60); do
  if curl -fsS "http://$HOST:$APP_PORT/orchestrator/health" >/dev/null 2>&1; then
    APP_READY=1
    log_pass "Frontend healthy (started in ${i}s)"
    break
  fi
  sleep 0.5
done

if [ "$APP_READY" = "0" ]; then
  log_fail "Frontend failed to start"
  exit 1
fi

# ═══════════════════════════════════════════════════════════════
log_section "1. HEALTH ENDPOINTS"
# ═══════════════════════════════════════════════════════════════

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/orchestrator/health")
assert_json_field "/orchestrator/health status" "$RESP" "status" "ok"
assert_json_field "/orchestrator/health auth_ready" "$RESP" "auth_ready" "true"
assert_json_field "/orchestrator/health upstream" "$RESP" "upstream_client_ready" "true"

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/health")
assert_json_field "/health status" "$RESP" "status" "ok"

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/auth/health")
assert_json_field "/auth/health status" "$RESP" "status" "ok"
assert_contains "/auth/health has jwks_uri" "$RESP" '"jwks_uri"'

# ═══════════════════════════════════════════════════════════════
log_section "2. JWKS ENDPOINT"
# ═══════════════════════════════════════════════════════════════

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/auth/.well-known/jwks.json")
assert_contains "JWKS has keys" "$RESP" '"keys"'
assert_contains "JWKS has kid" "$RESP" '"smoke-test"'
assert_contains "JWKS has EC key" "$RESP" '"kty":"EC"'

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/.well-known/jwks.json")
assert_contains "JWKS alias works" "$RESP" '"keys"'

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/jwks.json")
assert_contains "JWKS /jwks.json works" "$RESP" '"keys"'

# ═══════════════════════════════════════════════════════════════
log_section "3. JWKS RATE LIMITING"
# ═══════════════════════════════════════════════════════════════

docker exec valkey valkey-cli FLUSHDB >/dev/null 2>&1 || true

log_info "Sending 7 rapid JWKS requests..."
JWKS_429=0
for i in $(seq 1 7); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/auth/.well-known/jwks.json")
  if [ "$STATUS" = "429" ]; then
    JWKS_429=1
    log_info "  Request $i → HTTP 429"
    break
  fi
done
if [ "$JWKS_429" = "1" ]; then
  log_pass "JWKS rate limiting works (429 received)"
else
  log_fail "JWKS rate limiting did NOT trigger (JWKS is not rate-limited in current config — this is expected)"
fi

# ═══════════════════════════════════════════════════════════════
log_section "4. JWT TOKEN OPERATIONS"
# ═══════════════════════════════════════════════════════════════

TOKEN="$(python3 - <<'PY'
import os, secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from joserfc import jwk, jwt

pem = Path(os.environ["JWT_PRIVATE_KEY_PATH"]).read_text()
key = jwk.import_key(pem, "EC")
now = datetime.now(timezone.utc)
token = jwt.encode(
    {"alg": "ES256", "kid": os.environ["JWT_KID"]},
    {
        "iss": os.environ["JWT_ISS"],
        "aud": os.environ["JWT_AUD"],
        "sub": "test-user-42",
        "email": "test@example.com",
        "name": "Test User",
        "provider": "test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "jti": secrets.token_urlsafe(16),
    },
    key,
    algorithms=["ES256"],
)
print(token)
PY
)"
log_pass "JWT token minted"

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/auth/me" -H "Authorization: Bearer $TOKEN")
assert_json_field "/auth/me authenticated" "$RESP" "authenticated" "true"
assert_contains "/auth/me has sub" "$RESP" '"test-user-42"'
assert_contains "/auth/me has email" "$RESP" '"test@example.com"'

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/auth/me" -H "Authorization: Bearer invalid")
assert_status "Invalid token rejected" "401" "$STATUS"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/auth/me")
assert_status "Missing token rejected" "401" "$STATUS"

EXPIRED_TOKEN="$(python3 - <<'PY'
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from joserfc import jwk, jwt

pem = Path(os.environ["JWT_PRIVATE_KEY_PATH"]).read_text()
key = jwk.import_key(pem, "EC")
now = datetime.now(timezone.utc)
token = jwt.encode(
    {"alg": "ES256", "kid": os.environ["JWT_KID"]},
    {
        "iss": os.environ["JWT_ISS"],
        "aud": os.environ["JWT_AUD"],
        "sub": "expired-user",
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(hours=1),
    },
    key,
    algorithms=["ES256"],
)
print(token)
PY
)"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/auth/me" -H "Authorization: Bearer $EXPIRED_TOKEN")
assert_status "Expired token rejected" "401" "$STATUS"

# ═══════════════════════════════════════════════════════════════
log_section "5. /auth/me RATE LIMITING"
# ═══════════════════════════════════════════════════════════════

docker exec valkey valkey-cli FLUSHDB >/dev/null 2>&1 || true

log_info "Sending 7 rapid /auth/me requests (limit: 4/min)..."
ME_429=0
for i in $(seq 1 7); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/auth/me" -H "Authorization: Bearer $TOKEN")
  if [ "$STATUS" = "429" ]; then
    ME_429=1
    log_info "  Request $i → HTTP 429"
    break
  fi
done
if [ "$ME_429" = "1" ]; then
  log_pass "/auth/me rate limiting works (429 received)"
else
  log_fail "/auth/me rate limiting did NOT trigger (check Valkey connectivity)"
fi

# ═══════════════════════════════════════════════════════════════
log_section "6. GENERATE STREAM"
# ═══════════════════════════════════════════════════════════════

STREAM_OUT="$(curl -fsS --max-time 10 "http://$HOST:$APP_PORT/generate/stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"query":"test query","top_k":5,"return_chunks":true}')"

assert_contains "Stream has start event" "$STREAM_OUT" "event: start"
assert_contains "Stream has delta events" "$STREAM_OUT" "event: delta"
assert_contains "Stream has done event" "$STREAM_OUT" "event: done"
assert_contains "Stream has answer" "$STREAM_OUT" "Hello from mock retriever"
assert_contains "Stream has retrieval_mode" "$STREAM_OUT" '"retrieval_mode"'

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/generate/stream" \
  -H "Content-Type: application/json" --data '{"query":"test"}')
assert_status "Unauthenticated stream rejected" "401" "$STATUS"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/generate/stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" --data 'not json')
assert_status "Invalid JSON rejected" "400" "$STATUS"

# ═══════════════════════════════════════════════════════════════
log_section "7. STREAM RATE LIMITING"
# ═══════════════════════════════════════════════════════════════

docker exec valkey valkey-cli FLUSHDB >/dev/null 2>&1 || true

log_info "Sending 7 rapid stream requests (limit: 4/min)..."
STREAM_429=0
for i in $(seq 1 7); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://$HOST:$APP_PORT/generate/stream" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" --data '{"query":"test"}')
  if [ "$STATUS" = "429" ]; then
    STREAM_429=1
    log_info "  Request $i → HTTP 429"
    break
  fi
done
if [ "$STREAM_429" = "1" ]; then
  log_pass "Stream rate limiting works (429 received)"
else
  log_fail "Stream rate limiting did NOT trigger"
fi

# ═══════════════════════════════════════════════════════════════
log_section "8. CONCURRENCY"
# ═══════════════════════════════════════════════════════════════

CONCUR_TOKEN="$(python3 - <<'PY'
import os, secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from joserfc import jwk, jwt

pem = Path(os.environ["JWT_PRIVATE_KEY_PATH"]).read_text()
key = jwk.import_key(pem, "EC")
now = datetime.now(timezone.utc)
token = jwt.encode(
    {"alg": "ES256", "kid": os.environ["JWT_KID"]},
    {
        "iss": os.environ["JWT_ISS"],
        "aud": os.environ["JWT_AUD"],
        "sub": f"concurrent-{secrets.token_hex(4)}",
        "email": "concur@test.com",
        "name": "Concur Test",
        "provider": "test",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "jti": secrets.token_urlsafe(16),
    },
    key,
    algorithms=["ES256"],
)
print(token)
PY
)"

docker exec valkey valkey-cli FLUSHDB >/dev/null 2>&1 || true

log_info "Sending 6 concurrent stream requests (semaphore limit: 3)..."
CONCUR_DIR="$tmpdir/concurrent"
mkdir -p "$CONCUR_DIR"

for i in $(seq 1 6); do
  curl -s --max-time 15 -o "$CONCUR_DIR/out_$i.txt" -w "%{http_code}" \
    "http://$HOST:$APP_PORT/generate/stream" \
    -H "Authorization: Bearer $CONCUR_TOKEN" \
    -H "Content-Type: application/json" \
    --data '{"query":"concurrent"}' &
done

WAIT_COUNT=0
while [ "$(jobs -r | wc -l)" -gt 0 ] && [ "$WAIT_COUNT" -lt 30 ]; do
  sleep 0.5
  WAIT_COUNT=$((WAIT_COUNT + 1))
done

jobs -p | xargs -r kill 2>/dev/null || true
wait 2>/dev/null || true

SUCCESS=0
for i in $(seq 1 6); do
  if [ -f "$CONCUR_DIR/out_$i.txt" ]; then
    CODE=$(tail -c 4 "$CONCUR_DIR/out_$i.txt" 2>/dev/null || echo "000")
    if [ "$CODE" = "200" ]; then
      SUCCESS=$((SUCCESS+1))
    fi
  fi
done
log_info "Completed: $SUCCESS/6 streams"
if [ "$SUCCESS" -ge 3 ]; then
  log_pass "Concurrent streams ok ($SUCCESS/6)"
else
  log_fail "Concurrent streams low ($SUCCESS/6)"
fi

# ═══════════════════════════════════════════════════════════════
log_section "9. PROMETHEUS METRICS"
# ═══════════════════════════════════════════════════════════════

METRICS=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/metrics")

for metric in \
  frontend_requests_total \
  frontend_request_latency_seconds \
  frontend_active_requests \
  frontend_auth_events_total \
  frontend_rate_limit_events_total \
  frontend_upstream_stream_errors_total \
  frontend_jwks_requests_total \
  frontend_service_ready
do
  assert_contains "Metric: $metric" "$METRICS" "$metric"
done

for route in \
  '/orchestrator/health' \
  '/auth/me' \
  '/generate/stream' \
  '/auth/.well-known/jwks.json' \
  '/metrics'
do
  assert_contains "Route label: $route" "$METRICS" "$route"
done

assert_contains "Service ready = 1" "$METRICS" 'frontend_service_ready{service="frontend",environment="TEST"} 1.0'

# ═══════════════════════════════════════════════════════════════
log_section "10. UI ENDPOINTS"
# ═══════════════════════════════════════════════════════════════

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/")
assert_contains "Index has RAG UI" "$RESP" "RAG UI"
assert_contains "Index has JS" "$RESP" "validateAuth"

RESP=$(curl -fsS --max-time 5 "http://$HOST:$APP_PORT/auth/login")
assert_contains "Login page loads" "$RESP" "Sign in"

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/auth/logout")
assert_status "Logout returns 200" "200" "$STATUS"

# ═══════════════════════════════════════════════════════════════
log_section "11. EDGE CASES"
# ═══════════════════════════════════════════════════════════════

STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST:$APP_PORT/nonexistent")
assert_status "404 for unknown route" "404" "$STATUS"

RESP=$(curl -fsS --max-time 10 "http://$HOST:$APP_PORT/generate/stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"query":"<script>alert(1)</script>"}')
assert_contains "XSS-safe stream" "$RESP" "event: done"

# ═══════════════════════════════════════════════════════════════
log_section "RESULTS"
# ═══════════════════════════════════════════════════════════════

echo ""
echo -e "  ${GREEN}Passed: $PASS${NC}"
echo -e "  ${RED}Failed: $FAIL${NC}"
echo ""

if [ "$FAIL" -eq 0 ]; then
  echo -e "${GREEN}═══ ALL TESTS PASSED ✓ ═══${NC}"
  exit 0
else
  echo -e "${RED}═══ $FAIL TEST(S) FAILED ✗ ═══${NC}"
  exit 1
fi