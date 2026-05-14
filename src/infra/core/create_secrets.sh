#!/usr/bin/env bash

for ns in indexing inference logging grafana monitoring; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

# Frontend secrets
SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"
JWT_PRIVATE_KEY_PEM="${JWT_PRIVATE_KEY_PEM:-$(openssl ecparam -genkey -name prime256v1 -noout 2>/dev/null | openssl pkcs8 -topk8 -nocrypt -outform PEM 2>/dev/null)}"
JWT_PEM_FILE=$(mktemp)
echo "$JWT_PRIVATE_KEY_PEM" > "$JWT_PEM_FILE"
kubectl create secret generic frontend-secrets -n inference \
  --from-literal=SESSION_SECRET="$SESSION_SECRET" \
  --from-literal=GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=MS_CLIENT_ID="${MS_CLIENT_ID:-}" \
  --from-literal=MS_CLIENT_SECRET="${MS_CLIENT_SECRET:-}" \
  --from-file=JWT_PRIVATE_KEY_PEM="$JWT_PEM_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f "$JWT_PEM_FILE"

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

export CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_token)"
# Cloudflare tunnel secret
kubectl create secret generic cloudflared-token -n inference \
  --from-literal=token="${CLOUDFLARE_TUNNEL_TOKEN:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "All secrets created/updated"