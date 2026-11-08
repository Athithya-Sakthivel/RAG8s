#!/usr/bin/env bash
set -euo pipefail

# Minimal end-to-end test: build image, run container, verify ready/health, embed endpoint, metrics, and run Trivy scan.
# Usage (from repo root):
#   cd src/infra/services/dense
#   TEST_MODE=cpu DENSE_MODEL_NAME="BAAI/bge-small-en-v1.5" DENSE_DIM=384 ./test_dense.sh

# Configurable environment
MODE="${TEST_MODE:-cpu}"
FASTEMBED_GPU_ARG=0
case "${MODE,,}" in
  gpu) FASTEMBED_GPU_ARG=1 ;;
  cpu) FASTEMBED_GPU_ARG=0 ;;
  *) FASTEMBED_GPU_ARG=0 ;;
esac

TEST_MODE=cpu DENSE_MODEL_NAME="BAAI/bge-small-en-v1.5"
DENSE_DIM=384

IMAGE_TAG="${DENSE_IMAGE_TAG:-test}"
IMAGE_REPO="${IMAGE_REPO:-dense}"
IMAGE_LOCAL="${IMAGE_REPO}:${IMAGE_TAG}"
CONTAINER_NAME="${CONTAINER_NAME:-test-dense-${MODE}}"
HOST_PORT="${HOST_PORT:-9021}"
CONTAINER_PORT="${CONTAINER_PORT:-8200}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
SLEEP_BETWEEN_TRIES=1

: "${DENSE_MODEL_NAME:?DENSE_MODEL_NAME must be set}"
: "${DENSE_DIM:?DENSE_DIM must be set}"
if ! printf '%s' "${DENSE_DIM}" | grep -Eq '^[0-9]+$'; then
  echo "[ERROR] DENSE_DIM must be an integer" >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "[ERROR] docker CLI not found" >&2; exit 2; }

case "$(uname -m)" in
  x86_64|amd64) LOCAL_PLATFORM="linux/amd64" ;;
  aarch64|arm64) LOCAL_PLATFORM="linux/arm64" ;;
  *) LOCAL_PLATFORM="linux/amd64" ;;
esac

cleanup(){ set +e; docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true; set -e; }
trap cleanup EXIT

echo "[INFO] Ensure you are in the dense service directory (src/infra/services/dense)"
# If script invoked from repo root, change into service dir so docker build context '.' works.
if [ ! -f Dockerfile ] || [ ! -f host_dense.py ]; then
  if [ -d "src/infra/services/dense" ]; then
    cd src/infra/services/dense
  else
    echo "[ERROR] Cannot find Dockerfile or host_dense.py and src/infra/services/dense does not exist" >&2
    exit 1
  fi
fi

echo "[1/5] Building image ${IMAGE_LOCAL} (model=${DENSE_MODEL_NAME}, dim=${DENSE_DIM}, gpu=${FASTEMBED_GPU_ARG})"
docker build \
  --platform "${LOCAL_PLATFORM}" \
  --build-arg FASTEMBED_GPU="${FASTEMBED_GPU_ARG}" \
  --build-arg DENSE_MODEL_NAME="${DENSE_MODEL_NAME}" \
  --build-arg DENSE_DIM="${DENSE_DIM}" \
  -t "${IMAGE_LOCAL}" . || { echo "[ERROR] docker build failed" >&2; exit 3; }

echo "[2/5] Starting container ${CONTAINER_NAME}"
cleanup
docker run --name "${CONTAINER_NAME}" -d -p "${HOST_PORT}:${CONTAINER_PORT}" --shm-size=1.8g "${IMAGE_LOCAL}" >/dev/null

wait_for_ready(){
  local port=$1 timeout=$2 start body
  start=$(date +%s)
  while :; do
    # Prefer /readyz for readiness; fall back to /health if readyz not present
    body=$(curl -fsS --max-time 2 "http://127.0.0.1:${port}/readyz" 2>/dev/null || true)
    if [ -n "${body}" ]; then
      printf '%s\n' "${body}"
      return 0
    fi
    body=$(curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null || true)
    if [ -n "${body}" ]; then
      if printf '%s' "${body}" | grep -q '"status"' && printf '%s' "${body}" | grep -q '"ok"'; then
        printf '%s\n' "${body}"
        return 0
      fi
    fi
    if [ $(( $(date +%s) - start )) -ge "${timeout}" ]; then
      printf '%s\n' "${body:-<no-body>}" | sed -n '1,200p' || true
      return 1
    fi
    sleep "${SLEEP_BETWEEN_TRIES}"
  done
}

echo "[3/5] Waiting for readiness/health (timeout ${WAIT_TIMEOUT}s)"
if ! wait_for_ready "${HOST_PORT}" "${WAIT_TIMEOUT}"; then
  echo "[ERROR] Readiness/health check failed; container logs:" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 4
fi

EMBED_PAYLOAD='{"texts":["hello from test script"]}'
echo "[4/5] POST /embed"
resp=$(curl -fsS -X POST "http://127.0.0.1:${HOST_PORT}/embed" -H "Content-Type: application/json" -d "${EMBED_PAYLOAD}" ) || { echo "[ERROR] Embed POST failed"; docker logs --tail 200 "${CONTAINER_NAME}" || true; exit 5; }

if command -v jq >/dev/null 2>&1; then
  echo "${resp}" | jq .
  VEC_LEN=$(echo "${resp}" | jq -r '.vectors[0] | length')
else
  echo "${resp}"
  VEC_LEN=$(python3 - <<PY 2>/dev/null
import sys,json
j=json.load(sys.stdin)
v=j.get("vectors",[[]])
print(len(v[0]) if v and v[0] else "")
PY
<<<"${resp}")
fi

if [ -z "${VEC_LEN}" ]; then
  echo "[ERROR] Failed to parse vector length" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 6
fi

HEALTH_DIM=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/health" 2>/dev/null | (jq -r '.dim // empty' 2>/dev/null || python3 -c "import sys,json;print(json.load(sys.stdin).get('dim',''))") ) || true
HEALTH_DIM="${HEALTH_DIM:-}"

if [ -n "${HEALTH_DIM}" ] && [ "${HEALTH_DIM}" != "null" ]; then
  if [ "${VEC_LEN}" -ne "${HEALTH_DIM}" ]; then
    echo "[ERROR] Vector length mismatch: ${VEC_LEN} != ${HEALTH_DIM}" >&2
    docker logs --tail 200 "${CONTAINER_NAME}" || true
    exit 7
  fi
fi

metrics=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/metrics" 2>/dev/null || true)
if [ -z "${metrics}" ]; then
  echo "[WARN] /metrics returned empty or failed"
else
  if printf '%s' "${metrics}" | grep -Eq '(^|[[:space:]])dense_requests_total|dense_request_duration_seconds'; then
    echo "[OK] Prometheus metrics present (sample):"
    printf '%s\n' "${metrics}" | sed -n '1,120p'
  else
    echo "[WARN] /metrics returned but expected metrics not found; sample:"
    printf '%s\n' "${metrics}" | sed -n '1,120p'
  fi
fi

# Optional Trivy scan
TRIVY_IMAGE="${TRIVY_IMAGE:-ghcr.io/athithya-sakthivel/trivy:0.69.3-safe}"
TRIVY_CACHE_DIR="${TRIVY_CACHE_DIR:-$PWD/.trivy-cache}"
echo "[5/5] Scanning image ${IMAGE_LOCAL} with Trivy (CRITICAL severity will fail)"
mkdir -p "${TRIVY_CACHE_DIR}"
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${TRIVY_CACHE_DIR}:/root/.cache/trivy" \
  -v "$PWD:/workspace" \
  -w /workspace \
  "${TRIVY_IMAGE}" \
  image \
  --cache-dir /root/.cache/trivy \
  --scanners vuln \
  --severity CRITICAL \
  --exit-code 1 \
  "${IMAGE_LOCAL}" || {
    echo "[ERROR] Trivy scan failed (CRITICAL vulnerabilities found or scan error)" >&2
    docker logs --tail 200 "${CONTAINER_NAME}" || true
    exit 8
  }

echo "[SUCCESS] All checks passed. Cleaning up."
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
exit 0