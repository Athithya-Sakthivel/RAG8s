#!/usr/bin/env bash
set -euo pipefail

# End-to-end test for reranker service:
# build -> run -> readyz -> rerank -> metrics -> trivy scan
# Usage:
#   cd src/infra/services/reranker
#   TEST_MODE=cpu RERANKER_MODEL_NAME="Xenova/ms-marco-MiniLM-L-6-v2" RERANKER_MAX_DOCS=50 ./test_reranker.sh

MODE="${TEST_MODE:-cpu}"
FASTEMBED_GPU_ARG=0
case "${MODE,,}" in
  gpu) FASTEMBED_GPU_ARG=1 ;;
  cpu) FASTEMBED_GPU_ARG=0 ;;
  *) FASTEMBED_GPU_ARG=0 ;;
esac

RERANKER_MODEL_NAME="${RERANKER_MODEL_NAME:-Xenova/ms-marco-MiniLM-L-6-v2}"
RERANKER_MAX_DOCS="${RERANKER_MAX_DOCS:-50}"

IMAGE_TAG="${RERANKER_IMAGE_TAG:-${IMAGE_TAG:-test}}"
IMAGE_REPO="${IMAGE_REPO:-reranker}"
IMAGE_LOCAL="${IMAGE_REPO}:${IMAGE_TAG}"
CONTAINER_NAME="${CONTAINER_NAME:-test-reranker-${MODE}}"
HOST_PORT="${HOST_PORT:-9023}"
CONTAINER_PORT="${CONTAINER_PORT:-8202}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
SLEEP_BETWEEN_TRIES=1

if ! printf '%s' "${RERANKER_MAX_DOCS}" | grep -Eq '^[0-9]+$'; then
  echo "[ERROR] RERANKER_MAX_DOCS must be an integer" >&2
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

echo "[INFO] Ensure you are in the reranker service directory (src/infra/services/reranker)"
if [ ! -f Dockerfile ] || [ ! -f host_reranker.py ]; then
  if [ -d "src/infra/services/reranker" ]; then
    cd src/infra/services/reranker
  else
    echo "[ERROR] Cannot find Dockerfile or host_reranker.py and src/infra/services/reranker does not exist" >&2
    exit 1
  fi
fi

echo "[1/5] Building image ${IMAGE_LOCAL} (model=${RERANKER_MODEL_NAME}, max_docs=${RERANKER_MAX_DOCS}, gpu=${FASTEMBED_GPU_ARG})"
docker build \
  --platform "${LOCAL_PLATFORM}" \
  --build-arg FASTEMBED_GPU="${FASTEMBED_GPU_ARG}" \
  --build-arg RERANKER_MODEL_NAME="${RERANKER_MODEL_NAME}" \
  --build-arg RERANKER_MAX_DOCS="${RERANKER_MAX_DOCS}" \
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

RERANK_PAYLOAD='{"query":"best embedding service","documents":["dense embeddings are fast","rerankers improve ranking quality","spare text unrelated to search"]}'
echo "[4/5] POST /rerank"
resp=$(curl -fsS -X POST "http://127.0.0.1:${HOST_PORT}/rerank" -H "Content-Type: application/json" -d "${RERANK_PAYLOAD}") || {
  echo "[ERROR] Rerank POST failed" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 5
}

if command -v jq >/dev/null 2>&1; then
  echo "${resp}" | jq .
  SCORES_LEN=$(echo "${resp}" | jq -r '.scores | length')
else
  SCORES_LEN=$(printf '%s' "${resp}" | python3 - <<'PY'
import json, sys
j = json.load(sys.stdin)
scores = j.get("scores", [])
print(len(scores))
PY
)
fi

if [ -z "${SCORES_LEN}" ]; then
  echo "[ERROR] Failed to parse rerank response" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 6
fi

if [ "${SCORES_LEN}" -ne 3 ]; then
  echo "[ERROR] Expected 3 scores, got ${SCORES_LEN}" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 7
fi

metrics=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/metrics" 2>/dev/null || true)
if [ -z "${metrics}" ]; then
  echo "[WARN] /metrics returned empty or failed"
else
  if printf '%s' "${metrics}" | grep -Eq '(^|[[:space:]])reranker_requests_total|reranker_request_duration_seconds'; then
    echo "[OK] Prometheus metrics present (sample):"
    printf '%s\n' "${metrics}" | sed -n '1,120p'
  else
    echo "[WARN] /metrics returned but expected metrics not found; sample:"
    printf '%s\n' "${metrics}" | sed -n '1,120p'
  fi
fi

TRIVY_IMAGE="${TRIVY_IMAGE:-ghcr.io/athithya-sakthivel/trivy:0.69.3-safe@sha256:bcc376de8d77cfe086a917230e818dc9f8528e3c852f7b1aff648949b6258d1c}"
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