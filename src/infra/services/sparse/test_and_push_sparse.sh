#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-cpu}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_CONTEXT_DIR="${BUILD_CONTEXT_DIR:-${SCRIPT_DIR}}"
IMAGE_REPO="${IMAGE_REPO:-sparse}"
IMAGE_TAG="${SPARSE_IMAGE_TAG:-v1}"
ACR_NAME="${ACR_NAME:-}"
ACR_REPO_PREFIX="${ACR_REPO_PREFIX:-rag}"
AZURE_REGISTRY_RAW="${AZURE_REGISTRY:-true}"
DOCKER_USERNAME="${DOCKER_USERNAME:-}"
DOCKER_PASSWORD="${DOCKER_PASSWORD:-}"
HOST_PORT="${HOST_PORT:-9022}"
CONTAINER_PORT="${CONTAINER_PORT:-8201}"
CONTAINER_NAME="${CONTAINER_NAME:-test-sparse-${MODE}}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
SLEEP_BETWEEN_TRIES=1
RETRY_ATTEMPTS="${RETRY_ATTEMPTS:-3}"
RETRY_BACKOFF="${RETRY_BACKOFF:-2}"

FASTEMBED_GPU_ARG="0"
case "${MODE,,}" in
  gpu) FASTEMBED_GPU_ARG="1" ;;
  cpu) FASTEMBED_GPU_ARG="0" ;;
  *) FASTEMBED_GPU_ARG="0"; printf '%s\n' "[WARN] unknown MODE '${MODE}', falling back to 'cpu'" >&2 ;;
esac

normalize_bool(){
  local v="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "${v}" in 1|true|yes|y) printf '%s' "true" ;; 0|false|no|n|'') printf '%s' "false" ;; *) printf '%s' "false" ;; esac
}
AZURE_REGISTRY="$(normalize_bool "${AZURE_REGISTRY_RAW}")"

log(){ printf '\033[0;34m[INFO]\033[0m %s\n' "$*"; }
warn(){ printf '\033[0;33m[WARN]\033[0m %s\n' "$*" >&2; }
err(){ printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; }

cleanup_container(){ set +e; if docker ps -a --format '{{.Names}}' | grep -xq "${CONTAINER_NAME}"; then docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true; fi; set -e; }
trap 'cleanup_container' EXIT

retry_cmd(){
  local attempts=0 rc=0
  while :; do
    attempts=$((attempts+1))
    "$@" && { rc=0; break; } || rc=$?
    if [ "$attempts" -ge "$RETRY_ATTEMPTS" ]; then break; fi
    sleep $((RETRY_BACKOFF ** (attempts - 1)))
  done
  return $rc
}

wait_for_health(){
  local host_port="$1" path="$2" timeout="$3"
  local start now body
  start=$(date +%s)
  while true; do
    body=$(curl -fsS --max-time 2 "http://127.0.0.1:${host_port}${path}" 2>/dev/null || true)
    if [ -n "${body}" ]; then
      if printf '%s' "${body}" | grep -q '"status"' && printf '%s' "${body}" | grep -q '"ok"'; then
        if printf '%s' "${body}" | grep -q '"ready"\s*:\s*true' || ! printf '%s' "${body}" | grep -q '"ready"'; then
          if command -v jq >/dev/null 2>&1; then printf '%s\n' "${body}" | jq .; else printf '%s\n' "${body}"; fi
          return 0
        fi
      fi
    fi
    now=$(date +%s)
    if [ $((now-start)) -ge "${timeout}" ]; then
      printf '%s\n' "${body:-<no-body>}" | sed -n '1,200p' || true
      return 1
    fi
    sleep "${SLEEP_BETWEEN_TRIES}"
  done
}

if ! command -v docker >/dev/null 2>&1; then err "docker CLI not found"; exit 2; fi

log "Local arch: $(uname -m || true)"
case "$(uname -m || true)" in
  x86_64|amd64) LOCAL_PLATFORM="linux/amd64" ;;
  aarch64|arm64) LOCAL_PLATFORM="linux/arm64" ;;
  *) LOCAL_PLATFORM="linux/amd64"; warn "unknown local arch, assuming amd64" ;;
esac

# Required: enforce SPARSE_MODEL_NAME provided at build time
: "${SPARSE_MODEL_NAME:?SPARSE_MODEL_NAME must be set — e.g. Qdrant/minicoil-v1}"

log "Building image ${IMAGE_REPO}:${IMAGE_TAG} (model=${SPARSE_MODEL_NAME}, gpu=${FASTEMBED_GPU_ARG})"

docker build \
  --platform "${LOCAL_PLATFORM}" \
  --build-arg FASTEMBED_GPU="${FASTEMBED_GPU_ARG}" \
  --build-arg SPARSE_MODEL_NAME="${SPARSE_MODEL_NAME}" \
  -t "${IMAGE_REPO}:${IMAGE_TAG}" \
  "${BUILD_CONTEXT_DIR}" \
  || { err "docker build failed"; exit 3; }

cleanup_container
log "Running container ${CONTAINER_NAME} (mapping ${HOST_PORT}->${CONTAINER_PORT})"
docker run --name "${CONTAINER_NAME}" -d -p "${HOST_PORT}:${CONTAINER_PORT}" --shm-size=1.8g "${IMAGE_REPO}:${IMAGE_TAG}" >/dev/null

if ! wait_for_health "${HOST_PORT}" "/health" "${WAIT_TIMEOUT}"; then
  err "Health check failed"
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  exit 4
fi

EMBED_PAYLOAD='{"texts":["hello from test_and_push_sparse"]}'
resp=$(printf '%s' "${EMBED_PAYLOAD}" | curl -fsS -X POST "http://127.0.0.1:${HOST_PORT}/embed" -H "Content-Type: application/json" -d @- ) || { err "Embed POST failed"; docker logs --tail 200 "${CONTAINER_NAME}" || true; docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true; exit 5; }

if command -v jq >/dev/null 2>&1; then printf '%s\n' "${resp}" | jq .; else printf '%s\n' "${resp}"; fi

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

# Push logic (ACR or Docker Hub)
if [ "${AZURE_REGISTRY}" = "true" ]; then
  if ! command -v az >/dev/null 2>&1; then err "az CLI required for ACR push"; exit 6; fi
  if [ -z "${ACR_NAME}" ]; then err "AZURE_REGISTRY=true requires ACR_NAME"; exit 7; fi
  if ! printf '%s' "${ACR_NAME}" | grep -Eq '^[a-z0-9]{5,50}$'; then err "ACR_NAME must be 5-50 lowercase letters/numbers"; exit 8; fi
  log "Resolving ACR login server"
  ACR_LOGIN_SERVER="$(az acr show -n "${ACR_NAME}" --query loginServer -o tsv 2>/dev/null || true)"
  if [ -z "${ACR_LOGIN_SERVER}" ]; then err "Unable to resolve ACR '${ACR_NAME}'"; exit 9; fi
  log "Logging into ACR ${ACR_NAME}"
  retry_cmd az acr login --name "${ACR_NAME}" || { err "az acr login failed"; exit 10; }
  TARGET_IMAGE="${ACR_LOGIN_SERVER}/${ACR_REPO_PREFIX}/${IMAGE_REPO}:${IMAGE_TAG}"
  log "Tagging ${IMAGE_REPO}:${IMAGE_TAG} -> ${TARGET_IMAGE}"
  docker tag "${IMAGE_REPO}:${IMAGE_TAG}" "${TARGET_IMAGE}"
  log "Pushing ${TARGET_IMAGE}"
  if retry_cmd docker push "${TARGET_IMAGE}"; then log "Push complete: ${TARGET_IMAGE}"; exit 0; else err "docker push to ACR failed"; exit 11; fi
else
  if [ -z "${DOCKER_USERNAME}" ]; then log "DOCKER_USERNAME not set — skipping remote push"; exit 0; fi
  TARGET_IMAGE="${DOCKER_USERNAME}/${IMAGE_REPO}:${IMAGE_TAG}"
  if [ -n "${DOCKER_PASSWORD}" ]; then log "Logging into Docker registry"; printf '%s\n' "${DOCKER_PASSWORD}" | retry_cmd docker login -u "${DOCKER_USERNAME}" --password-stdin || { err "Docker login failed"; exit 12; }; else warn "DOCKER_PASSWORD not provided; attempting push may fail"; fi
  log "Tagging ${IMAGE_REPO}:${IMAGE_TAG} -> ${TARGET_IMAGE}"
  docker tag "${IMAGE_REPO}:${IMAGE_TAG}" "${TARGET_IMAGE}"
  log "Pushing ${TARGET_IMAGE}"
  if retry_cmd docker push "${TARGET_IMAGE}"; then log "Push complete: ${TARGET_IMAGE}"; exit 0; else err "docker push failed"; exit 13; fi
fi
