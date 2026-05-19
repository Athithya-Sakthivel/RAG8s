#!/usr/bin/env bash
# src/scripts/ch_query.sh
# Helper script to query logs.inference_logs in ClickHouse.
# Adds --levels support (comma-separated: info,warn,err,debug,critical,trace).
set -euo pipefail

CH_NS="${CH_NS:-logging}"
CH_DB="${CH_DB:-logs}"
CH_TABLE="${CH_TABLE:-inference_logs}"

SERVICE=""
LASTM=""
FROM_OFFSET=""
TO_OFFSET=""
LIMIT=""
FORMAT="PrettyCompact"
STRICT="false"
LEVELS=""

usage() {
  cat <<USAGE
Usage: $0 --service=<service> [--lastM=<minutes> | --from=<offset> --to=<offset>] [--limit=<N>] [--format=<PrettyCompact|TSV|JSONEachRow>] [--levels=<csv>] [--strict]
  --service: required. e.g. retrieval, frontend, qdrant, or any service name (if 'service' column exists).
             Special-case: qdrant -> queries namespace='qdrant'.
  Time selection (choose one):
    --lastM=<minutes>          legacy convenience (sets --from=<lastM>M --to=0M)
    OR
    --from=<offset> --to=<offset>
      offset format: <N><unit> where unit is s|S|m|M|h|H|d|D (seconds, minutes, hours, days).
      Example: --from=30M --to=0M  (last 30 minutes)
  --limit=<N>                 optional row limit (positive integer)
  --format=PrettyCompact|TSV|JSONEachRow  output format (default PrettyCompact)
  --levels=<csv>              optional comma-separated log levels (info,warn,err,debug,critical,trace)
  --strict                    exit non-zero when matched row count == 0 (useful for CI)
  -h|--help                   this message
Examples:
  $0 --service=retrieval --lastM=10
  $0 --service=frontend --from=30M --to=0M --limit=200 --format=JSONEachRow --levels=error,warn
  $0 --service=qdrant --from=20M --to=10M --levels=info
USAGE
  exit 2
}

# parse args
for arg in "$@"; do
  case "$arg" in
    --service=*) SERVICE="${arg#*=}" ;;
    --lastM=*) LASTM="${arg#*=}" ;;
    --from=*) FROM_OFFSET="${arg#*=}" ;;
    --to=*) TO_OFFSET="${arg#*=}" ;;
    --limit=*) LIMIT="${arg#*=}" ;;
    --format=*) FORMAT="${arg#*=}" ;;
    --levels=*) LEVELS="${arg#*=}" ;;
    --strict) STRICT="true" ;;
    -h|--help) usage ;;
    *) echo "[error] unknown arg: $arg" >&2; usage ;;
  esac
done

if [ -z "${SERVICE}" ]; then
  echo "[error] --service is required" >&2
  usage
fi

# translate lastM to from/to
if [ -n "${LASTM}" ]; then
  if ! printf '%s' "${LASTM}" | grep -Eq '^[0-9]+$'; then
    echo "[error] --lastM must be integer minutes" >&2
    exit 3
  fi
  FROM_OFFSET="${LASTM}M"
  TO_OFFSET="0M"
fi

if [ -z "${FROM_OFFSET}" ] || [ -z "${TO_OFFSET}" ]; then
  echo "[error] either --lastM or both --from and --to must be provided" >&2
  usage
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[error] kubectl not found" >&2
  exit 4
fi

# helpers
find_clickhouse_pod() {
  local pod
  pod="$(kubectl -n "${CH_NS}" get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -z "${pod}" ]; then
    pod="$(kubectl -n "${CH_NS}" get pods --no-headers 2>/dev/null | awk '/clickhouse|ch-single/ {print $1; exit}')"
  fi
  printf '%s' "${pod:-}"
}

check_clickhouse_ready() {
  local pod="$1"; local timeout="${2:-15}"
  local start now
  start=$(date +%s)
  while true; do
    if kubectl -n "${CH_NS}" exec -i "${pod}" -- bash -lc "clickhouse-client --query 'SELECT 1' >/dev/null 2>&1"; then
      return 0
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "${timeout}" ]; then
      return 1
    fi
    sleep 1
  done
}

offset_to_interval_func() {
  local off="$1"
  off="$(printf '%s' "$off" | tr '[:upper:]' '[:lower:]')"
  if ! printf '%s' "$off" | grep -Eq '^[0-9]+[smhd]$'; then
    echo "[error] invalid offset format: ${off}. Use e.g. 10M, 30m, 1H, 45s" >&2
    return 2
  fi
  local num unit
  num="$(printf '%s' "$off" | sed -E 's/([0-9]+).*/\1/')"
  unit="$(printf '%s' "$off" | sed -E 's/[0-9]+([smhd]).*/\1/')"
  case "${unit}" in
    s) printf 'toIntervalSecond(%s)' "${num}" ;;
    m) printf 'toIntervalMinute(%s)' "${num}" ;;
    h) printf 'toIntervalHour(%s)' "${num}" ;;
    d) printf 'toIntervalDay(%s)' "${num}" ;;
    *) echo "[error] unknown unit in offset: ${unit}" >&2; return 2 ;;
  esac
}

offset_to_seconds() {
  local off="$1"
  off="$(printf '%s' "$off" | tr '[:upper:]' '[:lower:]')"
  local num unit
  num="$(printf '%s' "$off" | sed -E 's/([0-9]+).*/\1/')"
  unit="$(printf '%s' "$off" | sed -E 's/[0-9]+([smhd]).*/\1/')"
  case "${unit}" in
    s) echo "${num}" ;;
    m) echo $((num * 60)) ;;
    h) echo $((num * 3600)) ;;
    d) echo $((num * 86400)) ;;
    *) echo "0" ;;
  esac
}

# locate clickhouse pod
CH_POD="$(find_clickhouse_pod)"
if [ -z "${CH_POD}" ]; then
  echo "[error] No ClickHouse pod found in namespace ${CH_NS}" >&2
  kubectl -n "${CH_NS}" get pods || true
  exit 8
fi
if ! check_clickhouse_ready "${CH_POD}" 15; then
  echo "[error] ClickHouse not responding in ${CH_NS}/${CH_POD}" >&2
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
  exit 9
fi

# discover columns
COLUMNS_RAW="$(kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "clickhouse-client --query \"SELECT name FROM system.columns WHERE database='${CH_DB}' AND table='${CH_TABLE}' ORDER BY position FORMAT TSV\" 2>/dev/null || true")"
column_exists() {
  local col="$1"
  printf '%s\n' "${COLUMNS_RAW}" | awk -v c="$col" 'BEGIN{found=0} {if($0==c) found=1} END{exit !found}'
}

# choose ts column
TS_COL=""
if column_exists "ts"; then
  TS_COL="ts"
elif column_exists "_time"; then
  TS_COL="_time"
elif column_exists "timestamp"; then
  TS_COL="timestamp"
elif column_exists "time"; then
  TS_COL="time"
else
  echo "[warn] no conventional timestamp column discovered; using 'ts' in query (may fail)" >&2
  TS_COL="ts"
fi

# For non-qdrant services, ensure service column exists
if [ "${SERVICE}" != "qdrant" ]; then
  if ! column_exists "service"; then
    echo "[error] table ${CH_DB}.${CH_TABLE} lacks 'service' column; cannot filter by service. Columns:" >&2
    printf '%s\n' "${COLUMNS_RAW}" >&2
    exit 10
  fi
fi

# validate format
case "${FORMAT}" in
  PrettyCompact|TSV|JSONEachRow) ;;
  *) echo "[error] unsupported format: ${FORMAT}" >&2; exit 11 ;;
esac

# validate limit
if [ -n "${LIMIT}" ]; then
  if ! printf '%s' "${LIMIT}" | grep -Eq '^[0-9]+$'; then
    echo "[error] --limit must be a positive integer" >&2
    exit 12
  fi
fi

# build interval functions and numeric check
FROM_FUNC="$(offset_to_interval_func "${FROM_OFFSET}")" || exit 2
TO_FUNC="$(offset_to_interval_func "${TO_OFFSET}")" || exit 2
FROM_SEC="$(offset_to_seconds "${FROM_OFFSET}")"
TO_SEC="$(offset_to_seconds "${TO_OFFSET}")"
if [ "${FROM_SEC}" -lt "${TO_SEC}" ]; then
  echo "[error] invalid window: --from (${FROM_OFFSET}, older) must be >= --to (${TO_OFFSET}, newer)" >&2
  exit 13
fi

# compose service predicate
if [ "${SERVICE}" = "qdrant" ]; then
  SERVICE_PREDICATE="namespace = 'qdrant'"
else
  if ! printf '%s' "${SERVICE}" | grep -Eq '^[A-Za-z0-9._-]+$'; then
    echo "[error] invalid service name: ${SERVICE}" >&2
    exit 14
  fi
  SERVICE_PREDICATE="service = '${SERVICE}'"
fi

TIME_PREDICATE="${TS_COL} >= now() - ${FROM_FUNC} AND ${TS_COL} <= now() - ${TO_FUNC}"

# Handle levels (backward-compatible): only apply if user provided and 'level' column exists
LEVEL_PREDICATE=""
if [ -n "${LEVELS}" ]; then
  if column_exists "level"; then
    # split csv, normalize tokens -> map to ClickHouse stored values
    IFS=',' read -r -a LV_ARR <<< "${LEVELS}"
    mapped=""
    for lv in "${LV_ARR[@]}"; do
      token="$(printf '%s' "$lv" | tr '[:upper:]' '[:lower:]' | xargs)"
      case "${token}" in
        info) mapped="${mapped}INFO," ;;
        warn|warning) mapped="${mapped}WARN," ;;
        err|error) mapped="${mapped}ERROR," ;;
        debug) mapped="${mapped}DEBUG," ;;
        critical|crit|fatal) mapped="${mapped}CRITICAL," ;;
        trace) mapped="${mapped}TRACE," ;;
        *) echo "[warn] unknown level token: ${lv} (ignored)" >&2 ;;
      esac
    done
    # dedupe & build IN-list
    if [ -n "${mapped}" ]; then
      # remove trailing comma, dedupe preserving order
      mapped="$(printf '%s' "${mapped}" | awk -F, '{ for(i=1;i<=NF;i++) if($i!="") if(!seen[$i]++){ out=(out==""?$i:out","$i) } } END{print out}')"
      # build SQL fragment
      # protect by single quotes
      IFS=',' read -r -a M_ARR <<< "${mapped}"
      vals_sql=""
      for v in "${M_ARR[@]}"; do
        vals_sql="${vals_sql}'${v}',"
      done
      vals_sql="${vals_sql%,}"
      LEVEL_PREDICATE="AND level IN (${vals_sql})"
    else
      echo "[warn] no valid level tokens parsed from --levels; continuing without level filter" >&2
    fi
  else
    echo "[warn] requested --levels but table ${CH_DB}.${CH_TABLE} has no 'level' column; ignoring levels filter" >&2
  fi
fi

LIMIT_CLAUSE=""
if [ -n "${LIMIT}" ]; then LIMIT_CLAUSE="LIMIT ${LIMIT}"; fi

# count first for diagnostics (include level predicate if set)
COUNT_SQL="SELECT count() FROM ${CH_DB}.${CH_TABLE} WHERE ${SERVICE_PREDICATE} AND ${TIME_PREDICATE} ${LEVEL_PREDICATE}"
echo "[info] count SQL -> ${COUNT_SQL}" >&2
COUNT_OUT="$(printf '%s\n' "${COUNT_SQL}" | kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- clickhouse-client 2>/dev/null || echo "ERR")"
if [ "${COUNT_OUT}" = "ERR" ]; then
  echo "[error] count query failed; dumping system.tables for diagnostics" >&2
  kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- clickhouse-client --query="SELECT database, name, engine FROM system.tables WHERE database='${CH_DB}' ORDER BY name" || true
  exit 15
fi
COUNT="$(printf '%s' "${COUNT_OUT}" | tr -d '[:space:]')"
COUNT="${COUNT:-0}"
echo "[info] matched rows: ${COUNT}" >&2

if [ "${COUNT}" -eq 0 ]; then
  echo "[info] 0 rows matched for service='${SERVICE}' window from=${FROM_OFFSET} to=${TO_OFFSET} levels='${LEVELS}'" >&2
  echo "[info] Detected columns:" >&2
  printf '%s\n' "${COLUMNS_RAW}" >&2
  if [ "${STRICT}" = "true" ]; then
    echo "[error] strict mode enabled and no rows found -> exiting non-zero" >&2
    exit 16
  fi
fi

# Build SELECT with graceful handling of missing columns (level/service)
SELECT_LIST="${TS_COL} AS ts,"
if column_exists "level"; then SELECT_LIST="${SELECT_LIST} level,"; else SELECT_LIST="${SELECT_LIST} '' AS level,"; fi
if column_exists "service"; then SELECT_LIST="${SELECT_LIST} service,"; else SELECT_LIST="${SELECT_LIST} '' AS service,"; fi
SELECT_LIST="${SELECT_LIST} message, namespace, pod"

Q="SELECT ${SELECT_LIST} FROM ${CH_DB}.${CH_TABLE} WHERE ${SERVICE_PREDICATE} AND ${TIME_PREDICATE} ${LEVEL_PREDICATE} ORDER BY ${TS_COL} DESC ${LIMIT_CLAUSE} FORMAT ${FORMAT};"

# Print SQL for audit
echo "[info] executing SQL:" >&2
echo "-----" >&2
echo "${Q}" >&2
echo "-----" >&2

# execute: pipe SQL into clickhouse-client inside pod
if ! printf '%s\n' "${Q}" | kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- clickhouse-client --format=${FORMAT}; then
  echo "[error] final query failed; dumping diagnostics" >&2
  kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- clickhouse-client --query="SELECT database, name, engine FROM system.tables WHERE database='${CH_DB}' ORDER BY name" || true
  exit 17
fi

exit 0