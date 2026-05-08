bash -lc '
LOG(){ printf "%s %s\n" "$(date -Iseconds)" "$*"; }
MUST=(kubectl curl python3)
MISSING=0
for c in "${MUST[@]}"; do command -v "$c" >/dev/null 2>&1 || { LOG "MISSING_CMD:$c"; MISSING=1; }; done
[ "$MISSING" -ne 0 ] && LOG "Install missing commands and re-run" && exit 1

NS_Q=${QDRANT_NAMESPACE:-qdrant}
NS_VM=${VM_NAMESPACE:-monitoring}
VICTORIA_SVC=${VICTORIA_SERVICE:-victoria-metrics}
VICTORIA_PORT=${VICTORIA_PORT:-8428}
VMAGENT_SVC=${VMAGENT_SERVICE:-vmagent}
VMAGENT_PORT=${VMAGENT_PORT:-8429}

# helper: free port
GET_FREE_PORT(){ python3 - <<PY
import socket,sys
s=socket.socket()
s.bind(("",0))
p=s.getsockname()[1]
s.close()
print(p)
PY
}

# 1) Qdrant running version(s)
LOG "QDRANT pod(s) and image(s):"
kubectl -n "$NS_Q" get pods -l "app.kubernetes.io/name=qdrant" -o jsonpath="{range .items[*]}{.metadata.name} .spec.containers[0].image\n{end}" 2>/dev/null || kubectl -n "$NS_Q" get pods -o wide -l app=qdrant -o jsonpath="{range .items[*]}{.metadata.name} .spec.containers[0].image\n{end}" 2>/dev/null || LOG "no qdrant pods found"

# 2) pick first qdrant pod and probe annotations + /metrics via port-forward
POD=$(kubectl -n "$NS_Q" get pods -l "app.kubernetes.io/name=qdrant" -o jsonpath="{.items[0].metadata.name}" 2>/dev/null || true)
[ -z "$POD" ] && POD=$(kubectl -n "$NS_Q" get pods -l app=qdrant -o jsonpath="{.items[0].metadata.name}" 2>/dev/null || true)
if [ -z "$POD" ]; then LOG "NO_POD_FOUND:$NS_Q"; else
  LOG "chosen pod: $POD"
  ANNS="$(kubectl -n "$NS_Q" get pod "$POD" -o jsonpath='\''{.metadata.annotations}'\'' 2>/dev/null || true)"
  LOG "annotations: $ANNS"
  PORT="$(kubectl -n "$NS_Q" get pod "$POD" -o jsonpath='\''{.metadata.annotations.monitoring\.io/port}'\'' 2>/dev/null || true)"
  PATH_ANN="$(kubectl -n "$NS_Q" get pod "$POD" -o jsonpath='\''{.metadata.annotations.monitoring\.io/path}'\'' 2>/dev/null || true)"
  PORT="${PORT:-6333}"
  PATH_ANN="${PATH_ANN:-/metrics}"
  LOCAL_P=$(GET_FREE_PORT)
  PF_LOG=$(mktemp /tmp/pf.qdrant.${POD}.XXXXXX.log)
  kubectl -n "$NS_Q" port-forward "pod/$POD" "${LOCAL_P}:${PORT}" >"$PF_LOG" 2>&1 &
  PFPID=$!
  LOG "port-forward pid=${PFPID} local:${LOCAL_P} -> ${POD}:${PORT} (waiting up to 12s)"
  START=$SECONDS
  READY=0
  while [ $((SECONDS-START)) -lt 12 ]; do
    curl -sS --max-time 2 "http://127.0.0.1:${LOCAL_P}${PATH_ANN}" >/dev/null 2>&1 && READY=1 && break
    sleep 1
  done
  if [ $READY -ne 1 ]; then
    LOG "FAILED_PULL_METRICS_FROM_POD; tail of ${PF_LOG}:"
    sed -n "1,200p" "$PF_LOG" || true
  else
    POD_METRICS=$(mktemp /tmp/qdrant.${POD}.metrics.XXXXXX)
    curl -sS --max-time 5 "http://127.0.0.1:${LOCAL_P}${PATH_ANN}" >"$POD_METRICS" 2>"${POD_METRICS}.err" || true
    LOG "scraped metrics -> ${POD_METRICS} (size $(wc -c <"$POD_METRICS"))"
    LOG "Top metric name prefixes from pod:"
    grep -E "^[a-zA-Z_:][a-zA-Z0-9_:]*" "$POD_METRICS" | sed -E "s/(\\S+).*/\\1/" | sort | uniq -c | sort -rn | head -n 50
    LOG "first 40 lines of /metrics for quick view:"
    sed -n "1,40p" "$POD_METRICS" || true
  fi
  # cleanup pf
  kill "$PFPID" >/dev/null 2>&1 || true
  wait "$PFPID" 2>/dev/null || true
fi

# 3) vmagent configmap sanity-check (replacement literal)
LOG "vmagent ConfigMap relabel replacement check"
kubectl -n "$NS_VM" get cm vmagent-config -o yaml 2>/dev/null > /tmp/vmagent.cm.$$ || true
if grep -Fq "replacement: '\''\$1:\$2'\''" /tmp/vmagent.cm.$$; then LOG "relabel replacement literal OK"; else LOG "WARNING: expected replacement: '\''\\\$1:\\\$2'\'' not found (inspect /tmp/vmagent.cm.$$)"; fi

# 4) probe vmagent runtime metrics (targets)
LOCAL_VMAGENT_P=$(GET_FREE_PORT)
PF_VMLOG=$(mktemp /tmp/pf.vmagent.log.XXXXXX)
kubectl -n "$NS_VM" port-forward svc/"$VMAGENT_SVC" "${LOCAL_VMAGENT_P}:${VMAGENT_PORT}" >"$PF_VMLOG" 2>&1 &
PFM_PID=$!
LOG "vmagent port-forward pid=${PFM_PID} local:${LOCAL_VMAGENT_P} -> ${VMAGENT_SVC}:${VMAGENT_PORT} (waiting up to 12s)"
START=$SECONDS; READY=0
while [ $((SECONDS-START)) -lt 12 ]; do
  curl -sS --max-time 2 "http://127.0.0.1:${LOCAL_VMAGENT_P}/metrics" >/dev/null 2>&1 && READY=1 && break
  sleep 1
done
if [ $READY -ne 1 ]; then
  LOG "FAILED_VMAGENT_METRICS; tail $PF_VMLOG:"
  sed -n "1,200p" "$PF_VMLOG" || true
else
  VMAG_MET=$(mktemp /tmp/vmagent.metrics.XXXXXX)
  curl -sS --max-time 5 "http://127.0.0.1:${LOCAL_VMAGENT_P}/metrics" >"$VMAG_MET" 2>"${VMAG_MET}.err" || true
  LOG "vmagent metrics snapshot saved -> ${VMAG_MET}"
  LOG "looking for vmagent target indicators (vm_promscrape targets / scrape_pool_targets):"
  egrep -n "vm_promscrape_targets|vm_promscrape_scrape_pool_targets|vm_promscrape_scrapers_started_total" "$VMAG_MET" | sed -n "1,160p" || true
  LOG "searching for targets specifically labeled namespace=\"${NS_Q}\" with status/up"
  egrep '\''namespace="'"${NS_Q}"'"'\'' "$VMAG_MET" | sed -n "1,120p" || true
  LOG "vmagent recent error lines (grep '\''skipping duplicate'\'' and '\''error'\''):"
  grep -iE "skipping duplicate|error|invalid address|invalid port" "$VMAG_MET" | sed -n "1,120p" || true
fi
kill "$PFM_PID" >/dev/null 2>&1 || true
wait "$PFM_PID" 2>/dev/null || true

# 5) query VictoriaMetrics for \"qdrant_.*\" metrics existence
LOCAL_VICT_P=$(GET_FREE_PORT)
PF_VMLOG2=$(mktemp /tmp/pf.victoria.log.XXXXXX)
kubectl -n "$NS_VM" port-forward svc/"$VICTORIA_SVC" "${LOCAL_VICT_P}:${VICTORIA_PORT}" >"$PF_VMLOG2" 2>&1 &
PFV_PID=$!
LOG "victoria port-forward pid=${PFV_PID} local:${LOCAL_VICT_P} -> ${VICTORIA_SVC}:${VICTORIA_PORT} (waiting up to 12s)"
START=$SECONDS; READY=0
while [ $((SECONDS-START)) -lt 12 ]; do
  curl -sS --max-time 2 "http://127.0.0.1:${LOCAL_VICT_P}/metrics" >/dev/null 2>&1 && READY=1 && break
  sleep 1
done
if [ $READY -ne 1 ]; then LOG "VICTORIA_METRICS_UNREACHABLE; tail ${PF_VMLOG2}"; sed -n "1,120p" "${PF_VMLOG2}" || true; else
  LOG "Querying VictoriaMetrics for count of qdrant_* series"
  curl -sG --data-urlencode '\''query=count({__name__=~"qdrant_.*"})'\'' "http://127.0.0.1:${LOCAL_VICT_P}/api/v1/query" | python3 -c "import sys,json; j=json.load(sys.stdin); print('\''VM_QUERY_STATUS'\'', j.get('\''status'\'')); print('\''RAW'\'', json.dumps(j.get('\''data'\'',{})) )"
  LOG "Querying VictoriaMetrics for up{namespace=\"${NS_Q}\"}"
  curl -sG --data-urlencode '\''query=max(up{namespace=\"'"${NS_Q}"'\"})'\'' "http://127.0.0.1:${LOCAL_VICT_P}/api/v1/query" | python3 -c "import sys,json; j=json.load(sys.stdin); print('\''VM_QUERY_STATUS'\'', j.get('\''status'\'')); print('\''RAW'\'', json.dumps(j.get('\''data'\'',{})) )"
fi
kill "$PFV_PID" >/dev/null 2>&1 || true
wait "$PFV_PID" 2>/dev/null || true

LOG "DIAGNOSTIC_COMPLETE: inspect outputs and follow remediation steps in the plan; no cluster mutation was performed by this script"
'