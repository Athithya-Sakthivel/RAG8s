#!/usr/bin/env bash
set -euo pipefail

# ── Observability Stack Setup ──────────────────────────────
# Deploys ClickHouse → Prometheus → Vector → Grafana via ArgoCD
#
# Required env vars (export before running):
#   export CLICKHOUSE_USER=vector
#   export CLICKHOUSE_PASSWORD=vectorpass
#   export SLACK_WEBHOOK_URL=
#   export PAGERDUTY_ROUTING_KEY=placeholder-for-now
#   export GRAFANA_ADMIN_PASSWORD=grafana
#
# Usage:
#   bash src/scripts/eks/observability_setup.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ARGOCD_DIR="$REPO_ROOT/src/infra/argocd"
DASHBOARDS_DIR="$REPO_ROOT/src/manifests/grafana-dashboards"

wait_for_resource() {
    local resource_type="$1" resource_name="$2" namespace="$3" timeout="${4:-120}"
    echo "==> Waiting for $resource_type/$resource_name in $namespace..."
    local start=$(date +%s)
    while ! kubectl get "$resource_type" "$resource_name" -n "$namespace" >/dev/null 2>&1; do
        if [ $(($(date +%s) - start)) -ge "$timeout" ]; then
            echo "ERROR: Timed out waiting for $resource_type/$resource_name"
            return 1
        fi
        sleep 3
    done
}

# ── Namespaces ─────────────────────────────────────────────
echo "==> Creating namespaces..."
for ns in logging monitoring grafana; do
    kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

# ── Secrets ─────────────────────────────────────────────────
echo "==> Creating secrets..."

kubectl create secret generic grafana-admin -n grafana \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="${GRAFANA_ADMIN_PASSWORD:-grafana}" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic clickhouse-credentials -n logging \
    --from-literal=username="${CLICKHOUSE_USER:-vector}" \
    --from-literal=password="${CLICKHOUSE_PASSWORD:-vectorpass}" \
    --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic alertmanager-secrets -n monitoring \
    --from-literal=slack-api-url="${SLACK_WEBHOOK_URL:-}" \
    --from-literal=pagerduty-routing-key="${PAGERDUTY_ROUTING_KEY:-placeholder-for-now}" \
    --dry-run=client -o yaml | kubectl apply -f -

# ── Phase 1: ClickHouse ────────────────────────────────────
echo "==> Deploying ClickHouse..."
kubectl apply -f "$ARGOCD_DIR/clickhouse-application.yaml"
wait_for_resource statefulset clickhouse logging 120
kubectl rollout status statefulset/clickhouse -n logging --timeout=300s

echo "==> Initialising ClickHouse schema..."
kubectl exec -i -n logging clickhouse-0 -- clickhouse-client --multiquery "
CREATE DATABASE IF NOT EXISTS logs;
CREATE TABLE IF NOT EXISTS logs.inference_logs (
    ts        DateTime64(3) DEFAULT now(),
    service   String,
    pod       String,
    namespace String,
    message   String,
    fields    String,
    level     String,
    container String,
    trace_id  String,
    span_id   String
) ENGINE = MergeTree()
ORDER BY ts TTL toDateTime(ts) + INTERVAL 30 DAY;
CREATE USER IF NOT EXISTS ${CLICKHOUSE_USER:-vector} IDENTIFIED WITH plaintext_password BY '${CLICKHOUSE_PASSWORD:-vectorpass}';
GRANT INSERT ON logs.* TO ${CLICKHOUSE_USER:-vector};
GRANT SELECT ON logs.* TO ${CLICKHOUSE_USER:-vector};
"

# ── Phase 2: Prometheus ────────────────────────────────────
echo "==> Deploying Prometheus..."
kubectl apply -f "$ARGOCD_DIR/prometheus-application.yaml"
wait_for_resource statefulset prometheus-server monitoring 120
kubectl rollout status statefulset/prometheus-server -n monitoring --timeout=300s

# Patch scrape targets
TMP_FILE="/tmp/prometheus.yml.$$"
trap 'rm -f "$TMP_FILE"' EXIT
kubectl get configmap prometheus-server -n monitoring -o jsonpath='{.data.prometheus\.yml}' > "$TMP_FILE"

if grep -q "job_name: alertmanager" "$TMP_FILE" && grep -q "job_name: grafana" "$TMP_FILE"; then
    echo "Scrape targets already present – skipping."
else
    echo "Adding alertmanager & grafana scrape targets..."
    cat >> "$TMP_FILE" <<'EOF'

- job_name: alertmanager
  scrape_interval: 30s
  static_configs:
    - targets:
        - prometheus-alertmanager.monitoring.svc.cluster.local:9093
- job_name: grafana
  scrape_interval: 30s
  static_configs:
    - targets:
        - grafana.grafana.svc.cluster.local:3000
EOF
    kubectl create configmap prometheus-server -n monitoring \
        --from-file=prometheus.yml="$TMP_FILE" \
        --dry-run=client -o yaml | kubectl apply -f -
    kubectl rollout restart statefulset prometheus-server -n monitoring
    kubectl rollout status statefulset prometheus-server -n monitoring --timeout=120s
fi

# ── Phase 3: Vector ────────────────────────────────────────
echo "==> Deploying Vector..."
kubectl apply -f "$ARGOCD_DIR/vector-application.yaml"

# ── Phase 4: Grafana ───────────────────────────────────────
echo "==> Deploying Grafana..."
kubectl apply -f "$ARGOCD_DIR/grafana-application.yaml"
wait_for_resource deployment grafana grafana 120
kubectl wait --for=condition=Available deployment/grafana -n grafana --timeout=300s

echo "==> Applying Grafana dashboard ConfigMaps..."
for f in "$DASHBOARDS_DIR"/*.yaml; do
    kubectl apply -f "$f"
done

echo ""
echo "=============================================="
echo " Observability stack deployed successfully."
echo " Grafana: admin / ${GRAFANA_ADMIN_PASSWORD:-grafana}"
echo ""
echo " Phase 1: ClickHouse + schema  ✓"
echo " Phase 2: Prometheus          ✓"
echo " Phase 3: Vector              ✓"
echo " Phase 4: Grafana + dashboards ✓"
echo "=============================================="