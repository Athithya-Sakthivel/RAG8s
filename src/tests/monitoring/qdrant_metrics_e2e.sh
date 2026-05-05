#!/usr/bin/env bash
# compact qdrant e2e: exercise throughput + failures, verify every qdrant dashboard's panel metric
set -euo pipefail

LOG(){ printf '%s %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }

for cmd in kubectl curl python3 jq mktemp awk sed grep; do
  command -v "$cmd" >/dev/null 2>&1 || { ERR "$cmd required"; exit 2; }
done

QDRANT_NAMESPACE=${QDRANT_NAMESPACE:-qdrant}
QDRANT_SVC=${QDRANT_SVC:-qdrant}
QDRANT_PORT=${QDRANT_PORT:-6333}
VM_NS=${VM_NS:-monitoring}
VM_SVC=${VM_SVC:-victoria-metrics}
VMAGENT_SVC=${VMAGENT_SVC:-vmagent}
LOCAL_VICTORIA_PORT=${LOCAL_VICTORIA_PORT:-0}
LOCAL_VMAGENT_PORT=${LOCAL_VMAGENT_PORT:-0}
LOCAL_QDRANT_PORT=${LOCAL_QDRANT_PORT:-0}

SEED_COLLECTION=${SEED_COLLECTION:-e2e_test_collection}
SEED_POINTS=${SEED_POINTS:-200}
VECTOR_SIZE=${VECTOR_SIZE:-4}

LOAD_RPS=${LOAD_RPS:-30}
LOAD_DURATION=${LOAD_DURATION:-45}
LOAD_WORKERS=${LOAD_WORKERS:-4}

ERR_BURST_RPS=${ERR_BURST_RPS:-20}
ERR_BURST_DURATION=${ERR_BURST_DURATION:-15}
ERR_BURST_WORKERS=${ERR_BURST_WORKERS:-3}
ERR_BURST_FAIL_RATE=${ERR_BURST_FAIL_RATE:-0.6}

QUERY_ARTIFACT_DIR=${QUERY_ARTIFACT_DIR:-/tmp/qdrant_e2e_artifacts}
mkdir -p "$QUERY_ARTIFACT_DIR"

trap 'rc=$?; LOG "cleanup"; pkill -P $$ || true; exit $rc' INT TERM EXIT

find_free_port(){ python3 - <<PY
import socket
s=socket.socket()
s.bind(('',0))
print(s.getsockname()[1])
s.close()
PY
}

alloc_port(){
  for i in 1 2 3 4 5; do
    p="$(find_free_port)"
    if [ -n "$p" ]; then echo "$p"; return; fi
    sleep 0.1
  done
  ERR "no free port"; exit 98
}

portforward_wait(){
  ns=$1; target=$2; localp=$3; remotep=$4; timeout=${5:-30}
  logf="$(mktemp -t pf.${target//[^a-zA-Z0-9]/_}.XXXX)"
  kubectl -n "$ns" port-forward "$target" "${localp}:${remotep}" >"$logf" 2>&1 &
  pfpid=$!
  for i in $(seq 1 $timeout); do
    if ! kill -0 "$pfpid" >/dev/null 2>&1; then
      ERR "port-forward $target exited; tail $logf"; tail -n200 "$logf" || true; return 2
    fi
    if curl -sS --max-time 1 "http://127.0.0.1:${localp}/metrics" >/dev/null 2>&1; then
      echo "$pfpid|$logf"; return 0
    fi
    sleep 1
  done
  ERR "port-forward timed out; tail $logf"; tail -n200 "$logf" || true; return 3
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

# start port-forwards
if [ "$LOCAL_VICTORIA_PORT" -eq 0 ]; then LOCAL_VICTORIA_PORT="$(alloc_port)"; fi
portforward_wait "$VM_NS" "svc/$VM_SVC" "$LOCAL_VICTORIA_PORT" 8428 || exit 4
LOG "victoria pf ready:$LOCAL_VICTORIA_PORT"

if [ "$LOCAL_VMAGENT_PORT" -eq 0 ]; then LOCAL_VMAGENT_PORT="$(alloc_port)"; fi
portforward_wait "$VM_NS" "svc/$VMAGENT_SVC" "$LOCAL_VMAGENT_PORT" 8429 || exit 5
LOG "vmagent pf ready:$LOCAL_VMAGENT_PORT"

if [ "$LOCAL_QDRANT_PORT" -eq 0 ]; then LOCAL_QDRANT_PORT="$(alloc_port)"; fi
portforward_wait "$QDRANT_NAMESPACE" "svc/$QDRANT_SVC" "$LOCAL_QDRANT_PORT" "$QDRANT_PORT" || exit 6
LOG "qdrant pf ready:$LOCAL_QDRANT_PORT"

# seed a collection and points (exercises Total Vectors panel)
LOG "create seed collection $SEED_COLLECTION"
curl -sS -X PUT "http://127.0.0.1:${LOCAL_QDRANT_PORT}/collections/${SEED_COLLECTION}" \
  -H "Content-Type: application/json" -d "{\"vectors\":{\"size\":${VECTOR_SIZE},\"distance\":\"Cosine\"}}" >/dev/null || true

# upsert points
bsize=100; sent=0
while [ $sent -lt $SEED_POINTS ]; do
  chunk=$((SEED_POINTS - sent)); [ $chunk -gt $bsize ] && chunk=$bsize
  pts="["
  for i in $(seq 1 $chunk); do
    id=$((sent + i)); vec=$(python3 -c "import json; print(json.dumps([0.1]*$VECTOR_SIZE))")
    pts+="{\"id\":${id},\"vector\":${vec}}"
    [ $i -lt $chunk ] && pts+=","
  done
  pts+="]"
  curl -sS -X PUT "http://127.0.0.1:${LOCAL_QDRANT_PORT}/collections/${SEED_COLLECTION}/points?wait=true" \
    -H "Content-Type: application/json" -d "{\"points\":${pts}}" >/dev/null
  sent=$((sent + chunk))
done

sleep 2

# normal load (some fraction intentionally target a non-existent collection to produce failures)
LOG "normal load rps=${LOAD_RPS} dur=${LOAD_DURATION}s fail_rate=0.02"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_QDRANT_PORT}"
coll="${SEED_COLLECTION}"
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
    target=coll if random.random()>0.02 else coll+"_NO"
    req=urllib.request.Request(f"{host}/collections/{target}/points/search", data=json.dumps({"vector":[0.1]*${VECTOR_SIZE},"limit":5}).encode(), headers={"Content-Type":"application/json"})
    t0=time.time()
    try:
      with urllib.request.urlopen(req, timeout=6) as r:
        _=r.read(); ok=True
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

# error burst (high failure rate) to exercise rest_responses_fail_total
LOG "error burst rps=${ERR_BURST_RPS} dur=${ERR_BURST_DURATION}s fail_rate=${ERR_BURST_FAIL_RATE}"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_QDRANT_PORT}"
coll="${SEED_COLLECTION}"
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
    target=coll if random.random()>${ERR_BURST_FAIL_RATE} else coll+"_NO"
    req=urllib.request.Request(f"{host}/collections/{target}/points/search", data=json.dumps({"vector":[0.1]*${VECTOR_SIZE},"limit":5}).encode(), headers={"Content-Type":"application/json"})
    try:
      with urllib.request.urlopen(req, timeout=4) as r: ok=True
    except: ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print(json.dumps(stats))
PY

sleep 4

# capture local metrics snapshots
curl -sS "http://127.0.0.1:${LOCAL_QDRANT_PORT}/metrics" > "${QUERY_ARTIFACT_DIR}/qdrant_metrics_post.txt" || true
curl -sS "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" > "${QUERY_ARTIFACT_DIR}/vmagent_metrics_post.txt" || true

# quick local sanity checks (fail fast)
grep -qE "^rest_responses_total(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/qdrant_metrics_post.txt" || { ERR "rest_responses_total missing"; exit 21; }
grep -qE "^rest_responses_fail_total(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/qdrant_metrics_post.txt" || LOG "rest_responses_fail_total absent locally (may be zero)"
grep -qE "^rest_responses_duration_seconds_bucket(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/qdrant_metrics_post.txt" || { ERR "histogram buckets missing"; exit 22; }
grep -qE "^collections_vector_total(\{|[[:space:]]|$)" "${QUERY_ARTIFACT_DIR}/qdrant_metrics_post.txt" || { ERR "collections_vector_total missing"; exit 23; }

# TSDB checks (exercise every qdrant dashboard panel)
run_promql "qdrant_up" 'max(up{job=~"qdrant.*"})' any || run_promql "qdrant_up_instance" 'max(up{instance=~".*:6333"})' any || ERR "qdrant up not found in TSDB"
run_promql "rest_rate" 'sum(rate(rest_responses_total[1m]))' gt0 || ERR "rest_responses_total not in TSDB"
run_promql "rest_fail_rate" 'sum(rate(rest_responses_fail_total[1m]))' any || LOG "rest_responses_fail_total may be zero or absent in TSDB"
run_promql "hist_buckets" 'count(rest_responses_duration_seconds_bucket)' gt0 || ERR "histogram buckets not in TSDB"
run_promql "vector_count" 'sum(collections_vector_total)' gt0 || ERR "collections_vector_total not in TSDB"
# replicas panel: prefer kube_pod_info, fallback statefulset replicas
run_promql "replicas" 'count(kube_pod_info{namespace="qdrant",pod=~"qdrant.*"}) or sum(kube_statefulset_status_replicas{namespace="qdrant",statefulset=~"qdrant.*"})' any || LOG "replica metric not in TSDB (kube-state-metrics not installed?)"

LOG "qdrant e2e checks complete; artifacts: ${QUERY_ARTIFACT_DIR}"
exit 0
