#!/usr/bin/env bash
set -eu -o pipefail

APP_DIR="${APP_DIR:-/workspace/src/services/frontend}"
HOST="127.0.0.1"
APP_PORT="8000"
RETRIEVER_PORT="8001"
VALKEY_PORT="6379"

tmpdir="$(mktemp -d)"
cleanup() {
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "${RET_PID:-}" ] && kill "$RET_PID" 2>/dev/null || true
  docker rm -f valkey 2>/dev/null || true
  rm -rf "$tmpdir"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmpdir"

# Generate EC key (output is written to file; openssl prints nothing on success)
openssl ecparam -name prime256v1 -genkey -noout -out "$tmpdir/jwt.key.pem"

# Start Valkey (Docker only, no local binary dependency)
# Run detached and non-interactive; --rm ensures container is removed when it exits
docker rm -f valkey 2>/dev/null || true
docker run --rm -d --name valkey -p "${VALKEY_PORT}:6379" valkey/valkey:latest </dev/null &

# Wait until Valkey is ready
for _ in $(seq 1 40); do
  if docker exec valkey valkey-cli ping >/dev/null 2>&1; then
    echo "valkey ready"
    break
  fi
  sleep 0.25
done

cat > "$tmpdir/mock_retriever.py" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if self.path.rstrip("/") != "/generate/stream":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        _ = self.rfile.read(length) if length else b""

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        self.wfile.write(b'{"type":"token","text":"mock-"}\n')
        self.wfile.flush()
        time.sleep(0.1)
        self.wfile.write(b'{"type":"token","text":"ok"}\n')
        self.wfile.write(b'{"type":"done"}\n')
        self.wfile.flush()

    def log_message(self, *args):
        pass

ThreadingHTTPServer(("127.0.0.1", 8001), H).serve_forever()
PY

# Start mock retriever in background with stdin closed so it cannot read from terminal
python "$tmpdir/mock_retriever.py" </dev/null &
RET_PID=$!

export SERVICE_NAME="frontend"
export ENV="TEST"
export HOST="$HOST"
export PORT="$APP_PORT"
export FRONTEND_BASE="http://127.0.0.1:$APP_PORT"
export GENERATE_STREAM_URL="http://127.0.0.1:$RETRIEVER_PORT/generate/stream"
export VALKEY_URL="redis://127.0.0.1:$VALKEY_PORT/0"

export SESSION_SECRET="$(python - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"

export JWT_ALG="ES256"
export JWT_ISS="stateless-openid-auth"
export JWT_AUD="rag-ui"
export JWT_TTL_SECONDS="900"
export JWT_CLOCK_SKEW_SECONDS="90"
export JWT_KID="smoke-test"
export JWT_PRIVATE_KEY_PATH="$tmpdir/jwt.key.pem"

export REQUIRE_AUTH="true"
export DISPLAY_SOURCES_IN_UI="false"
export DISPLAY_TOPK_IN_UI="false"

export RATE_LIMIT_AUTH_LOGIN="20/minute"
export RATE_LIMIT_AUTH_START="20/minute"
export RATE_LIMIT_AUTH_CALLBACK="20/minute"
export RATE_LIMIT_AUTH_ME="60/minute"
export RATE_LIMIT_AUTH_LOGOUT="20/minute"
export RATE_LIMIT_JWKS="120/minute"
export RATE_LIMIT_GENERATE_STREAM_AUTH="30/minute"
export RATE_LIMIT_GENERATE_STREAM_ANON="5/minute"
export RATE_LIMIT_GENERATE_STREAM_CONCURRENCY="2"

cd "$APP_DIR"

# Start the app in background; close stdin so it cannot prompt
uvicorn app:app --host "$HOST" --port "$APP_PORT" </dev/null &
APP_PID=$!

# Wait for app to become healthy
for _ in $(seq 1 80); do
  if curl -fsS "http://$HOST:$APP_PORT/orchestrator/health" >/dev/null 2>&1; then
    echo "app healthy"
    break
  fi
  sleep 0.25
done

# Verify endpoints (fail the script on error)
curl -fsS "http://$HOST:$APP_PORT/orchestrator/health"
curl -fsS "http://$HOST:$APP_PORT/auth/.well-known/jwks.json"

# Create JWT token
TOKEN="$(python - <<'PY'
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
        "sub": "smoke-user",
        "iat": now,
        "exp": now + timedelta(minutes=10),
    },
    key,
    algorithms=["ES256"],
)
print(token)
PY
)"

# Validate auth endpoint
curl -fsS "http://$HOST:$APP_PORT/auth/me" \
  -H "Authorization: Bearer $TOKEN"

# Call generate/stream and capture output
STREAM_OUT="$(curl -fsS "http://$HOST:$APP_PORT/generate/stream" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"query":"smoke"}')"

printf '%s\n' "$STREAM_OUT" | grep -q 'mock-ok'
printf '%s\n' "$STREAM_OUT" | grep -q '"type":"done"'

echo "e2e smoke test passed"
