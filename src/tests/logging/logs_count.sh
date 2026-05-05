#!/usr/bin/env bash
# Diagnostic block: inspect logs.kube_logs schema & check frontend/retrieval entries.
# Designed to be safe (no set -e) and to truncate large outputs.
make fix-dns

CH_NS=observability
CH_POD="$(kubectl -n "${CH_NS}" get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
echo "=== ClickHouse pod ==="
echo "$CH_POD"
echo

if [ -z "${CH_POD}" ]; then
  echo "[ERROR] no ClickHouse pod found in ${CH_NS}"
  exit 1
fi

run_q() {
  sql="$1"
  kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "clickhouse-client --query \"$sql\"" 2>&1 | sed -n '1,200p'
}

echo "=== DESCRIBE TABLE logs.kube_logs (first 200 lines) ==="
run_q "DESCRIBE TABLE logs.kube_logs"
echo
echo "=== SHOW CREATE TABLE logs.kube_logs (first 200 lines) ==="
run_q "SHOW CREATE TABLE logs.kube_logs"
echo

echo "=== Which columns and types in system.columns for logs.kube_logs ==="
run_q "SELECT name, type FROM system.columns WHERE database='logs' AND table='kube_logs' ORDER BY name" 
echo

echo "=== Top services by rowcount (helpful to find service names) ==="
run_q "SELECT service, count() AS c FROM logs.kube_logs GROUP BY service ORDER BY c DESC LIMIT 20"
echo

echo "=== Sample raw rows (LIMIT 10) - truncated ==="
run_q "SELECT * FROM logs.kube_logs LIMIT 10"
echo

# Use a token if you want to search for a run-specific marker (you can override TOKEN env var)
TOKEN="${TOKEN:-frontend-e2e-$(date +%s)}"
echo "=== Using TOKEN (for text search): ${TOKEN} ==="
echo "Count rows matching token in message:"
run_q "SELECT count() FROM logs.kube_logs WHERE message LIKE '%${TOKEN}%'"
echo

echo "=== Try extracting app timestamp & env via JSONExtractString (works when fields is JSON string) ==="
run_q "SELECT
  JSONExtractString(fields, 'timestamp') AS app_ts_json,
  JSONExtractString(fields, 'env') AS env_json,
  service,
  level,
  message
FROM logs.kube_logs
WHERE service = 'frontend' OR message LIKE '%${TOKEN}%'
ORDER BY app_ts_json DESC
LIMIT 20"
echo

echo "=== Try extracting via Map-index (works when fields is Map type) ==="
run_q "SELECT
  fields['timestamp'] AS app_ts_map,
  fields['env'] AS env_map,
  service,
  level,
  message
FROM logs.kube_logs
WHERE service = 'frontend' OR message LIKE '%${TOKEN}%'
LIMIT 20"
echo

echo "=== Parse app_ts (best-effort) and show newest frontend rows ==="
run_q "SELECT
  parseDateTimeBestEffort(JSONExtractString(fields,'timestamp')) AS app_time,
  JSONExtractString(fields,'env') AS env,
  service,
  level,
  message
FROM logs.kube_logs
WHERE service = 'frontend'
ORDER BY app_time DESC
LIMIT 20"
echo

echo "=== Finally: counts for frontend & retrieval in last 60 minutes (two approaches) ==="
run_q "SELECT
  service,
  count() AS c
FROM logs.kube_logs
WHERE (service IN ('frontend','retrieval')) AND toDate(now()) = toDate(now())
GROUP BY service ORDER BY c DESC"
echo
run_q "SELECT
  JSONExtractString(fields,'service') AS fields_service,
  count() AS c
FROM logs.kube_logs
WHERE JSONExtractString(fields,'service') IN ('frontend','retrieval')
GROUP BY fields_service ORDER BY c DESC"
echo

echo "=== END of diagnostic (outputs truncated). ==="
