#!/usr/bin/env bash
set -euo pipefail

# End-to-end test for sparse embedding service:
# build -> run -> readyz -> embed -> metrics -> trivy scan
# Usage (from repo root):
#   cd src/infra/services/sparse
#   TEST_MODE=cpu SPARSE_MODEL_NAME="prithivida/Splade_PP_en_v1" SPARSE_BATCH_SIZE=8 ./test_sparse.sh

MODE="${TEST_MODE:-cpu}"
FASTEMBED_GPU_ARG=0
case "${MODE,,}" in
  gpu) FASTEMBED_GPU_ARG=1 ;;
  cpu) FASTEMBED_GPU_ARG=0 ;;
  *) FASTEMBED_GPU_ARG=0 ;;
esac

SPARSE_MODEL_NAME="${SPARSE_MODEL_NAME:-prithivida/Splade_PP_en_v1}"
SPARSE_BATCH_SIZE="${SPARSE_BATCH_SIZE:-8}"

IMAGE_TAG="${SPARSE_IMAGE_TAG:-${IMAGE_TAG:-test}}"
IMAGE_REPO="${IMAGE_REPO:-sparse}"
IMAGE_LOCAL="${IMAGE_REPO}:${IMAGE_TAG}"
CONTAINER_NAME="${CONTAINER_NAME:-test-sparse-${MODE}}"
HOST_PORT="${HOST_PORT:-9022}"
CONTAINER_PORT="${CONTAINER_PORT:-8201}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
SLEEP_BETWEEN_TRIES=1

if ! printf '%s' "${SPARSE_BATCH_SIZE}" | grep -Eq '^[0-9]+$'; then
  echo "[ERROR] SPARSE_BATCH_SIZE must be an integer" >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "[ERROR] docker CLI not found" >&2; exit 2; }

case "$(uname -m)" in
  x86_64|amd64) LOCAL_PLATFORM="linux/amd64" ;;
  aarch64|arm64) LOCAL_PLATFORM="linux/arm64" ;;
  *) LOCAL_PLATFORM="linux/amd64" ;;
esac

cleanup() {
  set +e
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  set -e
}
trap cleanup EXIT

echo "[INFO] Ensure you are in the sparse service directory (src/infra/services/sparse)"
if [ ! -f Dockerfile ] || [ ! -f host_sparse.py ]; then
  if [ -d "src/infra/services/sparse" ]; then
    cd src/infra/services/sparse
  else
    echo "[ERROR] Cannot find Dockerfile or host_sparse.py and src/infra/services/sparse does not exist" >&2
    exit 1
  fi
fi

echo "[1/5] Building image ${IMAGE_LOCAL} (model=${SPARSE_MODEL_NAME}, batch=${SPARSE_BATCH_SIZE}, gpu=${FASTEMBED_GPU_ARG})"
docker build \
  --platform "${LOCAL_PLATFORM}" \
  --build-arg FASTEMBED_GPU="${FASTEMBED_GPU_ARG}" \
  --build-arg SPARSE_MODEL_NAME="${SPARSE_MODEL_NAME}" \
  --build-arg SPARSE_BATCH_SIZE="${SPARSE_BATCH_SIZE}" \
  -t "${IMAGE_LOCAL}" . || { echo "[ERROR] docker build failed" >&2; exit 3; }

echo "[2/5] Starting container ${CONTAINER_NAME}"
cleanup
docker run --name "${CONTAINER_NAME}" -d -p "${HOST_PORT}:${CONTAINER_PORT}" --shm-size=1.8g "${IMAGE_LOCAL}" >/dev/null

wait_for_ready() {
  local port=$1 timeout=$2 start body
  start=$(date +%s)
  while :; do
    body=$(curl -fsS --max-time 2 "http://127.0.0.1:${port}/readyz" 2>/dev/null || true)
    if [ -n "${body}" ]; then
      printf '%s\n' "${body}"
      return 0
    fi

    body=$(curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null || true)
    if [ -n "${body}" ]; then
      if printf '%s' "${body}" | grep -q '"status"'; then
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

EMBED_PAYLOAD='{"texts":["hello from sparse test script"]}'
echo "[4/5] POST /embed"
resp=$(curl -fsS -X POST "http://127.0.0.1:${HOST_PORT}/embed" -H "Content-Type: application/json" -d "${EMBED_PAYLOAD}") || {
  echo "[ERROR] Embed POST failed" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 5
}

if command -v jq >/dev/null 2>&1; then
  echo "${resp}" | jq .
  VECTORS_LEN=$(echo "${resp}" | jq -r '.vectors | length')
  INDICES_LEN=$(echo "${resp}" | jq -r '.vectors[0].indices | length')
  VALUES_LEN=$(echo "${resp}" | jq -r '.vectors[0].values | length')
else
  read -r VECTORS_LEN INDICES_LEN VALUES_LEN <<EOF
$(printf '%s' "${resp}" | python3 -c '
import json, sys
j = json.load(sys.stdin)
vectors = j.get("vectors", [])
if not vectors:
    print("0 0 0")
else:
    first = vectors[0]
    indices = first.get("indices", [])
    values = first.get("values", [])
    print(len(vectors), len(indices), len(values))
')
EOF
fi

if [ -z "${VECTORS_LEN}" ] || [ -z "${INDICES_LEN}" ] || [ -z "${VALUES_LEN}" ]; then
  echo "[ERROR] Failed to parse sparse response" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 6
fi

if [ "${VECTORS_LEN}" -ne 1 ]; then
  echo "[ERROR] Expected exactly 1 vector, got ${VECTORS_LEN}" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 7
fi

if [ "${INDICES_LEN}" -le 0 ] || [ "${VALUES_LEN}" -le 0 ]; then
  echo "[ERROR] Sparse embedding unexpectedly empty" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 8
fi

if [ "${INDICES_LEN}" -ne "${VALUES_LEN}" ]; then
  echo "[ERROR] indices/value length mismatch: ${INDICES_LEN} != ${VALUES_LEN}" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 9
fi

# ---- Metrics check ----
metrics=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/metrics" 2>/dev/null || true)
if [ -z "${metrics}" ]; then
  echo "[WARN] /metrics returned empty or failed"
else
  if printf '%s' "${metrics}" | grep -Eq '(^|[[:space:]])sparse_requests_total|sparse_request_duration_seconds'; then
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
    exit 10
  }

echo "[SUCCESS] All checks passed. Cleaning up."
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
exit 0