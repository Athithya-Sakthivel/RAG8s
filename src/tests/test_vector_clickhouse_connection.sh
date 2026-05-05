#!/usr/bin/env bash

APP_NS="${APP_NS:-models}"
APP_LABEL="${APP_LABEL:-app.kubernetes.io/name=dense}"
TOKEN="${TOKEN:-dense-e2e-$(date +%s)}"
STATE_FILE="${STATE_FILE:-infra/state/clickhouse.json}"
VECTOR_NS="${VECTOR_NS:-observability}"
CH_NS="${CH_NS:-observability}"
CH_SERVICE="${CH_SERVICE:-clickhouse}"
CH_DB="${CH_DB:-logs}"
CH_TABLE="${CH_TABLE:-kube_logs}"
DENSE_WAIT="${DENSE_WAIT:-120}"
VECTOR_WAIT_SECONDS="${VECTOR_WAIT_SECONDS:-20}"
OUT_LINES=200

log() { printf '%s %s\n' "$(date --utc +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [ -f "${STATE_FILE}" ]; then
  read -r CH_NS CH_SERVICE CH_DB CH_TABLE CH_SECRET CH_USER < <(jq -r '[.namespace, .service, .db, .table, .secret, .user] | @tsv' "${STATE_FILE}" 2>/dev/null) || true
  CH_NS="${CH_NS:-${CH_NS}}"
  CH_SERVICE="${CH_SERVICE:-${CH_SERVICE}}"
  CH_DB="${CH_DB:-${CH_DB}}"
  CH_TABLE="${CH_TABLE:-${CH_TABLE}}"
fi

log "test config: APP_NS=${APP_NS} APP_LABEL=${APP_LABEL} VECTOR_NS=${VECTOR_NS} CH_NS=${CH_NS} CH_SERVICE=${CH_SERVICE} TOKEN=${TOKEN}"

if ! command -v kubectl >/dev/null 2>&1; then
  log "[error] kubectl not found"
  exit 2
fi

log "[step 1/7] deploy dense"
make deploy-dense

log "[step 2/7] wait for dense pod ready"
if ! kubectl -n "${APP_NS}" wait pod -l "${APP_LABEL}" --for=condition=Ready --timeout="${DENSE_WAIT}s" >/dev/null 2>&1; then
  log "[error] no ready pods matching ${APP_LABEL} in ${APP_NS}"
  kubectl -n "${APP_NS}" get pods -l "${APP_LABEL}" || true
  exit 3
fi

DENSE_POD="$(kubectl -n "${APP_NS}" get pod -l "${APP_LABEL}" -o jsonpath='{.items[0].metadata.name}')"
DENSE_IP="$(kubectl -n "${APP_NS}" get pod "${DENSE_POD}" -o jsonpath='{.status.podIP}')"
log "dense pod: ${DENSE_POD} (${DENSE_IP})"

log "[step 3/7] generate logs"
for i in 1 2; do
  NAME="curltmp-${i}-${TOKEN}"
  kubectl -n "${APP_NS}" run "${NAME}" --restart=Never --image=curlimages/curl --command -- sh -c "curl -sS --max-time 5 'http://${DENSE_IP}:8200/health?e2e=${TOKEN}&i=${i}' || true"
  kubectl -n "${APP_NS}" wait pod "${NAME}" --for=condition=Succeeded --timeout=15s >/dev/null 2>&1 || true
  kubectl -n "${APP_NS}" delete pod "${NAME}" --ignore-not-found >/dev/null 2>&1 || true
  sleep 1
done

log "[step 4/7] wait ${VECTOR_WAIT_SECONDS}s for Vector to ship logs"
sleep "${VECTOR_WAIT_SECONDS}"

log "[step 5/7] resolve ClickHouse pod (service=${CH_SERVICE}, ns=${CH_NS})"
CH_POD="$(kubectl -n "${CH_NS}" get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -z "${CH_POD}" ]; then
  CH_POD="$(kubectl -n "${CH_NS}" get pods --no-headers | awk '/clickhouse|ch-single/ {print $1; exit}')"
fi
if [ -z "${CH_POD}" ]; then
  log "[error] no clickhouse pod found in ${CH_NS}"
  kubectl -n "${CH_NS}" get pods || true
  exit 6
fi
log "clickhouse pod: ${CH_POD}"

log "[step 6/7] ensure DB and table exist (idempotent)"
SQL="$(cat <<'EOF'
CREATE DATABASE IF NOT EXISTS logs;
CREATE TABLE IF NOT EXISTS logs.kube_logs (
  ts DateTime64(3) DEFAULT now(),
  service String,
  pod String,
  namespace String,
  message String,
  fields String,
  level String,
  container String,
  trace_id String,
  span_id String
) ENGINE = MergeTree() ORDER BY ts;
EOF
)"
echo "${SQL}" | base64 | kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "base64 -d | clickhouse-client --multiquery" >/dev/null 2>&1 || {
  log "[warn] running init SQL returned non-zero; fetching clickhouse pod logs"
  kubectl -n "${CH_NS}" logs "${CH_POD}" --tail=200 || true
}

log "[step 7/7] query for tokened logs"
Q="$(cat <<EOF
SELECT count() AS rows, any(service) AS service, any(pod) AS pod, any(namespace) AS namespace, min(ts) AS first_ts, max(ts) AS last_ts
FROM ${CH_DB}.${CH_TABLE}
WHERE message LIKE '%${TOKEN}%'
FORMAT Vertical
EOF
)"
kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "clickhouse-client --query \"$Q\"" || {
  log "[warn] query failed; listing DBs and tables"
  kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- clickhouse-client --query="SHOW DATABASES" || true
  kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- clickhouse-client --query="SHOW TABLES FROM ${CH_DB}" || true
}

SAMPLE_Q="SELECT ts, pod, namespace, message FROM ${CH_DB}.${CH_TABLE} WHERE message LIKE '%${TOKEN}%' ORDER BY ts DESC LIMIT 5 FORMAT Vertical"
kubectl -n "${CH_NS}" exec -i "${CH_POD}" -- bash -lc "clickhouse-client --query \"$SAMPLE_Q\"" || true

log "done"
