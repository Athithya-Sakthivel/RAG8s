#!/usr/bin/env bash
# bash src/infra/core/argo_setup.sh --delete --confirm
# bash src/infra/core/argo_setup.sh --rollout
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
GIT_CLONE_TIMEOUT=60
PORT_FORWARD_LOCAL=9090
PORT_FORWARD_REMOTE=443

MODE=""
CONFIRM="no"
PF_PID=""

usage() {
  cat <<EOF
Usage: $0 --rollout | --delete [--confirm]

Modes:
  --rollout     Apply CRDs (server-side) and install/upgrade Argo CD (ClusterIP).
  --delete      Delete Argo CD control plane and Argo CD CRs without pruning managed workloads. Requires --confirm.

Examples:
  $0 --rollout
  $0 --delete --confirm
EOF
  exit 1
}

log()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m%s\033[0m\n' "$*"; }

cleanup() {
  local rc=$?
  if [[ -n "${PF_PID}" ]]; then
    kill "${PF_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -d "${TMPDIR}" ]]; then
    rm -rf "${TMPDIR}"
  fi
  exit "${rc}"
}
trap cleanup EXIT INT TERM

require_cmds() {
  local miss=0
  for c in kubectl helm git curl jq timeout base64; do
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
    local arch bin_name tmp_bin
    arch="$(uname -m)"
    if [[ "${arch}" == "x86_64" || "${arch}" == "amd64" ]]; then
      bin_name="argocd-linux-amd64"
    elif [[ "${arch}" == "aarch64" || "${arch}" == "arm64" ]]; then
      bin_name="argocd-linux-arm64"
    else
      err "Unsupported architecture: ${arch}. Install argocd CLI manually."
      exit 1
    fi

    tmp_bin="${TMPDIR}/argocd"
    curl -sSL -o "${tmp_bin}" "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_APP_VERSION}/${bin_name}"
    chmod +x "${tmp_bin}"

    if command -v sudo >/dev/null 2>&1; then
      sudo mv "${tmp_bin}" /usr/local/bin/argocd
    else
      mkdir -p "${HOME}/.local/bin"
      mv "${tmp_bin}" "${HOME}/.local/bin/argocd"
      export PATH="${HOME}/.local/bin:${PATH}"
    fi

    if ! command -v argocd >/dev/null 2>&1; then
      err "Failed to install argocd CLI; please install it manually."
      exit 1
    fi
  fi
}

write_values() {
  mkdir -p "$(dirname "${VALUES_FILE}")"
  cat > "${VALUES_FILE}" <<'EOF'
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
      server.insecure: true

controller:
  replicas: 1
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

crds:
  install: false

configs:
  resourceTrackingMethod: "annotation"

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

apply_crds_server_side() {
  local kustomize_url="https://github.com/argoproj/argo-cd/manifests/crds?ref=${ARGOCD_APP_VERSION}"
  local raw_url="https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_APP_VERSION}/manifests/crds/install.yaml"
  local rc rc2

  log "Applying CRDs via remote kustomize: ${kustomize_url}"
  set +e
  kubectl apply --server-side --force-conflicts -k "${kustomize_url}"
  rc=$?
  set -e
  if [[ ${rc} -eq 0 ]]; then
    log "CRDs applied via remote kustomize."
    return 0
  fi

  warn "Remote kustomize failed (RC=${rc}). Trying raw install.yaml"
  set +e
  kubectl apply --server-side --force-conflicts -f "${raw_url}"
  rc2=$?
  set -e
  if [[ ${rc2} -eq 0 ]]; then
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
  fi

  err "Cloned repo missing manifests/crds; aborting."
  exit 1
}

wait_for_absent() {
  local resource="$1"
  local timeout_seconds="${2:-180}"
  local interval=2
  local elapsed=0

  while (( elapsed < timeout_seconds )); do
    if ! kubectl get "${resource}" -A -o name >/dev/null 2>&1; then
      return 0
    fi
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

    warn "Namespace ${NAMESPACE} still terminating; removing namespace finalizers"
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

post_install_setup() {
  log "Starting post-install setup"
  local admin_pwd repo_url

  admin_pwd="$(kubectl -n "${NAMESPACE}" get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 --decode)"
  log "Port-forwarding Argo CD server to localhost:${PORT_FORWARD_LOCAL}"
  kubectl -n "${NAMESPACE}" port-forward svc/argocd-server "${PORT_FORWARD_LOCAL}:${PORT_FORWARD_REMOTE}" >/dev/null 2>&1 &
  PF_PID=$!

  local seconds_waited=0
  until curl -k --silent --fail "https://localhost:${PORT_FORWARD_LOCAL}/healthz" >/dev/null 2>&1; do
    sleep 2
    seconds_waited=$((seconds_waited + 2))
    if [[ ${seconds_waited} -gt 120 ]]; then
      err "Timed out waiting for argocd-server on localhost:${PORT_FORWARD_LOCAL}"
      kill "${PF_PID}" >/dev/null 2>&1 || true
      exit 1
    fi
  done

  log "Logging in to Argo CD CLI"
  argocd login "localhost:${PORT_FORWARD_LOCAL}" --username admin --password "${admin_pwd}" --insecure

  repo_url=""
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git remote get-url origin >/dev/null 2>&1; then
      repo_url="$(git remote get-url origin)"
    else
      repo_url="$(git remote -v | awk '/origin/ {print $2; exit}')"
    fi
  fi

  if [[ -n "${repo_url}" ]]; then
    log "Registering Git repo ${repo_url} with Argo CD"
    argocd repo add "${repo_url}" || warn "argocd repo add returned non-zero; it may already exist"
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
  helm upgrade --install argocd "${CHART_NAME}" \
    --version "${CHART_VERSION}" \
    -n "${NAMESPACE}" \
    --create-namespace \
    -f "${VALUES_FILE}" \
    --wait \
    --timeout "${TIMEOUT}"

  log "Waiting for core deployments"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-server --timeout="${TIMEOUT}" || warn "argocd-server rollout check timed out"
  kubectl -n "${NAMESPACE}" rollout status deployment/argocd-repo-server --timeout="${TIMEOUT}" || warn "argocd-repo-server rollout check timed out"
  kubectl -n "${NAMESPACE}" rollout status statefulset/argocd-application-controller --timeout="${TIMEOUT}" || warn "argocd-application-controller rollout check timed out"

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

  if [[ "${CONFIRM}" != "yes" ]]; then
    err "Destructive action. Re-run with: --delete --confirm"
    exit 2
  fi

  log "Deleting only Argo CD control plane and Argo CD CRs; managed workloads will be orphaned, not pruned"
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