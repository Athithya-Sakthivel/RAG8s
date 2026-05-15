#!/usr/bin/env bash
kubectl apply -f src/infra/argocd

for ns in logging grafana monitoring; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done


kubectl create ns grafana --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic grafana-admin -n grafana \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="${GRAFANA_ADMIN_PASSWORD:-grafana}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ClickHouse/Vector secrets
kubectl create secret generic clickhouse-credentials -n logging \
  --from-literal=username="${CLICKHOUSE_USER:-vector}" \
  --from-literal=password="${CLICKHOUSE_PASSWORD:-vectorpass}" \
  --dry-run=client -o yaml | kubectl apply -f -

