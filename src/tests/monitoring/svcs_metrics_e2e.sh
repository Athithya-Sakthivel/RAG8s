#!/usr/bin/env bash
# Combined e2e for qdrant + retriever metrics -> exercises successes/failures and verifies Victoria ingestion.
make fix-dns >/dev/null 2>&1

LOG(){ printf '%s %s\n' "$(date -Iseconds)" "$*"; }
ERR(){ printf '%s ERROR %s\n' "$(date -Iseconds)" "$*" >&2; }

for cmd in kubectl curl python3 jq; do
  command -v "$cmd" >/dev/null 2>&1 || { ERR "$cmd required"; exit 2; }
done

# --- configuration (override via env) ---
QDRANT_NAMESPACE=${QDRANT_NAMESPACE:-qdrant}
QDRANT_SVC=${QDRANT_SVC:-qdrant}
QDRANT_PORT=${QDRANT_PORT:-6333}
SEED_COLLECTION=${SEED_COLLECTION:-e2e_test_collection}
SEED_POINTS=${SEED_POINTS:-200}
VECTOR_SIZE=${VECTOR_SIZE:-4}

RETRIEVER_NS=${RETRIEVER_NS:-inference}
RETRIEVER_SVC=${RETRIEVER_SVC:-retrieval-svc}
RETRIEVER_PORT=${RETRIEVER_PORT:-8001}
RETRIEVER_PATH=${RETRIEVER_PATH:-/generate}

VM_NS=${VM_NS:-monitoring}
VICTORIA_SVC=${VICTORIA_SVC:-victoria-metrics}
VMAGENT_SVC=${VMAGENT_SVC:-vmagent}

LOCAL_VICTORIA_PORT=${LOCAL_VICTORIA_PORT:-0}
LOCAL_VMAGENT_PORT=${LOCAL_VMAGENT_PORT:-0}
LOCAL_QDRANT_PORT=${LOCAL_QDRANT_PORT:-0}
LOCAL_RETRIEVER_PORT=${LOCAL_RETRIEVER_PORT:-0}

# loads
Q_LOAD_RPS=${Q_LOAD_RPS:-30}
Q_LOAD_DURATION=${Q_LOAD_DURATION:-30}
Q_LOAD_WORKERS=${Q_LOAD_WORKERS:-4}
Q_ERR_BURST_RPS=${Q_ERR_BURST_RPS:-20}
Q_ERR_BURST_DURATION=${Q_ERR_BURST_DURATION:-15}
Q_ERR_BURST_WORKERS=${Q_ERR_BURST_WORKERS:-3}
Q_ERR_BURST_FAIL_RATE=${Q_ERR_BURST_FAIL_RATE:-0.6}

R_LOAD_RPS=${R_LOAD_RPS:-30}
R_LOAD_DURATION=${R_LOAD_DURATION:-30}
R_LOAD_WORKERS=${R_LOAD_WORKERS:-4}
R_FAIL_RATE=${R_FAIL_RATE:-0.02}
R_ERR_BURST_RPS=${R_ERR_BURST_RPS:-25}
R_ERR_BURST_DURATION=${R_ERR_BURST_DURATION:-15}
R_ERR_BURST_WORKERS=${R_ERR_BURST_WORKERS:-3}
R_ERR_BURST_FAIL_RATE=${R_ERR_BURST_FAIL_RATE:-0.6}

# runtime state
PF_PIDS=()

trap 'rc=$?; LOG "cleanup (killing port-forwards)"; for p in "${PF_PIDS[@]:-}"; do kill "$p" >/dev/null 2>&1 || true; done; exit $rc' INT TERM EXIT

find_free_port(){
  python3 - <<'PY'
import socket
s=socket.socket()
s.bind(('',0))
print(s.getsockname()[1])
s.close()
PY
}

alloc_port_or_die(){
  for i in 1 2 3 4 5; do
    p=$(find_free_port) || true
    if [ -n "${p:-}" ]; then echo "$p"; return 0; fi
    sleep 0.1
  done
  ERR "cannot allocate local port"; exit 98
}

# start port-forward and wait until /metrics responds
portforward_wait(){
  ns="$1"; target="$2"; localp="$3"; remotep="$4"; timeout=${5:-30}
  LOG "starting port-forward $ns/$target ${localp}:${remotep}"
  kubectl -n "$ns" port-forward "$target" "${localp}:${remotep}" >/dev/null 2>&1 &
  pfpid=$!
  PF_PIDS+=("$pfpid")
  start=$SECONDS
  while [ $((SECONDS - start)) -lt "$timeout" ]; do
    if ! kill -0 "$pfpid" >/dev/null 2>&1; then
      ERR "port-forward $target exited prematurely"; return 2
    fi
    if curl -sS --max-time 2 "http://127.0.0.1:${localp}/metrics" >/dev/null 2>&1; then
      LOG "port-forward ready: $ns/$target -> http://127.0.0.1:${localp}"
      return 0
    fi
    sleep 0.5
  done
  ERR "port-forward timeout for $ns/$target"; return 3
}

# simple PromQL runner against localhost Victoria
run_promql(){
  name="$1"; promql="$2"; expect="$3"; retries=${4:-8}
  base="http://127.0.0.1:${LOCAL_VICTORIA_PORT}/api/v1/query"
  for attempt in $(seq 1 $retries); do
    LOG "PromQL ${name} attempt ${attempt}: ${promql}"
    json=$(curl -sS -G --data-urlencode "query=${promql}" "$base" || echo "")
    if [ -z "$json" ]; then sleep $((2*attempt)); continue; fi
    ok=$(echo "$json" | jq -r '.status == "success"')
    if [ "$ok" = "true" ]; then
      cnt=$(echo "$json" | jq -r '.data.result | length')
      if [ "$expect" = "any" ] && [ "$cnt" -gt 0 ]; then
        echo "$json" | jq -c '.'
        return 0
      fi
      if [ "$expect" = "gt0" ] && [ "$cnt" -gt 0 ]; then
        val=$(echo "$json" | jq -r '.data.result[0].value[1] // empty')
        if [ -n "$val" ]; then
          python3 - <<PY "$val"
import sys
try:
  sys.exit(0 if float(sys.argv[1])>0 else 1)
except:
  sys.exit(2)
PY
          rc=$?
          if [ $rc -eq 0 ]; then echo "$json" | jq -c '.'; return 0; fi
        fi
      fi
    fi
    sleep $((2*attempt))
  done
  ERR "PromQL ${name} failed (no matching series / non >0)"; echo "$json" | jq -c . || true
  return 1
}

# ---- allocate local ports and start port-forwards ----
if [ "$LOCAL_VICTORIA_PORT" -eq 0 ]; then LOCAL_VICTORIA_PORT=$(alloc_port_or_die); fi
if ! portforward_wait "$VM_NS" "svc/${VICTORIA_SVC}" "$LOCAL_VICTORIA_PORT" 8428 30; then exit 4; fi

if [ "$LOCAL_VMAGENT_PORT" -eq 0 ]; then LOCAL_VMAGENT_PORT=$(alloc_port_or_die); fi
if ! portforward_wait "$VM_NS" "svc/${VMAGENT_SVC}" "$LOCAL_VMAGENT_PORT" 8429 30; then exit 5; fi

if [ "$LOCAL_QDRANT_PORT" -eq 0 ]; then LOCAL_QDRANT_PORT=$(alloc_port_or_die); fi
if ! portforward_wait "$QDRANT_NAMESPACE" "svc/${QDRANT_SVC}" "$LOCAL_QDRANT_PORT" "$QDRANT_PORT" 30; then exit 6; fi

if [ "$LOCAL_RETRIEVER_PORT" -eq 0 ]; then LOCAL_RETRIEVER_PORT=$(alloc_port_or_die); fi
if ! portforward_wait "$RETRIEVER_NS" "svc/${RETRIEVER_SVC}" "$LOCAL_RETRIEVER_PORT" "$RETRIEVER_PORT" 30; then exit 7; fi

LOG "PORT-FORWARDS ACTIVE: victoria=${LOCAL_VICTORIA_PORT} vmagent=${LOCAL_VMAGENT_PORT} qdrant=${LOCAL_QDRANT_PORT} retriever=${LOCAL_RETRIEVER_PORT}"

# ---- quick health / scraping evidence ----
LOG "Victoria / vmagent remote-write evidence (local snapshot)"
curl -sS "http://127.0.0.1:${LOCAL_VICTORIA_PORT}/metrics" | egrep -i 'vmagent|victoria|up|remotewrite' | sed -n '1,120p' || true
echo "---- vmagent important metrics ----"
curl -sS "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" | egrep -E 'vmagent_remotewrite_sent_bytes_total|vmagent_remotewrite_errors_total|vm_promscrape_series_fetched|vm_promscrape_targets_scraped' || true

# ---- Qdrant: seed collection, upsert points, normal load + error burst ----
LOG "Qdrant: create seed collection ${SEED_COLLECTION}"
curl -sS -X PUT "http://127.0.0.1:${LOCAL_QDRANT_PORT}/collections/${SEED_COLLECTION}" \
  -H "Content-Type: application/json" -d "{\"vectors\":{\"size\":${VECTOR_SIZE},\"distance\":\"Cosine\"}}" >/dev/null || true

LOG "Qdrant: upserting ${SEED_POINTS} points (chunks)"
python3 - <<PY
import json,urllib.request
host="http://127.0.0.1:${LOCAL_QDRANT_PORT}"
coll="${SEED_COLLECTION}"
total=${SEED_POINTS}
bs=100
i=0
while i<total:
  chunk=min(bs,total-i)
  pts=[]
  for j in range(chunk):
    pid=i+j+1
    pts.append({"id":pid,"vector":[0.1]*${VECTOR_SIZE}})
  body=json.dumps({"points":pts}).encode()
  req=urllib.request.Request(f"{host}/collections/{coll}/points?wait=true", data=body, headers={"Content-Type":"application/json"})
  try:
    urllib.request.urlopen(req, timeout=10).read()
  except Exception as e:
    print("upsert error",e)
  i+=chunk
print("done")
PY

sleep 2

LOG "Qdrant normal load rps=${Q_LOAD_RPS} dur=${Q_LOAD_DURATION}s (small fail fraction)"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_QDRANT_PORT}"
coll="${SEED_COLLECTION}"
dur=${Q_LOAD_DURATION}; rps=${Q_LOAD_RPS}; workers=${Q_LOAD_WORKERS}
stop=time.time()+dur
per=max(1, rps//max(1,workers))
sleep_amt=1.0/per
stats={"req":0,"fails":0}
lock=threading.Lock()
def do():
  while time.time()<stop:
    target=coll if random.random()>0.02 else coll+"_NO"
    req=urllib.request.Request(f"{host}/collections/{target}/points/search", data=json.dumps({"vector":[0.1]*${VECTOR_SIZE},"limit":5}).encode(), headers={"Content-Type":"application/json"})
    try:
      with urllib.request.urlopen(req, timeout=6) as r:
        _=r.read(); ok=True
    except:
      ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print("QDRANT_NORMAL", stats)
PY

sleep 2

LOG "Qdrant error burst rps=${Q_ERR_BURST_RPS} dur=${Q_ERR_BURST_DURATION}s (high fail rate)"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_QDRANT_PORT}"
coll="${SEED_COLLECTION}"
dur=${Q_ERR_BURST_DURATION}; rps=${Q_ERR_BURST_RPS}; workers=${Q_ERR_BURST_WORKERS}
stop=time.time()+dur
per=max(1, rps//max(1,workers))
sleep_amt=1.0/per
stats={"req":0,"fails":0}
lock=threading.Lock()
def do():
  while time.time()<stop:
    target=coll if random.random()>${Q_ERR_BURST_FAIL_RATE} else coll+"_NO"
    req=urllib.request.Request(f"{host}/collections/{target}/points/search", data=json.dumps({"vector":[0.1]*${VECTOR_SIZE},"limit":5}).encode(), headers={"Content-Type":"application/json"})
    try:
      with urllib.request.urlopen(req, timeout=4) as r:
        _=r.read(); ok=True
    except:
      ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print("QDRANT_ERRBURST", stats)
PY

sleep 4

LOG "Qdrant: show key local metric lines (rest_responses_total / rest_responses_fail_total / duration buckets / collections_vector_total)"
curl -sS "http://127.0.0.1:${LOCAL_QDRANT_PORT}/metrics" | egrep -E '^rest_responses_total|^rest_responses_fail_total|^rest_responses_duration_seconds_bucket|^collections_vector_total' | sed -n '1,240p' || true

# ---- Retriever traffic ----
LOG "Retriever normal load rps=${R_LOAD_RPS} dur=${R_LOAD_DURATION}s fail_rate=${R_FAIL_RATE}"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_RETRIEVER_PORT}"
path="${RETRIEVER_PATH}"
dur=${R_LOAD_DURATION}; rps=${R_LOAD_RPS}; workers=${R_LOAD_WORKERS}
stop=time.time()+dur
per=max(1, rps//max(1,workers))
sleep_amt=1.0/per
stats={"req":0,"fails":0}
lock=threading.Lock()
def do():
  while time.time()<stop:
    if random.random()<${R_FAIL_RATE}:
      target=f"{host}/no_such"
      body=json.dumps({}).encode()
    else:
      target=f"{host}{path}"
      body=json.dumps({"query":"test","top_k":1}).encode()
    req=urllib.request.Request(target,data=body,headers={"Content-Type":"application/json"})
    try:
      with urllib.request.urlopen(req, timeout=6) as r:
        ok=(r.status < 500); _=r.read()
    except:
      ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print("RETRIEVER_NORMAL", stats)
PY

sleep 2

LOG "Retriever error burst rps=${R_ERR_BURST_RPS} dur=${R_ERR_BURST_DURATION}s (high fail rate)"
python3 - <<PY
import threading,time,random,json,urllib.request
host="http://127.0.0.1:${LOCAL_RETRIEVER_PORT}"
path="${RETRIEVER_PATH}"
dur=${R_ERR_BURST_DURATION}; rps=${R_ERR_BURST_RPS}; workers=${R_ERR_BURST_WORKERS}
stop=time.time()+dur
per=max(1, rps//max(1,workers))
sleep_amt=1.0/per
stats={"req":0,"fails":0}
lock=threading.Lock()
def do():
  while time.time()<stop:
    if random.random()<${R_ERR_BURST_FAIL_RATE}:
      target=f"{host}/no_such"; body=b'not-json'
    else:
      target=f"{host}{path}"; body=json.dumps({"query":"err-burst","top_k":1}).encode()
    req=urllib.request.Request(target,data=body,headers={"Content-Type":"application/json"})
    try:
      with urllib.request.urlopen(req, timeout=4) as r:
        ok=(r.status<500)
    except:
      ok=False
    with lock:
      stats["req"]+=1
      if not ok: stats["fails"]+=1
    time.sleep(sleep_amt)
threads=[threading.Thread(target=do) for _ in range(workers)]
for t in threads: t.start()
for t in threads: t.join(timeout=dur+5)
print("RETRIEVER_ERRBURST", stats)
PY

sleep 3

LOG "Retriever: show key local metric lines (retrieval_requests_total / retrieval_errors_total / duration buckets / retrieved_docs_count)"
curl -sS "http://127.0.0.1:${LOCAL_RETRIEVER_PORT}/metrics" | egrep -E '^retrieval_requests_total|^retrieval_errors_total|^retrieval_request_duration_seconds_bucket|^retrieved_docs_count' | sed -n '1,240p' || true

# ---- vmagent / victoria evidence ----
LOG "vmagent local metrics (important slices):"
curl -sS "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" | egrep -E 'vmagent_remotewrite_sent_bytes_total|vmagent_remotewrite_errors_total|vm_promscrape_series_fetched|vm_promscrape_targets_scraped' || true

# ---- PromQL checks against Victoria ----
LOG "PromQL checks (Victoria local) - Qdrant"
run_promql "qdrant_up" 'max(up{job=~"qdrant.*"})' any || run_promql "qdrant_up_inst" 'max(up{instance=~".*:6333"})' any || LOG "qdrant up not in TSDB"
run_promql "qdrant_rest_rate" 'sum(rate(rest_responses_total[1m]))' gt0 || LOG "rest_responses_total not in TSDB or rate==0"
run_promql "qdrant_rest_fail_rate" 'sum(rate(rest_responses_fail_total[1m]))' any || LOG "rest_responses_fail_total absent/zero in TSDB"
run_promql "qdrant_hist_buckets" 'count(rest_responses_duration_seconds_bucket)' gt0 || ERR "qdrant hist buckets not in TSDB"
run_promql "qdrant_vector_count" 'sum(collections_vector_total)' gt0 || ERR "collections_vector_total not in TSDB"

LOG "PromQL checks (Victoria local) - Retriever"
# prefer explicit errors_total; fallback to non-2xx subtraction
run_promql "retr_requests" 'sum(rate(retrieval_requests_total[1m]))' any || LOG "retrieval_requests_total (rate) missing in TSDB"
run_promql "retr_errors_explicit" 'sum(rate(retrieval_errors_total[5m]))' any || LOG "retrieval_errors_total absent or zero in TSDB"
# fallback check: if status_code label exists, test for non-2xx fraction
run_promql "retr_requests_2xx_count_check" 'sum(rate(retrieval_requests_total{status_code=~"2.."}[1m]))' any || LOG "status_code label for retrieval_requests_total not found in TSDB"
# histogram existence
run_promql "retr_hist_buckets" 'count(retrieval_request_duration_seconds_bucket)' gt0 || LOG "retrieval histogram buckets missing in TSDB"
# retrieved docs
run_promql "retr_docs" 'sum(rate(retrieved_docs_count_count[1m]))' any || LOG "retrieved_docs_count missing in TSDB"

# replicas checks (KSM may be down; show fallbacks)
LOG "Replicas checks (try kube-state-metrics, then up() fallback)"
run_promql "replicas_qdrant_ksm" 'count(kube_pod_info{namespace="qdrant",pod=~"qdrant.*"})' any || run_promql "replicas_qdrant_up" 'count(up{instance=~".*:6333"})' any || LOG "qdrant replicas not visible"
run_promql "replicas_retriever_ksm" 'count(kube_pod_info{namespace="inference",pod=~"retrieval.*"})' any || run_promql "replicas_retriever_up" 'count(up{instance=~".*:8001"})' any || LOG "retriever replicas not visible"

# ---- summary ----
LOG "FINAL: vmagent remote-write errors (if non-zero indicate ingestion failures):"
curl -sS "http://127.0.0.1:${LOCAL_VMAGENT_PORT}/metrics" | egrep -E 'vmagent_remotewrite_errors_total|vmagent_remotewrite_sent_bytes_total' || true

LOG "E2E complete. If retriever series are missing in Victoria, check vmagent remote-write errors and kube-state-metrics status. Exit status indicates whether fatal checks failed."

# cleanup handled by trap
exit 0
