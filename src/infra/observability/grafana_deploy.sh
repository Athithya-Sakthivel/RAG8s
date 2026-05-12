#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="grafana"
ADMIN_SECRET="grafana-admin"
ADMIN_USER="admin"
ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-grafana}"
DASHBOARDS_DIR="src/manifests/grafana-dashboards"

# ── Namespace and admin secret ──────────────────────────────
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic "$ADMIN_SECRET" \
  -n "$NAMESPACE" \
  --from-literal=admin-user="$ADMIN_USER" \
  --from-literal=admin-password="$ADMIN_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── Delete existing Application ──
kubectl delete application grafana -n argocd --ignore-not-found

# ── Apply the ArgoCD Application ──
kubectl apply -f src/argocd/grafana-application.yaml

# ── Wait for the Deployment to be created (ArgoCD needs time to render the Helm chart) ──
echo "Waiting for Grafana Deployment to appear..."
RETRIES=30
until kubectl get deployment grafana -n "$NAMESPACE" >/dev/null 2>&1; do
  RETRIES=$((RETRIES - 1))
  if [[ $RETRIES -eq 0 ]]; then
    echo "ERROR: Grafana Deployment did not appear after 150 seconds"
    kubectl get application grafana -n argocd -o yaml
    exit 1
  fi
  sleep 5
done

# ── Wait for the Deployment to be fully Available ─────────
echo "Waiting for Grafana Deployment to become Available..."
kubectl wait --for=condition=Available deployment/grafana -n "$NAMESPACE" --timeout=300s

# ── Apply dashboard ConfigMaps AFTER Grafana is Ready ──────
for f in "$DASHBOARDS_DIR"/*.yaml; do
  kubectl apply -f "$f"
done

echo ""
echo "✅ Grafana and all dashboards deployed."
echo "   ArgoCD manages the Helm release (not the ConfigMaps)."
echo "   Access: kubectl port-forward -n grafana svc/grafana 3000:80"
echo ""
echo "   Login: admin / $ADMIN_PASSWORD"