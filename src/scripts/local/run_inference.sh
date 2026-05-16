
aws eks update-kubeconfig --region ap-south-1 --name rag-eks-staging


export BACKUP_S3_BUCKET="$(cd src/infra/terraform/aws && tofu output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"
export DATA_S3_BUCKET="$(cd src/infra/terraform/aws && tofu output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"

bash src/infra/core/default_storage_class.sh
bash src/infra/core/create_secrets.sh

helm repo add qdrant https://qdrant.github.io/qdrant-helm --force-update
kubectl create namespace qdrant --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install "${QDRANT_RELEASE:-qdrant}" qdrant/qdrant \
  --version "${QDRANT_CHART_VERSION:-v1.17.1}" --namespace "$NS" \
  --set replicaCount=1,persistence.size=20Gi \
  --set resources.requests.cpu=1,resources.requests.memory=512Mi \
  --set resources.limits.cpu=1,resources.limits.memory=2Gi \
  --wait --timeout 15m


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





kubectl create configmap dense-image -n inference --from-literal=image='ghcr.io/athithya-sakthivel/dense:2026-05-06-06-15--70eca4b@sha256:c8a9fbb234cb355f530b3ecf6a14c1ebbecbaf24b74e6c865ca79a0073afbe70' --dry-run=client -o yaml | kubectl apply -f - && kubectl create configmap sparse-image -n inference --from-literal=image='ghcr.io/athithya-sakthivel/sparse:2026-05-06-06-26--13b7433@sha256:e977f4eedd896f28de999809eefe8db7c2048dc7fb9f7700d450a333449d334b' --dry-run=client -o yaml
 kubectl apply -f - && kubectl create configmap indexing-image -n indexing --from-literal=image='ghcr.io/athithya-sakthivel/indexing-pipeline:2026-05-09-16-28--d613be2@sha256:f293916991de710b8d725dfe71aaa74e2148c45913e420d76c77829a5482df69' --dry-run=client -o yaml | kubectl apply -f -