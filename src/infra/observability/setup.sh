#!/usr/bin/env bash
set -euo pipefail

# ── Create namespaces one at a time ──────────────────────────
kubectl create namespace logging   --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace grafana   --dry-run=client -o yaml | kubectl apply -f -

# ── Grafana admin secret ─────────────────────────────────────
kubectl create secret generic grafana-admin -n grafana \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="${GRAFANA_ADMIN_PASSWORD:-grafana}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── ClickHouse/Vector secrets ────────────────────────────────
kubectl create secret generic clickhouse-credentials -n logging \
  --from-literal=username="${CLICKHOUSE_USER:-vector}" \
  --from-literal=password="${CLICKHOUSE_PASSWORD:-vectorpass}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── Deploy Valkey and ClickHouse ─────────────────────────────
kubectl apply -f src/infra/argocd/valkey-application.yaml
kubectl apply -f src/infra/argocd/clickhouse-application.yaml

# ── Deploy Prometheus ────────────────────────────────────────
bash src/infra/observability/prometheus_setup.sh

# ── Deploy Vector ────────────────────────────────────────────
kubectl apply -f src/infra/argocd/vector-application.yaml

# ── Deploy Grafana ───────────────────────────────────────────
bash src/infra/observability/grafana_deploy.sh