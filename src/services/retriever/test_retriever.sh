#!/usr/bin/env bash
set -euo pipefail

MODE="${TEST_MODE:-metrics}"
IMAGE_TAG="${IMAGE_TAG:-${RETRIEVER_IMAGE_TAG:-test}}"
IMAGE_REPO="${IMAGE_REPO:-retriever}"
IMAGE_LOCAL="${IMAGE_REPO}:${IMAGE_TAG}"
CONTAINER_NAME="${CONTAINER_NAME:-test-retriever-${MODE}}"
HOST_PORT="${HOST_PORT:-9023}"
CONTAINER_PORT="${CONTAINER_PORT:-8203}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-60}"
SLEEP_BETWEEN_TRIES=1

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
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
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
  docker build \
    --platform "${LOCAL_PLATFORM}" \
    -t "${IMAGE_LOCAL}" .
fi

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

metric_value() {
  local metric_name=$1 labels_filter=$2 metrics_file=$3
  python3 - "$metric_name" "$labels_filter" "$metrics_file" <<'PY'
import re, sys

metric_name, labels_filter, path = sys.argv[1:4]
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

label_tokens = {}
if labels_filter:
    for part in labels_filter.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            label_tokens[k.strip()] = v.strip().strip('"')

pattern = re.compile(rf"^{re.escape(metric_name)}(?:\{{([^}}]*)\}})?\s+([0-9.eE+-]+)$")

for line in lines:
    m = pattern.match(line.strip())
    if not m:
        continue
    labels_raw, value = m.groups()
    if label_tokens:
        labels = {}
        if labels_raw:
            for item in labels_raw.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    labels[k.strip()] = v.strip().strip('"')
        if any(labels.get(k) != v for k, v in label_tokens.items()):
            continue
    print(value)
    sys.exit(0)

print("")
sys.exit(0)
PY
}

cleanup

echo "[1/7] Starting container ${CONTAINER_NAME}"
docker run \
  --name "${CONTAINER_NAME}" \
  -d \
  -p "${HOST_PORT}:${CONTAINER_PORT}" \
  --shm-size=1g \
  -e LOG_LEVEL="${LOG_LEVEL:-INFO}" \
  -e SERVICE_NAME="${SERVICE_NAME:-retrieval}" \
  -e ENV="${ENV:-CI}" \
  "${IMAGE_LOCAL}" >/dev/null

echo "[2/7] Waiting for /healthz (timeout ${WAIT_TIMEOUT}s)"
if ! wait_for_http "http://127.0.0.1:${HOST_PORT}/healthz" "${WAIT_TIMEOUT}" >/tmp/retriever-healthz.out; then
  echo "[ERROR] /healthz did not become available" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 4
fi

echo "[3/7] Checking /healthz and /readyz"
healthz=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/healthz")
readyz=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/readyz")
metrics_before=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/metrics")

if ! printf '%s' "${healthz}" | grep -q '"status":"ok"'; then
  echo "[ERROR] /healthz did not return expected payload" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 5
fi

if ! printf '%s' "${readyz}" | grep -q '"status"'; then
  echo "[ERROR] /readyz did not return expected payload" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 6
fi

if ! printf '%s' "${metrics_before}" | grep -Eq '(^|[[:space:]])retrieval_requests_total|(^|[[:space:]])retrieval_request_duration_seconds|(^|[[:space:]])service_ready'; then
  echo "[ERROR] /metrics did not expose expected retriever metrics" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 7
fi

tmp_before=$(mktemp)
tmp_after=$(mktemp)
printf '%s\n' "${metrics_before}" > "${tmp_before}"

echo "[4/7] Exercising /generate, /retrieve, /generate/stream"
gen_status=$(curl -sS -o /tmp/retriever-generate.out -w '%{http_code}' \
  -X POST "http://127.0.0.1:${HOST_PORT}/generate" \
  -H "Content-Type: application/json" \
  -d '{"query":"metrics smoke test query"}' || true)

retrieve_status=$(curl -sS -o /tmp/retriever-retrieve.out -w '%{http_code}' \
  -X POST "http://127.0.0.1:${HOST_PORT}/retrieve" \
  -H "Content-Type: application/json" \
  -d '{"query":"metrics smoke test query","include_cache":true,"rerank":true}' || true)

stream_status=$(curl -sS -o /tmp/retriever-stream.out -w '%{http_code}' \
  -X POST "http://127.0.0.1:${HOST_PORT}/generate/stream" \
  -H "Content-Type: application/json" \
  -d '{"query":"metrics smoke test query"}' || true)

for pair in \
  "generate:${gen_status}" \
  "retrieve:${retrieve_status}" \
  "stream:${stream_status}"
do
  name=${pair%%:*}
  code=${pair##*:}
  if [ "${code}" != "200" ] && [ "${code}" != "503" ]; then
    echo "[ERROR] /${name} returned unexpected HTTP ${code}" >&2
    case "${name}" in
      generate) cat /tmp/retriever-generate.out || true ;;
      retrieve) cat /tmp/retriever-retrieve.out || true ;;
      stream) cat /tmp/retriever-stream.out || true ;;
    esac
    docker logs --tail 200 "${CONTAINER_NAME}" || true
    exit 8
  fi
done

echo "[5/7] Re-reading metrics after requests"
metrics_after=$(curl -fsS "http://127.0.0.1:${HOST_PORT}/metrics")
printf '%s\n' "${metrics_after}" > "${tmp_after}"

gen_total_before=$(metric_value "retrieval_requests_total" 'endpoint="/generate"' "${tmp_before}")
gen_total_after=$(metric_value "retrieval_requests_total" 'endpoint="/generate"' "${tmp_after}")
ret_total_before=$(metric_value "retrieval_requests_total" 'endpoint="/retrieve"' "${tmp_before}")
ret_total_after=$(metric_value "retrieval_requests_total" 'endpoint="/retrieve"' "${tmp_after}")
stream_total_before=$(metric_value "retrieval_requests_total" 'endpoint="/generate/stream"' "${tmp_before}")
stream_total_after=$(metric_value "retrieval_requests_total" 'endpoint="/generate/stream"' "${tmp_after}")

req_dur_before=$(metric_value "retrieval_request_duration_seconds_count" 'endpoint="/generate"' "${tmp_before}")
req_dur_after=$(metric_value "retrieval_request_duration_seconds_count" 'endpoint="/generate"' "${tmp_after}")

ready_before=$(metric_value "service_ready" "" "${tmp_before}")
ready_after=$(metric_value "service_ready" "" "${tmp_after}")

check_increment() {
  local name=$1 before=$2 after=$3
  if [ -z "${before}" ] || [ -z "${after}" ]; then
    echo "[ERROR] Could not read metric ${name}" >&2
    docker logs --tail 200 "${CONTAINER_NAME}" || true
    exit 9
  fi
  python3 - "$name" "$before" "$after" <<'PY'
import sys
name, before, after = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
if after < before:
    print(f"[ERROR] metric {name} moved backwards: before={before} after={after}", file=sys.stderr)
    sys.exit(1)
if after == before:
    print(f"[ERROR] metric {name} did not change: before={before} after={after}", file=sys.stderr)
    sys.exit(2)
print(f"[OK] {name}: {before} -> {after}")
PY
}

echo "[6/7] Validating Prometheus counters increased"
check_increment "retrieval_requests_total{endpoint=/generate}" "${gen_total_before:-0}" "${gen_total_after:-0}"
check_increment "retrieval_requests_total{endpoint=/retrieve}" "${ret_total_before:-0}" "${ret_total_after:-0}"
check_increment "retrieval_requests_total{endpoint=/generate/stream}" "${stream_total_before:-0}" "${stream_total_after:-0}"
check_increment "retrieval_request_duration_seconds_count{endpoint=/generate}" "${req_dur_before:-0}" "${req_dur_after:-0}"

if [ -n "${ready_before}" ] && [ -n "${ready_after}" ]; then
  python3 - <<PY
before = float("${ready_before}")
after = float("${ready_after}")
if before != after:
    raise SystemExit(f"[ERROR] service_ready changed unexpectedly: before={before} after={after}")
print(f"[OK] service_ready stable at {after}")
PY
fi

echo "[7/7] Validating metrics exposition format"
if ! printf '%s' "${metrics_after}" | grep -q '^# TYPE retrieval_requests_total counter'; then
  echo "[ERROR] Prometheus exposition missing retrieval_requests_total type line" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 10
fi

if ! printf '%s' "${metrics_after}" | grep -q '^# TYPE service_ready gauge'; then
  echo "[ERROR] Prometheus exposition missing service_ready type line" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 11
fi

if ! printf '%s' "${metrics_after}" | grep -q '^# TYPE retrieval_request_duration_seconds histogram'; then
  echo "[ERROR] Prometheus exposition missing retrieval_request_duration_seconds type line" >&2
  docker logs --tail 200 "${CONTAINER_NAME}" || true
  exit 12
fi

echo "[SUCCESS] Prometheus setup validated for retriever service"
rm -f "${tmp_before}" "${tmp_after}"
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
exit 0
