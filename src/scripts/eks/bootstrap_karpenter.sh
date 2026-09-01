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

GH_REPO="${GH_REPO:-https://github.com/Athithya-Sakthivel/E2E-RAG-System.git}"
GH_BRANCH="${GH_BRANCH:-main}"
AWS_REGION="${AWS_REGION:-ap-south-1}"

KARPENTER_NODE_CLASS_NAME="${KARPENTER_NODE_CLASS_NAME:-compute}"
KARPENTER_NODE_POOL_NAME="${KARPENTER_NODE_POOL_NAME:-compute}"
KARPENTER_TAINT_KEY="${KARPENTER_TAINT_KEY:-node-type}"
KARPENTER_TAINT_VALUE="${KARPENTER_TAINT_VALUE:-compute}"

INSTANCE_CATEGORIES="${INSTANCE_CATEGORIES:-c}"
INSTANCE_GENERATIONS="${INSTANCE_GENERATIONS:-3}"
EXCLUDED_INSTANCE_TYPES="${EXCLUDED_INSTANCE_TYPES:-t2,t3,t4g}"

SPOT_ENABLED="${SPOT_ENABLED:-true}"

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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TOFU_DIR="$REPO_ROOT/src/infra/terraform/aws"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
MANIFESTS_DIR="$REPO_ROOT/src/manifests/karpenter"
KARPENTER_APP_YAML="$ARGOCD_APP_DIR/karpenter-application.yaml"
NODECLASS_YAML="$MANIFESTS_DIR/01-nodeclass.yaml"
NODEPOOL_YAML="$MANIFESTS_DIR/02-nodepool.yaml"

# ============================================
# HELPERS
# ============================================

check_prereqs() {
  command -v aws      >/dev/null 2>&1 || { log_error "aws CLI required"; exit 1; }
  command -v tofu     >/dev/null 2>&1 || { log_error "tofu required"; exit 1; }
  command -v kubectl  >/dev/null 2>&1 || { log_error "kubectl required"; exit 1; }
  command -v jq       >/dev/null 2>&1 || { log_error "jq required"; exit 1; }
  command -v git      >/dev/null 2>&1 || { log_error "git required"; exit 1; }
}

wait_for_resource() {
  local resource_type="$1"
  local resource_name="$2"
  local namespace="${3:-}"
  local timeout="${4:-120}"

  local ns_flag=""
  [ -n "$namespace" ] && ns_flag="-n $namespace"

  local start_time
  start_time=$(date +%s)

  while true; do
    if kubectl get "${resource_type}/${resource_name}" ${ns_flag} -o name &>/dev/null; then
      return 0
    fi
    local now
    now=$(date +%s)
    if [ $((now - start_time)) -ge "$timeout" ]; then
      return 1
    fi
    sleep 5
  done
}

wait_for_condition() {
  local resource_type="$1"
  local resource_name="$2"
  local condition="$3"
  local timeout="${4:-120}"
  local namespace="${5:-}"

  local ns_flag=""
  [ -n "$namespace" ] && ns_flag="-n $namespace"

  if kubectl wait --for="condition=${condition}" "${resource_type}/${resource_name}" ${ns_flag} --timeout="${timeout}s" 2>/dev/null; then
    return 0
  else
    return 1
  fi
}

diagnose_failure() {
  log_warn "Diagnosing Karpenter issues..."

  echo ""
  log_info "--- NodeClass Status ---"
  kubectl get ec2nodeclass "${KARPENTER_NODE_CLASS_NAME}" -o yaml 2>/dev/null | grep -A20 "status:" || echo "No NodeClass found"

  echo ""
  log_info "--- NodePool Status ---"
  kubectl get nodepool "${KARPENTER_NODE_POOL_NAME}" -o yaml 2>/dev/null | grep -A20 "status:" || echo "No NodePool found"

  echo ""
  log_info "--- Karpenter Controller Logs (errors only) ---"
  kubectl logs -n karpenter deployment/karpenter --tail=50 2>/dev/null | grep -i "error\|failed\|unable" | tail -10 || echo "No errors found"

  echo ""
  log_info "--- Recent Events ---"
  kubectl get events -n karpenter --sort-by='.lastTimestamp' 2>/dev/null | tail -15 || echo "No events found"

  echo ""
  log_info "--- Subnet Tags Check ---"
  local cluster_name
  cluster_name=$(tofu -chdir="$TOFU_DIR" output -raw cluster_name 2>/dev/null || echo "")
  if [ -n "$cluster_name" ]; then
    local subnet_count
    subnet_count=$(aws ec2 describe-subnets \
      --filters "Name=tag:karpenter.sh/discovery,Values=${cluster_name}" \
      --query 'length(Subnets)' --output text 2>/dev/null || echo "0")
    if [ "$subnet_count" -gt 0 ]; then
      log_info "Subnets tagged: $subnet_count"
    else
      log_error "No subnets have karpenter.sh/discovery=${cluster_name} tag!"
    fi

    local sg_count
    sg_count=$(aws ec2 describe-security-groups \
      --filters "Name=tag:karpenter.sh/discovery,Values=${cluster_name}" \
      --query 'length(SecurityGroups)' --output text 2>/dev/null || echo "0")
    if [ "$sg_count" -gt 0 ]; then
      log_info "Security groups tagged: $sg_count"
    else
      log_error "No security groups have karpenter.sh/discovery=${cluster_name} tag!"
    fi
  fi

  echo ""
  log_info "--- IAM Role SSM Permission Check ---"
  local role_name
  role_name=$(tofu -chdir="$TOFU_DIR" output -raw karpenter_controller_role_arn 2>/dev/null | xargs basename || echo "")
  if [ -n "$role_name" ]; then
    aws iam list-role-policies --role-name "$role_name" --query 'PolicyNames' --output table 2>/dev/null || echo "Cannot list role policies"
    aws iam list-attached-role-policies --role-name "$role_name" --query 'AttachedPolicies[*].PolicyName' --output table 2>/dev/null || echo "Cannot list attached policies"
  fi
}

# ============================================
# ROLLOUT
# ============================================


do_rollout() {
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Karpenter Rollout${NC}"
  echo -e "${GREEN}=========================================${NC}"

  check_prereqs

  # 1. Fetch Terraform outputs
  log_step "1/5 Fetching Terraform outputs"
  CLUSTER_NAME=$(tofu -chdir="$TOFU_DIR" output -raw cluster_name)
  KARPENTER_ROLE_ARN=$(tofu -chdir="$TOFU_DIR" output -raw karpenter_controller_role_arn)
  KARPENTER_NODE_ROLE_ARN=$(tofu -chdir="$TOFU_DIR" output -raw karpenter_node_role_arn)
  KARPENTER_NODE_ROLE=$(basename "$KARPENTER_NODE_ROLE_ARN")
  SG_ID=$(tofu -chdir="$TOFU_DIR" output -raw eks_cluster_security_group_id)

  log_info "Cluster:            $CLUSTER_NAME"
  log_info "Karpenter Role:     $KARPENTER_ROLE_ARN"
  log_info "Karpenter Node Role: $KARPENTER_NODE_ROLE"
  log_info "Security Group:     $SG_ID"

  # 2. Render YAMLs
  log_step "2/5 Rendering Karpenter YAML manifests"
  mkdir -p "$ARGOCD_APP_DIR" "$MANIFESTS_DIR"

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

  # Pre-compute values to avoid subshell issues with set -e inside heredoc
  local instance_cat_values
  instance_cat_values=$(echo "$INSTANCE_CATEGORIES" | tr ',' '\n' | sed 's/^/            - /')
  local excluded_family_values
  excluded_family_values=$(echo "$EXCLUDED_INSTANCE_TYPES" | tr ',' '\n' | sed 's/^/            - /')
  local spot_value
  spot_value=$([ "$SPOT_ENABLED" = "true" ] && echo "spot" || echo "on-demand")

  cat > "$NODEPOOL_YAML" << EOF
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: ${KARPENTER_NODE_POOL_NAME}
spec:
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 15m
    budgets:
    - nodes: "50%"
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
            - ${spot_value}
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values:
${instance_cat_values}
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values:
            - "$((INSTANCE_GENERATIONS - 1))"
        - key: karpenter.k8s.aws/instance-family
          operator: NotIn
          values:
${excluded_family_values}
  limits:
    cpu: "${CPU_LIMIT}"
EOF
  log_info "Generated: $NODEPOOL_YAML"

  # 3. Git commit & push
  log_step "3/5 Committing and pushing to Git"
  cd "$REPO_ROOT"

  git add "$KARPENTER_APP_YAML" "$NODECLASS_YAML" "$NODEPOOL_YAML"

  if ! git diff --cached --quiet; then
    git commit -m "chore: update karpenter manifests with AL2023"
    git push origin "$GH_BRANCH"
    log_info "Changes pushed to $GH_BRANCH"
  else
    log_info "No changes to commit"
  fi

  # 4. Apply ArgoCD Application (creates or updates the ArgoCD app object)
  log_step "4/5 Applying ArgoCD Application"
  kubectl apply -f "$KARPENTER_APP_YAML"

  # 5. Force ArgoCD to sync (clears any stuck failed state and triggers a fresh sync)
  log_step "5/5 Forcing ArgoCD sync"
  # Create the karpenter namespace first to prevent RBAC reconciliation race condition
  kubectl create namespace karpenter --dry-run=client -o yaml | kubectl apply -f -
  # Wait briefly for ArgoCD to process the application
  sleep 5

  # Clear any stuck operation state that would block a new sync
  kubectl patch application karpenter -n argocd --type='json' \
    -p='[{"op": "remove", "path": "/status/operationState"}]' 2>/dev/null || true

  # Trigger a manual sync with prune and ServerSideApply
  kubectl patch application karpenter -n argocd --type='merge' -p '{
    "operation": {
      "sync": {
        "revision": "'"${GH_BRANCH}"'",
        "prune": true,
        "syncOptions": ["ServerSideApply=true"]
      }
    }
  }'

  log_info "Sync triggered. Waiting for resources to become ready..."

  # --- Wait for controller deployment to exist ---
  log_info "Waiting for Karpenter deployment to be created (up to 60s)..."
  if ! wait_for_resource "deployment" "karpenter" "karpenter" 60; then
    log_error "Karpenter deployment was not created"
    diagnose_failure
    exit 1
  fi

  # --- Wait for deployment to become available ---
  log_info "Waiting for Karpenter controller deployment to become available (up to 120s)..."
  if wait_for_condition "deployment" "karpenter" "available" 120 "karpenter"; then
    log_info "Karpenter controller is available"
    kubectl get deployment -n karpenter karpenter
  else
    log_error "Karpenter controller did not become available"
    diagnose_failure
    exit 1
  fi

  # --- Wait for NodeClass ---
  log_info "Waiting for EC2NodeClass '${KARPENTER_NODE_CLASS_NAME}' to be ready (up to 180s)..."
  if wait_for_condition "ec2nodeclass" "${KARPENTER_NODE_CLASS_NAME}" "ready" 180; then
    log_info "EC2NodeClass is ready"
  else
    log_error "EC2NodeClass did not become ready"
    diagnose_failure
    exit 1
  fi

  # --- Wait for NodePool ---
  log_info "Waiting for NodePool '${KARPENTER_NODE_POOL_NAME}' to exist (up to 120s)..."
  if ! wait_for_resource "nodepool" "${KARPENTER_NODE_POOL_NAME}" "" 120; then
    log_error "NodePool resource was not created – ArgoCD sync may have failed"
    diagnose_failure
    exit 1
  fi

  log_info "Waiting for NodePool '${KARPENTER_NODE_POOL_NAME}' to be ready (up to 60s)..."
  if wait_for_condition "nodepool" "${KARPENTER_NODE_POOL_NAME}" "ready" 60; then
    log_info "NodePool is ready"
  else
    log_warn "NodePool not ready yet – check ArgoCD sync status"
  fi

  # --- Final status ---
  echo -e "\n${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Karpenter Setup Complete${NC}"
  echo -e "${GREEN}=========================================${NC}"
  echo -e "Cluster:        ${CLUSTER_NAME}"
  echo -e "AMI Family:     AL2023 (latest)"
  echo -e "Node Class:     ${KARPENTER_NODE_CLASS_NAME}"
  echo -e "Node Pool:      ${KARPENTER_NODE_POOL_NAME}"
  echo -e "Instance Types: ${INSTANCE_CATEGORIES}-family, gen ${INSTANCE_GENERATIONS}+"
  echo -e ""
  echo -e "Current Status:"
  kubectl get nodepools,ec2nodeclasses -A
  echo -e ""
  echo -e "To verify node provisioning, deploy a test pod or run:"
  echo -e "  kubectl logs -n karpenter deployment/karpenter --tail=20"
}


# ============================================
# DELETE
# ============================================

do_delete() {
  echo -e "${GREEN}=========================================${NC}"
  echo -e "${GREEN}  Deleting Karpenter${NC}"
  echo -e "${GREEN}=========================================${NC}"
  check_prereqs

  log_step "Deleting all NodeClaims (EC2 instances will terminate)"
  kubectl delete nodeclaims --all --ignore-not-found=true --timeout=120s || true
  log_info "NodeClaims deleted"

  log_step "Deleting all NodePools"
  kubectl delete nodepools --all --ignore-not-found=true || true
  log_info "NodePools deleted"

  log_step "Deleting all EC2NodeClasses"
  kubectl delete ec2nodeclasses --all --ignore-not-found=true || true
  log_info "EC2NodeClasses deleted"

  log_step "Removing ArgoCD Application"
  kubectl delete -f "$KARPENTER_APP_YAML" --ignore-not-found=true || true
  log_info "ArgoCD Application deleted"

  log_step "Deleting karpenter namespace"
  kubectl delete namespace karpenter --ignore-not-found=true --timeout=60s || true
  log_info "Namespace karpenter deleted"

  log_step "Removing Karpenter CRDs"
  kubectl get crd -o name 2>/dev/null | grep karpenter | xargs -r kubectl delete --ignore-not-found=true || true
  log_info "CRDs deleted"

  log_info "Karpenter uninstallation complete."
}

# ============================================
# INSPECT
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

  log_step "NodeClaims"
  kubectl get nodeclaims -A 2>/dev/null || echo "No NodeClaims found"

  log_step "Karpenter controller deployment"
  kubectl get deployment -n karpenter karpenter 2>/dev/null || echo "Deployment not found"

  log_step "Karpenter pods"
  kubectl get pods -n karpenter 2>/dev/null || echo "No pods found"

  log_step "Recent events (karpenter namespace)"
  kubectl get events -n karpenter --sort-by='.lastTimestamp' 2>/dev/null | tail -20 || true

  log_step "Controller logs (last 30 lines)"
  kubectl logs -n karpenter deployment/karpenter --tail=30 2>/dev/null || echo "Unable to fetch logs"
}

# ============================================
# MAIN
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
