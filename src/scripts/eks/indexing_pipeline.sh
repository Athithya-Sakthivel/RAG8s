
export IRSA_ROLE_ARN="$(cd src/infra/terraform/aws && tofu output -json irsa_role_arns | jq -r '.indexer')"
export DATA_S3_BUCKET="$(cd src/infra/terraform/aws && tofu output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"
export BACKUP_S3_BUCKET="$(cd src/infra/terraform/aws && tofu output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"
export AWS_REGION="$(cd src/infra/terraform/aws && tofu output -raw aws_region)"
export AWS_DEFAULT_REGION="$AWS_REGION"

kubectl apply -f src/manifests/dense-service
kubectl apply -f src/manifests/sparse-service

export USE_IRSA=true
export SERVICE_ACCOUNT_NAME=indexer

NAMESPACE="indexing"
MANIFEST_DIR="src/manifests/indexing-cronjob"
TEST_JOB_NAME="indexing-backup-manual"
WAIT_POLL=2
WAIT_TIMEOUT=120

log() { printf '%s\n' "$*"; }

log "Deploying indexing pipeline..."
python3 src/infra/rag/indexing_cronjob.py --apply

log "Waiting for CronJob indexing-backup-cronjob..."
start=$(date +%s)
while ! kubectl get cronjob indexing-backup-cronjob -n "$NAMESPACE" >/dev/null 2>&1; do
  if [ $(( $(date +%s) - start )) -ge $WAIT_TIMEOUT ]; then
    log "Timed out waiting for CronJob."
    exit 1
  fi
  sleep "$WAIT_POLL"
done

log "Deleting old job $TEST_JOB_NAME..."
kubectl delete job "$TEST_JOB_NAME" -n "$NAMESPACE" --ignore-not-found

log "Creating new job $TEST_JOB_NAME..."
kubectl create job --from=cronjob/indexing-backup-cronjob "$TEST_JOB_NAME" -n "$NAMESPACE"

log "Waiting for pod from job $TEST_JOB_NAME..."
start=$(date +%s)
POD=""
while [ -z "$POD" ]; do
  POD=$(kubectl get pods -n "$NAMESPACE" -l job-name="$TEST_JOB_NAME" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "$POD" ]; then
    log "Found pod: $POD"
    break
  fi
  if [ $(( $(date +%s) - start )) -ge $WAIT_TIMEOUT ]; then
    log "Timed out waiting for pod. Dumping job/events..."
    kubectl describe job "$TEST_JOB_NAME" -n "$NAMESPACE" || true
    kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' | tail -n 30 || true
    exit 1
  fi
  sleep "$WAIT_POLL"
done

sleep 200
log "Streaming logs from pod $POD..."
kubectl logs -n "$NAMESPACE" "$POD" -c indexer --follow --tail=200
