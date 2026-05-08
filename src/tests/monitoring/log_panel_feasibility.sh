# Diagnostic block — paste as one whole chunk (no exit/set -e terminators)
make fix-dns
clear
PROV_NS="${GRAFANA_PROVISIONING_NAMESPACE:-monitoring}"
KUBE_NS="${KUBE_NAMESPACE:-monitoring}"
OUT_DIR="infra/manifests/dashboards"
RET_FILE="$OUT_DIR/service-retriever.json"
QDR_FILE="$OUT_DIR/service-qdrant.json"

echo "=== CONTEXT ==="
echo "PROVISIONING_NS=$PROV_NS  KUBE_NS=$KUBE_NS  OUT_DIR=$OUT_DIR"
echo

# candidate ClickHouse HTTP endpoints (try common service FQDNs used previously)
CH_CANDIDATES=(
  "http://clickhouse.clickhouse.svc:8123"
  "http://clickhouse.clickhouse.svc.cluster.local:8123"
  "http://clickhouse.observability.svc:8123"
  "http://clickhouse.observability.svc.cluster.local:8123"
  "http://clickhouse.monitoring.svc:8123"
  "http://clickhouse:8123"
)

# prepare sample log rows (use UTC timestamp with ms)
NOW_MS=$(date -u +"%Y-%m-%d %H:%M:%S.%3N")
NOW2_MS=$(date -u -d "1 second" +"%Y-%m-%d %H:%M:%S.%3N")

# JSONEachRow payload lines — fields column kept as a JSON string (common pattern)
read -r -d '' PAYLOAD <<'EOF' || true
{"ts":"'"${NOW_MS}"'","level":"INFO","message":"retriever: fetched 12 docs for q=vector-sim test","fields":"{\"pod\":\"retriever-0\",\"service\":\"retriever\",\"request_id\":\"req-abc-123\",\"docs\":12}"}
{"ts":"'"${NOW2_MS}"'","level":"ERROR","message":"retriever: failed to fetch from upstream","fields":"{\"pod\":\"retriever-0\",\"service\":\"retriever\",\"request_id\":\"req-abc-124\",\"error\":\"timeout\"}"}
{"ts":"'"${NOW_MS}"'","level":"INFO","message":"qdrant: index update complete, added 100 vectors","fields":"{\"pod\":\"qdrant-0\",\"service\":\"qdrant\",\"collection\":\"users\",\"added\":100}"}
{"ts":"'"${NOW2_MS}"'","level":"WARN","message":"qdrant: slow query detected (500ms)","fields":"{\"pod\":\"qdrant-0\",\"service\":\"qdrant\",\"query\":\"knn\",\"lat_ms\":500}"}
EOF

echo "=== PAYLOAD (first 2 lines preview) ==="
echo "$PAYLOAD" | sed -n '1,2p'
echo

try_insert_and_verify() {
  local churl="$1"
  echo ">> Trying ClickHouse endpoint: $churl"

  # INSERT using JSONEachRow
  kubectl -n "$PROV_NS" run --rm -i insert-ch --image=curlimages/curl --restart=Never --command -- \
    sh -c "printf '%s\n' \"$PAYLOAD\" | curl -sS -X POST \"$churl/?query=INSERT%20INTO%20logs.kube_logs%20(ts,level,message,fields)%20FORMAT%20JSONEachRow\" --data-binary @- -w '\nHTTP_STATUS:%{http_code}\n' || true" 2>&1 | sed -n '1,120p'

  # short sleep to let ClickHouse persist
  sleep 1

  # Verify SELECT for retriever (limit 5) and qdrant
  echo "---- SELECT retriever (recent) ----"
  kubectl -n "$PROV_NS" run --rm -i select-ch-ret --image=curlimages/curl --restart=Never --command -- \
    sh -c "curl -sS -G --data-urlencode \"query=SELECT ts, level, message, fields FROM logs.kube_logs WHERE service='retriever' ORDER BY ts DESC LIMIT 5 FORMAT JSONCompact\" \"$churl\" || echo '__SELECT_RETR_FAILED__'" 2>&1 | sed -n '1,40p'

  echo "---- SELECT qdrant (recent) ----"
  kubectl -n "$PROV_NS" run --rm -i select-ch-qd --image=curlimages/curl --restart=Never --command -- \
    sh -c "curl -sS -G --data-urlencode \"query=SELECT ts, level, message, fields FROM logs.kube_logs WHERE service='qdrant' ORDER BY ts DESC LIMIT 5 FORMAT JSONCompact\" \"$churl\" || echo '__SELECT_QDR_FAILED__'" 2>&1 | sed -n '1,40p'
  echo
}

# Iterate candidates until one succeeds at least for DNS/connectivity or until all tried
ANY_OK=0
for CH in "${CH_CANDIDATES[@]}"; do
  # quick DNS/connect test by resolving host inside cluster via ephemeral pod using nslookup if available, fallback to curl
  HOST_ONLY=$(echo "$CH" | sed -E 's|https?://||; s|:[0-9]+||')
  echo "-> test name resolution/connectivity to $HOST_ONLY (from ephemeral pod in ns=$PROV_NS)"
  kubectl -n "$PROV_NS" run --rm -i nettest --image=busybox --restart=Never --command -- sh -c "nslookup $HOST_ONLY 2>&1 || echo 'NSLOOKUP_FAIL'" 2>&1 | sed -n '1,10p'
  echo "-> attempt HTTP HEAD to $CH (quick check)"
  kubectl -n "$PROV_NS" run --rm -i headtest --image=curlimages/curl --restart=Never --command -- sh -c "curl -sS -I --max-time 4 '$CH' -w '\nHTTP_STATUS:%{http_code}\n' || echo '__HEAD_FAILED__'" 2>&1 | sed -n '1,8p'
  echo

  # Try insert+select (this will show HTTP error or select results)
  try_insert_and_verify "$CH"

  # Heuristic: user-inspect output for "HTTP_STATUS:200" or valid SELECT JSON. We'll also grep for common failures.
  # Collect last few lines to detect success strings
  OUT=$(kubectl -n "$PROV_NS" run --rm -i check-echo --image=curlimages/curl --restart=Never --command -- sh -c "echo 'probe' && true" 2>/dev/null || true)
  # No programmatic determination here; operator should inspect the printed output above.
done

echo "=== LOCAL DASHBOARD EXPLORE LINKS (from generated JSON) ==="
for f in "$RET_FILE" "$QDR_FILE"; do
  if [ -f "$f" ]; then
    echo "-> $f: show 'Logs' panel and first Explore link (if any)"
    jq -r '.panels[] | select(.title=="Logs") | (.links // [] | .[].url // "NO-LINK")' "$f" 2>/dev/null | sed -n '1,2p' || echo "  (no logs panel link found or jq failed)"
  else
    echo "-> $f: missing"
  fi
done

echo
echo "=== NEXT STEPS / WHAT TO LOOK FOR ==="
echo "1) For a successful insertion you should see HTTP 200 (or 204) from the INSERT POST and the subsequent SELECT returning the JSON rows you inserted."
echo "2) If DNS fails (nslookup / curl HEAD show errors), try inserting to the ClickHouse pod IP or correct FQDN used in your cluster."
echo "3) If INSERT succeeds but Grafana/Explore doesn't surface rows: confirm the Grafana ClickHouse datasource URL matches the CH endpoint used above and that Grafana has permission to query it."
echo
echo "END OF DIAGNOSTIC BLOCK."
