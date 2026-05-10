#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ────────────────────────────────────────────
export SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
export PAGERDUTY_ROUTING_KEY="${PAGERDUTY_ROUTING_KEY:-placeholder-for-now}"
NAMESPACE="monitoring"
CM_NAME="prometheus-server"
PROMETHEUS_LABELS="app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server"
SCRAPE_JOB_ALERTMANAGER='- job_name: alertmanager\n  scrape_interval: 30s\n  static_configs:\n    - targets:\n        - prometheus-alertmanager.monitoring.svc.cluster.local:9093'
SCRAPE_JOB_GRAFANA='- job_name: grafana\n  scrape_interval: 30s\n  static_configs:\n    - targets:\n        - grafana.grafana.svc.cluster.local:3000'
TMP_FILE="/tmp/prometheus.yml.$$"

cleanup() { rm -f "$TMP_FILE"; }
trap cleanup EXIT

# ── 1. Create secrets (idempotent) ───────────────────────────
echo "==> Creating Alertmanager secrets..."
kubectl create secret generic alertmanager-secrets \
  -n "$NAMESPACE" \
  --from-literal="slack-api-url=${SLACK_WEBHOOK_URL}" \
  --from-literal="pagerduty-routing-key=${PAGERDUTY_ROUTING_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── 2. Deploy Prometheus via ArgoCD ──────────────────────────
echo "==> Deploying Prometheus Application..."
kubectl apply -f src/argocd/prometheus-application.yaml

# ── 3. Wait for the ConfigMap to exist ───────────────────────
echo "==> Waiting for ConfigMap ${CM_NAME} to be created..."
while ! kubectl get configmap "$CM_NAME" -n "$NAMESPACE" >/dev/null 2>&1; do
  echo "   ConfigMap not yet present, sleeping 5s..."
  sleep 5
done

# ── 4. Idempotent ConfigMap patch ───────────────────────────
echo "==> Checking if scrape targets already exist..."
kubectl get configmap "$CM_NAME" -n "$NAMESPACE" -o jsonpath='{.data.prometheus\.yml}' > "$TMP_FILE"

ALERTMANAGER_EXISTS=$(grep -c "job_name: alertmanager" "$TMP_FILE" || true)
GRAFANA_EXISTS=$(grep -c "job_name: grafana" "$TMP_FILE" || true)

if [ "$ALERTMANAGER_EXISTS" -eq 1 ] && [ "$GRAFANA_EXISTS" -eq 1 ]; then
  echo "   Scrape targets already present – skipping patch."
else
  echo "   Adding missing scrape targets..."
  printf '\n%s\n%s\n' "$SCRAPE_JOB_ALERTMANAGER" "$SCRAPE_JOB_GRAFANA" >> "$TMP_FILE"
  kubectl create configmap "$CM_NAME" -n "$NAMESPACE" \
    --from-file=prometheus.yml="$TMP_FILE" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

# ── 5. Wait for Prometheus pod to be ready ───────────────────
echo "==> Waiting for Prometheus server pod to be ready..."
kubectl wait --for=condition=ready pod \
  -l "$PROMETHEUS_LABELS" \
  -n "$NAMESPACE" \
  --timeout=300s

# ── 6. Reload Prometheus ──────────────────────────────────────
echo "==> Reloading Prometheus configuration..."
if kubectl exec -n "$NAMESPACE" prometheus-server-0 -c prometheus-server -- /bin/sh -c "kill -HUP 1" 2>/dev/null; then
  echo "   Prometheus reloaded via SIGHUP"
else
  echo "   Reload via SIGHUP failed, performing rollout restart..."
  kubectl rollout restart statefulset prometheus-server -n "$NAMESPACE"
  kubectl rollout status statefulset prometheus-server -n "$NAMESPACE" --timeout=120s
fi

echo ""
echo "=============================================="
echo " Prometheus is deployed and scrape targets"
echo " are patched idempotently."
echo " Alertmanager & Grafana metrics will be scraped."
echo ""
echo " Port-forward:"
echo "   kubectl port-forward -n $NAMESPACE svc/prometheus-server 9090:9090"
echo "=============================================="