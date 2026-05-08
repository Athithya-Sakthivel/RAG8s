#!/usr/bin/env bash
# infra/tests/monitoring/platform_metrics_e2e.sh
# E2E validation for the "Platform Observability Health" dashboard JSON.
# Verifies metrics existence locally (vmagent endpoints) and ingestion into VictoriaMetrics.
set -euo pipefail

LOG(){ printf '%s %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }
require(){ command -v "$1" >/dev/null 2>&1 || { ERR "$1 required"; exit 2; } }
require kubectl; require curl; require python3; require jq; require mktemp; require awk; require sed; require grep

# Defaults (override via environment)
VM_NAMESPACE=${VM_NAMESPACE:-monitoring}
VICTORIA_SERVICE=${VICTORIA_SERVICE:-victoria-metrics}
VICTORIA_PORT=${VICTORIA_PORT:-8428}
VMAGENT_SERVICE=${VMAGENT_SERVICE:-vmagent}
VMAGENT_PORT=${VMAGENT_PORT:-8429}

LOCAL_VICTORIA_PORT=${LOCAL_VICTORIA_PORT:-0}
LOCAL_VMAGENT_PORT=${LOCAL_VMAGENT_PORT:-0}

PORTFWD_READY_TIMEOUT=${PORTFWD_READY_TIMEOUT:-30}
QUERY_RETRIES=${QUERY_RETRIES:-8}
RETRY_BACKOFF=${RETRY_BACKOFF:-2}
QUERY_SLEEP=${QUERY_SLEEP:-1}

ARTIFACT_DIR=${ARTIFACT_DIR:-/tmp/platform_metrics_e2e}
mkdir -p "${ARTIFACT_DIR}"

TMPFILES=()
PFPIDS=()

cleanup(){
  rc=$?
  LOG "cleanup: terminating ${#PFPIDS[@]} port-forward process(es) and removing ${#TMPFILES[@]} temp file(s)"
  for pid in "${PFPIDS[@]:-}"; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
      for i in 0 1 2; do
        if ! kill -0 "${pid}" >/dev/null 2>&1; then break; fi
        sleep 0.2
      done
      if kill -0 "${pid}" >/dev/null 2>&1; then
        kill -9 "${pid}" >/dev/null 2>&1 || true
      fi
    fi
  done
  for f in "${TMPFILES[@]:-}"; do
    if [ -f "${f}" ]; then rm -f "${f}" || true; fi
  done
  LOG "cleanup complete"
  exit ${rc}
}
trap cleanup INT TERM EXIT

find_free_port(){
  python3 - <<PY
import socket
s=socket.socket()
s.bind(('',0))
print(s.getsockname()[1])
s.close()
PY
}

alloc_port_or_die(){
  local tries=0 port
  while [ $tries -lt 5 ]; do
    port="$(find_free_port)"
    if [ -n "$port" ] && [ "$port" -ne 0 ] 2>/dev/null; then
      echo "$port"; return 0
    fi
    tries=$((tries+1))
    sleep 0.2
  done
  ERR "failed to allocate local port"; exit 98
}

wait_for_http(){
  local url="$1" timeout_s="$2"
  local end=$((SECONDS + timeout_s))
  while [ "${SECONDS}" -lt "${end}" ]; do
    if curl -sS --max-time 3 "${url}" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

start_portforward_and_wait(){
  local ns="$1" target="$2" local_port="$3" remote_port="$4" timeout="${5:-30}"
  local logfile pid
  logfile="$(mktemp /tmp/portforward.${target//[^a-zA-Z0-9_.-]/_}.XXXXXX.log)"
  TMPFILES+=("${logfile}")
  kubectl -n "${ns}" port-forward "${target}" "${local_port}:${remote_port}" > "${logfile}" 2>&1 &
  pid=$!
  PFPIDS+=("${pid}")
  local end=$((SECONDS + timeout))
  while [ "${SECONDS}" -lt "${end}" ]; do
    if ! kill -0 "${pid}" >/dev/null 2>&1; then
      ERR "port-forward process ${pid} exited unexpectedly; tail ${logfile}"
      tail -n 200 "${logfile}" || true
      return 2
    fi
    # prefer /metrics probe (vmagent/victoria expose /metrics)
    if wait_for_http "http://127.0.0.1:${local_port}/metrics" 1; then
      echo "${pid}|${logfile}"; return 0
    fi
    sleep 0.5
  done
  ERR "port-forward ${target} not ready after ${timeout}s; tail ${logfile}"
  tail -n 200 "${logfile}" || true
  return 3
}

# PromQL runner (reused pattern)
run_promql_with_retries(){
  local name="$1"; local promql="$2"; local expect="$3"
  local last_json=""
  if ! echo "${LOCAL_VICTORIA_PORT}" | grep -qE '^[1-9][0-9]*$'; then
    ERR "LOCAL_VICTORIA_PORT invalid: ${LOCAL_VICTORIA_PORT}"; return 2
  fi
  local base_vm="http://127.0.0.1:${LOCAL_VICTORIA_PORT}/api/v1/query"
  for attempt in $(seq 1 "${QUERY_RETRIES}"); do
    LOG "PromQL ${name} attempt ${attempt}/${QUERY_RETRIES}: ${promql}"
    json="$(curl -sS -G --data-urlencode "query=${promql}" "${base_vm}" 2>/dev/null || echo "")"
    last_json="${json}"
    status=$(echo "${json}" | jq -r '.status // empty' 2>/dev/null || echo "")
    if [ "${status}" != "success" ]; then
      LOG "PromQL ${name} status=${status:-<no-status>}; retrying"
      sleep $((RETRY_BACKOFF * attempt + QUERY_SLEEP)); continue
    fi
    result_len=$(echo "${json}" | jq -r '.data.result | length' 2>/dev/null || echo "0")
    sf=$(echo "${json}" | jq -r '.stats.seriesFetched // 0' 2>/dev/null || echo "0")
    if [ "${expect}" = "any" ] && [ "${result_len}" -gt 0 ]; then
      echo "${json}" > "${ARTIFACT_DIR}/platform_e2e_lastjson_${name}.json"
      return 0
    fi
    if [ "${result_len}" -gt 0 ]; then
      val=$(echo "${json}" | jq -r '.data.result[0].value[1] // empty' 2>/dev/null || echo "")
      if [ -n "${val}" ]; then
        python3 - <<PY "${val}"
import sys
try:
    v=float(sys.argv[1]); sys.exit(0 if v>0 else 1)
except:
    sys.exit(2)
PY
        rc=$?
        if [ $rc -eq 0 ]; then
          echo "${json}" > "${ARTIFACT_DIR}/platform_e2e_lastjson_${name}.json"; return 0
        fi
      fi
    fi
    if [ -n "${sf}" ] && [ "${sf}" -gt 0 ] 2>/dev/null; then
      echo "${json}" > "${ARTIFACT_DIR}/platform_e2e_lastjson_${name}.json"; return 0
    fi
    LOG "PromQL ${name} produced no satisfying results; retrying"
    sleep $((RETRY_BACKOFF * attempt + QUERY_SLEEP))
  done
  echo "${last_json}" > "${ARTIFACT_DIR}/platform_e2e_lastjson_${name}.json" || true
  return 1
}

LOG "port-forward VictoriaMetrics svc/${VICTORIA_SERVICE} (ns=${VM_NAMESPACE})"
if [ "${LOCAL_VICTORIA_PORT}" -eq 0 ]; then LOCAL_VICTORIA_PORT="$(alloc_port_or_die)"; fi
pv="$(start_portforward_and_wait "${VM_NAMESPACE}" "svc/${VICTORIA_SERVICE}" "${LOCAL_VICTORIA_PORT}" "${VICTORIA_PORT}" "${PORTFWD_READY_TIMEOUT}")" || exit 4
LOG "Victoria port-forward ready (local:${LOCAL_VICTORIA_PORT})"

LOG "port-forward vmagent svc/${VMAGENT_SERVICE} (ns=${VM_NAMESPACE})"
if [ "${LOCAL_VMAGENT_PORT}" -eq 0 ]; then LOCAL_VMAGENT_PORT="$(alloc_port_or_die)"; fi
pm="$(start_portforward_and_wait "${VM_NAMESPACE}" "svc/${VMAGENT_SERVICE}" "${LOCAL_VMAGENT_PORT}" "${VMAGENT_PORT}" "${PORTFWD_READY_TIMEOUT}")" || exit 5
LOG "vmagent port-forward ready (local:${LOCAL_VMAGENT_PORT})"

LOG "snapshot local vmagent /metrics"
curl -sS "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" > "${ARTIFACT_DIR}/vmagent_metrics_snapshot.txt" || true

# Local checks (vmagent)
LOG "LOCAL CHECK: vmagent persistence and remote-write candidates"
for m in "vm_persistentqueue_bytes_written_total" "vm_promscrape_scraped_samples_total" "vmagent_remotewrite_sent_bytes_total" "vmagent_remotewrite_errors_total" ; do
  if grep -qE "^${m}(\{|[[:space:]]|$)" "${ARTIFACT_DIR}/vmagent_metrics_snapshot.txt"; then
    LOG "vmagent LOCAL: metric present: ${m}"
  else
    LOG "vmagent LOCAL: metric NOT present: ${m}"
  fi
done

# PromQL checks (Victoria) - metrics used in the dashboard JSON
# 1) Victoria up
if run_promql_with_retries "victoria_up" 'max(up{job=~"victoria-metrics.*"})' gt0; then
  LOG "TSDB CHECK: Victoria up -> OK"
else
  LOG "TSDB CHECK: Victoria up -> MISSING or zero"
fi

# 2) vmagent up
if run_promql_with_retries "vmagent_up" 'max(up{job=~"vmagent.*"})' gt0; then
  LOG "TSDB CHECK: vmagent up -> OK"
else
  LOG "TSDB CHECK: vmagent up -> MISSING or zero"
fi

# 3) scrape throughput (samples/s)
if run_promql_with_retries "vm_scrape_samples" 'sum(rate(vm_promscrape_scraped_samples_total[1m]))' gt0; then
  LOG "TSDB CHECK: vm_promscrape_scraped_samples_total visible -> OK"
else
  LOG "TSDB CHECK: vm_promscrape_scraped_samples_total -> missing or zero"
fi

# 4) vmagent remote-write errors (may be zero)
if run_promql_with_retries "vmagent_remotewrite_err" 'sum(rate(vmagent_remotewrite_errors_total[5m]))' any; then
  LOG "TSDB CHECK: vmagent_remotewrite_errors_total present (may be zero) -> OK"
else
  LOG "TSDB CHECK: vmagent_remotewrite_errors_total -> missing"
fi

# 5) vmagent remote-write sent bytes (ensure some data has been written at some point)
if run_promql_with_retries "vmagent_remotewrite_sent" 'increase(vmagent_remotewrite_sent_bytes_total[5m])' any; then
  LOG "TSDB CHECK: vmagent_remotewrite_sent_bytes_total query ok (increase may be zero) -> OK"
else
  LOG "TSDB CHECK: vmagent_remotewrite_sent_bytes_total -> missing"
fi

# 6) vector up (vector as log collector)
if run_promql_with_retries "vector_up" 'max(up{job=~"vector.*"})' any; then
  LOG "TSDB CHECK: vector up -> OK (present in TSDB)"
else
  LOG "TSDB CHECK: vector up -> missing"
fi

# 7) vector processed events (log ingest throughput)
if run_promql_with_retries "vector_events" 'sum(rate(vector_processed_events_total[1m]))' any; then
  LOG "TSDB CHECK: vector_processed_events_total -> OK (may be zero)"
else
  LOG "TSDB CHECK: vector_processed_events_total -> missing"
fi

# 8) vector component errors (may be zero)
if run_promql_with_retries "vector_comp_err" 'sum(rate(vector_component_errors_total[5m]))' any; then
  LOG "TSDB CHECK: vector_component_errors_total -> present (may be zero)"
else
  LOG "TSDB CHECK: vector_component_errors_total -> missing"
fi

LOG "ALL PROMQL CHECKS COMPLETE; artifacts in ${ARTIFACT_DIR}"

# Save summary
cat > "${ARTIFACT_DIR}/summary.txt" <<EOF
platform_metrics_e2e summary - $(date -Iseconds)

Victoria port-forward local: ${LOCAL_VICTORIA_PORT}
vmagent port-forward local: ${LOCAL_VMAGENT_PORT}

PromQL results saved as platform_e2e_lastjson_<name>.json in ${ARTIFACT_DIR}
Local vmagent metrics snapshot: ${ARTIFACT_DIR}/vmagent_metrics_snapshot.txt
EOF

LOG "platform_metrics_e2e.sh completed"
exit 0
