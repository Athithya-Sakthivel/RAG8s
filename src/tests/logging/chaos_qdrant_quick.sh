#!/usr/bin/env bash

set -u
TOKEN="qdrant-e2e-$(date +%s)"
LOOKBACK_MIN="${LOOKBACK_MIN:-10}"
CH_NS="${CH_NS:-observability}"
Q_NS="${Q_NS:-qdrant}"

echo "=== CHAOS QDRANT LOGGING CHECK ==="
echo "token=${TOKEN} lookback=${LOOKBACK_MIN}m"

fail() {
  echo
  echo " FAIL: $1"
  exit 1
}

pass() {
  echo " pass $1"
}

CH_POD="$(kubectl -n "${CH_NS}" get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -z "${CH_POD}" ] && CH_POD="$(kubectl -n "${CH_NS}" get pods --no-headers | awk '/clickhouse|ch-single/ {print $1; exit}')"
[ -z "${CH_POD}" ] && fail "ClickHouse pod not found in ${CH_NS}"

Q_POD="$(kubectl -n "${Q_NS}" get pods -l app=qdrant -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
[ -z "${Q_POD}" ] && fail "Qdrant pod not found"

echo "clickhouse pod: ${CH_POD}"
echo "qdrant pod: ${Q_POD}"

echo
echo "=== STEP 1: generate qdrant traffic with unique token ==="

kubectl run qdrant-chaos-curl \
  -n "${Q_NS}" \
  --rm -i --restart=Never \
  --image=curlimages/curl:8.5.0 \
  -- \
  curl -sS "http://qdrant.${Q_NS}.svc.cluster.local:6333/health?e2e=${TOKEN}" >/dev/null \
  || fail "Failed to issue curl to qdrant"

pass "qdrant probe sent"

echo
echo "=== STEP 2: wait for Vector + ClickHouse ==="
sleep 8

echo
echo "=== STEP 3: verify log row exists ==="

COUNT="$(
kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "
clickhouse-client --query \"
SELECT count()
FROM logs.kube_logs
WHERE namespace='${Q_NS}'
  AND message LIKE '%${TOKEN}%'
  AND ts >= now() - toIntervalMinute(${LOOKBACK_MIN})
\"
" 2>/dev/null | tr -d '[:space:]'
)"

[ "${COUNT:-0}" = "0" ] && fail "No qdrant log row containing token found in ClickHouse"

pass "log row found (${COUNT})"

echo
echo "=== STEP 4: verify raw column exists and populated ==="

RAW_COUNT="$(
kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "
clickhouse-client --query \"
SELECT count()
FROM logs.kube_logs
WHERE namespace='${Q_NS}'
  AND message LIKE '%${TOKEN}%'
  AND raw != ''
  AND ts >= now() - toIntervalMinute(${LOOKBACK_MIN})
\"
" 2>/dev/null | tr -d '[:space:]'
)"

[ "${RAW_COUNT:-0}" = "0" ] && fail "raw column missing or empty for qdrant logs"

pass "raw column populated"

echo
echo "=== STEP 5: verify trace columns exist (may be empty) ==="

kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "
clickhouse-client --query \"
SELECT
  name,
  type
FROM system.columns
WHERE database='logs'
  AND table='kube_logs'
  AND name IN ('trace_id','span_id')
FORMAT PrettyCompact
\"
" >/tmp/qdrant_trace_cols.txt 2>/dev/null

grep -q trace_id /tmp/qdrant_trace_cols.txt || fail "trace_id column missing"
grep -q span_id /tmp/qdrant_trace_cols.txt || fail "span_id column missing"

pass "trace_id / span_id columns exist"

echo
echo "=== STEP 6: show final sample row ==="

kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "
clickhouse-client --query \"
SELECT ts, pod, message, raw, trace_id, span_id
FROM logs.kube_logs
WHERE namespace='${Q_NS}'
  AND message LIKE '%${TOKEN}%'
ORDER BY ts DESC
LIMIT 1
FORMAT PrettyCompact
\"
" || true

echo
echo " QDRANT LOGGING E2E VERIFIED"
