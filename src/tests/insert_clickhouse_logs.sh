#!/usr/bin/env bash
# scripts/insert_clickhouse_logs.sh
# Non-interactive script to create logs.kube_logs (if missing) and insert realistic synthetic rows.
# Usage:
#   NAMESPACE=observability POD=ch-single-0 ROWS=200 ./scripts/insert_clickhouse_logs.sh

make fix-dns 

NAMESPACE="${NAMESPACE:-observability}"
POD="${POD:-ch-single-0}"
DB="${DB:-logs}"
TABLE="${TABLE:-kube_logs}"
ROWS="${ROWS:-200}"   # number of synthetic rows to insert (numbers(ROWS) will be used)

KC="$(command -v kubectl || true)"
if [ -z "$KC" ]; then
  echo "kubectl not found in PATH; install kubectl or run this from a machine with kubectl configured." >&2
  exit 2
fi

# quick existence check for pod
if ! $KC -n "$NAMESPACE" get pod "$POD" >/dev/null 2>&1; then
  echo "Pod ${POD} not found in namespace ${NAMESPACE}" >&2
  echo "Available pods in ${NAMESPACE}:" >&2
  $KC -n "$NAMESPACE" get pods --no-headers -o custom-columns=NAME:.metadata.name || true
  exit 3
fi

# Build SQL payload (non-quoted heredoc to allow ROWS expansion)
read -r -d '' SQL_PAYLOAD <<SQL || true
CREATE DATABASE IF NOT EXISTS ${DB};

CREATE TABLE IF NOT EXISTS ${DB}.${TABLE}
(
  ts DateTime64(3),
  service String,
  pod String,
  namespace String,
  message String,
  fields String,
  level String,
  container String,
  trace_id String,
  span_id String
)
ENGINE = MergeTree()
ORDER BY (ts);

INSERT INTO ${DB}.${TABLE} (ts, service, pod, namespace, message, fields, level, container, trace_id, span_id)
SELECT
  now64(3) AS ts,
  arrayElement(['retriever','qdrant','api','worker'], toUInt64(n % 4) + 1) AS service,
  concat(service, '-', toString(toUInt64(n % 8))) AS pod,
  arrayElement(['monitoring','inference','qdrant','default'], toUInt64(n % 4) + 1) AS namespace,
  concat('simulated log message #', toString(n), ' (for ', service, ')') AS message,
  '{}' AS fields,
  arrayElement(['INFO','WARN','ERROR','DEBUG'], toUInt64(n % 4) + 1) AS level,
  arrayElement(['app','qdrant','sidecar','proxy'], toUInt64(n % 4) + 1) AS container,
  toString(rand64()) AS trace_id,
  toString(rand64()) AS span_id
FROM numbers(${ROWS}) AS n;
SQL

# Run the SQL inside the ClickHouse pod non-interactively
echo "Applying ${ROWS} synthetic rows into ${DB}.${TABLE} on pod ${POD} (ns: ${NAMESPACE})..."
# Using --multiquery so CREATE + INSERT run in one shot; use -n to read from stdin
$KC -n "$NAMESPACE" exec -i "$POD" -- clickhouse-client --multiquery --database="${DB}" <<< "$SQL_PAYLOAD"

# Quick verification: show counts by service/namespace
echo
echo "Verification: counts by service / namespace (from ClickHouse)"
$KC -n "$NAMESPACE" exec -i "$POD" -- clickhouse-client --query "SELECT count() AS cnt, service, namespace FROM ${DB}.${TABLE} GROUP BY service, namespace ORDER BY cnt DESC"

echo
echo "Done."


NAMESPACE="${NAMESPACE:-observability}"
POD="${POD:-ch-single-0}"
DB="${DB:-logs}"
TABLE="${TABLE:-kube_logs}"
ROWS="${ROWS:-200}"   # number of synthetic rows to insert (numbers(ROWS) will be used)

KC="$(command -v kubectl || true)"
if [ -z "$KC" ]; then
  echo "kubectl not found in PATH; install kubectl or run this from a machine with kubectl configured." >&2
  exit 2
fi

# quick existence check for pod
if ! $KC -n "$NAMESPACE" get pod "$POD" >/dev/null 2>&1; then
  echo "Pod ${POD} not found in namespace ${NAMESPACE}" >&2
  echo "Available pods in ${NAMESPACE}:" >&2
  $KC -n "$NAMESPACE" get pods --no-headers -o custom-columns=NAME:.metadata.name || true
  exit 3
fi

# Build SQL payload (non-quoted heredoc to allow ROWS expansion)
read -r -d '' SQL_PAYLOAD <<SQL || true
CREATE DATABASE IF NOT EXISTS ${DB};

CREATE TABLE IF NOT EXISTS ${DB}.${TABLE}
(
  ts DateTime64(3),
  service String,
  pod String,
  namespace String,
  message String,
  fields String,
  level String,
  container String,
  trace_id String,
  span_id String
)
ENGINE = MergeTree()
ORDER BY (ts);

INSERT INTO ${DB}.${TABLE} (ts, service, pod, namespace, message, fields, level, container, trace_id, span_id)
SELECT
  now64(3) AS ts,
  arrayElement(['retriever','qdrant','api','worker'], toUInt64(n % 4) + 1) AS service,
  concat(service, '-', toString(toUInt64(n % 8))) AS pod,
  arrayElement(['monitoring','inference','qdrant','default'], toUInt64(n % 4) + 1) AS namespace,
  concat('simulated log message #', toString(n), ' (for ', service, ')') AS message,
  '{}' AS fields,
  arrayElement(['INFO','WARN','ERROR','DEBUG'], toUInt64(n % 4) + 1) AS level,
  arrayElement(['app','qdrant','sidecar','proxy'], toUInt64(n % 4) + 1) AS container,
  toString(rand64()) AS trace_id,
  toString(rand64()) AS span_id
FROM numbers(${ROWS}) AS n;
SQL

# Run the SQL inside the ClickHouse pod non-interactively
echo "Applying ${ROWS} synthetic rows into ${DB}.${TABLE} on pod ${POD} (ns: ${NAMESPACE})..."
# Using --multiquery so CREATE + INSERT run in one shot; use -n to read from stdin
$KC -n "$NAMESPACE" exec -i "$POD" -- clickhouse-client --multiquery --database="${DB}" <<< "$SQL_PAYLOAD"

# Quick verification: show counts by service/namespace
echo
echo "Verification: counts by service / namespace (from ClickHouse)"
$KC -n "$NAMESPACE" exec -i "$POD" -- clickhouse-client --query "SELECT count() AS cnt, service, namespace FROM ${DB}.${TABLE} GROUP BY service, namespace ORDER BY cnt DESC"

echo
echo "Done."