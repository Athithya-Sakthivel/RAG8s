#!/usr/bin/env bash
LOG(){ printf '%s %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }

VM_NAMESPACE=${VM_NAMESPACE:-monitoring}
VICTORIA_PORT=${VICTORIA_PORT:-8428}
VMAGENT_PORT=${VMAGENT_PORT:-8429}

RETRIEVAL_NAMESPACE=${RETRIEVAL_NAMESPACE:-inference}
RETRIEVAL_NAME=${RETRIEVAL_NAME:-retrieval}
RETRIEVAL_METRICS_PORT=${RETRIEVAL_METRICS_PORT:-8001}

LOAD_GEN=${LOAD_GEN:-true}
LOAD_SECONDS=${LOAD_SECONDS:-20}
QUERY_RETRIES=${QUERY_RETRIES:-6}
RETRY_BACKOFF=${RETRY_BACKOFF:-3}
QUERY_SLEEP=${QUERY_SLEEP:-1}

CURL_BIN=${CURL_BIN:-curl}
PYTHON_BIN=${PYTHON_BIN:-python3}

require(){ command -v "$1" >/dev/null 2>&1 || { ERR "$1 required"; exit 2; } }
require kubectl
require "${CURL_BIN}"
require "${PYTHON_BIN}"
require jq
require mktemp
require sed
require awk
require grep

TMPFILES=()
PFPIDS=()
cleanup(){
  rc=$?
  for pid in "${PFPIDS[@]:-}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  for f in "${TMPFILES[@]:-}"; do
    [ -f "$f" ] && rm -f "$f" || true
  done
  exit $rc
}
trap cleanup INT TERM EXIT

find_free_port(){
  "${PYTHON_BIN}" - <<PY
import socket
s=socket.socket()
s.bind(('',0))
p=s.getsockname()[1]
s.close()
print(p)
PY
}

start_portforward(){
  local ns="$1"; local target="$2"; local local_port="$3"; local remote_port="$4"
  local logfile
  logfile="$(mktemp /tmp/portforward.${target//[^a-zA-Z0-9_.-]/_}.XXXXXX.log)"
  TMPFILES+=("${logfile}")
  kubectl -n "${ns}" port-forward "${target}" "${local_port}:${remote_port}" > "${logfile}" 2>&1 &
  local pid=$!
  PFPIDS+=("${pid}")
  printf '%s|%s' "${pid}" "${logfile}"
}

wait_for_http(){
  local url="$1"; local timeout="$2"; local end=$((SECONDS + timeout))
  while [ "${SECONDS}" -lt "${end}" ]; do
    if "${CURL_BIN}" -sS --max-time 3 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

probe_vmagent_targets(){
  local port="$1"
  local tries=0 max=20
  while [ $tries -lt $max ]; do
    if "${CURL_BIN}" -sS "http://127.0.0.1:${port}/metrics" | grep -E 'vm_promscrape|vmagent_' >/dev/null 2>&1; then
      LOG "vmagent exposes scrape metrics locally"
      return 0
    fi
    tries=$((tries+1))
    sleep 2
  done
  ERR "vmagent metrics unavailable locally after wait"
  return 1
}

run_promql_with_retries(){
  local name="$1"; local promql="$2"; local expect="$3"
  local last_json=""
  local base="http://127.0.0.1:${LOCAL_VICTORIA_PORT}/api/v1/query"
  for attempt in $(seq 1 "${QUERY_RETRIES}"); do
    LOG "PromQL ${name} attempt ${attempt}/${QUERY_RETRIES}: ${promql}"
    json="$("${CURL_BIN}" -sS -G --data-urlencode "query=${promql}" "${base}" 2>/dev/null || echo "")"
    last_json="${json}"
    ok=$(echo "${json}" | jq -r 'select(.status=="success") | .data.result | length' 2>/dev/null || echo "0")
    if [ "${ok}" != "0" ]; then
      LOG "PromQL ${name} returned ${ok} result(s)"
      val=$(echo "${json}" | jq -r '.data.result[0].value[1]' 2>/dev/null || echo "")
      if [ "${expect}" = "gt0" ]; then
        if [ -n "${val}" ] && "${PYTHON_BIN}" - <<PY "${val}"
import sys
try: v=float(sys.argv[1]); sys.exit(0 if v>0 else 1)
except: sys.exit(2)
PY
        then LOG "PASS ${name} -> ${val}"; return 0; fi
      elif [ "${expect}" = "eq1" ]; then
        if [ -n "${val}" ] && "${PYTHON_BIN}" - <<PY "${val}"
import sys,math
try: v=float(sys.argv[1]); sys.exit(0 if math.isclose(v,1.0,rel_tol=0,abs_tol=1e-9) else 1)
except: sys.exit(2)
PY
        then LOG "PASS ${name} -> ${val}"; return 0; fi
      elif [ "${expect}" = "anynum" ]; then
        if [ -n "${val}" ] && "${PYTHON_BIN}" - <<PY "${val}"
import sys
try: float(sys.argv[1]); sys.exit(0)
except: sys.exit(2)
PY
        then LOG "PASS ${name} -> ${val}"; return 0; fi
      fi
    else
      LOG "PromQL ${name} produced no results; retrying"
    fi
    sleep $((RETRY_BACKOFF * attempt + QUERY_SLEEP))
  done
  ERR "FAIL ${name} after ${QUERY_RETRIES} attempts; last response:"
  if [ -n "${last_json}" ]; then
    echo "${last_json}" | jq . || echo "${last_json}"
  else
    echo "{}"
  fi
  return 1
}

LOG "starting VictoriaMetrics port-forward (svc/victoria-metrics ns=${VM_NAMESPACE})"
if [ "${LOCAL_VICTORIA_PORT:-0}" -eq 0 ]; then LOCAL_VICTORIA_PORT="$(find_free_port)"; fi
pv="$(start_portforward "${VM_NAMESPACE}" "svc/victoria-metrics" "${LOCAL_VICTORIA_PORT}" "${VICTORIA_PORT}")"
LOG "waiting up to ${PORTFWD_READY_TIMEOUT}s for VictoriaMetrics /metrics"
if ! wait_for_http "http://127.0.0.1:${LOCAL_VICTORIA_PORT}/metrics" "${PORTFWD_READY_TIMEOUT}"; then
  pfile="$(printf '%s' "${pv}" | awk -F'|' '{print $2}')"
  ERR "victoria-metrics port-forward not ready; tail ${pfile}"
  tail -n 200 "${pfile}" || true
  exit 4
fi
LOG "VictoriaMetrics port-forward ready (local:${LOCAL_VICTORIA_PORT})"

LOG "starting vmagent port-forward (svc/vmagent ns=${VM_NAMESPACE})"
if [ "${LOCAL_VMAGENT_PORT:-0}" -eq 0 ]; then LOCAL_VMAGENT_PORT="$(find_free_port)"; fi
pm="$(start_portforward "${VM_NAMESPACE}" "svc/vmagent" "${LOCAL_VMAGENT_PORT}" "${VMAGENT_PORT}")"
LOG "waiting up to ${PORTFWD_READY_TIMEOUT}s for vmagent /metrics"
if ! wait_for_http "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" "${PORTFWD_READY_TIMEOUT}"; then
  pfile="$(printf '%s' "${pm}" | awk -F'|' '{print $2}')"
  ERR "vmagent port-forward not ready; tail ${pfile}"
  tail -n 200 "${pfile}" || true
  exit 5
fi
LOG "vmagent port-forward ready (local:${LOCAL_VMAGENT_PORT})"

LOG "probing vmagent for scrape metrics"
if ! probe_vmagent_targets "${LOCAL_VMAGENT_PORT}"; then
  ERR "vmagent does not expose scrape metrics; cannot continue"
  exit 6
fi

LOG "discovering retriever pods in namespace ${RETRIEVAL_NAMESPACE}"
PODS="$(kubectl -n "${RETRIEVAL_NAMESPACE}" get pods -l "app.kubernetes.io/name=${RETRIEVAL_NAME}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)"
if [ -z "${PODS}" ]; then
  PODS="$(kubectl -n "${RETRIEVAL_NAMESPACE}" get pods -l "app=${RETRIEVAL_NAME}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)"
fi
if [ -z "${PODS}" ]; then
  ERR "no retriever pods found in ${RETRIEVAL_NAMESPACE}"
  exit 7
fi
LOG "found retriever pods: ${PODS}"

for pod in ${PODS}; do
  LOG "validating annotations for pod ${pod}"
  ann_scrape="$(kubectl -n "${RETRIEVAL_NAMESPACE}" get pod "${pod}" -o jsonpath='{.metadata.annotations.monitoring\.io/scrape}' 2>/dev/null || true)"
  ann_port="$(kubectl -n "${RETRIEVAL_NAMESPACE}" get pod "${pod}" -o jsonpath='{.metadata.annotations.monitoring\.io/port}' 2>/dev/null || true)"
  ann_path="$(kubectl -n "${RETRIEVAL_NAMESPACE}" get pod "${pod}" -o jsonpath='{.metadata.annotations.monitoring\.io/path}' 2>/dev/null || true)"
  LOG "annotations: scrape=${ann_scrape:-<unset>} port=${ann_port:-<unset>} path=${ann_path:-<unset>}"
  if [ "${ann_scrape}" != "true" ]; then ERR "annotation monitoring.io/scrape=true missing on ${pod}"; exit 8; fi
  if ! printf '%s' "${ann_port}" | grep -qE '^[0-9]+$'; then ERR "annotation monitoring.io/port must be numeric on ${pod} (found: ${ann_port})"; exit 9; fi
  if [ "${ann_port}" != "${RETRIEVAL_METRICS_PORT}" ]; then ERR "annotation monitoring.io/port mismatch expected ${RETRIEVAL_METRICS_PORT} found ${ann_port}"; exit 10; fi
  if [ "${ann_path}" != "/metrics" ]; then ERR "annotation monitoring.io/path must be /metrics on ${pod} (found: ${ann_path})"; exit 11; fi
done

LOG "port-forwarding service/retrieval in ${RETRIEVAL_NAMESPACE}"
LOCAL_RETRIEVAL_PORT="$(find_free_port)"
rv="$(start_portforward "${RETRIEVAL_NAMESPACE}" "svc/${RETRIEVAL_NAME}" "${LOCAL_RETRIEVAL_PORT}" "${RETRIEVAL_METRICS_PORT}")"
if ! wait_for_http "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/metrics" 10; then
  pfile="$(printf '%s' "${rv}" | awk -F'|' '{print $2}')"
  ERR "retriever service /metrics not ready; tail ${pfile}"
  tail -n 200 "${pfile}" || true
  exit 12
fi
LOG "retriever /metrics ready at localhost:${LOCAL_RETRIEVAL_PORT}"

if [ "$(printf '%s' "${LOAD_GEN}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
  LOG "LOAD_GEN=true: generating load for ${LOAD_SECONDS}s against retriever svc"
  i=0
  while [ "$i" -lt "${LOAD_SECONDS}" ]; do
    "${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/" >/dev/null 2>&1 || true
    i=$((i+1))
    sleep 1
  done
  LOG "load generation complete"
else
  LOG "LOAD_GEN not enabled; skipping synthetic requests"
fi

LOG "fetching head of retriever /metrics"
"${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/metrics" | sed -n '1,120p' || true

LOG "validating vmagent -> victoria ingestion for retriever via PromQL"
run_promql_with_retries "retriever_series_any" 'count({__name__=~"app_info|retrieval_requests_total|retrieval_errors_total"})' gt0 || { ERR "named retriever metrics not present in TSDB; scraping or remote-write broken"; exit 13; }

run_promql_with_retries "retriever_series_with_service_label" 'count({__name__=~"app_info|retrieval_requests_total|retrieval_errors_total",service=~".+"})' anynum || { ERR "retriever series present but missing exported 'service' label; update vmagent relabel to inject 'service' from pod labels"; exit 14; }

run_promql_with_retries "up_service_retrieval" 'max(up{service="'${RETRIEVAL_NAME}'"})' eq1 || { ERR "up{service=${RETRIEVAL_NAME}} not present in TSDB"; exit 15; }

if [ "$(printf '%s' "${LOAD_GEN}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
  run_promql_with_retries "retriever_requests_active" 'sum(increase(retrieval_requests_total{service="'${RETRIEVAL_NAME}'"}[30s]))' gt0 || { ERR "no request traffic observed for ${RETRIEVAL_NAME} after load generation"; exit 16; }
fi

LOG "ALL retriever checks passed"
exit 0
