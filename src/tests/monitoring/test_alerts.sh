#!/usr/bin/env bash
python3 infra/generators/alerting.py --delete --confirm && python3 infra/generators/alerting.py --apply

NS=${NS:-monitoring}
VICTORIA_SVC=${VICTORIA_SVC:-victoria-metrics}
VMALERT_SVC=${VMALERT_SVC:-vmalert}
ALERTM_SVC=${ALERTM_SVC:-alertmanager}
TMPDIR="$(mktemp -d /tmp/test_alerts.XXXX)"
VICTORIA_LOG="${TMPDIR}/victoria.log"
VMALERT_LOG="${TMPDIR}/vmalert.log"
ALERTM_LOG="${TMPDIR}/alertm.log"
PIDS=()
UUID="$(cat /proc/sys/kernel/random/uuid)"
command -v jq >/dev/null 2>&1 || { echo "ERROR: jq required"; rm -rf "${TMPDIR}"; exit 2; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl required"; rm -rf "${TMPDIR}"; exit 3; }

find_free_port() {
  python3 - <<PY
import socket
s=socket.socket()
s.bind(('',0))
print(s.getsockname()[1])
s.close()
PY
}

kill_matching_pf() {
  pgrep -af "kubectl .*port-forward.*${1}" | awk '{print $1}' | xargs -r -n1 kill || true
}

cleanup() {
  for p in "${PIDS[@]}"; do kill "${p}" 2>/dev/null || true; done
  rm -rf "${TMPDIR}"
}
trap cleanup EXIT

ALERTING_PAGING_SEVERITY_LEVELS="${ALERTING_PAGING_SEVERITY_LEVELS:-critical}"
ALERTING_SLACK_SEVERITY_LEVELS="${ALERTING_SLACK_SEVERITY_LEVELS:-warning,critical}"

parse_csv_to_array() {
  local raw="$1"
  echo "$raw" | tr ',' '\n' | sed -E 's/^[[:space:]]+|[[:space:]]+$//g' | awk 'NF{print tolower($0)}' | awk '!seen[$0]++'
}

pick_n() {
  local n="$1"; shift
  local arr=("$@")
  local out=()
  for v in "${arr[@]}"; do
    out+=("$v")
    if [ "${#out[@]}" -ge "$n" ]; then break; fi
  done
  echo "${out[@]}"
}

wait_for_pf() {
  local log="$1"
  local timeout="${2:-12}"
  local start ts
  start=$(date +%s)
  while true; do
    ts=$(date +%s)
    if grep -q "Forwarding from" "${log}" 2>/dev/null; then
      return 0
    fi
    if [ $((ts - start)) -ge "$timeout" ]; then
      return 1
    fi
    sleep 1
  done
}

wait_for_http() {
  local url="$1" local timeout="${2:-15}" local start ts
  start=$(date +%s)
  while true; do
    ts=$(date +%s)
    if [ $((ts - start)) -ge "$timeout" ]; then
      return 1
    fi
    if curl -sS -m2 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
}

echo "$(date -Iseconds) INFO cleanup any previous port-forwards"
kill_matching_pf "${VICTORIA_SVC}"
kill_matching_pf "${VMALERT_SVC}"
kill_matching_pf "${ALERTM_SVC}"
sleep 1

VICTORIA_LOCAL=$(find_free_port)
VMALERT_LOCAL=$(find_free_port)
ALERTM_LOCAL=$(find_free_port)

echo "$(date -Iseconds) INFO starting port-forwards on local ports victoria=${VICTORIA_LOCAL} vmalert=${VMALERT_LOCAL} alertm=${ALERTM_LOCAL}"
kubectl -n "${NS}" port-forward "svc/${VICTORIA_SVC}" "${VICTORIA_LOCAL}:8428" >"${VICTORIA_LOG}" 2>&1 & PIDS+=($!)
if ! wait_for_pf "${VICTORIA_LOG}" 10; then echo "$(date -Iseconds) ERROR victoria port-forward failed; tail ${VICTORIA_LOG}"; tail -n 80 "${VICTORIA_LOG}"; exit 4; fi
kubectl -n "${NS}" port-forward "svc/${VMALERT_SVC}" "${VMALERT_LOCAL}:8080" >"${VMALERT_LOG}" 2>&1 & PIDS+=($!)
if ! wait_for_pf "${VMALERT_LOG}" 10; then echo "$(date -Iseconds) ERROR vmalert port-forward failed; tail ${VMALERT_LOG}"; tail -n 80 "${VMALERT_LOG}"; exit 5; fi
kubectl -n "${NS}" port-forward "svc/${ALERTM_SVC}" "${ALERTM_LOCAL}:9093" >"${ALERTM_LOG}" 2>&1 & PIDS+=($!)
if ! wait_for_pf "${ALERTM_LOG}" 12; then echo "$(date -Iseconds) ERROR alertmanager port-forward failed; tail ${ALERTM_LOG}"; tail -n 80 "${ALERTM_LOG}"; exit 6; fi

echo "$(date -Iseconds) INFO waiting for Alertmanager to answer /api/v2/status"
if ! wait_for_http "http://127.0.0.1:${ALERTM_LOCAL}/api/v2/status" 20; then echo "$(date -Iseconds) ERROR Alertmanager not responding; tail ${ALERTM_LOG}"; tail -n 120 "${ALERTM_LOG}"; exit 7; fi

echo "$(date -Iseconds) INFO waiting for vmalert /metrics"
if ! wait_for_http "http://127.0.0.1:${VMALERT_LOCAL}/metrics" 20; then echo "$(date -Iseconds) ERROR vmalert not responding; tail ${VMALERT_LOG}"; tail -n 120 "${VMALERT_LOG}"; exit 8; fi

sleep 1

NOW_MS=$(date +%s000)
T1=$((NOW_MS - 120000))
T2=$((NOW_MS - 60000))
cat > "${TMPDIR}/metrics.txt" <<__EOF__
retrieval_requests_total{service="retrieval",test_run="${UUID}"} 100 ${T1}
retrieval_errors_total{service="retrieval",test_run="${UUID}"} 60 ${T1}
retrieval_requests_total{service="retrieval",test_run="${UUID}"} 200 ${T2}
retrieval_errors_total{service="retrieval",test_run="${UUID}"} 140 ${T2}
retrieval_requests_total{service="retrieval",test_run="${UUID}"} 300 ${NOW_MS}
retrieval_errors_total{service="retrieval",test_run="${UUID}"} 220 ${NOW_MS}
__EOF__

echo "$(date -Iseconds) INFO pushing synthetic metrics (compact)"
curl -sS --data-binary @"${TMPDIR}/metrics.txt" "http://127.0.0.1:${VICTORIA_LOCAL}/api/v1/import/prometheus" -o "${TMPDIR}/victoria_push.json" || { echo "$(date -Iseconds) ERROR push failed"; tail -n 200 "${VICTORIA_LOG}"; exit 9; }
jq -c '.status' "${TMPDIR}/victoria_push.json" 2>/dev/null || echo '"unknown"'

paging_arr=($(parse_csv_to_array "${ALERTING_PAGING_SEVERITY_LEVELS}"))
slack_arr=($(parse_csv_to_array "${ALERTING_SLACK_SEVERITY_LEVELS}"))

unique_slack_arr=()
for s in "${slack_arr[@]}"; do
  skip=0
  for p in "${paging_arr[@]}"; do
    if [ "$s" = "$p" ]; then skip=1; break; fi
  done
  if [ "$skip" -eq 0 ]; then unique_slack_arr+=("$s"); fi
done

paging_choice=($(pick_n 2 "${paging_arr[@]}"))
if [ "${#paging_choice[@]}" -lt 2 ]; then
  fallback=("critical" "critical")
  paging_choice=("${paging_choice[@]}" "${fallback[@]}")
  paging_choice=($(pick_n 2 "${paging_choice[@]}"))
fi

nonpaging_choice=($(pick_n 2 "${unique_slack_arr[@]}"))
if [ "${#nonpaging_choice[@]}" -lt 2 ]; then
  addf=("warning" "info")
  nonpaging_choice=("${nonpaging_choice[@]}" "${addf[@]}")
  nonpaging_choice=($(pick_n 2 "${nonpaging_choice[@]}"))
fi

declare -a ALERTS
ALERTS+=("paging|${paging_choice[0]}|safety|qdrant")
ALERTS+=("paging|${paging_choice[1]}|safety|retriever")
ALERTS+=("nonpaging|${nonpaging_choice[0]}|slo|test-channel1")
ALERTS+=("nonpaging|${nonpaging_choice[1]}|slo|test-channel2")
post_and_check() {
  local mode="$1"; local severity="$2"; local plane="$3"; local service="$4"
  local labels_json annotations_json out match recv rb runbook_file runbook_url
  local alertname="${service}-${mode}"
  labels_json=$(jq -n --arg an "${alertname}" --arg pl "${plane}" --arg sv "${severity}" --arg svc "${service}" --arg tid "${UUID}" '{"alertname":$an,"plane":$pl,"severity":$sv,"service":$svc,"test_run":$tid}')

  # compute kebab-case filename for runbook, same logic as alerting.py.alertname_to_kebab
  runbook_file=$(python3 - <<'PY'
import sys,re
n = sys.argv[1]
s1 = re.sub("([a-z0-9])([A-Z])", r"\1-\2", n)
s2 = re.sub("([A-Z]+)([A-Z][a-z0-9])", r"\1-\2", s1)
k = re.sub(r"[^a-zA-Z0-9\-]+", "-", s2).strip("-").lower()
print(k + ".html")
PY
  "$alertname")

  if [ -n "${RUNBOOK_BASE_URL:-}" ]; then
    runbook_url="${RUNBOOK_BASE_URL%/}/${runbook_file}"
  else
    # fallback used only for local test; replace with your canonical runbook base as needed
    runbook_url="https://example.runbook/test"
  fi

  annotations_json=$(jq -n --arg sum "synthetic ${service} ${mode}" --arg rb "${runbook_url}" '{"summary":$sum,"runbook":$rb}')
  payload=$(jq -n --argjson labels "${labels_json}" --argjson ann "${annotations_json}" '[{labels:$labels,annotations:$ann}]')

  curl -sS -XPOST "http://127.0.0.1:${ALERTM_LOCAL}/api/v2/alerts" -H "Content-Type: application/json" -d "${payload}" >/dev/null || true

  local attempts=0 max=20
  while [ $attempts -lt $max ]; do
    sleep 1
    out=$(curl -sS "http://127.0.0.1:${ALERTM_LOCAL}/api/v2/alerts")
    match=$(echo "${out}" | jq -c --arg svc "${service}" --arg an "${alertname}" '.[] | select(.labels.service==$svc and .labels.alertname==$an)' 2>/dev/null || true)
    if [ -n "${match}" ]; then
      recv=$(echo "${match}" | jq -r '.receivers[0].name // empty')
      rb=$(echo "${match}" | jq -r '.annotations.runbook // empty')
      echo "$(date -Iseconds) INFO found alert ${alertname} severity=${severity} plane=${plane} receiver=${recv:-<empty>} runbook_present=${rb:+yes} runbook=${rb:-<empty>}"
      if [ "${mode}" = "paging" ]; then
        if [ "${PAGERDUTY_INTEGRATION_KEY:-}${PAGERDUTY_ROUTING_KEY:-}" != "" ]; then
          if [ "${recv}" != "pagerduty" ]; then
            echo "$(date -Iseconds) ERROR expected pagerduty receiver but got='${recv}'"
            return 11
          fi
        fi
      else
        if [ -n "${ALERTMANAGER_SLACK_WEBHOOK:-}" ]; then
          if [ "${recv}" != "slack" ]; then
            echo "$(date -Iseconds) ERROR expected slack receiver but got='${recv}'"
            return 12
          fi
        fi
      fi
      return 0
    fi
    attempts=$((attempts+1))
  done
  echo "$(date -Iseconds) ERROR alert ${alertname} did not appear in Alertmanager"
  return 10
}

echo "$(date -Iseconds) INFO posting sanity alert to ensure pipelines warm"
curl -sS -XPOST "http://127.0.0.1:${ALERTM_LOCAL}/api/v2/alerts" -H "Content-Type: application/json" -d "[{\"labels\":{\"alertname\":\"SanityWarm-${UUID}\",\"plane\":\"slo\",\"severity\":\"info\",\"service\":\"sanity\"},\"annotations\":{\"summary\":\"sanity\"}}]" >/dev/null || true
sleep 3

fail=0
for a in "${ALERTS[@]}"; do
  IFS='|' read -r kind sev plane svc <<<"$a"
  post_and_check "$kind" "$sev" "$plane" "$svc" || fail=1
done

echo "$(date -Iseconds) INFO verify Victoria ingested series for test_run (compact)"
curl -sG "http://127.0.0.1:${VICTORIA_LOCAL}/api/v1/query" --data-urlencode "query=retrieval_requests_total{test_run=\"${UUID}\"}" | jq -c '.data' 2>/dev/null || echo '{"vm":"no-jq-output"}'

for p in "${PIDS[@]}"; do kill "${p}" 2>/dev/null || true; done
rm -rf "${TMPDIR}"

if [ "$fail" -eq 0 ]; then
  echo "$(date -Iseconds) INFO all checks passed"
  exit 0
else
  echo "$(date -Iseconds) ERROR one or more checks failed"
  exit 20
fi
