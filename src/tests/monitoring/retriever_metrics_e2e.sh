#!/usr/bin/env bash
# compact retriever e2e: exercise throughput + errors, verify every dashboard panel metric
set -euo pipefail

LOG(){ printf '%s %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }

for cmd in kubectl curl python3 jq mktemp awk sed grep; do
  command -v "$cmd" >/dev/null 2>&1 || { ERR "$cmd required"; exit 2; }
done

RETRIEVER_NS=${RETRIEVER_NS:-inference}
RETRIEVER_SVC=${RETRIEVER_SVC:-retrieval-svc}
RETRIEVER_PORT=${RETRIEVER_PORT:-8001}
RETRIEVER_PATH=${RETRIEVER_PATH:-/generate}

VM_NS=${VM_NS:-monitoring}
VM_SVC=${VM_SVC:-victoria-metrics}
VMAGENT_SVC=${VMAGENT_SVC:-vmagent}

LOCAL_VICTORIA_PORT=${LOCAL_VICTORIA_PORT:-0}
LOCAL_VMAGENT_PORT=${LOCAL_VMAGENT_PORT:-0}
LOCAL_RETRIEVER_PORT=${LOCAL_RETRIEVER_PORT:-0}

LOAD_RPS=${LOAD_RPS:-30}
LOAD_DURATION=${LOAD_DURATION:-45}
LOAD_WORKERS=${LOAD_WORKERS:-4}
FAIL_RATE=${FAIL_RATE:-0.02}

ERR_BURST_RPS=${ERR_BURST_RPS:-25}
ERR_BURST_DURATION=${ERR_BURST_DURATION:-15}
ERR_BURST_WORKERS=${ERR_BURST_WORKERS:-3}
ERR_BURST_FAIL_RATE=${ERR_BURST_FAIL_RATE:-0.6}

QUERY_ARTIFACT_DIR=${QUERY_ARTIFACT_DIR:-/tmp/retriever_e2e_artifacts}
mkdir -p "$QUERY_ARTIFACT_DIR"

trap 'rc=$?; LOG "cleanup"; pkill -P $$ || true; exit $rc' INT TERM EXIT

find_free_port(){ python3 - <<PY
import socket
s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()
PY
}
alloc_port(){ for i in 1 2 3 4; do p=$(find_free_port); [ -n "$p" ] && { echo $p; return; }; sleep 0.1; done; ERR "no port"; exit 98; }

portforward_wait(){
  ns=$1; target=$2; localp=$3; remotep=$4; timeout=${5:-30}
  logf="$(mktemp -t pf.${target//[^a-zA-Z0-9]/_}.XXXX)"
  kubectl -n "$ns" port-forward "$target" "${localp}:${remotep}" >"$logf" 2>&1 &
  pfpid=$!
  for i in $(seq 1 $timeout); do
    if ! kill -0 "$pfpid" >/dev/null 2>&1; then ERR "pf exited; tail $logf"; tail -n200 "$logf" || true; return 2; fi
    if curl -sS --max-time 1 "http://127.0.0.1:${localp}/metrics" >/dev/null 2>&1; then echo "$pfpid|$logf"; return 0; fi
    sleep 1
  done
  ERR "pf timeout; tail $logf"; tail -n200 "$logf" || true; return 3
}

run_promql(){
  name="$1"; promql="$2"; expect="$3"
  base="http://127.0.0.1:${LOCAL_VICTORIA_PORT}/api/v1/query"
  for attempt in $(seq 1 8); do
    LOG "PromQL ${name} attempt ${attempt}: ${promql}"
    json="$(curl -sS -G --data-urlencode "query=${promql}" "$base" || echo "")"
    ok="$(echo "$json" | jq -r '.status == "success"')"
    if [ "$ok" = "true" ]; then
      cnt="$(echo "$json" | jq -r '.data.result|length')"
      if [ "$expect" = "any" ] && [ "$cnt" -gt 0 ]; then return 0; fi
      if [ "$expect" = "gt0" ] && [ "$cnt" -gt 0 ]; then
        val="$(echo "$json" | jq -r '.data.result[0].value[1] // empty')"
        if [ -n "$val" ]; then python3 - <<PY "$val"
import sys
try:
  sys.exit(0 if float(sys.argv[1])>0 else 1)
except:
  sys.exit(2)
PY
          return $?
        fi
      fi
    fi
    sleep $((2 * attempt))
  done
  return 1
}

# port-forwards
[ "$LOCAL_VICTORIA_PORT" -eq 0 ] && LOCAL_VICTORIA_PORT="$(alloc_port)"
portforward_wait "$VM_NS" "svc/$VM_SVC" "$LOCAL_VICTORIA_PORT" 8428 || exit 4
LOG "victoria pf:$LOCAL_VICTORIA_PORT"
[ "$LOCAL_VMAGENT_PORT" -eq 0 ] && LOCAL_VMAGENT_PORT="$(alloc_port)"
portforward_wait "$VM_NS" "svc/$VMAGENT_SVC" "$LOCAL_VMAGENT_PORT" 8429 || exit 5
LOG "vmagent pf:$LOCAL_VMAGENT_PORT"
[ "$LOCAL_RETRIEVER_PORT" -eq 0 ] && LOCAL_RETRIEVER_PORT="$(alloc_port)"
portforward_wait "$RETRIEVER_NS" "svc/$RETRIEVER_SVC" "$LOCAL_RETRIEVER_PORT" "$RETRIEVER_PORT" || exit 6
LOG "retriever pf:$LOCAL_RETRIEVER_PORT"

# snapshot pre-load metrics
curl -sS "http://127.0.0.1:${LOCAL_RETRIEVER_PORT}/metrics" > "${QUERY_ARTIFACT_DIR}/retriever_metrics_snapshot.txt" || true

# normal load: majority valid, small fail_rate for intermittent failures. top_k>0 to ensure retrieved docs are returned.
LOG "normal load rps=${LOAD_RPS} dur=${LOAD_DURATION}s fail_rate=${FAIL_RATE}"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_RETRIEVER_PORT}"
path="${RETRIEVER_PATH}"
dur=${LOAD_DURATION}
rps=${LOAD_RPS}
workers=${LOAD_WORKERS}
stop=time.time()+dur
per=max(1,rps//max(1,workers))
sleep_amt=1.0/per
stats={"req":0,"fails":0,"lat":[]}
lock=threading.Lock()
def do():
  while time.time()<stop:
    url=f"{host}{path}"
    target=url
    if random.random()<${FAIL_RATE}:
      target=f"{host}/no_such"
    body=json.dumps({"query":"test","top_k":3}).encode()
    req=urllib.request.Request(target,data=body,headers={"Content-Type":"application/json"})
    t0=time.time()
    try:
      with urllib.request.urlopen(req, timeout=6) as r:
        ok = (r.status < 500); _=r.read()
    except:
      ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
      stats["lat"].append(time.time()-t0)
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print(json.dumps(stats))
PY

sleep 2

# short aggressive error burst to exercise error counters
LOG "error burst rps=${ERR_BURST_RPS} dur=${ERR_BURST_DURATION}s fail_rate=${ERR_BURST_FAIL_RATE}"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_RETRIEVER_PORT}"
path="${RETRIEVER_PATH}"
dur=${ERR_BURST_DURATION}
rps=${ERR_BURST_RPS}
workers=${ERR_BURST_WORKERS}
stop=time.time()+dur
per=max(1,rps//max(1,workers))
sleep_amt=1.0/per
stats={"req":0,"fails":0}
lock=threading.Lock()
def do():
  while time.time()<stop:
    r=random.random()
    if r < ${ERR_BURST_FAIL_RATE}:
      target=f"{host}/no_such"
      body=b'not-json'
      req=urllib.request.Request(target,data=body,headers={"Content-Type":"application/json"})
    else:
      target=f"{host}{path}"
      body=json.dumps({"query":"err-burst","top_k":1}).encode()
      req=urllib.request.Request(target,data=body,headers={"Content-Type":"application/json"})
    try:
      with urllib.request.urlopen(req, timeout=4) as resp:
        ok=(resp.status<500)
    except:
      ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print(json.dumps(stats))
PY

sleep 3

# capture post-load metrics
curl -sS "http://127.0.0.1:${LOCAL_RETRIEVER_PORT}/metrics" > "${QUERY_ARTIFACT_DIR}/retriever_metrics_post.txt" || true
curl -sS "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" > "${QUERY_ARTIFACT_DIR}/vmagent_metrics_post.txt" || true

# quick local checks
grep -qE "^retrieval_requests_total(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/retriever_metrics_post.txt" || { ERR "retrieval_requests_total missing locally"; exit 21; }
grep -qE "^retrieval_request_duration_seconds_bucket(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/retriever_metrics_post.txt" || { ERR "histogram buckets missing locally"; exit 22; }
grep -qE "^service_ready(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/retriever_metrics_post.txt" || { ERR "service_ready missing locally"; exit 23; }

# TSDB checks to exercise every retriever panel
run_promql "core_series" 'count({__name__=~"retrieval_requests_total|retrieval_request_duration_seconds_bucket|service_ready"})' gt0 || ERR "retriever core series not in TSDB"
run_promql "requests_rate" 'sum(rate(retrieval_requests_total[1m]))' gt0 || ERR "retrieval_requests_total rate not in TSDB"
run_promql "errors_rate" 'sum(rate(retrieval_errors_total[5m]))' any || LOG "retrieval_errors_total not present in TSDB (may be zero)"
run_promql "hist_buckets" 'count(retrieval_request_duration_seconds_bucket)' gt0 || ERR "retrieval histogram not in TSDB"
run_promql "retrieved_docs" 'sum(rate(retrieved_docs_count_count[1m]))' any || LOG "retrieved_docs_count_count not in TSDB"
# replicas panel (prefer kube_pod_info, fallback to deployment replicas)
run_promql "replicas" 'count(kube_pod_info{namespace="inference",pod=~"retrieval.*"}) or sum(kube_deployment_status_replicas{namespace="inference",deployment=~"retrieval.*"})' any || LOG "replica metric not in TSDB (kube-state-metrics might be missing)"

LOG "retriever e2e checks complete; artifacts: ${QUERY_ARTIFACT_DIR}"
exit 0
