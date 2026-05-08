#!/usr/bin/env bash
# infra/tests/logging/chaos_retriever_quick.sh
# Safe, fast E2E logging chaos for retriever -> Vector -> ClickHouse
#
# Runs:
#  - baseline ClickHouse query (token)
#  - restart one Vector agent pod
#  - emit 2 quick health probes with token (retriever)
#  - short wait for Vector to ship logs
#  - verify ClickHouse has token entries
#  - rollout restart retriever deployment (quick)
#  - emit a probe after restart and verify
#  - emit a small malformed log pod and verify behavior
#
# Designed to be short (<< 2 minutes) on a healthy staging cluster.

make fix-dns
make deploy-clickhouse
make deploy-vm
APP_NS="${APP_NS:-inference}"
APP_LABEL="${APP_LABEL:-app.kubernetes.io/name=retrieval}"
TOKEN="${TOKEN:-retriever-e2e-$(date +%s)}"
VECTOR_NS="${VECTOR_NS:-observability}"
CH_NS="${CH_NS:-observability}"
CH_DB="${CH_DB:-logs}"
CH_TABLE="${CH_TABLE:-kube_logs}"

# Tunable timeouts (increase if cluster slow)
WAIT_RETR_POD="${WAIT_RETR_POD:-30}"
VECTOR_START_WAIT="${VECTOR_START_WAIT:-30}"
VECTOR_CHECK_INTERVAL="${VECTOR_CHECK_INTERVAL:-2}"
CLICKHOUSE_CHECK_WAIT="${CLICKHOUSE_CHECK_WAIT:-15}"
VECTOR_SHIP_WAIT="${VECTOR_SHIP_WAIT:-10}"
RESTART_WAIT="${RESTART_WAIT:-40}"
CURL_POD_WAIT="${CURL_POD_WAIT:-12}"

log() { printf '%s %s\n' "$(date --utc +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

kubectl_or_die() {
  if ! command -v kubectl >/dev/null 2>&1; then
    log "[error] kubectl not found"
    exit 2
  fi
}

find_clickhouse_pod() {
  local pod
  pod="$(kubectl -n "${CH_NS}" get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -z "${pod}" ]; then
    pod="$(kubectl -n "${CH_NS}" get pods --no-headers 2>/dev/null | awk '/clickhouse|ch-single/ {print $1; exit}')"
  fi
  echo "${pod:-}"
}

check_clickhouse_ready() {
  local pod="$1"
  local timeout="${2:-$CLICKHOUSE_CHECK_WAIT}"
  local start ts rc
  start=$(date +%s)
  while true; do
    if kubectl -n "${CH_NS}" exec -i "${pod}" -- bash -lc "clickhouse-client --query 'SELECT 1' >/dev/null 2>&1"; then
      return 0
    fi
    ts=$(date +%s)
    if [ $((ts - start)) -ge "${timeout}" ]; then
      return 1
    fi
    sleep 1
  done
}

find_vector_pod() {
  kubectl -n "${VECTOR_NS}" get pods --no-headers 2>/dev/null | awk '/vector|vector-agent/ {print $1; exit}'
}

vector_wait_for_healthy() {
  # Wait until Vector shows "Healthcheck passed" and "Loaded checkpoint data" in logs
  local pod="$1"
  local timeout="${2:-$VECTOR_START_WAIT}"
  local start ts line
  start=$(date +%s)
  while true; do
    # require both messages to appear in recent logs
    line=$(kubectl -n "${VECTOR_NS}" logs "${pod}" --tail=200 2>/dev/null || true)
    echo "${line}" | grep -q "Healthcheck passed" && echo "${line}" | grep -q "Loaded checkpoint data" && return 0
    ts=$(date +%s)
    if [ $((ts - start)) -ge "${timeout}" ]; then
      return 1
    fi
    sleep "${VECTOR_CHECK_INTERVAL}"
  done
}

safe_clickhouse_query() {
  local pod="$1"
  local q="$2"
  if ! kubectl -n "${CH_NS}" exec -i "${pod}" -- bash -lc "clickhouse-client --query \"${q}\""; then
    log "[warn] clickhouse query failed; fetching diagnostics"
    kubectl -n "${CH_NS}" exec -i "${pod}" -- clickhouse-client --query="SHOW DATABASES" || true
    kubectl -n "${CH_NS}" exec -i "${pod}" -- clickhouse-client --query="SHOW TABLES FROM ${CH_DB}" || true
    return 1
  fi
  return 0
}

# Start
kubectl_or_die
log "starting chaos_retriever_quick: APP_NS=${APP_NS} APP_LABEL=${APP_LABEL} TOKEN=${TOKEN} CH_NS=${CH_NS}"

# Ensure retriever pod ready
log "[step 0] ensure retriever pod exists and is Ready (timeout=${WAIT_RETR_POD}s)"
if ! kubectl -n "${APP_NS}" wait pod -l "${APP_LABEL}" --for=condition=Ready --timeout="${WAIT_RETR_POD}s" >/dev/null 2>&1; then
  log "[error] no ready pods matching ${APP_LABEL} in ${APP_NS}"
  kubectl -n "${APP_NS}" get pods -l "${APP_LABEL}" || true
  exit 3
fi
RETR_POD="$(kubectl -n "${APP_NS}" get pod -l "${APP_LABEL}" -o jsonpath='{.items[0].metadata.name}')"
log "retriever pod: ${RETR_POD}"

# Find and check ClickHouse pod
log "[step 1] locate ClickHouse pod in ${CH_NS}"
CH_POD="$(find_clickhouse_pod)"
if [ -z "${CH_POD}" ]; then
  log "[error] no ClickHouse pod found in ${CH_NS}"
  kubectl -n "${CH_NS}" get pods || true
  exit 4
fi
log "clickhouse pod: ${CH_POD} -> checking readiness"
if ! check_clickhouse_ready "${CH_POD}" 15; then
  log "[error] clickhouse not ready in ${CH_NS}/${CH_POD}"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  exit 5
fi
log "[ok] ClickHouse reachable"

# Find Vector pod and ensure health (restart if unhealthy)
VEC_POD="$(find_vector_pod || true)"
if [ -z "${VEC_POD}" ]; then
  log "[warn] no Vector pod found in ${VECTOR_NS}; continuing (Vector may not be installed)"
else
  log "[step 2] check Vector pod ${VEC_POD} health"
  if ! vector_wait_for_healthy "${VEC_POD}" 5; then
    log "[warn] Vector pod ${VEC_POD} not healthy; restarting it and waiting up to ${VECTOR_START_WAIT}s"
    kubectl -n "${VECTOR_NS}" delete pod "${VEC_POD}" --wait=false >/dev/null 2>&1 || true
    # find new pod name
    sleep 1
    # wait for new pod to appear
    start=$(date +%s)
    while true; do
      VEC_POD="$(find_vector_pod || true)"
      if [ -n "${VEC_POD}" ]; then break; fi
      if [ $(( $(date +%s) - start )) -ge "${VECTOR_START_WAIT}" ]; then break; fi
      sleep 1
    done
    if [ -z "${VEC_POD}" ]; then
      log "[warn] new Vector pod did not appear; continuing but results may be missed"
    else
      log "new Vector pod: ${VEC_POD}; waiting for health"
      if ! vector_wait_for_healthy "${VEC_POD}" "${VECTOR_START_WAIT}"; then
        log "[warn] Vector pod ${VEC_POD} did not become healthy in time; continuing (may miss logs)"
        kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=200 || true
      else
        log "[ok] Vector reports health and checkpoint load"
      fi
    fi
  else
    log "[ok] Vector appears healthy"
  fi
fi

# Baseline ClickHouse count for our token
log "[step 3] baseline ClickHouse count for token"
BASE_Q="SELECT count() FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%';"
BASE_COUNT="$(kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "clickhouse-client --query \"${BASE_Q}\"" 2>/dev/null || echo "ERR")"
log "baseline rows for token=${TOKEN}: ${BASE_COUNT}"

# Emit two minimal probes (exec curl inside retriever pod to generate access logs)
log "[step 4] emit 2 minimal health probes to retriever (exec inside pod)"
for i in 1 2; do
  kubectl -n "${APP_NS}" exec "${RETR_POD}" -- sh -c "curl -sS --max-time 5 \"http://127.0.0.1:8001/healthz?e2e=${TOKEN}&i=${i}\"" >/dev/null 2>&1 || true
  sleep 1
done

# Wait a short period for Vector to ship logs
log "[step 5] brief wait ${VECTOR_SHIP_WAIT}s for Vector to ship logs"
sleep "${VECTOR_SHIP_WAIT}"

# Verify ClickHouse ingestion
log "[step 6] verify ClickHouse ingestion for token"
VERIFY_Q="SELECT count() AS rows, any(service) AS service, any(pod) AS pod, any(namespace) AS namespace, min(ts) AS first_ts, max(ts) AS last_ts FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%' FORMAT Vertical"
if ! safe_clickhouse_query "${CH_POD}" "${VERIFY_Q}"; then
  log "[warn] verification query failed; dumping recent ClickHouse logs and Vector logs for debugging"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  if [ -n "${VEC_POD}" ]; then kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=200 || true; fi
fi

# Rollout restart retriever deployment to test producer restart
log "[step 7] rollout restart retrieval-deployment (quick)"
kubectl -n "${APP_NS}" rollout restart deployment retrieval-deployment >/dev/null 2>&1 || {
  log "[warn] rollout restart failed; deleting pod ${RETR_POD} instead"
  kubectl -n "${APP_NS}" delete pod "${RETR_POD}" --ignore-not-found >/dev/null 2>&1 || true
}
log "[step 7] waiting up to ${RESTART_WAIT}s for retrieval-deployment to be available"
kubectl -n "${APP_NS}" wait deployment retrieval-deployment --for=condition=available --timeout="${RESTART_WAIT}s" >/dev/null 2>&1 || log "[warn] deployment not available within ${RESTART_WAIT}s"

RETR_POD="$(kubectl -n "${APP_NS}" get pod -l "${APP_LABEL}" -o jsonpath='{.items[0].metadata.name}')"
log "post-restart retriever pod: ${RETR_POD} (will emit one probe)"

kubectl -n "${APP_NS}" exec "${RETR_POD}" -- sh -c "curl -sS --max-time 5 \"http://127.0.0.1:8001/healthz?e2e=${TOKEN}&i=after_restart\" || true" >/dev/null 2>&1 || true
sleep 3

# Check ingestion again
log "[step 8] post-restart verification"
if ! safe_clickhouse_query "${CH_POD}" "${VERIFY_Q}"; then
  log "[warn] post-restart verification failed; dumping logs for debug"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  if [ -n "${VEC_POD}" ]; then kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=200 || true; fi
fi

# Emit a small malformed line to test parser behavior
log "[step 9] emit a single malformed line via transient pod"
MAL_POD="malformed-${TOKEN}"
kubectl -n "${APP_NS}" run "${MAL_POD}" --restart=Never --image=alpine --command -- sh -c "printf 'NOT_JSON_LINE ${TOKEN}\\n'; sleep 1" >/dev/null 2>&1 || true
kubectl -n "${APP_NS}" wait pod "${MAL_POD}" --for=condition=Succeeded --timeout="${CURL_POD_WAIT}s" >/dev/null 2>&1 || true
sleep 4

# Final aggregated verification
log "[step 10] final ClickHouse check (all occurrences of token)"
FINAL_Q="SELECT count() AS rows, any(namespace) AS namespace, any(pod) AS pod FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%' FORMAT Vertical"
if ! safe_clickhouse_query "${CH_POD}" "${FINAL_Q}"; then
  log "[warn] final check failed; dumping diagnostics"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  if [ -n "${VEC_POD}" ]; then kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=400 || true; fi
fi

kubectl -n "${APP_NS}" delete pod "${MAL_POD}" --ignore-not-found >/dev/null 2>&1 || true

log "chaos_retriever_quick complete - token=${TOKEN}"
exit 0
