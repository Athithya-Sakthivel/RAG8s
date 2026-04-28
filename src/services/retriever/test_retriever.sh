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
  echo "[INFO] Image ${IMAGE_LOCAL} not found locally, building it"
  docker build --platform "${LOCAL_PLATFORM}" -t "${IMAGE_LOCAL}" .
fi

docker network create "${NETWORK}" >/dev/null 2>&1 || true

WORKDIR="$(mktemp -d)"
COLLECTOR_CONFIG="${WORKDIR}/collector.yaml"
COLLECTOR_OUT="${WORKDIR}/otel-out"
mkdir -p "${COLLECTOR_OUT}"

cat >"${COLLECTOR_CONFIG}" <<'YAML'
receivers:
  otlp:
    protocols:
      grpc:
      http:

processors:
  batch: {}

exporters:
  file/traces:
    path: /out/traces.json
    create_directory: true
    format: json
  file/metrics:
    path: /out/metrics.json
    create_directory: true
    format: json
  file/logs:
    path: /out/logs.json
    create_directory: true
    format: json

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [file/traces]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [file/metrics]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [file/logs]
YAML

wait_for_http() {
  local url=$1 timeout=$2 start body
  start=$(date +%s)
  while :; do
    body=$(curl -fsS --max-time 2 "${url}" 2>/dev/null || true)
    if [ -n "${body}" ]; then
      printf '%s\n' "${body}"
      return 0
    fi
    if [ $(( $(date +%s) - start )) -ge "${timeout}" ]; then
      printf '%s\n' "<timeout waiting for ${url}>" >&2
      return 1
    fi
    sleep "${SLEEP_BETWEEN_TRIES}"
  done
}

wait_for_file() {
  local path=$1 timeout=$2 start
  start=$(date +%s)
  while :; do
    if [ -s "${path}" ]; then
      return 0
    fi
    if [ $(( $(date +%s) - start )) -ge "${timeout}" ]; then
      printf '%s\n' "<timeout waiting for ${path}>" >&2
      return 1
    fi
    sleep "${SLEEP_BETWEEN_TRIES}"
  done
}

json_file_contains() {
  local path=$1
  shift
  python3 - "$path" "$@" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
needles = sys.argv[2:]
text = path.read_text(encoding="utf-8", errors="replace")

missing = [n for n in needles if n not in text]
if missing:
    print(f"[ERROR] missing needles in {path.name}: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

print(f"[OK] {path.name} contains: {', '.join(needles)}")
PY
}

http_code() {
  local method=$1 url=$2 data=$3 out_file=$4
  if [ -n "${data}" ]; then
    curl -sS --max-time 15 -o "${out_file}" -w '%{http_code}' \
      -X "${method}" "${url}" \
      -H "Content-Type: application/json" \
      -d "${data}" || true
  else
    curl -sS --max-time 15 -o "${out_file}" -w '%{http_code}' \
      -X "${method}" "${url}" || true
  fi
}

echo "[INFO] starting collector"
docker run \
  --name "${COLLECTOR_CONTAINER}" \
  -d \
  --network "${NETWORK}" \
  -v "${COLLECTOR_CONFIG}:/etc/otelcol-contrib/config.yaml:ro" \
  -v "${COLLECTOR_OUT}:/out:rw" \
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
  "${IMAGE_LOCAL}" >/dev/null

if ! wait_for_http "http://127.0.0.1:${HOST_PORT}/healthz" "${WAIT_TIMEOUT}" >/tmp/retriever-healthz.out; then
  docker logs --tail 200 "${APP_CONTAINER}" || true
  docker logs --tail 200 "${COLLECTOR_CONTAINER}" || true
  exit 4
fi

healthz=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/healthz")
readyz=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/readyz" || true)

printf '%s' "${healthz}" | grep -q '"status":"ok"'
printf '%s' "${readyz}" | grep -q '"status"'

# 1) Validation error -> guaranteed structured log + active request span.
invalid_generate_status=$(
  http_code POST "http://127.0.0.1:${HOST_PORT}/generate" '{}' /tmp/retriever-generate-invalid.out
)
if [ "${invalid_generate_status}" != "422" ]; then
  cat /tmp/retriever-generate-invalid.out || true
  docker logs --tail 200 "${APP_CONTAINER}" || true
  docker logs --tail 200 "${COLLECTOR_CONTAINER}" || true
  exit 8
fi

# 2) Valid requests -> request spans and OTEL metrics.
gen_status=$(
  http_code POST "http://127.0.0.1:${HOST_PORT}/generate" \
  '{"query":"otel smoke test query"}' /tmp/retriever-generate.out
)
retrieve_status=$(
  http_code POST "http://127.0.0.1:${HOST_PORT}/retrieve" \
  '{"query":"otel smoke test query","include_cache":true,"rerank":true}' /tmp/retriever-retrieve.out
)
stream_status=$(
  http_code POST "http://127.0.0.1:${HOST_PORT}/generate/stream" \
  '{"query":"otel smoke test query"}' /tmp/retriever-stream.out
)

for pair in "generate:${gen_status}" "retrieve:${retrieve_status}" "stream:${stream_status}"; do
  name=${pair%%:*}
  code=${pair##*:}
  if [ "${code}" != "200" ] && [ "${code}" != "503" ]; then
    case "${name}" in
      generate) cat /tmp/retriever-generate.out || true ;;
      retrieve) cat /tmp/retriever-retrieve.out || true ;;
      stream) cat /tmp/retriever-stream.out || true ;;
    esac
    docker logs --tail 200 "${APP_CONTAINER}" || true
    docker logs --tail 200 "${COLLECTOR_CONTAINER}" || true
    exit 9
  fi
done

# Stop the app to force batch flushes before inspecting the collector output.
docker stop -t 10 "${APP_CONTAINER}" >/dev/null || true
sleep 2

TRACE_FILE="${COLLECTOR_OUT}/traces.json"
METRICS_FILE="${COLLECTOR_OUT}/metrics.json"
LOGS_FILE="${COLLECTOR_OUT}/logs.json"

wait_for_file "${TRACE_FILE}" "${WAIT_TIMEOUT}"
wait_for_file "${METRICS_FILE}" "${WAIT_TIMEOUT}"
wait_for_file "${LOGS_FILE}" "${WAIT_TIMEOUT}"

# Collector file exporter writes one signal type per file in OTLP JSON lines.
# Check each signal has arrived and contains expected retriever telemetry.
json_file_contains "${TRACE_FILE}" \
  "resourceSpans" \
  "retrieval.pipeline.build" \
  "http.request.method" \
  "http.route" \
  "generate" \
  "retrieve"

json_file_contains "${METRICS_FILE}" \
  "resourceMetrics" \
  "http.server.request.count" \
  "http.server.request.duration" \
  "http.server.errors" \
  "retrieval.pipeline.duration" \
  "retrieval.qdrant.query.count" \
  "retrieval.cache.lookup.count"

json_file_contains "${LOGS_FILE}" \
  "resourceLogs" \
  "request.validation_failed" \
  "telemetry" \
  "traceId" \
  "spanId"

# Optional sanity check: the validation log should have been correlated.
python3 - "${LOGS_FILE}" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
if "request.validation_failed" not in text:
    raise SystemExit("[ERROR] validation log missing")
if "traceId" not in text and "trace_id" not in text:
    raise SystemExit("[ERROR] log file missing trace correlation field")
if "spanId" not in text and "span_id" not in text:
    raise SystemExit("[ERROR] log file missing span correlation field")
print("[OK] correlated log entry found")
PY

echo "[OK] OTel telemetry verified through Collector files"
exit 0
