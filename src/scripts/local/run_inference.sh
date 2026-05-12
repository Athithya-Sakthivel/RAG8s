
bash src/scripts/local/local_cluster.sh
sleep 30
bash src/infra/core/default_storage_class.sh
bash src/infra/core/create_secrets.sh

helm repo add qdrant https://qdrant.github.io/qdrant-helm --force-update
kubectl create namespace qdrant --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install "${QDRANT_RELEASE:-qdrant}" qdrant/qdrant \
  --version "${QDRANT_CHART_VERSION:-v1.17.1}" --namespace "$NS" \
  --set replicaCount=1,persistence.size=20Gi \
  --set resources.requests.cpu=1,resources.requests.memory=2Gi \
  --set resources.limits.cpu=1,resources.limits.memory=2Gi \
  --wait --timeout 15m
sleep 200
export PER_POD=true
export QDRANT_BACKUP_S3_PREFIX=qdrant/backups/
export BACKUP_S3_BUCKET=$DATA_S3_BUCKET
bash src/scripts/backups_and_restore.sh restore

bash src/infra/core/argo_setup.sh --rollout

bash src/infra/observability/prometheus_setup.sh
sleep 800 
kubectl exec -it -n logging clickhouse-0 -- clickhouse-client --multiquery "
CREATE DATABASE IF NOT EXISTS logs;
CREATE TABLE IF NOT EXISTS logs.inference_logs (
    ts        DateTime64(3) DEFAULT now(),
    service   String,
    pod       String,
    namespace String,
    message   String,
    fields    String,
    level     String,
    container String,
    trace_id  String,
    span_id   String
) ENGINE = MergeTree()
ORDER BY ts TTL toDateTime(ts) + INTERVAL 30 DAY;
CREATE USER IF NOT EXISTS vector IDENTIFIED WITH plaintext_password BY 'vectorpass';
GRANT INSERT ON logs.* TO vector;
GRANT SELECT ON logs.* TO vector;
"
kubectl exec -n logging clickhouse-0 -- clickhouse-client --query "SELECT count() FROM logs.inference_logs"

git add . && git commit -m "argocd full sync" && git push origin main

# kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d

