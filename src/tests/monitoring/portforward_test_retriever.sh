#!/usr/bin/env bash
make deploy-models
make deploy-retriever
LOG(){ printf '%s %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }

RETRIEVAL_NAMESPACE=${RETRIEVAL_NAMESPACE:-inference}
RETRIEVAL_SERVICE=${RETRIEVAL_NAME:-retrieval}
RETRIEVAL_METRICS_PORT=${RETRIEVAL_METRICS_PORT:-8001}
RETRIEVAL_HTTP_PORT=${RETRIEVAL_HTTP_PORT:-8001}
VM_NAMESPACE=${VM_NAMESPACE:-monitoring}
VICTORIA_SERVICE=${VICTORIA_SERVICE:-victoria-metrics}
VICTORIA_PORT=${VICTORIA_PORT:-8428}

CURL_BIN=${CURL_BIN:-curl}
PYTHON_BIN=${PYTHON_BIN:-python3}
KUBECTL_BIN=${KUBECTL_BIN:-kubectl}

DURATION=0
PORTFWD_VICTORIA=false

parse_args(){
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --victoria) PORTFWD_VICTORIA=true; shift;;
      --duration) DURATION="${2:-0}"; shift 2;;
      --help) echo "usage: $0 [--victoria] [--duration N]"; exit 0;;
      *) ERR "unknown arg $1"; exit 2;;
    esac
  done
}

require(){ command -v "$1" >/dev/null 2>&1 || { ERR "$1 required"; exit 2; } }
require "${KUBECTL_BIN}"
require "${CURL_BIN}"
require "${PYTHON_BIN}"
require mktemp
require awk
require sed
require grep
require jq

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
import socket,sys
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
  "${KUBECTL_BIN}" -n "${ns}" port-forward "${target}" "${local_port}:${remote_port}" > "${logfile}" 2>&1 &
  local pid=$!
  PFPIDS+=("${pid}")
  printf '%s|%s' "${pid}" "${logfile}"
}

wait_for_http(){
  local url="$1"; local timeout="${2:-20}"; local end=$((SECONDS + timeout))
  while [ "${SECONDS}" -lt "${end}" ]; do
    if "${CURL_BIN}" -sS --max-time 3 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

display_retriever_context(){
  LOG "Retrieval namespace: ${RETRIEVAL_NAMESPACE}"
  local pod
  pod="$("${KUBECTL_BIN}" -n "${RETRIEVAL_NAMESPACE}" get pods -l "app=${RETRIEVAL_SERVICE}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -z "${pod}" ]; then
    pod="$("${KUBECTL_BIN}" -n "${RETRIEVAL_NAMESPACE}" get pods -l "app.kubernetes.io/name=${RETRIEVAL_SERVICE}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  fi
  LOG "retriever pod: ${pod:-<none>}"
  if [ -n "${pod}" ]; then
    LOG "pod status:"
    "${KUBECTL_BIN}" -n "${RETRIEVAL_NAMESPACE}" get pod "${pod}" -o wide || true
    LOG "last 200 lines of pod logs (container name=${RETRIEVAL_SERVICE}):"
    "${KUBECTL_BIN}" -n "${RETRIEVAL_NAMESPACE}" logs "${pod}" -c "${RETRIEVAL_SERVICE}" --tail=200 || true
  fi
}

parse_args "$@"

LOG "discovering retriever service/pod"
svc_exists="$("${KUBECTL_BIN}" -n "${RETRIEVAL_NAMESPACE}" get svc "${RETRIEVAL_SERVICE}" -o name 2>/dev/null || true)"
pod_exists="$("${KUBECTL_BIN}" -n "${RETRIEVAL_NAMESPACE}" get pods -l "app=${RETRIEVAL_SERVICE}" -o name 2>/dev/null || true)"
if [ -z "${svc_exists}" ] && [ -z "${pod_exists}" ]; then
  ERR "retriever service/pod not found in namespace ${RETRIEVAL_NAMESPACE}"
  exit 3
fi
display_retriever_context

LOCAL_RETRIEVAL_PORT="$(find_free_port)"
pfinfo="$(start_portforward "${RETRIEVAL_NAMESPACE}" "svc/${RETRIEVAL_SERVICE}" "${LOCAL_RETRIEVAL_PORT}" "${RETRIEVAL_HTTP_PORT}")"
pfpid="$(printf '%s' "${pfinfo}" | awk -F'|' '{print $1}')"
pflog="$(printf '%s' "${pfinfo}" | awk -F'|' '{print $2}')"
LOG "started port-forward svc/${RETRIEVAL_SERVICE} -> localhost:${LOCAL_RETRIEVAL_PORT} (pid=${pfpid} log=${pflog})"

LOG "waiting for retriever /readyz"
if ! wait_for_http "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/readyz" 20; then
  ERR "retriever /readyz not responding; tail ${pflog}"
  tail -n 200 "${pflog}" || true
  exit 4
fi
LOG "retriever /readyz OK"

LOG "fetching /healthz and /metrics from retriever"
"${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/healthz" | jq -c . || true
echo
"${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/metrics" | egrep -i 'retrieval_requests_total|retrieval_errors_total|service_ready' | sed -n '1,120p' || true

PRE_REQ_COUNT="$("${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/metrics" | grep -E '^retrieval_requests_total' | awk -F' ' '{print $2}' | awk '{s+=$1}END{print s+0}' || echo 0)"

if [ "${DURATION}" -gt 0 ]; then
  LOG "generating synthetic load (${DURATION}s) against retriever root"
  end=$((SECONDS + DURATION))
  while [ "${SECONDS}" -lt "${end}" ]; do
    "${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/" >/dev/null 2>&1 || true
    sleep 0.5
  done
  LOG "load generation complete"
fi

LOG "exercising /generate (sample request)"
GEN_OUT="$("${CURL_BIN}" -sS -X POST "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/generate" -H "Content-Type: application/json" -d '{"query":"test query for monitoring","enable_tracing":false,"top_k":3}' -m 10 || true)"
echo "generate -> ${GEN_OUT}"
echo

LOG "fetching metrics after exercise (filtered)"
"${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/metrics" | egrep -i 'retrieval_requests_total|retrieval_errors_total|retrieval_request_duration_seconds' | sed -n '1,160p' || true

POST_REQ_COUNT="$("${CURL_BIN}" -sS "http://127.0.0.1:${LOCAL_RETRIEVAL_PORT}/metrics" | grep -E '^retrieval_requests_total' | awk -F' ' '{print $2}' | awk '{s+=$1}END{print s+0}' || echo 0)"

LOG "requests_total before=${PRE_REQ_COUNT} after=${POST_REQ_COUNT}"

if [ "${PORTFWD_VICTORIA}" = true ]; then
  LOG "also port-forwarding victoria-metrics to validate ingestion"
  LOCAL_VICTORIA_PORT="$(find_free_port)"
  vpf="$(start_portforward "${VM_NAMESPACE}" "svc/${VICTORIA_SERVICE}" "${LOCAL_VICTORIA_PORT}" "${VICTORIA_PORT}")"
  vpfpid="$(printf '%s' "${vpf}" | awk -F'|' '{print $1}')"
  vpflog="$(printf '%s' "${vpf}" | awk -F'|' '{print $2}')"
  LOG "waiting for VictoriaMetrics /metrics"
  if ! wait_for_http "http://127.0.0.1:${LOCAL_VICTORIA_PORT}/metrics" 30; then
    ERR "victoria /metrics not ready; tail ${vpflog}"
    tail -n 200 "${vpflog}" || true
    exit 5
  fi
  LOG "VictoriaMetrics ready on ${LOCAL_VICTORIA_PORT}"
  LOG "PromQL check for retrieval_requests_total increase"
  Q='increase(retrieval_requests_total[5m])'
  resp="$("${CURL_BIN}" -sS -G --data-urlencode "query=${Q}" "http://127.0.0.1:${LOCAL_VICTORIA_PORT}/api/v1/query" || true)"
  LOG "Victoria PromQL raw response:"
  echo "${resp}" | jq -c . || echo "${resp}"
fi

LOG "done; cleaning up port-forwards (trap will ensure this)"
if kill -0 "${pfpid}" >/dev/null 2>&1; then
  kill "${pfpid}" >/dev/null 2>&1 || true
  wait "${pfpid}" 2>/dev/null || true
fi
if [ "${PORTFWD_VICTORIA}" = true ] && kill -0 "${vpfpid}" >/dev/null 2>&1; then
  kill "${vpfpid}" >/dev/null 2>&1 || true
  wait "${vpfpid}" 2>/dev/null || true
fi
exit 0
