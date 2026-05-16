

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

kubectl apply -f src/infra/argocd/valkey-application.yaml
kubectl apply -f src/infra/argocd/clickhouse-application.yaml
bash src/infra/observability/prometheus_setup.sh
kubectl apply -f src/infra/argocd/vector-application.yaml
bash src/infra/observability/grafana_deploy.sh


