#!/bin/bash
set -euo pipefail

# ============================================
# Karpenter Dynamic Setup Script
# ============================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${BLUE}[STEP]${NC} ${YELLOW}$*${NC}"; }

# ============================================
# CONFIGURATION KNOBS
# ============================================

# ---- HUMAN-REQUIRED INPUTS ----
GH_REPO="${GH_REPO:-https://github.com/Athithya-Sakthivel/E2E-RAG-System.git}"
GH_BRANCH="${GH_BRANCH:-main}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

# ---- STRONG DEFAULTS ----
KARPENTER_NODE_CLASS_NAME="${KARPENTER_NODE_CLASS_NAME:-compute}"
KARPENTER_NODE_POOL_NAME="${KARPENTER_NODE_POOL_NAME:-compute}"
KARPENTER_TAINT_KEY="${KARPENTER_TAINT_KEY:-node-type}"
KARPENTER_TAINT_VALUE="${KARPENTER_TAINT_VALUE:-compute}"

INSTANCE_CATEGORIES="${INSTANCE_CATEGORIES:-c}"
INSTANCE_GENERATIONS="${INSTANCE_GENERATIONS:-3}"
EXCLUDED_INSTANCE_TYPES="${EXCLUDED_INSTANCE_TYPES:-t2,t3,t4g}"

SPOT_ENABLED="${SPOT_ENABLED:-false}"

CPU_LIMIT="${CPU_LIMIT:-50}"
MAX_NODE_COUNT="${MAX_NODE_COUNT:-10}"

CONSOLIDATION_ENABLED="${CONSOLIDATION_ENABLED:-true}"
CONSOLIDATION_POLICY="${CONSOLIDATION_POLICY:-WhenUnderutilized}"
TTL_SECONDS_UNTIL_EXPIRED="${TTL_SECONDS_UNTIL_EXPIRED:-259200}"
TTL_SECONDS_AFTER_EMPTY="${TTL_SECONDS_AFTER_EMPTY:-300}"

SUBNET_TAG_KEY="${SUBNET_TAG_KEY:-karpenter.sh/discovery}"
SG_TAG_KEY="${SG_TAG_KEY:-karpenter.sh/discovery}"

ROOT_VOLUME_SIZE="${ROOT_VOLUME_SIZE:-50Gi}"
ENCRYPTED_ROOT_VOLUME="${ENCRYPTED_ROOT_VOLUME:-true}"

# ---- COMPUTED PATHS ----
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
MANIFESTS_DIR="$REPO_ROOT/src/manifests/karpenter"
KARPENTER_APP_YAML="$ARGOCD_APP_DIR/karpenter-application.yaml"
NODECLASS_YAML="$MANIFESTS_DIR/01-nodeclass.yaml"
NODEPOOL_YAML="$MANIFESTS_DIR/02-nodepool.yaml"

# ============================================
# PRE-FLIGHT CHECKS (common)
# ============================================

check_prereqs() {
  command -v aws      >/dev/null 2>&1 || { log_error "aws CLI required"; exit 1; }
  command -v tofu     >/dev/null 2>&1 || { log_error "tofu required"; exit 1; }
  command -v kubectl  >/dev/null 2>&1 || { log_error "kubectl required"; exit 1; }
  command -v jq       >/dev/null 2>&1 || { log_error "jq required"; exit 1; }
  command -v git      >/dev/null 2>&1 || { log_error "git required"; exit 1; }
}

# ============================================
# ROLLOUT: Full setup
# ============================================

do_rollout() {
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Karpenter Rollout${NC}"
  echo -e "${GREEN}=========================================${NC}"

  check_prereqs

  # 1. Fetch Terraform outputs
  log_step "1/4 Fetching Terraform outputs"
  CLUSTER_NAME=$(tofu -chdir="$TOFU_DIR" output -raw cluster_name)
  KARPENTER_ROLE_ARN=$(tofu -chdir="$TOFU_DIR" output -raw karpenter_controller_role_arn)
  KARPENTER_NODE_ROLE_ARN=$(tofu -chdir="$TOFU_DIR" output -raw karpenter_node_role_arn)
  KARPENTER_NODE_ROLE=$(basename "$KARPENTER_NODE_ROLE_ARN")
  SG_ID=$(tofu -chdir="$TOFU_DIR" output -raw eks_cluster_security_group_id)

  log_info "Cluster:            $CLUSTER_NAME"
  log_info "Karpenter Role:     $KARPENTER_ROLE_ARN"
  log_info "Karpenter Node Role: $KARPENTER_NODE_ROLE"
  log_info "Security Group:     $SG_ID"

  # 3. Render YAMLs
  log_step "2/4 Rendering Karpenter YAML manifests"
  mkdir -p "$ARGOCD_APP_DIR" "$MANIFESTS_DIR"

  # ArgoCD Application
  cat > "$KARPENTER_APP_YAML" << EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: karpenter
  namespace: argocd
spec:
  project: default
  destination:
    server: https://kubernetes.default.svc
    namespace: karpenter
  sources:
    - repoURL: public.ecr.aws/karpenter
      chart: karpenter
      targetRevision: 1.9.0
      helm:
        values: |
          serviceAccount:
            create: true
            name: karpenter
            annotations:
              eks.amazonaws.com/role-arn: ${KARPENTER_ROLE_ARN}
          settings:
            clusterName: ${CLUSTER_NAME}
            clusterEndpoint: $(tofu -chdir="$TOFU_DIR" output -raw eks_cluster_endpoint)
    - repoURL: ${GH_REPO}
      targetRevision: ${GH_BRANCH}
      path: src/manifests/karpenter
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
EOF
  log_info "Generated: $KARPENTER_APP_YAML"

  # EC2NodeClass (AL2023)
  cat > "$NODECLASS_YAML" << EOF
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: ${KARPENTER_NODE_CLASS_NAME}
spec:
  amiFamily: AL2023
  amiSelectorTerms:
    - alias: al2023@latest
  role: ${KARPENTER_NODE_ROLE}
  subnetSelectorTerms:
    - tags:
        ${SUBNET_TAG_KEY}: ${CLUSTER_NAME}
  securityGroupSelectorTerms:
    - tags:
        ${SG_TAG_KEY}: ${CLUSTER_NAME}
  tags:
    karpenter.sh/discovery: ${CLUSTER_NAME}
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: ${ROOT_VOLUME_SIZE}
        volumeType: gp3
        encrypted: ${ENCRYPTED_ROOT_VOLUME}
        deleteOnTermination: true
EOF
  log_info "Generated: $NODECLASS_YAML"

  # NodePool
  cat > "$NODEPOOL_YAML" << EOF
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ${KARPENTER_NODE_POOL_NAME}
spec:
  template:
    metadata:
      labels:
        node-type: ${KARPENTER_TAINT_VALUE}
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: ${KARPENTER_NODE_CLASS_NAME}
      taints:
        - key: ${KARPENTER_TAINT_KEY}
          value: ${KARPENTER_TAINT_VALUE}
          effect: NoSchedule
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values:
            - amd64
        - key: kubernetes.io/os
          operator: In
          values:
            - linux
        - key: karpenter.sh/capacity-type
          operator: In
          values:
            - $([ "$SPOT_ENABLED" = "true" ] && echo "spot" || echo "on-demand")
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values:
$(echo "$INSTANCE_CATEGORIES" | tr ',' '\n' | sed 's/^/            - /')
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values:
            - "$((INSTANCE_GENERATIONS - 1))"
        - key: karpenter.k8s.aws/instance-family
          operator: NotIn
          values:
$(echo "$EXCLUDED_INSTANCE_TYPES" | tr ',' '\n' | sed 's/^/            - /')
  limits:
    cpu: "${CPU_LIMIT}"
EOF
  log_info "Generated: $NODEPOOL_YAML"

  # 4. Git commit & push
  log_step "3/4 Committing and pushing to Git"
  cd "$REPO_ROOT"
  if ! git diff --quiet HEAD -- "$KARPENTER_APP_YAML" "$NODECLASS_YAML" "$NODEPOOL_YAML"; then
    git add "$KARPENTER_APP_YAML" "$NODECLASS_YAML" "$NODEPOOL_YAML"
    git commit -m "chore: update karpenter manifests with AL2023"
    git push origin "$GH_BRANCH"
    log_info "Changes pushed to $GH_BRANCH"
  else
    log_info "No changes to commit"
  fi

  # 5. Apply ArgoCD app
  log_step "4/4 Applying ArgoCD Application"
  kubectl apply -f "$KARPENTER_APP_YAML"
  log_info "Waiting for ArgoCD to sync Karpenter (30s)..."
  sleep 30

  if kubectl get deployment -n karpenter karpenter &>/dev/null; then
    log_info "Karpenter controller deployment found"
    kubectl get deployment -n karpenter karpenter
  else
    log_warn "Karpenter deployment not yet visible. Check ArgoCD sync status."
  fi

  echo -e "\n${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Karpenter Setup Complete${NC}"
  echo -e "${GREEN}=========================================${NC}"
  echo -e "Cluster:        ${CLUSTER_NAME}"
  echo -e "AMI Family:     AL2023 (latest)"
  echo -e "Node Class:     ${KARPENTER_NODE_CLASS_NAME}"
  echo -e "Node Pool:      ${KARPENTER_NODE_POOL_NAME}"
  echo -e "Instance Types: ${INSTANCE_CATEGORIES}-family, gen ${INSTANCE_GENERATIONS}+"
}

# ============================================
# DELETE: Remove Karpenter from cluster
# ============================================


do_delete() {
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Deleting Karpenter${NC}"
  echo -e "${GREEN}=========================================${NC}"
  check_prereqs

  # 1. Delete all NodeClaims (this terminates the EC2 instances)
  log_step "Deleting all NodeClaims"
  kubectl delete nodeclaims --all --ignore-not-found=true || true
  log_info "NodeClaims deleted (EC2 instances terminating)"

  # 2. Delete NodePools
  log_step "Deleting all NodePools"
  kubectl delete nodepools --all --ignore-not-found=true || true
  log_info "NodePools deleted"

  # 3. Delete EC2NodeClasses
  log_step "Deleting all EC2NodeClasses"
  kubectl delete ec2nodeclasses --all --ignore-not-found=true || true
  log_info "EC2NodeClasses deleted"

  # 4. Remove ArgoCD Application
  log_step "Removing ArgoCD Application"
  kubectl delete -f "$KARPENTER_APP_YAML" --ignore-not-found=true || true
  log_info "ArgoCD Application deleted"

  # 5. Delete karpenter namespace
  log_step "Deleting karpenter namespace"
  kubectl delete namespace karpenter --ignore-not-found=true --timeout=60s || true
  log_info "Namespace karpenter deleted"

  # 6. Remove Karpenter CRDs (optional - comment out if other tools use them)
  log_step "Removing Karpenter CRDs"
  kubectl get crd -o name | grep karpenter | xargs -r kubectl delete --ignore-not-found=true || true
  log_info "CRDs deleted"

  log_info "Karpenter uninstallation complete."
}


# ============================================
# INSPECT: Show Karpenter status
# ============================================

do_inspect() {
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Karpenter Inspection${NC}"
  echo -e "${GREEN}=========================================${NC}"
  check_prereqs

  log_step "NodePools"
  kubectl get nodepools -A 2>/dev/null || echo "No NodePools found"

  log_step "EC2NodeClasses"
  kubectl get ec2nodeclasses -A 2>/dev/null || echo "No EC2NodeClasses found"

  log_step "Karpenter controller deployment"
  kubectl get deployment -n karpenter karpenter 2>/dev/null || echo "Deployment not found in karpenter namespace"

  log_step "Recent events (karpenter namespace)"
  kubectl get events -n karpenter --sort-by='.lastTimestamp' 2>/dev/null | tail -20 || true

  log_step "Controller logs (last 20 lines)"
  kubectl logs -n karpenter deployment/karpenter --tail=20 2>/dev/null || echo "Unable to fetch logs"
}

# ============================================
# MAIN: Argument parsing
# ============================================

usage() {
  echo "Usage: $0 {--rollout|--delete|--inspect}"
  exit 1
}

if [ $# -eq 0 ]; then
  usage
fi

case "$1" in
  --rollout)
    do_rollout
    ;;
  --delete)
    do_delete
    ;;
  --inspect)
    do_inspect
    ;;
  *)
    echo "Unknown option: $1"
    usage
    ;;
esac