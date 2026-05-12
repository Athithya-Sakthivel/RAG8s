#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="grafana"
ADMIN_SECRET="grafana-admin"
ADMIN_USER="admin"
ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-grafana}"


# ── Namespace and admin secret ──────────────────────────────
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic "$ADMIN_SECRET" \
  -n "$NAMESPACE" \
  --from-literal=admin-user="$ADMIN_USER" \
  --from-literal=admin-password="$ADMIN_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── Delete existing Application to switch from single-source to multi-source ──
kubectl delete application grafana -n argocd --ignore-not-found

# ── Apply the ArgoCD Application (Helm chart + ConfigMaps) ──
kubectl apply -f src/argocd/grafana-application.yaml

echo " Grafana Application applied. ArgoCD will now sync the Helm chart and dashboards."
echo "   Watch: kubectl get pods -n grafana -w"
echo "   Access: kubectl port-forward -n grafana svc/grafana 3000:80"