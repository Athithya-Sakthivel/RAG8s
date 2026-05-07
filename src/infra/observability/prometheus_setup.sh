
# Add the repository
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

# Update repository cache
helm repo update

# Now deploy
helm upgrade --install prometheus prometheus-community/prometheus \
  --version 29.6.0 \
  --namespace monitoring \
  --create-namespace \
  --values /workspace/src/argocd/prometheus-values.yaml
  

helm upgrade --install prometheus prometheus-community/prometheus \
  --version 29.6.0 \
  --namespace monitoring \
  --create-namespace \
  --values /workspace/src/argocd/prometheus-values.yaml \
  --set kube-state-metrics.enabled=false \
  --set prometheus-node-exporter.enabled=false \
  --set prometheus-pushgateway.enabled=false