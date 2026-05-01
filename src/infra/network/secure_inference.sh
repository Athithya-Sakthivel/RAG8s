bash src/infra/observability/mimic_traffic_medium.sh

aws s3 rm s3://e2e-mlops-data-681802563986/postgres_backups/ --recursive

export K8S_CLUSTER=kind
export PG_BACKUPS_S3_BUCKET=e2e-mlops-data-681802563986
export PG_CLUSTER_ID=cnpg-cluster-kind
export PG_SERVER_NAME=mlsecops

make pg-cluster

export CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir=src/terraform/cloudflare output -raw cloudflare_tunnel_token)"
export CLOUDFLARE_TUNNEL_NAME="$(tofu -chdir=src/terraform/cloudflare output -raw cloudflare_tunnel_name)"
export CLOUDFLARE_SECRET_NAME="cloudflared-token"
export CLOUDFLARE_SECRET_KEY="token"
export DOMAIN="athithya.site"

python3 src/infra/security/cloudflared.py --rollout

python3 src/infra/security/oidc_server.py --rollout && sleep 60 && kubectl get pods -A
