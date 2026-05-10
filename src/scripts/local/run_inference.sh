
export CLOUDFLARE_ACCOUNT_ID=$CLOUDFLARE_ACCOUNT_I
export CLOUDFLARE_GLOBAL_API_KEY=$CLOUDFLARE_GLOBAL_API_KEY
export CLOUDFLARE_EMAIL="athithya651@gmail.com"
export TF_VAR_account_id="$CLOUDFLARE_ACCOUNT_ID"
export TF_VAR_domain="athithya.site"
export TF_VAR_zone_id=$(curl -s -H "X-Auth-Key: $CLOUDFLARE_GLOBAL_API_KEY" -H "X-Auth-Email: $CLOUDFLARE_EMAIL" "https://api.cloudflare.com/client/v4/zones?name=${TF_VAR_domain}" | jq -r '.result[0].id')
bash src/infra/terraform/cloudflare/run.sh --apply
# login to cloudflare



export DENSE_MODEL_NAME=BAAI/bge-small-en-v1.5
export DENSE_DIM=384
export DENSE_BATCH_SIZE=16 # upper bound
python3 src/infra/rag/dense_service.py --rollout

export SPARSE_MODEL_NAME=Qdrant/minicoil-v1
export SPARSE_BATCH_SIZE=16 # upper bound
python3 src/infra/rag/sparse_service.py --generate

export RERANKER_MODEL_NAME=Xenova/ms-marco-MiniLM-L-6-v2
export RERANKER_MAX_DOCS=20 # upper bound
python3 src/infra/rag/reranker_service.py --generate


python3 src/infra/rag/qdrant_service.py --rollout
export PER_POD=true
export QDRANT_BACKUP_S3_PREFIX=qdrant/backups/
export BACKUP_S3_BUCKET=$DATA_S3_BUCKET
bash src/scripts/backups_and_restore.sh restore

kubectl delete -f src/manifests/retriever
sleep 5
python3 src/infra/rag/retriever_service.py --apply-secrets
python3 src/infra/rag/retriever_service.py --write
kubectl apply -f src/manifests/retriever



kubectl delete -f src/manifests/cloudflared || true
export CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_token)"
export CLOUDFLARE_TUNNEL_NAME="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_name)"
export CLOUDFLARE_SECRET_NAME="cloudflared-token"
export CLOUDFLARE_SECRET_KEY="token"
export DOMAIN="athithya.site"
python3 src/infra/core/cloudflared_setup.py --write
kubectl apply -f /workspace/src/manifests/cloudflared


bash src/infra/core/valkey_service.sh

export FRONTEND_HOSTNAME=athithya.site
kubectl delete -f src/manifests/frontend || true
python3 src/infra/rag/spa_service.py --apply-secrets
python3 src/infra/rag/spa_service.py --write
python3 src/infra/rag/spa_service.py --apply
sleep 40
kubectl get pods -A



python3 src/infra/observability/clickhouse.py --delete --confirm
python3 src/infra/observability/clickhouse.py --rollout

python3 src/infra/observability/vector.py --delete --confirm
python3 src/infra/observability/vector.py --rollout
sleep 5
kubectl get pods -n logging


kubectl delete ns monitoring && kubectl apply -f src/argocd/prometheus-application.yaml && sleep 20 && kubectl get pods -n monitoring

sleep 5
find src/manifests -name "00-namespace.yaml" -delete || true
sleep 5
bash src/infra/core/argo_setup.sh --rollout
kubectl apply -f src/argocd
git add . && git commit -m "argocd full sync" && git push origin main



sleep 5
find src/manifests -name "00-namespace.yaml" -delete || true
sleep 5
bash src/infra/core/argo_setup.sh --rollout
git add . && git commit -m "new" && git push origin main

# kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
# kubectl port-forward service/argocd-server -n argocd 8080:443 argocd 8080:443
