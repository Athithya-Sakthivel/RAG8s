#!/usr/bin/env bash

IMAGE_TAG="${IMAGE_TAG:-${RETRIEVER_IMAGE_TAG:-test}}"
IMAGE_REPO="${IMAGE_REPO:-retriever}"
IMAGE_LOCAL="${IMAGE_REPO}:${IMAGE_TAG}"

APP_CONTAINER="${CONTAINER_NAME:-test-retriever-otel}"
COLLECTOR_CONTAINER="${COLLECTOR_NAME:-test-retriever-otel-collector}"
NETWORK="${NETWORK_NAME:-test-retriever-otel-net}"

HOST_PORT="${HOST_PORT:-9023}"
CONTAINER_PORT="${CONTAINER_PORT:-8001}"

WAIT_TIMEOUT="${WAIT_TIMEOUT:-90}"
SLEEP_BETWEEN_TRIES="${SLEEP_BETWEEN_TRIES:-1}"

COLLECTOR_IMAGE="${COLLECTOR_IMAGE:-otel/opentelemetry-collector-contrib@sha256:a516c26968aa1feb5e5fc0562e3338ea13755cb4f373603226bcc4e276374ad0}"

command -v docker >/dev/null 2>&1 || { echo "[ERROR] docker CLI not found" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl CLI not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 CLI not found" >&2; exit 2; }

case "$(uname -m)" in
  x86_64|amd64) LOCAL_PLATFORM="linux/amd64" ;;
  aarch64|arm64) LOCAL_PLATFORM="linux/arm64" ;;
  *) LOCAL_PLATFORM="linux/amd64" ;;
esac

cleanup() {
  set +e
  docker rm -f "${APP_CONTAINER}" >/dev/null 2>&1 || true
  docker rm -f "${COLLECTOR_CONTAINER}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
  set -e
}
trap cleanup EXIT

if [ ! -f Dockerfile ]; then
  if [ -d "src/services/retriever" ]; then
    cd src/services/retriever
  else
    echo "[ERROR] Dockerfile not found and src/services/retriever does not exist" >&2
    exit 1
  fi
fi

if ! docker image inspect "${IMAGE_LOCAL}" >/dev/null 2>&1; then
  echo "[INFO] image ${IMAGE_LOCAL} not found locally, building it"
  docker build --platform "${LOCAL_PLATFORM}" -t "${IMAGE_LOCAL}" .
fi

docker network create "${NETWORK}" >/dev/null 2>&1 || true

WORKDIR="$(mktemp -d)"
COLLECTOR_CONFIG="${WORKDIR}/collector.yaml"

cat >"${COLLECTOR_CONFIG}" <<'YAML'
receivers:
  otlp:
    protocols:
      grpc:
      http:

processors:
  batch: {}

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
YAML

wait_for_http() {
  local url=$1
  local timeout=$2
  local start
  start=$(date +%s)

  while :; do
    if curl -fsS --max-time 2 "${url}" >/dev/null 2>&1; then
      return 0
    fi

    if [ $(( $(date +%s) - start )) -ge "${timeout}" ]; then
      printf '%s\n' "<timeout waiting for ${url}>" >&2
      return 1
    fi

    sleep "${SLEEP_BETWEEN_TRIES}"
  done
}

assert_contains() {
  local haystack=$1
  local needle=$2
  local label=$3

  if ! printf '%s' "${haystack}" | grep -qF "${needle}"; then
    echo "[ERROR] missing ${label}: ${needle}" >&2
    exit 1
  fi
  echo "[OK] found ${label}: ${needle}"
}

echo "[INFO] starting collector"
docker run \
  --name "${COLLECTOR_CONTAINER}" \
  -d \
  --network "${NETWORK}" \
  -v "${COLLECTOR_CONFIG}:/etc/otelcol-contrib/config.yaml:ro" \
  "${COLLECTOR_IMAGE}" \
  --config /etc/otelcol-contrib/config.yaml >/dev/null

echo "[INFO] starting retriever"
docker run \
  --name "${APP_CONTAINER}" \
  -d \
  --network "${NETWORK}" \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  --shm-size=1g \
  -e PORT="${CONTAINER_PORT}" \
  -e LOG_LEVEL="${LOG_LEVEL:-INFO}" \
  -e SERVICE_NAME="${SERVICE_NAME:-retrieval}" \
  -e SERVICE_VERSION="${SERVICE_VERSION:-ci}" \
  -e ENV="${ENV:-CI}" \
  -e DEPLOYMENT_ENVIRONMENT="${DEPLOYMENT_ENVIRONMENT:-CI}" \
  -e CLUSTER_NAME="${CLUSTER_NAME:-ci}" \
  -e SERVICE_INSTANCE_ID="${SERVICE_INSTANCE_ID:-ci-1}" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT="http://${COLLECTOR_CONTAINER}:4317" \
  -e OTEL_TIMEOUT_SECONDS="${OTEL_TIMEOUT_SECONDS:-2}" \
  -e OTEL_METRIC_EXPORT_INTERVAL_MS="${OTEL_METRIC_EXPORT_INTERVAL_MS:-1000}" \
  -e OTEL_METRIC_EXPORT_TIMEOUT_MS="${OTEL_METRIC_EXPORT_TIMEOUT_MS:-1000}" \
  -e OTEL_TRACES_SAMPLER="${OTEL_TRACES_SAMPLER:-parentbased_traceidratio}" \
  -e OTEL_TRACES_SAMPLER_ARG="${OTEL_TRACES_SAMPLER_ARG:-1.0}" \
  -e ENABLE_OTEL_TRACES="${ENABLE_OTEL_TRACES:-true}" \
  -e ENABLE_OTEL_METRICS="${ENABLE_OTEL_METRICS:-true}" \
  -e ENABLE_OTEL_LOGS="${ENABLE_OTEL_LOGS:-true}" \
  -e QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:65530}" \
  -e DENSE_URL="${DENSE_URL:-http://127.0.0.1:65531}" \
  -e SPARSE_URL="${SPARSE_URL:-http://127.0.0.1:65532}" \
  -e RERANKER_URL="${RERANKER_URL:-http://127.0.0.1:65533}" \
  -e AWS_REGION="${AWS_REGION:-us-east-1}" \
  -e BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-test-model}" \
  "${IMAGE_LOCAL}" >/dev/null

if ! wait_for_http "http://127.0.0.1:${HOST_PORT}/healthz" "${WAIT_TIMEOUT}"; then
  echo "[ERROR] retriever did not become healthy"
  docker logs --tail 200 "${APP_CONTAINER}" || true
  docker logs --tail 200 "${COLLECTOR_CONTAINER}" || true
  exit 4
fi

healthz="$(curl -fsS "http://127.0.0.1:${HOST_PORT}/healthz")"
readyz="$(curl -fsS "http://127.0.0.1:${HOST_PORT}/readyz" || true)"

printf '%s' "${healthz}" | grep -q '"status":"ok"'
printf '%s' "${readyz}" | grep -q '"status"'

# Minimal, non-flaky telemetry trigger:
# - hits the HTTP middleware
# - forces a 422 validation log
# - records request metrics
# - creates a request span
invalid_status="$(
  curl -sS --max-time 15 \
    -o /tmp/retriever-invalid.out \
    -w '%{http_code}' \
    -X POST "http://127.0.0.1:${HOST_PORT}/generate" \
    -H "Content-Type: application/json" \
    -d '{}' || true
)"

if [ "${invalid_status}" != "422" ]; then
  cat /tmp/retriever-invalid.out || true
  docker logs --tail 200 "${APP_CONTAINER}" || true
  docker logs --tail 200 "${COLLECTOR_CONTAINER}" || true
  exit 8
fi

# Flush exporters cleanly.
docker stop -t 15 "${APP_CONTAINER}" >/dev/null || true
sleep 2
docker stop -t 15 "${COLLECTOR_CONTAINER}" >/dev/null || true

APP_LOGS="$(docker logs "${APP_CONTAINER}" 2>&1 || true)"
COLLECTOR_LOGS="$(docker logs "${COLLECTOR_CONTAINER}" 2>&1 || true)"

# App-side sanity: request error log happened.
assert_contains "${APP_LOGS}" "request.validation_failed" "app log event"
assert_contains "${APP_LOGS}" "\"trace_id\"" "app trace correlation"
assert_contains "${APP_LOGS}" "\"span_id\"" "app span correlation"

# Collector-side sanity: each signal arrived through OTLP and was exported by the debug exporter.
assert_contains "${COLLECTOR_LOGS}" "ResourceSpans" "trace export"
assert_contains "${COLLECTOR_LOGS}" "ResourceMetrics" "metric export"
assert_contains "${COLLECTOR_LOGS}" "ResourceLogs" "log export"

# Stronger check for the retriever-specific signal names.
assert_contains "${COLLECTOR_LOGS}" "http.server.request.count" "http metric"
assert_contains "${COLLECTOR_LOGS}" "http.server.request.duration" "http duration metric"
assert_contains "${COLLECTOR_LOGS}" "retrieval.pipeline.duration" "pipeline metric"
assert_contains "${COLLECTOR_LOGS}" "telemetry.initialize.complete" "startup log"
assert_contains "${COLLECTOR_LOGS}" "request.validation_failed" "correlated request log"

echo "[OK] OTel telemetry CI smoke test passed"
exit 0
