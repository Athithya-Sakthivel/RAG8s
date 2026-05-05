#!/usr/bin/env bash
# infra/tests/qdrant_scaling.sh
# Hardcoded prod-like test that deploys Qdrant via infra/generators/qdrant_cluster.sh
# then exercises collection creation, upsert/read/delete, and replication/write_consistency behavior.
set -euo pipefail
IFS=$'\n\t'

########################
# HARD-CODED TEST CONFIG
########################
ENV="PROD"
QDRANT_RELEASE="qdrant"
QDRANT_NAMESPACE="qdrant"

# StatefulSet replica count (must be >= QDRANT_REPLICATION_FACTOR)
QDRANT_REPLICAS="2"
QDRANT_CPU="0.8"         # CPU per pod (cores) - string allowed, script converts to millicores
QDRANT_MEMORY="650Mi"
QDRANT_IMAGE="qdrant/qdrant:v1.16.0"
CHART_VERSION="1.16.0"

# Collection / cluster test parameters (collection is created by this test)
COLLECTION_NAME="default_rag_collection1"
DENSE_DIM="384"
QDRANT_SHARD_NUMBER="2"
QDRANT_REPLICATION_FACTOR="2"
QDRANT_WRITE_CONSISTENCY_FACTOR="2"  # intentionally equal to replication_factor to test failure on single node outage

# HNSW tuning
QDRANT_HNSW_EF_CONSTRUCT="128"
QDRANT_HNSW_M="32"
QDRANT_HNSW_FULL_SCAN_THRESHOLD="10000"
QDRANT_ONDISK="true"

# API key (the script will create k8s secret if provided)
SECRET_SERVICE_NAME="qdrant-service-creds"
QDRANT_API_KEY="strongpassword1"   # hardcoded for the test

# Persistence (AKS): enable if you want PVs to be created; empty storage class will use cluster default if available
QDRANT_PERSISTENCE_ENABLED="true"
QDRANT_PERSISTENCE_SIZE="20Gi"
QDRANT_PERSISTENCE_STORAGE_CLASS="managed-premium"  # replace with your AKS storage class name if different

# Timeouts & logs
POD_READY_TIMEOUT=240
HTTP_WAIT_TIMEOUT=120
PF_LOG="/tmp/qdrant-port-forward.log"

# Safety: set to "1" to force deploy despite capacity warnings (NOT recommended)
FORCE_DEPLOY="0"

# Paths to required tools (assume available)
KUBECTL="$(command -v kubectl || true)"
CURL="$(command -v curl || true)"
JQ="$(command -v jq || true)"
PYTHON="$(command -v python3 || true)"


# convert cpu string (e.g. "0.5" or "500m") to millicores integer
cpu_to_mcpu() {
  local v="$1"
  if [[ "$v" =~ ^[0-9]+m$ ]]; then
    echo "${v%m}"
    return
  fi
  # decimal or integer cores -> multiply by 1000
  awk -v x="$v" 'BEGIN{printf "%d", int(x*1000)}'
}

# cluster total allocatable millicores
total_alloc_mcpu() {
  # sum allocatable.cpu across nodes
  $KUBECTL get nodes -o json | $JQ -r '.items[].status.allocatable.cpu' 2>/dev/null | \
    awk '{v=$1; if(index(v,"m")){sub(/m$/,"",v); print v} else {printf "%d\n", int(v*1000)}}' 2>/dev/null | awk '{s+=$1} END{print (s+0)}'
}

# safe curl wrapper to include api-key header if provided
curl_with_key() {
  local method="$1"; shift
  local url="$1"; shift
  local out="$1"; shift || true
  local extra=("$@")
  if [ -n "${QDRANT_API_KEY:-}" ]; then
    $CURL -sS -H "api-key: ${QDRANT_API_KEY}" -X "${method}" "${extra[@]}" "${url}" -o "${out}"
  else
    $CURL -sS -X "${method}" "${extra[@]}" "${url}" -o "${out}"
  fi
}

# small JSON POST helper returning both body and http code
curl_json_code() {
  local method="$1"; shift
  local url="$1"; shift
  local data_arg=()
  if [ "$method" = "POST" ] || [ "$method" = "PUT" ]; then
    data_arg=(-H "Content-Type: application/json" --data-binary @"$1"); shift
  fi
  if [ -n "${QDRANT_API_KEY:-}" ]; then
    $CURL -s -w "\n%{http_code}" -H "api-key: ${QDRANT_API_KEY}" "${data_arg[@]}" "$url"
  else
    $CURL -s -w "\n%{http_code}" "${data_arg[@]}" "$url"
  fi
}

########################
# VALIDATIONS
########################
# replication constraints
if ! [[ "${QDRANT_REPLICAS}" =~ ^[0-9]+$ && "${QDRANT_REPLICATION_FACTOR}" =~ ^[0-9]+$ && "${QDRANT_WRITE_CONSISTENCY_FACTOR}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: replicas/replication_factor/write_consistency_factor must be integers" >&2
  exit 2
fi
if [ "${QDRANT_REPLICAS}" -lt "${QDRANT_REPLICATION_FACTOR}" ]; then
  echo "ERROR: QDRANT_REPLICAS (${QDRANT_REPLICAS}) must be >= QDRANT_REPLICATION_FACTOR (${QDRANT_REPLICATION_FACTOR})" >&2
  exit 2
fi
if [ "${QDRANT_WRITE_CONSISTENCY_FACTOR}" -gt "${QDRANT_REPLICATION_FACTOR}" ]; then
  echo "ERROR: QDRANT_WRITE_CONSISTENCY_FACTOR (${QDRANT_WRITE_CONSISTENCY_FACTOR}) cannot exceed QDRANT_REPLICATION_FACTOR (${QDRANT_REPLICATION_FACTOR})" >&2
  exit 2
fi

# cluster capacity check
REQ_CPU_M=$( cpu_to_mcpu "${QDRANT_CPU}" )
REQUIRED_TOTAL_MCPU=$(( REQ_CPU_M * QDRANT_REPLICAS ))
TOTAL_ALLOC_MCPU=$( total_alloc_mcpu )
echo "[info] preflight: requested qdrant pods=${QDRANT_REPLICAS}, cpu_per_pod=${QDRANT_CPU} -> ${REQ_CPU_M}m each, total=${REQUIRED_TOTAL_MCPU}m"
echo "[info] cluster allocatable CPU total: ${TOTAL_ALLOC_MCPU}m"
SAFETY_MCPU=500
if [ "${TOTAL_ALLOC_MCPU:-0}" -lt $(( REQUIRED_TOTAL_MCPU + SAFETY_MCPU )) ]; then
  echo "[warn] Cluster likely lacks sufficient allocatable CPU for requested Qdrant pods."
  echo "[warn] required=${REQUIRED_TOTAL_MCPU}m + safety=${SAFETY_MCPU}m = $(( REQUIRED_TOTAL_MCPU + SAFETY_MCPU ))m ; allocatable=${TOTAL_ALLOC_MCPU}m"
  if [ "${FORCE_DEPLOY}" != "1" ]; then
    echo "[error] aborting. Set FORCE_DEPLOY=1 to override (not recommended for production tests)." >&2
    exit 4
  else
    echo "[warn] FORCE_DEPLOY=1 set — proceeding despite capacity warning."
  fi
fi

########################
# DEPLOY: create namespace, secret, and call generator
########################
echo "[info] ensuring namespace ${QDRANT_NAMESPACE}"
$KUBECTL create namespace "${QDRANT_NAMESPACE}" --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null 2>&1

# create API key secret (create or update)
if [ -n "${QDRANT_API_KEY}" ]; then
  echo "[info] creating/updating API key secret ${SECRET_SERVICE_NAME} in namespace ${QDRANT_NAMESPACE}"
  $KUBECTL -n "${QDRANT_NAMESPACE}" create secret generic "${SECRET_SERVICE_NAME}" --from-literal=QDRANT__SERVICE__API_KEY="${QDRANT_API_KEY}" --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null 2>&1
fi

# export envs for the generator so it writes values.yaml correctly
export ENV QDRANT_RELEASE QDRANT_NAMESPACE QDRANT_REPLICAS QDRANT_CPU QDRANT_MEMORY QDRANT_IMAGE CHART_VERSION
export QDRANT__SERVICE__API_KEY="${QDRANT_API_KEY}"
export QDRANT_SHARD_NUMBER QDRANT_REPLICATION_FACTOR QDRANT_WRITE_CONSISTENCY_FACTOR
export QDRANT_HNSW_EF_CONSTRUCT QDRANT_HNSW_M QDRANT_HNSW_FULL_SCAN_THRESHOLD QDRANT_ONDISK
export QDRANT_PERSISTENCE_ENABLED QDRANT_PERSISTENCE_SIZE QDRANT_PERSISTENCE_STORAGE_CLASS
echo "[info] invoking generator to deploy qdrant"
python3 infra/generators/qdrant_cluster.py --apply

########################
# WAIT: qdrant pods readiness
########################
echo "[info] waiting up to ${POD_READY_TIMEOUT}s for ${QDRANT_REPLICAS} qdrant pods to be ready"
end=$(( SECONDS + POD_READY_TIMEOUT ))
ready_count=0
while [ $SECONDS -lt $end ]; do
  # containerStatuses array may be missing for pods not fully initialized; count "true" entries robustly
  ready_count=$($KUBECTL -n "${QDRANT_NAMESPACE}" get pods -l app.kubernetes.io/name="${QDRANT_RELEASE}" -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.containerStatuses[0].ready}{"\n"}{end}' 2>/dev/null | awk -F'|' '$2=="true"{count++}END{print (count+0)}')
  total_count=$($KUBECTL -n "${QDRANT_NAMESPACE}" get pods -l app.kubernetes.io/name="${QDRANT_RELEASE}" --no-headers 2>/dev/null | wc -l)
  echo "[info] pods ready: ${ready_count}/${total_count}"
  if [ "${ready_count}" -ge "${QDRANT_REPLICAS}" ] && [ "${total_count}" -ge "${QDRANT_REPLICAS}" ]; then break; fi
  sleep 3
done
if [ "${ready_count:-0}" -lt "${QDRANT_REPLICAS}" ]; then
  echo "[error] not enough qdrant pods ready after ${POD_READY_TIMEOUT}s" >&2
  $KUBECTL -n "${QDRANT_NAMESPACE}" get pods -o wide
  exit 5
fi

########################
# PORT-FORWARD & API checks
########################
# pick pod (prefer qdrant-0)
POD_NAME=$($KUBECTL -n "${QDRANT_NAMESPACE}" get pods -l app.kubernetes.io/name="${QDRANT_RELEASE}" -o jsonpath="{.items[0].metadata.name}")
if [ -z "${POD_NAME}" ]; then
  echo "[error] could not select a pod for port-forward" >&2
  exit 6
fi
echo "[info] selected pod for port-forward: ${POD_NAME}"

# start port-forward in background
echo "[info] starting kubectl port-forward to 127.0.0.1:6333,6334,6335 -> pod/${POD_NAME}"
$KUBECTL -n "${QDRANT_NAMESPACE}" port-forward "pod/${POD_NAME}" 6333:6333 6334:6334 6335:6335 > "${PF_LOG}" 2>&1 &
PF_PID=$!
echo "${PF_PID}" > /tmp/qdrant-port-forward.pid

# wait for HTTP
QURL="http://127.0.0.1:6333"
echo "[info] waiting up to ${HTTP_WAIT_TIMEOUT}s for ${QURL}/collections"
end=$(( SECONDS + HTTP_WAIT_TIMEOUT ))
ok=0
while [ $SECONDS -lt $end ]; do
  http_code=$($CURL -s -o /dev/null -w "%{http_code}" -H "api-key: ${QDRANT_API_KEY}" "${QURL}/collections" || true)
  if [ "$http_code" = "200" ]; then ok=1; break; fi
  sleep 1
done
if [ "${ok}" -ne 1 ]; then
  echo "[error] qdrant HTTP endpoint did not respond 200 within ${HTTP_WAIT_TIMEOUT}s (last code=${http_code})" >&2
  echo "[log] tail of port-forward:"
  tail -n 200 "${PF_LOG}" || true
  kill "${PF_PID}" >/dev/null 2>&1 || true
  exit 7
fi
echo "[info] qdrant HTTP reachable"

########################
# CREATE collection with requested sharding/replication/write_consistency
########################
TMP_CREATE_JSON="$(mktemp)"
${PYTHON} - <<PY > "${TMP_CREATE_JSON}"
import json, os
body = {
  "vectors": {
    "dense": {
      "size": int(os.environ.get("DENSE_DIM","384")),
      "distance": "Cosine",
      "hnsw_config": {
        "m": int(os.environ.get("QDRANT_HNSW_M","32")),
        "ef_construct": int(os.environ.get("QDRANT_HNSW_EF_CONSTRUCT","128")),
        "full_scan_threshold": int(os.environ.get("QDRANT_HNSW_FULL_SCAN_THRESHOLD","10000")),
        "on_disk": True if str(os.environ.get("QDRANT_ONDISK","true")).lower() in ("1","true","yes") else False
      }
    }
  },
  "sparse_vectors": {"sparse": {}},
  "shard_number": int(os.environ.get("QDRANT_SHARD_NUMBER","1")),
  "replication_factor": int(os.environ.get("QDRANT_REPLICATION_FACTOR","1")),
  "write_consistency_factor": int(os.environ.get("QDRANT_WRITE_CONSISTENCY_FACTOR","1")),
  "on_disk_payload": True
}
print(json.dumps(body))
PY

echo "[info] creating collection ${COLLECTION_NAME}"
echo "=== create body ==="
sed -n '1,200p' "${TMP_CREATE_JSON}"
$CURL -s -H "api-key: ${QDRANT_API_KEY}" -X PUT -H "Content-Type: application/json" --data-binary @"${TMP_CREATE_JSON}" "${QURL}/collections/${COLLECTION_NAME}" | $JQ .

echo "[info] verifying collection config & cluster mapping"
$CURL -s -H "api-key: ${QDRANT_API_KEY}" "${QURL}/collections/${COLLECTION_NAME}" | $JQ '.result.config.params, .result.status'
$CURL -s -H "api-key: ${QDRANT_API_KEY}" "${QURL}/collections/${COLLECTION_NAME}/cluster" | $JQ .

echo "[info] points count:"
$CURL -s -H "api-key: ${QDRANT_API_KEY}" -X POST "${QURL}/collections/${COLLECTION_NAME}/points/count" -H "Content-Type: application/json" --data-raw '{}' | $JQ .

########################
# INSERT a single sample point (vector must be under named vector 'dense')
########################
PAYLOAD_FILE="$(mktemp)"
${PYTHON} - <<PY > "${PAYLOAD_FILE}"
import json, os, time
dim = int(os.environ.get("DENSE_DIM","384"))
pid = int(1000000000 + int(time.time() % 1000000))
vec = [0.001] * dim
payload = {"points":[{"id": pid, "vectors": {"dense": vec}, "payload": {"diag_test": True}}]}
print(json.dumps(payload))
PY

echo "[info] upserting sample point (wait=true)"
$CURL -s -H "api-key: ${QDRANT_API_KEY}" -X PUT -H "Content-Type: application/json" --data-binary @"${PAYLOAD_FILE}" "${QURL}/collections/${COLLECTION_NAME}/points?wait=true" | $JQ .

# extract id for read-back
SAMPLE_ID=$(${PYTHON} - <<PY
import json
p=json.load(open("${PAYLOAD_FILE}"))
print(p["points"][0]["id"])
PY
)
echo "[info] retrieving sample point id=${SAMPLE_ID}"
$CURL -s -H "api-key: ${QDRANT_API_KEY}" -X POST -H "Content-Type: application/json" --data-raw "{\"ids\":[${SAMPLE_ID}]}" "${QURL}/collections/${COLLECTION_NAME}/points" | $JQ .

########################
# SIMULATE REPLICA OUTAGE -> delete one non-primary pod and attempt upsert
########################
echo "[info] simulating replica outage: deleting a non-primary qdrant pod"
POD_TO_DELETE=$($KUBECTL -n "${QDRANT_NAMESPACE}" get pods -l app.kubernetes.io/name="${QDRANT_RELEASE}" -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep -v '^'"${POD_NAME}"'$' | head -n1 || true)
if [ -z "${POD_TO_DELETE}" ]; then
  echo "[info] no alternate pod found; will delete ${POD_NAME}"
  POD_TO_DELETE="${POD_NAME}"
fi
echo "[info] deleting pod ${POD_TO_DELETE} (grace 0, force)"
$KUBECTL -n "${QDRANT_NAMESPACE}" delete pod "${POD_TO_DELETE}" --grace-period=0 --force >/dev/null 2>&1 || true

# wait a little for cluster to register outage
sleep 8

echo "[info] attempting upsert while one replica is down (wait=true); with write_consistency=${QDRANT_WRITE_CONSISTENCY_FACTOR} this may FAIL]"
UPRESP=$($CURL -s -w "\n%{http_code}" -H "api-key: ${QDRANT_API_KEY}" -X PUT -H "Content-Type: application/json" --data-binary @"${PAYLOAD_FILE}" "${QURL}/collections/${COLLECTION_NAME}/points?wait=true" || true)
echo "-> raw upsert result (body + HTTP code):"
echo "${UPRESP}" | sed -n '1,200p'
echo "-> pretty-printed body (if json):"
echo "${UPRESP}" | sed -n '1,$p' | head -n -1 | jq . -C || true
echo "-> HTTP code: $(echo "${UPRESP}" | tail -n1)"

# restore statefulset replica count (idempotent)
echo "[info] ensuring statefulset replicas are ${QDRANT_REPLICAS}"
$KUBECTL -n "${QDRANT_NAMESPACE}" scale statefulset "${QDRANT_RELEASE}" --replicas="${QDRANT_REPLICAS}" || true

echo "[info] waiting 30s for recovery"
sleep 30

echo "[info] final upsert after recovery (wait=true)"
$CURL -s -H "api-key: ${QDRANT_API_KEY}" -X PUT -H "Content-Type: application/json" --data-binary @"${PAYLOAD_FILE}" "${QURL}/collections/${COLLECTION_NAME}/points?wait=true" | $JQ . || true

########################
# CLEANUP & FINAL NOTES
########################
rm -f "${TMP_CREATE_JSON}" "${PAYLOAD_FILE}" "${PAYLOAD_FILE}.out" || true

if [ -n "${PF_PID:-}" ]; then
  echo "[info] stopping port-forward (pid ${PF_PID})"
  kill "${PF_PID}" >/dev/null 2>&1 || true
  wait "${PF_PID}" 2>/dev/null || true
fi
rm -f /tmp/qdrant-port-forward.pid || true

echo "[info] test finished. Inspect outputs above:"
echo " - collection config (.result.config.params)"
echo " - cluster mapping (/collections/<name>/cluster)"
echo " - upsert attempt result while replica down (expected failure if write_consistency > available replicas)"
echo " - final successful upsert after recovery (if recovery succeeded)"
exit 0
