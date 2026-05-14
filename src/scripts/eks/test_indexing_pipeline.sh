#!/bin/bash
set -euo pipefail

GH_REPO="${GH_REPO:-https://github.com/Athithya-Sakthivel/E2E-RAG-System.git}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
NAMESPACE="indexing"
CRONJOB_NAME="indexing-backup-cronjob"

QDRANT_APP="$ARGOCD_APP_DIR/qdrant-application.yaml"
FASTEMBED_APP="$ARGOCD_APP_DIR/fastembed-application.yaml"
INDEXING_APP="$ARGOCD_APP_DIR/indexing-cronjob-application.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

rm -rf data
cp -r src/scripts/archive/data $(pwd)



log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BLUE}[STEP]${NC} ${YELLOW}$*${NC}"; }

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}  Indexing Pipeline – ArgoCD Deployment${NC}"
echo -e "${GREEN}=========================================${NC}"

command -v tofu   >/dev/null 2>&1 || { log_error "tofu required"; exit 1; }
command -v aws    >/dev/null 2>&1 || { log_error "aws CLI required"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { log_error "kubectl required"; exit 1; }
command -v jq     >/dev/null 2>&1 || { log_error "jq required"; exit 1; }

# ---- helpers ----
wait_for_pods() {
    local ns="$1" label="$2" timeout="${3:-300}"
    log_info "Waiting for pods in ns='$ns' label='$label' (timeout=${timeout}s)..."
    if kubectl wait --for=condition=Ready pod -n "$ns" -l "$label" --timeout="${timeout}s" 2>/dev/null; then
        log_info "Pods with label '$label' are Ready."
    else
        log_warn "Timeout waiting for pods. Continuing anyway."
    fi
}

run_test_job() {
    local test_job_name="test-indexing-run-$(date +%s)"
    log_step "Running Test Indexing Job"
    if ! kubectl get cronjob "$CRONJOB_NAME" -n "$NAMESPACE" &>/dev/null; then
        log_error "CronJob '$CRONJOB_NAME' not found."
        return 1
    fi
    log_info "Creating job: $test_job_name"
    kubectl create job --from=cronjob/"$CRONJOB_NAME" "$test_job_name" -n "$NAMESPACE"

    log_info "Waiting for pod..."
    local pod_name=""
    for i in $(seq 1 20); do
        pod_name=$(kubectl get pods -n "$NAMESPACE" -l "job-name=$test_job_name" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
        if [ -n "$pod_name" ]; then
            local phase=$(kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}')
            if [ "$phase" != "Pending" ]; then break; fi
        fi
        sleep 3
    done
    if [ -z "$pod_name" ]; then
        log_error "Pod not created. Events:"; kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' | tail -10; return 1
    fi
    log_info "Pod: $pod_name"
    kubectl get pod "$pod_name" -n "$NAMESPACE" -o wide

    echo -e "\n${BLUE}=== Container Logs (Ctrl+C to stop) ===${NC}\n"
    kubectl logs -f "$pod_name" -n "$NAMESPACE" 2>&1 || true

    echo -e "\n${GREEN}Final status:${NC}"
    kubectl get job "$test_job_name" -n "$NAMESPACE"
    kubectl get pod -l "job-name=$test_job_name" -n "$NAMESPACE"
}

# ============================================
# MAIN
# ============================================

# 1. Infrastructure outputs
log_step "1/6 Fetching infrastructure outputs from Terraform"
AWS_REGION="$(tofu -chdir="$TOFU_DIR" output -raw aws_region)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IRSA_ROLE_ARN="$(tofu -chdir="$TOFU_DIR" output -json irsa_role_arns | jq -r '.indexer')"
DATA_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"
BACKUP_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"
ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_TAG="${IMAGE_TAG:-staging}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

log_info "Region:   $AWS_REGION"
log_info "ECR:      $ECR_URL"
log_info "S3 data:  $DATA_S3_BUCKET"
log_info "S3 backup:$BACKUP_S3_BUCKET"
log_info "Image:    $IMAGE_TAG"
log_info "IRSA:     $IRSA_ROLE_ARN"
sleep 2

# 2. Upload to S3
log_step "2/6 Uploading data to S3"
if ! python3 "$REPO_ROOT/src/scripts/local/force_sync_s3_local_fs.py" --upload; then
    log_warn "S3 upload failed, continuing"
fi

# 3. Generate ArgoCD Application YAML (no image digest, uses tag only)
log_step "3/6 Generating ArgoCD Application YAMLs"
mkdir -p "$ARGOCD_APP_DIR"
cat > "$INDEXING_APP" << EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: indexing-cronjob
  namespace: argocd
  labels:
    app.kubernetes.io/name: indexing-cronjob
    app.kubernetes.io/managed-by: argocd
    app.kubernetes.io/part-of: e2e-rag-system
  annotations:
    description: Indexing CronJob Helm deployment
    deployed-at: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    aws-region: "${AWS_REGION}"
    ecr-repository: "${ECR_URL}/indexer"
spec:
  project: default
  source:
    repoURL: ${GH_REPO}
    targetRevision: main
    path: src/infra/helm/indexing_cronjob
    helm:
      values: |
        image:
          registry: ${ECR_URL}
          repository: indexer
          tag: "${IMAGE_TAG}"
          pullPolicy: IfNotPresent
        serviceAccount:
          annotations:
            eks.amazonaws.com/role-arn: "${IRSA_ROLE_ARN}"
        env:
          S3_BUCKET: "${BACKUP_S3_BUCKET}"
          DATA_S3_BUCKET: "${DATA_S3_BUCKET}"
        resources:
          requests:
            cpu: "500m"
            memory: 512Mi
          limits:
            cpu: "2"
            memory: 2Gi
        nodeSelector:
          node-type: compute
        tolerations:
          - key: "node-type"
            operator: "Equal"
            value: "compute"
            effect: "NoSchedule"
  destination:
    server: https://kubernetes.default.svc
    namespace: ${NAMESPACE}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - PrunePropagationPolicy=foreground
      - PruneLast=true
    retry:
      limit: 3
      backoff:
        duration: 10s
        factor: 2
        maxDuration: 3m
EOF
log_info "Generated: $INDEXING_APP"

# 4. Apply Qdrant
log_step "4/6 Deploying Qdrant via ArgoCD"
kubectl apply -f "$QDRANT_APP"
sleep 10

# 5. Apply FastEmbed
log_step "5/6 Deploying FastEmbed via ArgoCD"
kubectl apply -f "$FASTEMBED_APP"
sleep 10

# Wait for dependencies (pods Running, no HTTP checks)
log_step "Waiting for Qdrant pod"
wait_for_pods "qdrant" "app.kubernetes.io/name=qdrant" 300

log_step "Waiting for FastEmbed dense pod"
wait_for_pods "fastembed" "app.kubernetes.io/name=dense" 300

log_step "Waiting for FastEmbed sparse pod"
wait_for_pods "fastembed" "app.kubernetes.io/name=sparse" 300

# 6. Deploy Indexing CronJob
log_step "6/6 Deploying Indexing CronJob via ArgoCD"
kubectl apply -f "$INDEXING_APP"
log_info "Waiting 15s for CronJob to sync..."
sleep 15

echo -e "\n${GREEN}=========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
kubectl get cronjobs -n "$NAMESPACE" 2>/dev/null || log_warn "No cronjobs yet"

RUB_TEST=true
if [ "${RUN_TEST:-true}" = "true" ]; then
    run_test_job
fi

echo -e "\n${BLUE}Quick commands:${NC}"
echo -e "  ${CYAN}kubectl get applications -n argocd${NC}"
echo -e "  ${CYAN}kubectl get cronjobs -n $NAMESPACE${NC}"
echo -e "  ${CYAN}kubectl create job --from=cronjob/$CRONJOB_NAME test-$(date +%s) -n $NAMESPACE${NC}"