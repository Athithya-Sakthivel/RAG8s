#!/usr/bin/env bash

TS_MS=$(date +%s)000

log(){ echo "[monitoring-test] $*"; }

log "starting victoria-metrics port-forward"
kubectl -n monitoring port-forward svc/victoria-metrics 0:8428 >/tmp/_vm_pf.log 2>&1 & PF_VM=$!
sleep 1
VM_PORT=$(grep -oE '127.0.0.1:[0-9]+' /tmp/_vm_pf.log | tail -1 | cut -d: -f2)
test -n "$VM_PORT" || { log "victoria port-forward failed"; kill $PF_VM 2>/dev/null; exit 1; }
log "victoria-metrics on port ${VM_PORT}"

log "starting vmagent port-forward"
kubectl -n monitoring port-forward svc/vmagent 0:8429 >/tmp/_va_pf.log 2>&1 & PF_VA=$!
sleep 1
VA_PORT=$(grep -oE '127.0.0.1:[0-9]+' /tmp/_va_pf.log | tail -1 | cut -d: -f2)
test -n "$VA_PORT" || { log "vmagent port-forward failed"; kill $PF_VM $PF_VA 2>/dev/null; exit 1; }
log "vmagent on port ${VA_PORT}"

log "waiting for vmagent readiness"
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:${VA_PORT}/metrics" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:${VA_PORT}/metrics" >/dev/null || {
  log "vmagent never became ready"
  kill $PF_VM $PF_VA 2>/dev/null
  exit 1
}

PAYLOAD=$(
cat <<EOF
vm_promscrape_discovery_kubernetes_objects{role="pod"} 14 ${TS_MS}
vmagent_remotewrite_bytes_sent_total{url="1:secret-url"} 500000 ${TS_MS}
vmagent_remotewrite_rows_pushed_after_relabel_total{path="/vmagent-remotewrite-data"} 1200 ${TS_MS}
vmagent_remotewrite_samples_dropped_total 0 ${TS_MS}
vmagent_remotewrite_pending_data_bytes{path="/vmagent-remotewrite-data"} 0 ${TS_MS}
vm_promscrape_scrape_errors_total 0 ${TS_MS}
retrieval_requests_total{service="retriever"} 500 ${TS_MS}
retrieval_errors_total{service="retriever"} 5 ${TS_MS}
retrieval_request_duration_seconds_bucket{service="retriever",le="0.1"} 100 ${TS_MS}
retrieval_request_duration_seconds_bucket{service="retriever",le="0.5"} 200 ${TS_MS}
retrieval_request_duration_seconds_bucket{service="retriever",le="1"} 250 ${TS_MS}
retrieval_request_duration_seconds_bucket{service="retriever",le="+Inf"} 250 ${TS_MS}
retrieval_request_duration_seconds_sum{service="retriever"} 80 ${TS_MS}
retrieval_request_duration_seconds_count{service="retriever"} 250 ${TS_MS}
rest_responses_total{service="qdrant"} 300 ${TS_MS}
rest_responses_fail_total{service="qdrant"} 2 ${TS_MS}
rest_responses_duration_seconds_bucket{service="qdrant",le="0.1"} 50 ${TS_MS}
rest_responses_duration_seconds_bucket{service="qdrant",le="0.5"} 180 ${TS_MS}
rest_responses_duration_seconds_bucket{service="qdrant",le="1"} 250 ${TS_MS}
rest_responses_duration_seconds_bucket{service="qdrant",le="+Inf"} 300 ${TS_MS}
rest_responses_duration_seconds_sum{service="qdrant"} 40 ${TS_MS}
rest_responses_duration_seconds_count{service="qdrant"} 300 ${TS_MS}
collection_dead_replicas 0 ${TS_MS}
snapshot_creation_running 0 ${TS_MS}
EOF
)

log "injecting synthetic metrics"
curl -sf -X POST --data-binary "$PAYLOAD" \
  "http://127.0.0.1:${VM_PORT}/api/v1/import/prometheus" >/tmp/_vm_post.json || {
  log "metric injection failed"
  kill $PF_VM $PF_VA 2>/dev/null
  exit 1
}

sleep 2

query(){
  local q="$1"
  resp="$(curl -sf --get --data-urlencode "query=${q}" \
    "http://127.0.0.1:${VM_PORT}/api/v1/query" 2>/dev/null || true)"
  if [ -z "$resp" ]; then
    echo "N"
    return
  fi
  python3 - <<PY "$resp"
import sys,json
try:
    j=json.loads(sys.argv[1])
    r=j.get("data",{}).get("result",[])
    print(r[0]["value"][1] if r else "N")
except Exception:
    print("N")
PY
}

echo "=== COMPACT VERIFICATION ==="
echo "scrape%=$(query 'sum(up)/count(up)*100')"
echo "discovered_pods=$(query 'vm_promscrape_discovery_kubernetes_objects{role=\"pod\"}')"
echo "rows_5m=$(query 'increase(vmagent_remotewrite_rows_pushed_after_relabel_total[5m])')"
echo "retrieval_req_5m=$(query 'increase(retrieval_requests_total[5m])')"
echo "retrieval_err_5m=$(query 'increase(retrieval_errors_total[5m])')"
echo "retriever_p95=$(query 'histogram_quantile(0.95, sum(rate(retrieval_request_duration_seconds_bucket[5m])) by (le))')"
echo "qdrant_fail_5m=$(query 'increase(rest_responses_fail_total[5m])')"

echo
echo "=== vmagent /metrics sample (truncated) ==="
curl -sf "http://127.0.0.1:${VA_PORT}/metrics" \
  | egrep -i 'vmagent_remotewrite|vm_promscrape_discovery|vm_promscrape_scrape_errors' \
  | head -n 80

kill $PF_VM $PF_VA 2>/dev/null || true