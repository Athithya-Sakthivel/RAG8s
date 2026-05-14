#!/bin/bash
set -euo pipefail

REPO_ROOT=$(pwd)
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
HELM_DIR="$REPO_ROOT/src/infra/helm/indexing_cronjob"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
NAMESPACE="indexing"
RELEASE_NAME="indexing-pipeline"
CRONJOB_NAME="indexing-backup-cronjob"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}  Indexing Pipeline Deployment Script${NC}"
echo -e "${GREEN}=========================================${NC}"

# Verify prerequisites
command -v tofu >/dev/null 2>&1 || { echo -e "${RED}Error: tofu is required but not installed${NC}" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo -e "${RED}Error: aws CLI is required but not installed${NC}" >&2; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo -e "${RED}Error: kubectl is required but not installed${NC}" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo -e "${RED}Error: jq is required but not installed${NC}" >&2; exit 1; }
command -v helm >/dev/null 2>&1 || { echo -e "${RED}Error: helm is required but not installed${NC}" >&2; exit 1; }

# Fetch infrastructure outputs
echo -e "\n${YELLOW}Fetching infrastructure outputs from Terraform...${NC}"

AWS_REGION="$(tofu -chdir="$TOFU_DIR" output -raw aws_region)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# Get IRSA role ARN for indexer
IRSA_ROLE_ARN="$(tofu -chdir="$TOFU_DIR" output -json irsa_role_arns | jq -r '.indexer')"

# Get S3 bucket names
DATA_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.DATA_S3_BUCKET')"
BACKUP_S3_BUCKET="$(tofu -chdir="$TOFU_DIR" output -json s3_bucket_name_map | jq -r '.QDRANT_BACKUPS_BUCKET')"

# Construct ECR URL
ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_TAG="${IMAGE_TAG:-staging}"
INDEXER_IMAGE="${ECR_URL}/indexer:${IMAGE_TAG}"

# Export AWS config
export AWS_REGION
export AWS_DEFAULT_REGION="$AWS_REGION"

# Display configuration
echo -e "\n${GREEN}Deployment Configuration:${NC}"
echo -e "  AWS Region:        ${YELLOW}$AWS_REGION${NC}"
echo -e "  Account ID:        ${YELLOW}$ACCOUNT_ID${NC}"
echo -e "  IRSA Role ARN:     ${YELLOW}$IRSA_ROLE_ARN${NC}"
echo -e "  Data S3 Bucket:    ${YELLOW}$DATA_S3_BUCKET${NC}"
echo -e "  Backup S3 Bucket:  ${YELLOW}$BACKUP_S3_BUCKET${NC}"
echo -e "  ECR URL:           ${YELLOW}$ECR_URL${NC}"
echo -e "  Indexer Image:     ${YELLOW}$INDEXER_IMAGE${NC}"
echo -e "  Namespace:         ${YELLOW}$NAMESPACE${NC}"
echo -e "  Release Name:      ${YELLOW}$RELEASE_NAME${NC}"
echo -e "  Test Run:          ${YELLOW}${RUN_TEST:-true}${NC}"

# Create temporary values file for Helm
create_helm_values() {
    local tmp_file
    tmp_file=$(mktemp)
    
    cat > "$tmp_file" << EOF
image:
  registry: ${ECR_URL}
  repository: indexer
  tag: "${IMAGE_TAG}"
  digest: ""
  pullPolicy: IfNotPresent

serviceAccount:
  name: indexer
  annotations:
    eks.amazonaws.com/role-arn: "${IRSA_ROLE_ARN}"

env:
  S3_BUCKET: "${BACKUP_S3_BUCKET}"
  DATA_S3_BUCKET: "${DATA_S3_BUCKET}"
EOF
    
    echo "$tmp_file"
}

# Function to trigger and monitor a test run
run_test_job() {
    local test_job_name="test-indexing-run-$(date +%s)"
    
    echo -e "\n${BLUE}=========================================${NC}"
    echo -e "${BLUE}  Running Test Indexing Job${NC}"
    echo -e "${BLUE}=========================================${NC}"
    
    # Check if CronJob exists
    if ! kubectl get cronjob "$CRONJOB_NAME" -n "$NAMESPACE" &>/dev/null; then
        echo -e "${RED}Error: CronJob '$CRONJOB_NAME' not found in namespace '$NAMESPACE'${NC}"
        echo -e "${YELLOW}Available CronJobs:${NC}"
        kubectl get cronjobs -n "$NAMESPACE"
        return 1
    fi
    
    echo -e "\n${YELLOW}Creating test job from CronJob...${NC}"
    kubectl create job --from=cronjob/"$CRONJOB_NAME" "$test_job_name" -n "$NAMESPACE"
    
    echo -e "${GREEN}Test job '$test_job_name' created!${NC}"
    
    # Wait for pod to be created
    echo -e "\n${YELLOW}Waiting for pod to start...${NC}"
    sleep 3
    
    # Get the pod name
    local pod_name=""
    local max_retries=10
    local retry=0
    
    while [ $retry -lt $max_retries ]; do
        pod_name=$(kubectl get pods -n "$NAMESPACE" -l "job-name=$test_job_name" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
        if [ -n "$pod_name" ]; then
            break
        fi
        echo -e "${YELLOW}Waiting for pod... (${retry}/${max_retries})${NC}"
        sleep 2
        retry=$((retry + 1))
    done
    
    if [ -z "$pod_name" ]; then
        echo -e "${RED}Error: Pod not found for job '$test_job_name'${NC}"
        echo -e "${YELLOW}Check pods manually:${NC}"
        kubectl get pods -n "$NAMESPACE"
        return 1
    fi
    
    echo -e "\n${GREEN}Pod found: ${CYAN}$pod_name${NC}"
    
    # Show pod details
    echo -e "\n${YELLOW}Pod Status:${NC}"
    kubectl get pod "$pod_name" -n "$NAMESPACE" -o wide
    
    # Wait for container to start
    echo -e "\n${YELLOW}Waiting for container to be ready...${NC}"
    kubectl wait --for=condition=Ready pod/"$pod_name" -n "$NAMESPACE" --timeout=120s 2>/dev/null || true
    
    # Stream logs in real-time
    echo -e "\n${BLUE}=========================================${NC}"
    echo -e "${BLUE}  Container Logs (Streaming)${NC}"
    echo -e "${BLUE}=========================================${NC}"
    echo -e "${CYAN}Press Ctrl+C to stop watching logs (job will continue running)${NC}\n"
    
    # Trap Ctrl+C to gracefully exit log streaming
    trap 'echo -e "\n${YELLOW}Stopped log streaming. Job is still running.${NC}"; return 0' INT
    
    # Follow logs
    kubectl logs -f "$pod_name" -n "$NAMESPACE" --tail=10 2>&1 || {
        local exit_code=$?
        if [ $exit_code -ne 130 ]; then  # 130 is SIGINT
            echo -e "${RED}Error streaming logs (exit code: $exit_code)${NC}"
        fi
    }
    
    # Reset trap
    trap - INT
    
    # Check job completion status after logs
    echo -e "\n${YELLOW}Checking job status...${NC}"
    kubectl get job "$test_job_name" -n "$NAMESPACE"
    
    # Show final pod status
    echo -e "\n${YELLOW}Final Pod Status:${NC}"
    kubectl get pod "$pod_name" -n "$NAMESPACE" 2>/dev/null || echo "Pod terminated"
    
    echo -e "\n${GREEN}Test run completed!${NC}"
    echo -e "${YELLOW}To see full logs again:${NC}"
    echo -e "  ${CYAN}kubectl logs $pod_name -n $NAMESPACE${NC}"
}

# Upload data to S3
echo -e "\n${YELLOW}Uploading data to S3...${NC}"
if ! python3 "$REPO_ROOT/src/scripts/local/force_sync_s3_local_fs.py" --upload; then
    echo -e "${RED}Warning: S3 upload failed, continuing with deployment...${NC}"
fi

# Create namespace
echo -e "\n${YELLOW}Creating namespace if it doesn't exist...${NC}"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
DEPLOY_MODE=skip
# Direct Helm deployment (default mode)
if [ "${DEPLOY_MODE:-helm}" == "helm" ]; then
    echo -e "\n${GREEN}Deploying with Helm directly...${NC}"
    
    HELM_VALUES=$(create_helm_values)
    echo -e "${YELLOW}Using values:${NC}"
    cat "$HELM_VALUES"
    
    if helm upgrade --install "$RELEASE_NAME" "$HELM_DIR" \
      --namespace="$NAMESPACE" \
      --values="$HELM_VALUES" \
      --wait \
      --timeout 10m; then
        echo -e "\n${GREEN}Helm deployment complete!${NC}"
        echo -e "\n${YELLOW}CronJob Status:${NC}"
        kubectl get cronjobs -n "$NAMESPACE"
    else
        echo -e "\n${RED}Helm deployment failed!${NC}"
        echo -e "${YELLOW}Checking pod status...${NC}"
        kubectl get pods -n "$NAMESPACE" 2>/dev/null || echo "No pods found"
        echo -e "\n${YELLOW}Checking Helm release status...${NC}"
        helm status "$RELEASE_NAME" -n "$NAMESPACE" || true
        rm -f "$HELM_VALUES"
        exit 1
    fi
    
    rm -f "$HELM_VALUES"
fi

# Run test job if requested
if [ "${RUN_TEST:-false}" == "true" ]; then
    run_test_job
else
    echo -e "\n${YELLOW}Skipping test run. To run a test, use:${NC}"
    echo -e "  ${CYAN}RUN_TEST=true ./deploy-indexing.sh${NC}"
    echo -e "${YELLOW}Or manually trigger:${NC}"
    echo -e "  ${CYAN}kubectl create job --from=cronjob/$CRONJOB_NAME test-run-\$(date +%s) -n $NAMESPACE${NC}"
    echo -e "  ${CYAN}kubectl logs -f -n $NAMESPACE -l app.kubernetes.io/name=$CRONJOB_NAME${NC}"
fi

# Generate dynamic ArgoCD Application YAML
echo -e "\n${YELLOW}Generating ArgoCD Application YAML with dynamic values...${NC}"

mkdir -p "$ARGOCD_APP_DIR"

cat > "$ARGOCD_APP_DIR/indexing-cronjob-application.yaml" << EOF
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
    deployed-by: "$(whoami)"
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

echo -e "${GREEN}Generated ArgoCD Application: ${ARGOCD_APP_DIR}/indexing-cronjob-application.yaml${NC}"

# Apply ArgoCD Application if requested
if [ "${DEPLOY_MODE:-helm}" == "argocd" ] || [ "${APPLY_ARGOCD:-false}" == "true" ]; then
    echo -e "\n${YELLOW}Applying ArgoCD Application...${NC}"
    kubectl apply -f "$ARGOCD_APP_DIR/indexing-cronjob-application.yaml"
    
    echo -e "\n${GREEN}ArgoCD Application applied!${NC}"
    echo -e "To check status:"
    echo -e "  ${YELLOW}kubectl get application -n argocd indexing-cronjob${NC}"
    echo -e "  ${YELLOW}argocd app get indexing-cronjob${NC}"
else
    echo -e "\n${YELLOW}ArgoCD Application YAML generated but not applied.${NC}"
    echo -e "To apply manually:"
    echo -e "  ${YELLOW}kubectl apply -f ${ARGOCD_APP_DIR}/indexing-cronjob-application.yaml${NC}"
    echo -e "Or re-run with:"
    echo -e "  ${YELLOW}DEPLOY_MODE=argocd ./deploy-indexing.sh${NC}"
fi

echo -e "\n${GREEN}=========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"

# Quick reference
echo -e "\n${BLUE}Quick Commands:${NC}"
echo -e "  ${CYAN}# Check CronJob status${NC}"
echo -e "  kubectl get cronjobs -n $NAMESPACE"
echo -e ""
echo -e "  ${CYAN}# Run test job now${NC}"
echo -e "  RUN_TEST=true $0"
echo -e ""
echo -e "  ${CYAN}# Manual test run${NC}"
echo -e "  kubectl create job --from=cronjob/$CRONJOB_NAME test-run-\$(date +%s) -n $NAMESPACE"
echo -e "  kubectl logs -f -n $NAMESPACE -l app.kubernetes.io/name=$CRONJOB_NAME"
echo -e ""
echo -e "  ${CYAN}# Watch for scheduled jobs${NC}"
echo -e "  watch -n 5 kubectl get cronjobs,jobs,pods -n $NAMESPACE"