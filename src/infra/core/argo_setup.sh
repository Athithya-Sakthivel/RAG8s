#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

CHART_REPO_NAME="argo"
CHART_REPO_URL="https://argoproj.github.io/argo-helm"
CHART_NAME="argo/argo-cd"
CHART_VERSION="9.5.4"
ARGOCD_APP_VERSION="v3.3.8"
NAMESPACE="argocd"
VALUES_FILE="src/infra/core/values-kind.yaml"
TIMEOUT="10m"
TMPDIR="$(mktemp -d)"
GIT_CLONE_TIMEOUT=60
PORT_FORWARD_LOCAL=9090
PORT_FORWARD_REMOTE=443

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

write_values() {
  mkdir -p "$(dirname "${VALUES_FILE}")"
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
  enabled: true
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

cleanup() {
  rc=$?
  if [[ -n "${PF_PID:-}" ]]; then
    kill "${PF_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -d "${TMPDIR}" ]]; then
    rm -rf "${TMPDIR}"
  fi
  exit $rc
}
trap cleanup EXIT

log() { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m%s\033[0m\n' "$*"; }

require_cmds() {
  local miss=0
  for c in kubectl helm git curl jq; do
    if ! command -v "${c}" >/dev/null 2>&1; then
      err "Required command not found: ${c}"
      miss=1
    fi
  done
  if [[ ${miss} -ne 0 ]]; then
    exit 1
  fi
  if ! command -v argocd >/dev/null 2>&1; then
    log "argocd CLI not found; installing ${ARGOCD_APP_VERSION}"
    ARCH="$(uname -m)"
    if [[ "${ARCH}" == "x86_64" || "${ARCH}" == "amd64" ]]; then
      BIN_NAME="argocd-linux-amd64"
    elif [[ "${ARCH}" == "aarch64" || "${ARCH}" == "arm64" ]]; then
      BIN_NAME="argocd-linux-arm64"
    else
      err "Unsupported architecture: ${ARCH}. Install argocd CLI manually."
      exit 1
    fi
    TMP_BIN="${TMPDIR}/argocd"
    curl -sSL -o "${TMP_BIN}" "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_APP_VERSION}/${BIN_NAME}"
    chmod +x "${TMP_BIN}"
    if command -v sudo >/dev/null 2>&1; then
      sudo mv "${TMP_BIN}" /usr/local/bin/argocd
    else
      mkdir -p "${HOME}/.local/bin"
      mv "${TMP_BIN}" "${HOME}/.local/bin/argocd"
      export PATH="${HOME}/.local/bin:${PATH}"
    fi
    if ! command -v argocd >/dev/null 2>&1; then
      err "Failed to install argocd CLI; please install it manually."
      exit 1
    fi
  fi
}

apply_crds_server_side() {
  local kustomize_url="https://github.com/argoproj/argo-cd/manifests/crds?ref=${ARGOCD_APP_VERSION}"
  local raw_url="https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_APP_VERSION}/manifests/crds/install.yaml"
  log "Applying CRDs via remote kustomize: ${kustomize_url}"
  set +e
  kubectl apply --server-side --force-conflicts -k "${kustomize_url}"
  RC=$?
  set -e
  if [[ ${RC} -eq 0 ]]; then
    log "CRDs applied via remote kustomize."
    return 0
  fi
  warn "Remote kustomize failed (RC=${RC}). Trying raw install.yaml"
  set +e
  kubectl apply --server-side --force-conflicts -f "${raw_url}"
  RC2=$?
  set -e
  if [[ ${RC2} -eq 0 ]]; then
    log "CRDs applied via raw install.yaml."
    return 0
  fi
  warn "Raw install failed. Falling back to shallow git clone."
  log "Cloning argoproj/argo-cd@${ARGOCD_APP_VERSION}"
  if ! timeout "${GIT_CLONE_TIMEOUT}" git clone --depth 1 --branch "${ARGOCD_APP_VERSION}" https://github.com/argoproj/argo-cd.git "${TMPDIR}/argo-cd" >/dev/null 2>&1; then
    err "git clone failed; aborting."
    exit 1
  fi
  if [[ -d "${TMPDIR}/argo-cd/manifests/crds" ]]; then
    kubectl apply --server-side --force-conflicts -k "${TMPDIR}/argo-cd/manifests/crds"
    log "Local CRDs applied."
    return 0
  else
    err "Cloned repo missing manifests/crds; aborting."
    exit 1
  fi
}

post_install_setup() {
  log "Starting post-install setup"
  ADMIN_PWD="$(kubectl -n ${NAMESPACE} get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode)"
  log "Port-forwarding Argo CD server to localhost:${PORT_FORWARD_LOCAL}"
  kubectl -n ${NAMESPACE} port-forward svc/argocd-server ${PORT_FORWARD_LOCAL}:${PORT_FORWARD_REMOTE} >/dev/null 2>&1 &
  PF_PID=$!
  SECONDS_WAITED=0
  until curl -k --silent --fail "https://localhost:${PORT_FORWARD_LOCAL}/healthz" >/dev/null 2>&1; do
    sleep 2
    SECONDS_WAITED=$((SECONDS_WAITED+2))
    if [[ ${SECONDS_WAITED} -gt 120 ]]; then
      err "Timed out waiting for argocd-server on localhost:${PORT_FORWARD_LOCAL}"
      kill "${PF_PID}" >/dev/null 2>&1 || true
      exit 1
    fi
  done
  log "Logging in to Argo CD CLI"
  argocd login "localhost:${PORT_FORWARD_LOCAL}" --username admin --password "${ADMIN_PWD}" --insecure
  REPO_URL=""
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git remote get-url origin >/dev/null 2>&1; then
      REPO_URL="$(git remote get-url origin)"
    else
      REPO_URL="$(git remote -v | awk '/origin/ {print $2; exit}')"
    fi
  fi
  if [[ -n "${REPO_URL}" ]]; then
    log "Registering Git repo ${REPO_URL} with Argo CD"
    argocd repo add "${REPO_URL}" || warn "argocd repo add returned non-zero; it may already exist"
  else
    warn "No git remote found; skipping repo registration"
  fi
  log "Post-install setup complete"
}

do_rollout() {
  require_cmds
  write_values
  log "Adding/updating Helm repo ${CHART_REPO_NAME}"
  helm repo add "${CHART_REPO_NAME}" "${CHART_REPO_URL}" >/dev/null 2>&1 || true
  helm repo update >/dev/null
  log "Applying CRDs pinned to ${ARGOCD_APP_VERSION}"
  apply_crds_server_side
  log "Installing/upgrading Helm chart ${CHART_NAME} (version ${CHART_VERSION})"
  helm upgrade --install argocd "${CHART_NAME}" --version "${CHART_VERSION}" -n "${NAMESPACE}" --create-namespace -f "${VALUES_FILE}" --wait --timeout "${TIMEOUT}"
  log "Waiting for core deployments"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-server --timeout=${TIMEOUT} || warn "argocd-server rollout check timed out"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-repo-server --timeout=${TIMEOUT} || warn "argocd-repo-server rollout check timed out"
  kubectl -n "${NAMESPACE}" rollout status statefulset/argocd-application-controller --timeout=${TIMEOUT} || warn "argocd-application-controller rollout check timed out"
  log "Argo CD rollout complete. Pods:"
  kubectl -n "${NAMESPACE}" get pods -o wide || true
  log "Initial admin password (base64-decoded):"
  kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode && echo
  log "Applying Argo CD bootstrap manifests from src/argocd"
  kubectl apply -f src/argocd -n "${NAMESPACE}" || warn "kubectl apply for src/argocd returned non-zero"
  post_install_setup
}

do_delete() {
  require_cmds
  if [[ "${CONFIRM:-}" != "yes" ]]; then
    err "Destructive action. Re-run with: --delete --confirm"
    exit 2
  fi
  log "Deleting Argo CD Application resources cluster-wide"
  kubectl delete applications --all --all-namespaces --ignore-not-found --wait --timeout=5m || warn "Timed out deleting Applications; continuing"
  log "Deleting ApplicationSet resources cluster-wide"
  kubectl delete applicationsets --all --all-namespaces --ignore-not-found --wait --timeout=3m || warn "Timed out deleting ApplicationSets; continuing"
  log "Deleting AppProject resources cluster-wide"
  kubectl delete appprojects --all --all-namespaces --ignore-not-found --wait --timeout=3m || warn "Timed out deleting AppProjects; continuing"
  if helm status argocd -n "${NAMESPACE}" >/dev/null 2>&1; then
    log "Uninstalling Helm release 'argocd' from namespace ${NAMESPACE}"
    helm uninstall argocd -n "${NAMESPACE}" || warn "helm uninstall returned non-zero"
  else
    warn "Helm release 'argocd' not found; skipping helm uninstall"
  fi
  if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
    log "Deleting namespace ${NAMESPACE} and waiting for termination"
    kubectl delete namespace "${NAMESPACE}" --ignore-not-found || warn "namespace delete returned non-zero"
    for i in {1..30}; do
      if ! kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
        log "Namespace ${NAMESPACE} deleted"
        break
      fi
      sleep 10
    done
    if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
      warn "Namespace ${NAMESPACE} still exists after wait; attempting to remove finalizers"
      kubectl get namespace "${NAMESPACE}" -o json | jq '.spec.finalizers=[]' > "${TMPDIR}/ns-no-finalizers.json" 2>/dev/null || true
      if [[ -f "${TMPDIR}/ns-no-finalizers.json" ]]; then
        kubectl replace --raw "/api/v1/namespaces/${NAMESPACE}/finalize" -f "${TMPDIR}/ns-no-finalizers.json" || warn "Failed to remove finalizers"
      fi
    fi
  else
    warn "Namespace ${NAMESPACE} not found; skipping namespace deletion"
  fi
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
  log "Argo CD uninstall sequence complete."
}

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
