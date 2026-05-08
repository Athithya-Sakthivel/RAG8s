#!/usr/bin/env bash
# infra/tests/logging/chaos_frontend_quick.sh
# Safe, fast E2E logging chaos for frontend -> Vector -> ClickHouse
#
# Pattern: baseline -> transient curl pod -> short wait -> verify ClickHouse -> rollout restart -> verify -> malformed pod -> final verify
# Keep waits conservative so script runs quickly on healthy clusters.

make fix-dns

APP_NS="${APP_NS:-inference}"
APP_LABEL="${APP_LABEL:-app.kubernetes.io/name=frontend}"
TOKEN="${TOKEN:-frontend-e2e-$(date +%s)}"
VECTOR_NS="${VECTOR_NS:-observability}"
CH_NS="${CH_NS:-observability}"
CH_DB="${CH_DB:-logs}"
CH_TABLE="${CH_TABLE:-kube_logs}"
FR_SVC="${FR_SVC:-frontend-svc.${APP_NS}.svc.cluster.local:8000}"   # svc FQDN:port
CURL_IMG="${CURL_IMG:-curlimages/curl:8.4.0}"

# Timing tunables (short)
WAIT_APP_POD="${WAIT_APP_POD:-30}"
VECTOR_SHIP_WAIT="${VECTOR_SHIP_WAIT:-8}"
CLICKHOUSE_CHECK_WAIT="${CLICKHOUSE_CHECK_WAIT:-12}"
ROLLING_WAIT="${ROLLING_WAIT:-40}"
CURL_POD_TIMEOUT="${CURL_POD_TIMEOUT:-20}"
MALFORMED_POD_WAIT="${MALFORMED_POD_WAIT:-6}"

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
  local pod="$1"; local timeout="${2:-$CLICKHOUSE_CHECK_WAIT}"
  local start now
  start=$(date +%s)
  while true; do
    if kubectl -n "${CH_NS}" exec -i "${pod}" -- bash -lc "clickhouse-client --query 'SELECT 1' >/dev/null 2>&1"; then
      return 0
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "${timeout}" ]; then
      return 1
    fi
    sleep 1
  done
}

find_vector_pod() {
  kubectl -n "${VECTOR_NS}" get pods --no-headers 2>/dev/null | awk '/vector|vector-agent/ {print $1; exit}' || true
}

vector_wait_for_healthy() {
  local pod="$1"; local timeout="${2:-20}"
  local start now lines
  start=$(date +%s)
  while true; do
    lines="$(kubectl -n "${VECTOR_NS}" logs "${pod}" --tail=200 2>/dev/null || true)"
    echo "${lines}" | grep -q "Healthcheck passed" && echo "${lines}" | grep -q "Loaded checkpoint data" && return 0 || true
    now=$(date +%s)
    if [ $((now - start)) -ge "${timeout}" ]; then
      return 1
    fi
    sleep 1
  done
}

safe_clickhouse_query() {
  local pod="$1"; local q="$2"
  if ! kubectl -n "${CH_NS}" exec -i "${pod}" -- bash -lc "clickhouse-client --query \"${q}\""; then
    log "[warn] clickhouse query failed; printing diagnostics"
    kubectl -n "${CH_NS}" exec -i "${pod}" -- clickhouse-client --query="SHOW DATABASES" || true
    kubectl -n "${CH_NS}" exec -i "${pod}" -- clickhouse-client --query="SHOW TABLES FROM ${CH_DB}" || true
    return 1
  fi
  return 0
}

# Start
kubectl_or_die
log "starting chaos_frontend_quick: APP_NS=${APP_NS} APP_LABEL=${APP_LABEL} TOKEN=${TOKEN} CH_NS=${CH_NS}"

# Ensure frontend pod ready
log "[step 0] ensure frontend pod exists and Ready (timeout ${WAIT_APP_POD}s)"
if ! kubectl -n "${APP_NS}" wait pod -l "${APP_LABEL}" --for=condition=Ready --timeout="${WAIT_APP_POD}s" >/dev/null 2>&1; then
  log "[error] no ready pods matching ${APP_LABEL} in ${APP_NS}"
  kubectl -n "${APP_NS}" get pods -l "${APP_LABEL}" || true
  exit 3
fi
FR_POD="$(kubectl -n "${APP_NS}" get pod -l "${APP_LABEL}" -o jsonpath='{.items[0].metadata.name}')"
log "frontend pod: ${FR_POD}"

# Locate ClickHouse
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

# Vector health (best-effort)
VEC_POD="$(find_vector_pod || true)"
if [ -z "${VEC_POD:-}" ]; then
  log "[warn] no Vector pod found in ${VECTOR_NS}; continuing (Vector may not be installed)"
else
  log "[step 2] check Vector pod ${VEC_POD} health (best-effort)"
  if vector_wait_for_healthy "${VEC_POD}" 6; then
    log "[ok] Vector appears healthy"
  else
    log "[warn] Vector did not show health quickly; continuing (may miss logs)"
    kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=80 || true
  fi
fi

# Baseline ClickHouse count
log "[step 3] baseline ClickHouse count for token"
BASE_Q="SELECT count() FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%';"
BASE_COUNT="$(kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "clickhouse-client --query \"${BASE_Q}\"" 2>/dev/null || echo "ERR")"
log "baseline rows for token=${TOKEN}: ${BASE_COUNT}"

# Emit two probes using transient curl pod (external client pattern)
log "[step 4] emit 2 minimal health probes to frontend via transient curl pod"
CTP="curltmp-frontend-$(date +%s)"
kubectl -n "${APP_NS}" run "${CTP}" --restart=Never --image="${CURL_IMG}" --command -- \
  sh -c "curl -sS --max-time 8 'http://${FR_SVC}/orchestrator/health?e2e=${TOKEN}&i=1' || true; sleep 1; curl -sS --max-time 8 'http://${FR_SVC}/orchestrator/health?e2e=${TOKEN}&i=2' || true" >/dev/null 2>&1 || true
kubectl -n "${APP_NS}" wait pod "${CTP}" --for=condition=Succeeded --timeout="${CURL_POD_TIMEOUT}s" >/dev/null 2>&1 || true
kubectl -n "${APP_NS}" delete pod "${CTP}" --ignore-not-found >/dev/null 2>&1 || true
log "transient curl pod done"

# Short wait for Vector to ship logs
log "[step 5] brief wait ${VECTOR_SHIP_WAIT}s for Vector to ship logs"
sleep "${VECTOR_SHIP_WAIT}"

# Verify ClickHouse ingestion for token
log "[step 6] verify ClickHouse ingestion for token"
VERIFY_Q="SELECT count() AS rows, any(service) AS service, any(pod) AS pod, any(namespace) AS namespace, min(ts) AS first_ts, max(ts) AS last_ts FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%' FORMAT Vertical"
if ! safe_clickhouse_query "${CH_POD}" "${VERIFY_Q}"; then
  log "[warn] verification query failed; dumping diagnostics"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  if [ -n "${VEC_POD:-}" ]; then kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=200 || true; fi
fi

# Rollout restart frontend deployment to test producer restart
log "[step 7] rollout restart frontend deployment (quick)"
DEPLOY_NAME="$(kubectl -n "${APP_NS}" get deployments -l "${APP_LABEL}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -n "${DEPLOY_NAME}" ]; then
  kubectl -n "${APP_NS}" rollout restart deployment "${DEPLOY_NAME}" >/dev/null 2>&1 || true
  log "[step 7] waiting up to ${ROLLING_WAIT}s for ${DEPLOY_NAME} to become available"
  kubectl -n "${APP_NS}" wait deployment "${DEPLOY_NAME}" --for=condition=available --timeout="${ROLLING_WAIT}s" >/dev/null 2>&1 || log "[warn] deployment not fully available within ${ROLLING_WAIT}s"
  NEW_POD="$(kubectl -n "${APP_NS}" get pod -l "${APP_LABEL}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  log "post-restart frontend pod: ${NEW_POD}"
  # Emit a single probe from a transient curl pod again
  CT2="curltmp-frontend-after-$(date +%s)"
  kubectl -n "${APP_NS}" run "${CT2}" --restart=Never --image="${CURL_IMG}" --command -- \
    sh -c "curl -sS --max-time 8 'http://${FR_SVC}/orchestrator/health?e2e=${TOKEN}&i=after_restart' || true" >/dev/null 2>&1 || true
  kubectl -n "${APP_NS}" wait pod "${CT2}" --for=condition=Succeeded --timeout="${CURL_POD_TIMEOUT}s" >/dev/null 2>&1 || true
  kubectl -n "${APP_NS}" delete pod "${CT2}" --ignore-not-found >/dev/null 2>&1 || true
else
  log "[warn] no frontend deployment found; skipping rollout restart"
fi

# Small sleep then re-check
sleep 3
log "[step 8] post-restart verification"
if ! safe_clickhouse_query "${CH_POD}" "${VERIFY_Q}"; then
  log "[warn] post-restart verification failed; dumping diagnostics"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  if [ -n "${VEC_POD:-}" ]; then kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=200 || true; fi
fi

# Emit a small malformed line to test parser resilience
log "[step 9] emit a single malformed line via transient pod"
MAL_POD="malformed-frontend-${TOKEN}"
kubectl -n "${APP_NS}" run "${MAL_POD}" --restart=Never --image=alpine --command -- \
  sh -c "printf 'NOT_JSON_LINE ${TOKEN}\\n'; sleep 1" >/dev/null 2>&1 || true
kubectl -n "${APP_NS}" wait pod "${MAL_POD}" --for=condition=Succeeded --timeout="${MALFORMED_POD_WAIT}s" >/dev/null 2>&1 || true
kubectl -n "${APP_NS}" delete pod "${MAL_POD}" --ignore-not-found >/dev/null 2>&1 || true
sleep 3

# Final aggregated verification
log "[step 10] final ClickHouse check (all occurrences of token)"
FINAL_Q="SELECT count() AS rows, any(namespace) AS namespace, any(pod) AS pod FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%' FORMAT Vertical"
if ! safe_clickhouse_query "${CH_POD}" "${FINAL_Q}"; then
  log "[warn] final check failed; dumping diagnostics"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  if [ -n "${VEC_POD:-}" ]; then kubectl -n "${VECTOR_NS}" logs "${VEC_POD}" --tail=400 || true; fi
fi

log "chaos_frontend_quick complete - token=${TOKEN}"
exit 0
