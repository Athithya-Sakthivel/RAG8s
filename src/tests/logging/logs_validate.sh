#!/usr/bin/env bash
# validate-observability.sh — Complete observability pipeline health check
# Dumps all context about Vector, ClickHouse, schema, and log pipeline status.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

NS_LOGGING="logging"
NS_MONITORING="monitoring"

divider() { echo -e "\n${BLUE}═══════════════════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}═══════════════════════════════════════════════════════════════${NC}"; }

# ────────────────────────────────────────────────────────────────
divider "1. POD STATUS"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- Logging namespace ---"
kubectl get pods -n "$NS_LOGGING" -o wide
echo ""
echo "--- Monitoring namespace ---"
kubectl get pods -n "$NS_MONITORING" -o wide

# ────────────────────────────────────────────────────────────────
divider "2. CLICKHOUSE SCHEMA & STORAGE"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- Table Schema ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  DESCRIBE TABLE logs.inference_logs
"

echo ""
echo "--- Table Engine & Settings ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT
    name,
    engine,
    partition_key,
    sorting_key,
    primary_key
  FROM system.tables
  WHERE database = 'logs' AND name = 'inference_logs'
"

echo ""
echo "--- Table Size ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT
    total_rows,
    formatReadableSize(total_bytes) AS size,
    total_parts
  FROM system.tables
  WHERE database = 'logs' AND name = 'inference_logs'
"

echo ""
echo "--- Active Partitions ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT
    partition,
    name,
    rows,
    formatReadableSize(bytes_on_disk) AS size,
    modification_time
  FROM system.parts
  WHERE database = 'logs' AND table = 'inference_logs' AND active
  ORDER BY modification_time DESC
  LIMIT 10
"

# ────────────────────────────────────────────────────────────────
divider "3. LOG DATA DISTRIBUTION"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- Total Logs ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT count() AS total_logs FROM logs.inference_logs
"

echo ""
echo "--- Logs Per Service ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT
    CASE WHEN service = '' THEN '(empty)' ELSE service END AS service_name,
    count() AS cnt
  FROM logs.inference_logs
  GROUP BY service
  ORDER BY cnt DESC
  LIMIT 10
"

echo ""
echo "--- Logs Per Level ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT level, count() AS cnt
  FROM logs.inference_logs
  GROUP BY level
  ORDER BY cnt DESC
"

echo ""
echo "--- Logs Per Pod ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT pod, service, count() AS cnt
  FROM logs.inference_logs
  WHERE pod != ''
  GROUP BY pod, service
  ORDER BY cnt DESC
  LIMIT 10
"

echo ""
echo "--- Logs Per Hour (Last 24h) ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT
    toStartOfHour(ts) AS hour,
    service,
    count() AS cnt
  FROM logs.inference_logs
  WHERE ts > now() - INTERVAL 24 HOUR
  GROUP BY hour, service
  ORDER BY hour DESC, service
  LIMIT 30
"

echo ""
echo "--- Latest 10 Logs ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT ts, level, service, pod, substring(message, 1, 120) AS message_preview
  FROM logs.inference_logs
  ORDER BY ts DESC
  LIMIT 10
"

echo ""
echo "--- Log Ingestion Rate (Last 5 min) ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT count() AS logs_last_5min
  FROM logs.inference_logs
  WHERE ts > now() - INTERVAL 5 MINUTE
"

echo ""
echo "--- Latest Log Per Service ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT service, max(ts) AS last_seen
  FROM logs.inference_logs
  GROUP BY service
  ORDER BY last_seen DESC
"

# ────────────────────────────────────────────────────────────────
divider "4. SAMPLE LOG ENTRIES (with JSON fields)"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- Retriever logs with fields ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT ts, level, message, fields
  FROM logs.inference_logs
  WHERE service = 'retriever' AND fields != ''
  ORDER BY ts DESC
  LIMIT 3
" --format Vertical

echo ""
echo "--- Frontend logs with fields ---"
kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "
  SELECT ts, level, message, fields
  FROM logs.inference_logs
  WHERE service = 'frontend' AND fields != ''
  ORDER BY ts DESC
  LIMIT 3
" --format Vertical

# ────────────────────────────────────────────────────────────────
divider "5. VECTOR DAEMONSET STATUS"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- DaemonSet ---"
kubectl get daemonset vector -n "$NS_LOGGING" -o wide

echo ""
echo "--- Pod Events ---"
kubectl describe pod -n "$NS_LOGGING" -l app=vector | tail -30

echo ""
echo "--- Vector Logs (last 30 lines, errors only) ---"
kubectl logs -n "$NS_LOGGING" -l app=vector --tail=30 2>/dev/null | grep -iE "error|warn|fail" || echo "No errors found"

echo ""
echo "--- Vector Config (source + sink) ---"
kubectl get configmap vector-config -n "$NS_LOGGING" -o jsonpath='{.data.vector\.toml}' | grep -A 8 '\[sources.kube_logs\]'
echo ""
kubectl get configmap vector-config -n "$NS_LOGGING" -o jsonpath='{.data.vector\.toml}' | grep -A 12 '\[sinks.clickhouse\]'

echo ""
echo "--- Vector Security Context ---"
kubectl get daemonset vector -n "$NS_LOGGING" -o jsonpath='{.spec.template.spec.containers[0].securityContext}' | python3 -m json.tool 2>/dev/null || \
kubectl get daemonset vector -n "$NS_LOGGING" -o yaml | grep -A 8 'securityContext'

# ────────────────────────────────────────────────────────────────
divider "6. VECTOR INTERNAL METRICS"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- Port-forwarding Vector API (background) ---"
kubectl port-forward -n "$NS_LOGGING" daemonset/vector 8688:8686 &>/dev/null &
PF_PID=$!
sleep 2

echo ""
echo "--- Vector Component Events ---"
curl -s http://localhost:8688/metrics 2>/dev/null | grep -E '^vector_component_events_in_total|^vector_component_events_out_total|^vector_component_errors_total|^vector_component_discarded_events_total' | grep -v '^#' | sort

echo ""
echo "--- Vector Processed Bytes ---"
curl -s http://localhost:8688/metrics 2>/dev/null | grep 'vector_processed_bytes_total' | grep -v '^#' || echo "No processed bytes metric found"

# Clean up port-forward
kill $PF_PID 2>/dev/null || true
wait $PF_PID 2>/dev/null || true

# ────────────────────────────────────────────────────────────────
divider "7. PROMETHEUS TARGET HEALTH"
# ────────────────────────────────────────────────────────────────
echo ""
echo "--- ClickHouse target ---"
kubectl port-forward -n "$NS_MONITORING" prometheus-server-0 9090:9090 &>/dev/null &
PF_PID=$!
sleep 2
curl -s http://localhost:9090/api/v1/targets 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
for t in d['data']['activeTargets']:
    if t['labels'].get('job') in ('clickhouse','vector'):
        print(f\"  {t['labels']['job']:15s} → {t['health']:4s}  ({t['labels'].get('instance','')})\")
" 2>/dev/null || echo "Could not query Prometheus targets"
kill $PF_PID 2>/dev/null || true
wait $PF_PID 2>/dev/null || true

# ────────────────────────────────────────────────────────────────
divider "8. SUMMARY"
# ────────────────────────────────────────────────────────────────
TOTAL=$(kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "SELECT count() FROM logs.inference_logs" 2>/dev/null || echo "0")
RECENT=$(kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "SELECT count() FROM logs.inference_logs WHERE ts > now() - INTERVAL 5 MINUTE" 2>/dev/null || echo "0")
SIZE=$(kubectl exec -n "$NS_LOGGING" clickhouse-0 -- clickhouse-client --query "SELECT formatReadableSize(total_bytes) FROM system.tables WHERE name='inference_logs'" 2>/dev/null || echo "unknown")

echo ""
echo -e "  ${GREEN}Total logs:${NC}  $TOTAL"
echo -e "  ${GREEN}Recent (5min):${NC} $RECENT"
echo -e "  ${GREEN}Storage:${NC}      $SIZE"
echo ""
echo -e "  Pipeline: Vector → ClickHouse (logs.inference_logs)"
echo -e "  Retention: 30 days (TTL)"
echo -e "  Scraped by Prometheus: clickhouse (:8001), vector (:9598)"
echo ""