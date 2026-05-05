#!/usr/bin/env bash
set -euo pipefail
LOG(){ printf '%s INFO %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }
REQUIRE(){ command -v "$1" >/dev/null 2>&1 || { ERR "$1 required"; exit 2; } }
REQUIRE kubectl
REQUIRE curl
REQUIRE jq
UUID=$(cat /proc/sys/kernel/random/uuid)
VM_NAMESPACE=${VM_NAMESPACE:-monitoring}
VICTORIA_SVC=${VICTORIA_SVC:-victoria-metrics}
ALERTM_SVC=${ALERTM_SVC:-alertmanager}
QDRANT_NS=${QDRANT_NS:-qdrant}
RETRIEVAL_NS=${RETRIEVAL_NS:-inference}
ALLOW_SCALE=$(awk 'BEGIN{print ("'${ALLOW_SCALE:-true}'" ~ /^(1|true|yes|on)$/)?"true":"false"}')
PERMIT_SLACK=$(awk 'BEGIN{print ("'${PERMIT_SLACK:-false}'" ~ /^(1|true|yes|on)$/)?"true":"false"}')
PERMIT_PAGERDUTY=$(awk 'BEGIN{print ("'${PERMIT_PAGERDUTY:-false}'" ~ /^(1|true|yes|on)$/)?"true":"false"}')
DO_POD_DELETE=$(awk 'BEGIN{print ("'${DO_POD_DELETE:-true}'" ~ /^(1|true|yes|on)$/)?"true":"false"}')
MAX_WAIT=${MAX_WAIT:-120}
TMPDIR=$(mktemp -d /tmp/test_alerts_real.${UUID}.XXXX)
PF_PIDS=()
trap 'rc=$?; for p in "${PF_PIDS[@]:-}"; do kill "${p}" 2>/dev/null || true; done; rm -rf "${TMPDIR}"; exit $rc' INT TERM EXIT
find_deploy(){ local ns=$1; shift; local names=("$@"); for n in "${names[@]}"; do if kubectl -n "${ns}" get deploy "${n}" >/dev/null 2>&1; then printf '%s' "${n}"; return 0; fi; done; local cand; cand=$(kubectl -n "${ns}" get deploy -o json 2>/dev/null | jq -r '.items[0].metadata.name // ""'); printf '%s' "${cand}"; }
record_replicas(){ local ns=$1; local deploy=$2; kubectl -n "${ns}" get deploy "${deploy}" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "absent"; }
get_selector_kv(){ local ns=$1; local deploy=$2; kubectl -n "${ns}" get deploy "${deploy}" -o json | jq -r '.spec.selector.matchLabels // {} | to_entries | map("\(.key)=\(.value)") | join(" ")'; }
apply_deny_np(){ local ns=$1; local name=$2; shift; local kvs=("$@"); local tmp="${TMPDIR}/${name}.yaml"; { printf '%s\n' "apiVersion: networking.k8s.io/v1" "kind: NetworkPolicy" "metadata:" "  name: ${name}" "  labels:" "    managed-by: test_alerts_real" "spec:" "  podSelector:" "    matchLabels:"; for kv in "${kvs[@]}"; do k=${kv%%=*}; v=${kv#*=}; printf '      %s: \"%s\"\n' "${k}" "${v}"; done; printf '%s\n' "  policyTypes: [\"Ingress\",\"Egress\"]" "  ingress: []" "  egress: []"; } > "${tmp}"; kubectl -n "${ns}" apply -f "${tmp}" >/dev/null 2>&1 || true; }
delete_np(){ local ns=$1; local name=$2; kubectl -n "${ns}" delete networkpolicy "${name}" --ignore-not-found >/dev/null 2>&1 || true; }
scale_deploy(){ local ns=$1; local deploy=$2; local replicas=$3; kubectl -n "${ns}" scale deployment "${deploy}" --replicas="${replicas}" >/dev/null 2>&1 || { ERR "kubectl scale failed for ${ns}/${deploy}"; return 1; } }
delete_one_pod(){ local ns=$1; shift; local kvs=("$@"); local sel=""; for kv in "${kvs[@]}"; do k=${kv%%=*}; v=${kv#*=}; sel="${sel}${k}=${v},"; done; sel=${sel%,}; local pod; pod=$(kubectl -n "${ns}" get pods -l "${sel}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true); if [ -n "${pod}" ]; then kubectl -n "${ns}" delete pod "${pod}" --wait=false >/dev/null 2>&1 || true; fi }
port_forward_svc(){ local ns=$1; local svc=$2; local remote_port=${3:-}; local PF_LOG="${TMPDIR}/pf_${svc}.log"; local PORT; PORT=$(python3 - <<PY
import socket
s=socket.socket()
s.bind(('',0))
p=s.getsockname()[1]
s.close()
print(p)
PY
); if [ -z "${remote_port}" ] || [ "${remote_port}" = "0" ]; then case "${svc}" in "${VICTORIA_SVC}") remote_port=8428;; "${ALERTM_SVC}") remote_port=9093;; *) remote_port=80;; esac; fi; kubectl -n "${ns}" port-forward "svc/${svc}" "${PORT}:${remote_port}" > "${PF_LOG}" 2>&1 & PF_PIDS+=($!); sleep 1; local attempt=0; while [ $attempt -lt 12 ]; do if grep -q "Forwarding from" "${PF_LOG}" 2>/dev/null; then printf '%s|%s' "${PORT}" "${PF_LOG}"; return 0; fi; attempt=$((attempt+1)); sleep 1; done; ERR "port-forward for ${ns}/${svc} failed; tail ${PF_LOG}"; tail -n 80 "${PF_LOG}" || true; return 1; }
inject_metrics(){ local victoria_url=$1; local payload_file=$2; curl -sS --data-binary @"${payload_file}" "${victoria_url}/api/v1/import/prometheus" -o "${TMPDIR}/victoria_push_${UUID}.json" || true; }
post_alertmanager(){ local alertm_url=$1; local payload_file=$2; curl -sS -XPOST -H "Content-Type: application/json" "${alertm_url}/api/v2/alerts" --data @"${payload_file}" -o "${TMPDIR}/alertm_post_${UUID}.json" || true; }
wait_for_alerts_in_alertm(){ local alertm_url=$1; local test_run=$2; local timeout=${3:-60}; local start; start=$(date +%s); while [ $(( $(date +%s) - start )) -lt "${timeout}" ]; do out=$(curl -sS "${alertm_url}/api/v2/alerts" || true); if [ -n "${out}" ] && echo "${out}" | jq -e --arg tr "${test_run}" '.[] | select(.labels.test_run == $tr)' >/dev/null 2>&1; then printf '%s' "${out}"; return 0; fi; sleep 1; done; return 1; }
LOG "begin test run id=${UUID}"
QDRANT_DEPLOY=$(find_deploy "${QDRANT_NS}" qdrant qdrant-deployment qdrant-server || true)
RETR_DEPLOY=$(find_deploy "${RETRIEVAL_NS}" retrieval retriever retriever-deployment || true)
LOG "detected targets qdrant=${QDRANT_DEPLOY:-absent} retriever=${RETR_DEPLOY:-absent}"
declare -A ORIGINAL_REPLICAS
if [ -n "${QDRANT_DEPLOY}" ]; then ORIGINAL_REPLICAS["qdrant"]="$(record_replicas "${QDRANT_NS}" "${QDRANT_DEPLOY}")"; else ORIGINAL_REPLICAS["qdrant"]="absent"; fi
if [ -n "${RETR_DEPLOY}" ]; then ORIGINAL_REPLICAS["retriever"]="$(record_replicas "${RETRIEVAL_NS}" "${RETR_DEPLOY}")"; else ORIGINAL_REPLICAS["retriever"]="absent"; fi
LOG "original replicas: qdrant=${ORIGINAL_REPLICAS["qdrant"]} retriever=${ORIGINAL_REPLICAS["retriever"]}"
LOG "attempting to create network isolation to simulate outages"
for svc in qdrant retriever; do
  if [ "${svc}" = "qdrant" ]; then ns="${QDRANT_NS}"; deploy="${QDRANT_DEPLOY}"; else ns="${RETRIEVAL_NS}"; deploy="${RETR_DEPLOY}"; fi
  if [ -z "${deploy}" ]; then LOG "${svc} deployment absent; skipping NP and scale"; continue; fi
  sel_kv=$(get_selector_kv "${ns}" "${deploy}" || true)
  if [ -z "${sel_kv}" ]; then sel_kv="app=${svc}"; fi
  IFS=' ' read -r -a kvs <<< "${sel_kv}"
  np_name="test-alerts-deny-${svc}-${UUID}"
  apply_deny_np "${ns}" "${np_name}" "${kvs[@]}"
  LOG "applied deny NetworkPolicy ${np_name} in ${ns}"
  if [ "${DO_POD_DELETE}" = "true" ]; then delete_one_pod "${ns}" "${kvs[@]}"; LOG "deleted one pod for ${svc} to exercise restart path"; fi
  if [ "${ALLOW_SCALE}" = "true" ]; then LOG "scaling ${ns}/${deploy} -> 0 to simulate outage"; scale_deploy "${ns}" "${deploy}" 0 || LOG "scale to 0 failed for ${ns}/${deploy}"; fi
done
LOG "starting port-forwards for victoria and alertmanager"
PF1=$(port_forward_svc "${VM_NAMESPACE}" "${VICTORIA_SVC}" 8428) || { ERR "victoria port-forward failed"; exit 4; }
VICTORIA_LOCAL=$(printf '%s' "${PF1}" | cut -d'|' -f1)
PF2=$(port_forward_svc "${VM_NAMESPACE}" "${ALERTM_SVC}" 9093) || { ERR "alertmanager port-forward failed"; exit 5; }
ALERTM_LOCAL=$(printf '%s' "${PF2}" | cut -d'|' -f1)
VICTORIA_URL="http://127.0.0.1:${VICTORIA_LOCAL}"
ALERTM_URL="http://127.0.0.1:${ALERTM_LOCAL}"
LOG "Victoria local=${VICTORIA_URL} Alertmanager local=${ALERTM_URL}"
LOG "pushing synthetic metrics to Victoria to trigger vmalert rules"
NOW_MS=$(date +%s000)
T1=$((NOW_MS - 120000))
cat > "${TMPDIR}/metrics_${UUID}.txt" <<__EOF__
retrieval_requests_total{service="retrieval",test_run="${UUID}"} 100 ${T1}
retrieval_errors_total{service="retrieval",test_run="${UUID}"} 90 ${T1}
retrieval_requests_total{service="retrieval",test_run="${UUID}"} 200 ${NOW_MS}
retrieval_errors_total{service="retrieval",test_run="${UUID}"} 190 ${NOW_MS}
collections_total{test_run="${UUID}"} 0 ${T1}
collections_total{test_run="${UUID}"} 0 ${NOW_MS}
__EOF__
inject_metrics "${VICTORIA_URL}" "${TMPDIR}/metrics_${UUID}.txt"
LOG "metric injection attempted; now post synthetic direct alerts to Alertmanager for routing checks"
ALERTS_PAYLOAD="${TMPDIR}/alerts_${UUID}.json"
RUNBOOK_URL=${RUNBOOK_URL:-"https://example.runbook/test"}
jq -n --arg tr "${UUID}" --arg rb "${RUNBOOK_URL}" '[{labels: {alertname: ("qdrant-paging-"+$tr), plane: "safety", severity: "critical", service: "qdrant", test_run: $tr}, annotations: {summary: "synthetic qdrant paging test", runbook: $rb}}, {labels: {alertname: ("retriever-paging-"+$tr), plane: "safety", severity: "critical", service: "retriever", test_run: $tr}, annotations: {summary: "synthetic retriever paging test", runbook: $rb}}, {labels: {alertname: ("test-channel1-nonpaging-"+$tr), plane: "slo", severity: "warning", service: "sanity", test_run: $tr}, annotations: {summary: "synthetic nonpaging slack test"}}]' > "${ALERTS_PAYLOAD}"
post_alertmanager "${ALERTM_URL}" "${ALERTS_PAYLOAD}"
LOG "posted synthetic alerts; waiting up to ${MAX_WAIT}s for alerts to appear in Alertmanager (eval + routing checks)"
if out=$(wait_for_alerts_in_alertm "${ALERTM_URL}" "${UUID}" "${MAX_WAIT}"); then
  LOG "alerts observed in Alertmanager; inspecting receivers"
  echo "${out}" | jq -r --arg tr "${UUID}" '.[] | select(.labels.test_run == $tr) | "\(.labels.alertname) -> receiver=\((.receivers[0].name // "<none>")) runbook=\((.annotations.runbook // "<none>"))"'
else
  ERR "alerts did not appear in Alertmanager within ${MAX_WAIT}s"
fi
LOG "waiting 20s to allow external delivery to Slack/PagerDuty if permitted"
sleep 20
LOG "restoring networkpolicies and replica counts (best-effort)"
for svc in qdrant retriever; do
  if [ "${svc}" = "qdrant" ]; then ns="${QDRANT_NS}"; deploy="${QDRANT_DEPLOY}"; else ns="${RETRIEVAL_NS}"; deploy="${RETR_DEPLOY}"; fi
  np_name="test-alerts-deny-${svc}-${UUID}"
  delete_np "${ns}" "${np_name}"
  LOG "deleted NP ${np_name} in ${ns}"
  orig=${ORIGINAL_REPLICAS["${svc}"]}
  if [ "${orig}" != "absent" ] && [ -n "${deploy}" ]; then
    if [ "${ALLOW_SCALE}" = "true" ]; then
      LOG "restoring ${ns}/${deploy} -> replicas=${orig}"
      scale_deploy "${ns}" "${deploy}" "${orig}" || LOG "restore scale failed for ${ns}/${deploy}"
      if [ "${orig}" != "absent" ]; then kubectl -n "${ns}" rollout status "deployment/${deploy}" --timeout=120s || LOG "rollout wait timed out for ${ns}/${deploy}"; fi
    fi
  fi
done
LOG "attempting to expunge synthetic alerts from Alertmanager (best-effort)"
curl -sS -XDELETE "${ALERTM_URL}/api/v2/alerts" -H "Content-Type: application/json" -d "[{\"matchers\":[{\"name\":\"test_run\",\"value\":\"${UUID}\",\"isRegex\":false}]}]" >/dev/null 2>&1 || true
LOG "cleanup complete; test run ${UUID} finished"
