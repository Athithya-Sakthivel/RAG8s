#!/bin/bash
set -euo pipefail

REPO_ROOT=$(pwd)
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
HELM_DIR="$REPO_ROOT/src/infra/helm/indexing_cronjob"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
NAMESPACE="indexing"
RELEASE_NAME="indexing-pipeline"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Upload data to S3
echo -e "\n${YELLOW}Uploading data to S3...${NC}"
if ! python3 "$REPO_ROOT/src/scripts/local/force_sync_s3_local_fs.py" --upload; then
    echo -e "${RED}Warning: S3 upload failed, continuing with deployment...${NC}"
fi

# Create namespace
echo -e "\n${YELLOW}Creating namespace if it doesn't exist...${NC}"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

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
        kubectl get pods -n "$NAMESPACE"
    else
        echo -e "\n${RED}Helm deployment failed!${NC}"
        echo -e "${YELLOW}Checking pod status...${NC}"
        kubectl get pods -n "$NAMESPACE"
        echo -e "\n${YELLOW}Checking Helm release status...${NC}"
        helm status "$RELEASE_NAME" -n "$NAMESPACE" || true
        rm -f "$HELM_VALUES"
        exit 1
    fi
    
    rm -f "$HELM_VALUES"
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
DEPLOY_MODE=argocd

# Apply ArgoCD Application if requested
# Direct Helm deployment (default mode)
if [ "${DEPLOY_MODE:-helm}" == "helm" ]; then
    echo -e "\n${GREEN}Deploying with Helm directly...${NC}"
    
    HELM_VALUES=$(create_helm_values)
    echo -e "${YELLOW}Using values:${NC}"
    cat "$HELM_VALUES"
    
    # Create namespace first (since we removed it from the chart)
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    
    if helm upgrade --install "$RELEASE_NAME" "$HELM_DIR" \
      --namespace="$NAMESPACE" \
      --values="$HELM_VALUES" \
      --wait \
      --timeout 10m; then
        echo -e "\n${GREEN}Helm deployment complete!${NC}"
        kubectl get pods -n "$NAMESPACE"
    else
        echo -e "\n${RED}Helm deployment failed!${NC}"
        echo -e "${YELLOW}Checking pod status...${NC}"
        kubectl get pods -n "$NAMESPACE"
        echo -e "\n${YELLOW}Checking Helm release status...${NC}"
        helm status "$RELEASE_NAME" -n "$NAMESPACE" || true
        rm -f "$HELM_VALUES"
        exit 1
    fi
    
    rm -f "$HELM_VALUES"
fi

echo -e "\n${GREEN}=========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"