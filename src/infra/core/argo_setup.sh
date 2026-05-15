#!/usr/bin/env bash
# bash src/infra/core/argo_setup.sh --rollout
# bash src/infra/core/argo_setup.sh --delete --confirm
# PRIVATE_REPO=true GIT_PAT="ghp_xxx" bash src/infra/core/argo_setup.sh --rollout
# CONTROLLER_REPLICAS=2 bash src/infra/core/argo_setup.sh --rollout

set -euo pipefail
IFS=$'\n\t'

CHART_REPO_NAME="argo"
CHART_REPO_URL="https://argoproj.github.io/argo-helm"
CHART_NAME="argo/argo-cd"
CHART_VERSION="9.5.4"
ARGOCD_APP_VERSION="v3.3.8"
NAMESPACE="argocd"
VALUES_FILE="/tmp/argocd.yaml"
TIMEOUT="10m"
TMPDIR="$(mktemp -d)"

# ---- REPOSITORY CONFIGURATION ----
# Default to false to match usage/help and avoid accidental secret creation.
PRIVATE_REPO="${PRIVATE_REPO:-true}"
GIT_PAT="${GIT_PAT:-}"
GH_REPO="${GH_REPO:-https://github.com/Athithya-Sakthivel/E2E-RAG-System.git}"

# ---- ARGOCD COMPONENT SCALING ----
CONTROLLER_REPLICAS="${CONTROLLER_REPLICAS:-2}"

MODE=""
CONFIRM="no"

usage() {
  cat <<EOF
Usage: $0 --rollout | --delete [--confirm]

Modes:
  --rollout     Install/upgrade Argo CD with Helm.
  --delete      Delete Argo CD control plane and Argo CD CRs without pruning managed workloads. Requires --confirm.

Environment Variables:
  PRIVATE_REPO           Set to "true" to configure private repo access (default: false)
  GIT_PAT                GitHub Personal Access Token (required if PRIVATE_REPO=true)
  GH_REPO                GitHub repository URL (default: https://github.com/Athithya-Sakthivel/E2E-RAG-System.git)
  CONTROLLER_REPLICAS    Number of application-controller replicas (default: 2)

Examples:
  # Public repo with defaults
  $0 --rollout

  # Private repo
  PRIVATE_REPO=true GIT_PAT="ghp_xxxxxxxxxxxx" $0 --rollout

  # HA controller
  CONTROLLER_REPLICAS=2 $0 --rollout

  # Delete
  $0 --delete --confirm
EOF
  exit 1
}

log()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m%s\033[0m\n' "$*"; }

cleanup() {
  local rc=$?
  if [[ -d "${TMPDIR}" ]]; then
    rm -rf "${TMPDIR}"
  fi
  exit "${rc}"
}
trap cleanup EXIT INT TERM

require_cmds() {
  local miss=0
  for c in kubectl helm curl jq base64; do
    if ! command -v "${c}" >/dev/null 2>&1; then
      err "Required command not found: ${c}"
      miss=1
    fi
  done

  if [[ ${miss} -ne 0 ]]; then
    exit 1
  fi
}

validate_git_pat() {
  local pat="$1"

  if [[ ! "$pat" =~ ^(ghp_|github_pat_) ]]; then
    err "Invalid GIT_PAT format. Expected a GitHub token starting with 'ghp_' or 'github_pat_'."
    return 1
  fi

  if [[ ${#pat} -lt 40 ]]; then
    err "GIT_PAT seems too short (${#pat} chars)."
    return 1
  fi

  return 0
}

configure_private_repo() {
  local repo_url="$1"
  local pat="$2"

  log "Configuring private repository access for: ${repo_url}"

  if [[ -z "$pat" ]]; then
    err "GIT_PAT is required when PRIVATE_REPO=true"
    return 1
  fi

  validate_git_pat "$pat" || return 1

  kubectl delete secret private-repo-creds -n "$NAMESPACE" --ignore-not-found=true >/dev/null 2>&1 || true

  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: private-repo-creds
  namespace: ${NAMESPACE}
  labels:
    argocd.argoproj.io/secret-type: repository
type: Opaque
stringData:
  type: git
  url: ${repo_url}
  username: git
  password: ${pat}
EOF

  if kubectl get secret private-repo-creds -n "$NAMESPACE" >/dev/null 2>&1; then
    log "Repository secret 'private-repo-creds' created successfully"
  else
    err "Failed to create repository secret"
    return 1
  fi

  log "Private repository configured successfully"
  return 0
}

write_values() {
  mkdir -p "$(dirname "${VALUES_FILE}")"

  cat > "${VALUES_FILE}" <<EOF
server:
  service:
    type: ClusterIP
  resources:
    requests:
      cpu: "150m"
      memory: "256Mi"
    limits:
      cpu: "500m"
      memory: "750Mi"

configs:
  params:
    server.insecure: "true"
  resourceTrackingMethod: "annotation"

controller:
  replicas: ${CONTROLLER_REPLICAS}
  resources:
    requests:
      cpu: "150m"
      memory: "256Mi"
    limits:
      cpu: "500m"
      memory: "1Gi"

repoServer:
  replicas: 1
  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "400m"
      memory: "700Mi"
  cache:
    enabled: false

dex:
  enabled: false

redis:
  enabled: true
  resources:
    requests:
      cpu: "50m"
      memory: "128Mi"
    limits:
      cpu: "200m"
      memory: "256Mi"

# Keep CRDs managed by the chart to avoid brittle external kustomize fetches.
crds:
  install: true

resources:
  requests:
    cpu: "50m"
    memory: "64Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"

rbac:
  create: true
EOF
}

wait_for_rollout() {
  local kind="$1"
  local name="$2"

  if kubectl -n "$NAMESPACE" get "${kind}/${name}" >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" rollout status "${kind}/${name}" --timeout="${TIMEOUT}" || warn "${kind}/${name} rollout check timed out"
    return 0
  fi

  return 1
}

wait_for_application_controller() {
  if kubectl -n "$NAMESPACE" get statefulset/argocd-application-controller >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" rollout status statefulset/argocd-application-controller --timeout="${TIMEOUT}" || warn "argocd-application-controller rollout check timed out"
    return 0
  fi

  if kubectl -n "$NAMESPACE" get deployment/argocd-application-controller >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" rollout status deployment/argocd-application-controller --timeout="${TIMEOUT}" || warn "argocd-application-controller rollout check timed out"
    return 0
  fi

  warn "argocd-application-controller workload not found"
  return 1
}

wait_for_absent() {
  local resource="$1"
  local timeout_seconds="${2:-180}"
  local interval=2
  local elapsed=0

  while (( elapsed < timeout_seconds )); do
    if ! kubectl get "${resource}" -A -o name 2>/dev/null | grep -q .; then
      return 0
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
  done

  return 1
}

patch_all_finalizers() {
  local resource="$1"
  local items

  items="$(kubectl get "${resource}" -A -o json 2>/dev/null | jq -r '.items[]? | [.metadata.namespace, .metadata.name] | @tsv' || true)"
  [[ -z "${items}" ]] && return 0

  while IFS=$'\t' read -r ns name; do
    [[ -z "${ns}" || -z "${name}" ]] && continue
    kubectl patch -n "${ns}" "${resource}" "${name}" --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
  done <<< "${items}"
}

delete_argo_crs_without_pruning() {
  log "Removing finalizers from ApplicationSets"
  patch_all_finalizers "applicationsets.argoproj.io"

  log "Deleting ApplicationSets without cascading workloads"
  kubectl delete applicationsets.argoproj.io \
    --all \
    --all-namespaces \
    --cascade=orphan \
    --wait=false \
    --ignore-not-found >/dev/null 2>&1 || true

  wait_for_absent "applicationsets.argoproj.io" 120 || warn "ApplicationSets still present after wait; continuing"

  log "Removing finalizers from Applications"
  patch_all_finalizers "applications.argoproj.io"

  log "Deleting Applications without pruning managed workloads"
  kubectl delete applications.argoproj.io \
    --all \
    --all-namespaces \
    --cascade=orphan \
    --wait=false \
    --ignore-not-found >/dev/null 2>&1 || true

  wait_for_absent "applications.argoproj.io" 300 || warn "Applications still present after wait; continuing"

  log "Removing finalizers from AppProjects"
  patch_all_finalizers "appprojects.argoproj.io"

  log "Deleting AppProjects"
  kubectl delete appprojects.argoproj.io \
    --all \
    --all-namespaces \
    --cascade=orphan \
    --wait=false \
    --ignore-not-found >/dev/null 2>&1 || true

  wait_for_absent "appprojects.argoproj.io" 120 || warn "AppProjects still present after wait; continuing"
}

delete_namespace_safely() {
  if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
    log "Deleting namespace ${NAMESPACE}"
    kubectl delete namespace "${NAMESPACE}" --wait=false --ignore-not-found >/dev/null 2>&1 || true

    local i
    for i in {1..30}; do
      if ! kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
        log "Namespace ${NAMESPACE} deleted"
        return 0
      fi
      sleep 10
    done

    warn "Namespace ${NAMESPACE} still terminating; attempting to remove namespace finalizers"
    kubectl get namespace "${NAMESPACE}" -o json \
      | jq '.spec.finalizers=[]' \
      | kubectl replace --raw "/api/v1/namespaces/${NAMESPACE}/finalize" -f - >/dev/null 2>&1 || warn "Namespace finalizer removal failed"
  else
    warn "Namespace ${NAMESPACE} not found; skipping namespace deletion"
  fi
}

delete_crds_last() {
  local crds=(
    applications.argoproj.io
    applicationsets.argoproj.io
    appprojects.argoproj.io
    argocdextensions.argoproj.io
  )

  log "Deleting Argo CD CRDs last"
  kubectl delete crd "${crds[@]}" --wait=false --ignore-not-found >/dev/null 2>&1 || true

  local i
  for i in {1..30}; do
    if ! kubectl get crd 2>/dev/null | grep -E '^(applications\.argoproj\.io|applicationsets\.argoproj\.io|appprojects\.argoproj\.io|argocdextensions\.argoproj\.io)\b' >/dev/null; then
      log "Argo CD CRDs deleted"
      return 0
    fi
    sleep 2
  done

  warn "Some Argo CD CRDs may still be terminating; continuing"
}

do_rollout() {
  require_cmds
  write_values

  log "Adding/updating Helm repo ${CHART_REPO_NAME}"
  helm repo add "${CHART_REPO_NAME}" "${CHART_REPO_URL}" >/dev/null 2>&1 || true
  helm repo update >/dev/null

  log "Installing/upgrading Helm chart ${CHART_NAME} (version ${CHART_VERSION})"
  helm upgrade --install argocd "${CHART_NAME}" \
    --version "${CHART_VERSION}" \
    -n "${NAMESPACE}" \
    --create-namespace \
    -f "${VALUES_FILE}" \
    --set crds.install=true \
    --wait \
    --atomic \
    --timeout "${TIMEOUT}"

  log "Waiting for core workloads"
  wait_for_rollout deployment argocd-server
  wait_for_rollout deployment argocd-repo-server
  wait_for_application_controller

  log "Argo CD rollout complete. Pods:"
  kubectl -n "${NAMESPACE}" get pods -o wide || true

  if kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret >/dev/null 2>&1; then
    log "Initial admin password (base64-decoded):"
    kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode && echo
  else
    warn "Initial admin secret not found yet"
  fi

  if [[ "${PRIVATE_REPO}" == "true" ]]; then
    printf '\n'
    configure_private_repo "$GH_REPO" "$GIT_PAT" || {
      err "Failed to configure private repo access"
      err "ArgoCD is running but cannot access private repositories"
      exit 1
    }
  else
    log ""
    log "PRIVATE_REPO is not true; skipping private repo configuration"
    log "To configure later, create a repository secret in namespace ${NAMESPACE}"
  fi
}

do_delete() {
  require_cmds

  if [[ "${CONFIRM}" != "yes" ]]; then
    err "Destructive action. Re-run with: --delete --confirm"
    exit 2
  fi

  log "Deleting only Argo CD control plane and Argo CD CRs; managed workloads will be orphaned, not pruned"

  if kubectl get secret private-repo-creds -n "${NAMESPACE}" &>/dev/null; then
    log "Removing private repo secret"
    kubectl delete secret private-repo-creds -n "${NAMESPACE}" --ignore-not-found=true >/dev/null 2>&1 || true
  fi

  delete_argo_crs_without_pruning

  if helm status argocd -n "${NAMESPACE}" >/dev/null 2>&1; then
    log "Uninstalling Helm release 'argocd' from namespace ${NAMESPACE}"
    helm uninstall argocd -n "${NAMESPACE}" --wait=false || warn "helm uninstall returned non-zero"
  else
    warn "Helm release 'argocd' not found; skipping helm uninstall"
  fi

  delete_namespace_safely
  delete_crds_last

  log "Cleanup: remove Helm repo entry (optional)"
  helm repo remove "${CHART_REPO_NAME}" >/dev/null 2>&1 || true

  log "Argo CD uninstall sequence complete."
}

if [[ $# -lt 1 ]]; then
  usage
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rollout)
      if [[ -n "${MODE}" && "${MODE}" != "rollout" ]]; then
        err "Choose either --rollout or --delete, not both."
        usage
      fi
      MODE="rollout"
      shift
      ;;
    --delete)
      if [[ -n "${MODE}" && "${MODE}" != "delete" ]]; then
        err "Choose either --rollout or --delete, not both."
        usage
      fi
      MODE="delete"
      shift
      ;;
    --confirm)
      CONFIRM="yes"
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      err "Unknown arg: $1"
      usage
      ;;
  esac
done

if [[ "${MODE}" == "rollout" ]]; then
  do_rollout
  exit 0
fi

if [[ "${MODE}" == "delete" ]]; then
  do_delete
  exit 0
fi

usage