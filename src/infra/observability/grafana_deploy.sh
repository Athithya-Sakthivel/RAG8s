#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="grafana"
ADMIN_SECRET="grafana-admin"
ADMIN_USER="admin"
ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-grafana}"

[[ -n "$ADMIN_PASSWORD" ]] || { echo "ERROR: set GRAFANA_ADMIN_PASSWORD"; exit 1; }

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic "$ADMIN_SECRET" \
  -n "$NAMESPACE" \
  --from-literal=admin-user="$ADMIN_USER" \
  --from-literal=admin-password="$ADMIN_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f src/argocd/grafana-application.yaml

echo "Grafana Application applied. ArgoCD will now deploy Grafana."

