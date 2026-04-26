#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# -------------------------
# Configurable pins & flags
# -------------------------
CHART_REPO_NAME="argo"
CHART_REPO_URL="https://argoproj.github.io/argo-helm"
CHART_NAME="argo/argo-cd"
CHART_VERSION="9.5.4"           # pinned chart version
ARGOCD_APP_VERSION="v3.3.8"     # pinned Argo CD upstream release (for CRDs)
NAMESPACE="argocd"
VALUES_FILE="src/manifests/argocd/values-minimal.yaml"
TIMEOUT="10m"
TMPDIR="$(mktemp -d)"
GIT_CLONE_TIMEOUT=60

# -------------------------
# Usage
# -------------------------
usage() {
  cat <<EOF
Usage: $0 --rollout | --delete [--confirm]

Modes:
  --rollout     Apply CRDs (server-side) and install/upgrade Argo CD (ClusterIP).
  --delete      Delete Argo CD resources safely and remove CRDs. Requires --confirm to execute.

Examples:
  $0 --rollout
  $0 --delete --confirm
EOF
  exit 1
}

# -------------------------
# Minimal values (ClusterIP by default)
# -------------------------
write_values() {
  cat > "${VALUES_FILE}" <<'EOF'
server:
  service:
    type: ClusterIP
controller:
  replicas: 1
repoServer:
  replicas: 1
dex:
  enabled: false
redis:
  enabled: false
crds:
  install: false
configs:
  params:
    server.insecure: true
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "250m"
    memory: "256Mi"
EOF
}

# -------------------------
# Helpers
# -------------------------
cleanup() {
  rc=$?
  if [[ -d "${TMPDIR}" ]]; then
    rm -rf "${TMPDIR}"
  fi
  exit $rc
}
trap cleanup EXIT

log() { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m%s\033[0m\n' "$*"; }

# -------------------------
# Prereqs
# -------------------------
require_cmds() {
  local miss=0
  for c in kubectl helm; do
    if ! command -v "${c}" >/dev/null 2>&1; then
      err "Required command not found: ${c}"
      miss=1
    fi
  done
  if [[ ${miss} -ne 0 ]]; then
    exit 1
  fi
}

# -------------------------
# CRD apply (server-side) with fallbacks
# -------------------------
apply_crds_server_side() {
  local kustomize_url="https://github.com/argoproj/argo-cd/manifests/crds?ref=${ARGOCD_APP_VERSION}"
  local raw_url="https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_APP_VERSION}/manifests/crds/install.yaml"

  log "Attempting server-side apply of CRDs via remote kustomize: ${kustomize_url}"
  set +e
  kubectl apply --server-side --force-conflicts -k "${kustomize_url}"
  RC=$?
  set -e

  if [[ ${RC} -eq 0 ]]; then
    log "CRDs server-side applied via remote kustomize."
    return 0
  fi

  warn "Remote kustomize apply failed (RC=${RC}). Trying raw install.yaml: ${raw_url}"
  set +e
  kubectl apply --server-side --force-conflicts -f "${raw_url}"
  RC2=$?
  set -e

  if [[ ${RC2} -eq 0 ]]; then
    log "CRDs server-side applied via raw install.yaml."
    return 0
  fi

  warn "Raw install.yaml apply failed (RC=${RC2}). Falling back to shallow git clone."
  if ! command -v git >/dev/null 2>&1; then
    err "git not available; cannot perform fallback. Aborting CRD apply."
    exit 1
  fi

  log "Cloning argoproj/argo-cd@${ARGOCD_APP_VERSION} into ${TMPDIR}"
  if ! timeout "${GIT_CLONE_TIMEOUT}" git clone --depth 1 --branch "${ARGOCD_APP_VERSION}" https://github.com/argoproj/argo-cd.git "${TMPDIR}/argo-cd" >/dev/null 2>&1; then
    err "git clone failed or timed out; aborting."
    exit 1
  fi

  if [[ -d "${TMPDIR}/argo-cd/manifests/crds" ]]; then
    log "Applying local CRDs via server-side apply from cloned repo"
    kubectl apply --server-side --force-conflicts -k "${TMPDIR}/argo-cd/manifests/crds"
    log "Local CRDs applied."
    return 0
  else
    err "Cloned repo missing manifests/crds; aborting."
    exit 1
  fi
}

# -------------------------
# Rollout (install/upgrade)
# -------------------------
do_rollout() {
  require_cmds
  write_values

  log "Adding/updating Helm repo ${CHART_REPO_NAME}"
  helm repo add "${CHART_REPO_NAME}" "${CHART_REPO_URL}" >/dev/null 2>&1 || true
  helm repo update >/dev/null

  log "Applying CRDs (server-side) pinned to ${ARGOCD_APP_VERSION}"
  apply_crds_server_side

  log "Installing/upgrading Helm chart ${CHART_NAME} (version ${CHART_VERSION})"
  helm upgrade --install argocd "${CHART_NAME}" \
    --version "${CHART_VERSION}" \
    -n "${NAMESPACE}" --create-namespace \
    -f "${VALUES_FILE}" \
    --wait --timeout "${TIMEOUT}"

  log "Waiting for core deployments"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-server --timeout=${TIMEOUT} || warn "argocd-server rollout check timed out"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-repo-server --timeout=${TIMEOUT} || warn "argocd-repo-server rollout check timed out"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-application-controller --timeout=${TIMEOUT} || warn "argocd-application-controller rollout check timed out"

  log "Argo CD rollout complete. Pods:"
  kubectl -n "${NAMESPACE}" get pods -o wide || true

  log "Initial admin password (base64-decoded):"
  kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode && echo
  log "Access: kubectl -n ${NAMESPACE} port-forward svc/argocd-server 8080:443 &"
}

# -------------------------
# Safe delete (uninstall)
# -------------------------
do_delete() {
  require_cmds

  # Confirm
  if [[ "${CONFIRM:-}" != "yes" ]]; then
    err "Destructive action. Re-run with: --delete --confirm"
    exit 2
  fi

  log "Starting safe uninstall of Argo CD resources."

  # 1) Delete Argo CD managed Applications cluster-wide (namespaced resources)
  log "Deleting Argo CD Application resources cluster-wide (may take time)"
  kubectl delete applications --all --all-namespaces --ignore-not-found --wait --timeout=5m || warn "Timed out deleting Applications; continuing"

  # 2) Delete ApplicationSets and AppProjects cluster-wide
  log "Deleting ApplicationSet resources cluster-wide"
  kubectl delete applicationsets --all --all-namespaces --ignore-not-found --wait --timeout=3m || warn "Timed out deleting ApplicationSets; continuing"

  log "Deleting AppProject resources cluster-wide"
  kubectl delete appprojects --all --all-namespaces --ignore-not-found --wait --timeout=3m || warn "Timed out deleting AppProjects; continuing"

  # 3) Uninstall Helm release (controllers and server)
  if helm status argocd -n "${NAMESPACE}" >/dev/null 2>&1; then
    log "Uninstalling Helm release 'argocd' from namespace ${NAMESPACE}"
    helm uninstall argocd -n "${NAMESPACE}" || warn "helm uninstall returned non-zero"
  else
    warn "Helm release 'argocd' not found; skipping helm uninstall"
  fi

  # 4) Delete namespace resources (finalizers may block; use graceful deletion)
  if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
    log "Deleting namespace ${NAMESPACE} and waiting for termination"
    kubectl delete namespace "${NAMESPACE}" --ignore-not-found || warn "namespace delete returned non-zero"
    # wait up to 5 minutes for namespace to terminate
    for i in {1..30}; do
      if ! kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
        log "Namespace ${NAMESPACE} deleted"
        break
      fi
      sleep 10
    done
    if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
      warn "Namespace ${NAMESPACE} still exists after wait; attempting to remove finalizers"
      # remove finalizers if present
      kubectl get namespace "${NAMESPACE}" -o json | jq '.spec.finalizers=[]' > "${TMPDIR}/ns-no-finalizers.json" 2>/dev/null || true
      if [[ -f "${TMPDIR}/ns-no-finalizers.json" ]]; then
        kubectl replace --raw "/api/v1/namespaces/${NAMESPACE}/finalize" -f "${TMPDIR}/ns-no-finalizers.json" || warn "Failed to remove finalizers"
      fi
    fi
  else
    warn "Namespace ${NAMESPACE} not found; skipping namespace deletion"
  fi

  # 5) Delete CRDs (use same pinned upstream manifests; server-side delete)
  log "Deleting Argo CD CRDs from upstream tag ${ARGOCD_APP_VERSION}"
  if command -v git >/dev/null 2>&1; then
    log "Cloning repo for CRD deletion"
    if timeout "${GIT_CLONE_TIMEOUT}" git clone --depth 1 --branch "${ARGOCD_APP_VERSION}" https://github.com/argoproj/argo-cd.git "${TMPDIR}/argo-cd" >/dev/null 2>&1; then
      if [[ -d "${TMPDIR}/argo-cd/manifests/crds" ]]; then
        kubectl delete -k "${TMPDIR}/argo-cd/manifests/crds" --ignore-not-found || warn "kubectl delete -k returned non-zero"
      else
        warn "Cloned repo missing manifests/crds; attempting remote delete via raw URL"
        kubectl delete -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_APP_VERSION}/manifests/crds/install.yaml" --ignore-not-found || warn "remote CRD delete failed"
      fi
    else
      warn "git clone failed; attempting remote CRD delete"
      kubectl delete -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_APP_VERSION}/manifests/crds/install.yaml" --ignore-not-found || warn "remote CRD delete failed"
    fi
  else
    warn "git not available; attempting remote CRD delete"
    kubectl delete -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_APP_VERSION}/manifests/crds/install.yaml" --ignore-not-found || warn "remote CRD delete failed"
  fi

  log "Cleanup: remove Helm repo entry (optional)"
  helm repo remove "${CHART_REPO_NAME}" >/dev/null 2>&1 || true

  log "Argo CD uninstall sequence complete. Verify cluster state manually if needed."
}

# -------------------------
# CLI parse
# -------------------------
if [[ $# -lt 1 ]]; then
  usage
fi

MODE=""
CONFIRM="no"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rollout) MODE="rollout"; shift ;;
    --delete) MODE="delete"; shift ;;
    --confirm) CONFIRM="yes"; shift ;;
    -h|--help) usage ;;
    *) err "Unknown arg: $1"; usage ;;
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
