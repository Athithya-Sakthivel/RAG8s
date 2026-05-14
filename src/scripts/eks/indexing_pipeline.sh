#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
HELM_DIR="$REPO_ROOT/src/infra/helm/indexing_cronjob"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
NAMESPACE="indexing"
CRONJOB_NAME="indexing-backup-cronjob"

QDRANT_APP="$ARGOCD_APP_DIR/qdrant-application.yaml"
FASTEMBED_APP="$ARGOCD_APP_DIR/fastembed-application.yaml"
INDEXING_APP="$ARGOCD_APP_DIR/indexing-cronjob-application.yaml"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BLUE}[STEP]${NC} ${YELLOW}$*${NC}"; }

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}  Indexing Pipeline – ArgoCD Deployment${NC}"
echo -e "${GREEN}=========================================${NC}"

# Prerequisites
command -v tofu   >/dev/null 2>&1 || { log_error "tofu required"; exit 1; }
command -v aws    >/dev/null 2>&1 || { log_error "aws CLI required"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { log_error "kubectl required"; exit 1; }
command -v jq     >/dev/null 2>&1 || { log_error "jq required"; exit 1; }

# ------------------------------------------------------------
# wait_for_pods – wait until at least one pod with the given label is Ready
# ------------------------------------------------------------
wait_for_pods() {
    local namespace="$1"
    local label="$2"
    local timeout="${3:-300}"
    log_info "Waiting for pods in ns='$namespace' with label='$label' (timeout=${timeout}s)..."
    if kubectl wait --for=condition=Ready pod -n "$namespace" -l "$label" --timeout="${timeout}s" 2>/dev/null; then
        log_info "Pods with label '$label' are Ready."
    else
        log_error "Timeout waiting for pods. Current state:"
        kubectl get pods -n "$namespace" -l "$label" --no-headers 2>/dev/null || echo "  No pods found"
        return 1
    fi
}

# ------------------------------------------------------------
# wait_for_pod_with_label – wait for a single pod with a label and optional HTTP check
# ------------------------------------------------------------
wait_for_pod_with_http() {
    local namespace="$1"
    local label="$2"
    local url="$3"
    local timeout="${4:-300}"
    log_info "Waiting for pod with label='$label' in ns='$namespace'..."
    # Wait for pod to be Ready
    if ! kubectl wait --for=condition=Ready pod -n "$namespace" -l "$label" --timeout="${timeout}s" 2>/dev/null; then
        log_error "Pod with label '$label' did not become Ready."
        return 1
    fi
    # Now check the HTTP endpoint
    local pod
    pod=$(kubectl get pods -n "$namespace" -l "$label" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    log_info "Pod $pod is Ready. Checking endpoint $url ..."
    local attempt=1
    while [ $attempt -le 30 ]; do
        local code
        code=$(kubectl exec -n "$namespace" "$pod" -- curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
        if [ "$code" = "200" ]; then
            log_info "Endpoint $url returned 200 OK."
            return 0
        fi
        log_info "  attempt $attempt: HTTP $code, retrying in 5s..."
        sleep 5
        attempt=$((attempt + 1))
    done
    log_error "Endpoint $url did not become healthy."
    return 1
}

# ------------------------------------------------------------
# run_test_job – create a manual job from the CronJob and stream logs
# ------------------------------------------------------------
run_test_job() {
    local test_job_name="test-indexing-run-$(date +%s)"

    log_step "Running Test Indexing Job"
    if ! kubectl get cronjob "$CRONJOB_NAME" -n "$NAMESPACE" &>/dev/null; then
        log_error "CronJob '$CRONJOB_NAME' not found in ns '$NAMESPACE'"
        return 1
    fi

    log_info "Creating test job: $test_job_name"
    kubectl create job --from=cronjob/"$CRONJOB_NAME" "$test_job_name" -n "$NAMESPACE"

    log_info "Waiting for pod to be scheduled..."
    local pod_name=""
    for i in $(seq 1 20); do
        pod_name=$(kubectl get pods -n "$NAMESPACE" -l "job-name=$test_job_name" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
        if [ -n "$pod_name" ]; then
            local phase=$(kubectl get pod "$pod_name" -n "$NAMESPACE" -o jsonpath='{.status.phase}')
            if [ "$phase" != "Pending" ]; then
                break
            fi
        fi
        sleep 3
    done

    if [ -z "$pod_name" ]; then
        log_error "Pod not created. Recent events:"
        kubectl get events -n "$NAMESPACE" --sort-by='.lastTimestamp' | tail -10
        return 1
    fi

    log_info "Pod: $pod_name"
    kubectl get pod "$pod_name" -n "$NAMESPACE" -o wide

    echo -e "\n${BLUE}=== Container Logs (Ctrl+C to stop) ===${NC}\n"
    kubectl logs -f "$pod_name" -n "$NAMESPACE" 2>&1 || true

    echo -e "\n${GREEN}Log streaming ended. Final status:${NC}"
    kubectl get job "$test_job_name" -n "$NAMESPACE"
    kubectl get pod -l "job-name=$test_job_name" -n "$NAMESPACE"
}

# ============================================
# MAIN
# ============================================

# 1. Fetch infrastructure outputs
log_step "1/6 Fetching infrastructure outputs from Terraform"
AWS_REGION="$(tofu -chdir="$TOFU_DIR" output -raw aws_region)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
IRSA_ROLE_ARN="$(tofu -chdir="$TOFU_DIR" output -json irsa_role_arns | jq -r '.indexer')"
DATA_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"
BACKUP_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"
ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_TAG="${IMAGE_TAG:-staging}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

log_info "Region:       $AWS_REGION"
log_info "ECR:          $ECR_URL"
log_info "Data S3:      $DATA_S3_BUCKET"
log_info "Backup S3:    $BACKUP_S3_BUCKET"
log_info "Image tag:    $IMAGE_TAG"
log_info "IRSA Role:    $IRSA_ROLE_ARN"

sleep 3

# 2. Upload data to S3
log_step "2/6 Uploading data to S3"
if ! python3 "$REPO_ROOT/src/scripts/local/force_sync_s3_local_fs.py" --upload; then
    log_warn "S3 upload failed, continuing anyway"
fi

# 3. Generate ArgoCD Application YAML for indexing cronjob
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
    repoURL: https://github.com/Athithya-Sakthivel/E2E-RAG-System.git
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
          name: indexer
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
        nodeSelector: {}
        tolerations: []
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

bash src/infra/core/default_storage_class.sh
# 4. Deploy Qdrant
log_step "4/6 Deploying Qdrant via ArgoCD"

kubectl apply -f "$QDRANT_APP"
log_info "Waiting 10 seconds for ArgoCD to start reconciliation..."
sleep 10

# 5. Deploy FastEmbed
log_step "5/6 Deploying FastEmbed via ArgoCD"
kubectl apply -f "$FASTEMBED_APP"
log_info "Waiting another 10 seconds for FastEmbed application..."
sleep 10

# ---- Wait for dependencies (using correct labels) ----
log_step "Waiting for Qdrant to become ready"
# Qdrant Helm chart labels: app.kubernetes.io/name=qdrant
wait_for_pod_with_http "qdrant" "app.kubernetes.io/name=qdrant" "http://localhost:6333/healthz" 300 || {
    log_warn "Qdrant healthz check failed but pods might be running; continuing..."
}

log_step "Waiting for FastEmbed models to become ready"
# Wait for dense pod
wait_for_pod_with_http "fastembed" "app.kubernetes.io/name=dense" "http://localhost:8200/health" 600 || {
    log_error "Dense model not ready"
    exit 1
}
# Wait for sparse pod
wait_for_pod_with_http "fastembed" "app.kubernetes.io/name=sparse" "http://localhost:8201/health" 600 || {
    log_error "Sparse model not ready"
    exit 1
}

log_info "All dependencies are ready."

# 6. Deploy Indexing CronJob
log_step "6/6 Deploying Indexing CronJob via ArgoCD"
kubectl apply -f "$INDEXING_APP"
log_info "Waiting 15 seconds for CronJob to be registered..."
sleep 15

# Final status
echo -e "\n${GREEN}=========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
log_info "CronJob status:"
kubectl get cronjobs -n "$NAMESPACE" 2>/dev/null || log_warn "No cronjobs yet"

if [ "${RUN_TEST:-true}" = "true" ]; then
    run_test_job
fi

echo -e "\n${BLUE}Quick commands:${NC}"
echo -e "  ${CYAN}kubectl get applications -n argocd${NC}"
echo -e "  ${CYAN}kubectl get cronjobs -n $NAMESPACE${NC}"
echo -e "  ${CYAN}kubectl create job --from=cronjob/$CRONJOB_NAME test-$(date +%s) -n $NAMESPACE${NC}"