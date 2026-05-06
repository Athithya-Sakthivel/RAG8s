#!/usr/bin/env bash
#
# signoz_install_internal_final.sh
#
# Fully automated SigNoz install using the chart's internal ClickHouse.
# New: --apply-secrets mode will create namespace and secrets in-cluster,
# render & apply ArgoCD Application YAML, and exit immediately (no Helm wait).
#
set -euo pipefail
IFS=$'\n\t'
umask 077

# --- CLI flags ---
APPLY_SECRETS=false
while [[ "${#}" -gt 0 ]]; do
  case "$1" in
    --apply-secrets) APPLY_SECRETS=true; shift ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--apply-secrets]

Options:
  --apply-secrets   Create namespace and secrets in-cluster, render & apply ArgoCD Application YAML, then exit.
  -h, --help        Show this help and exit.
USAGE
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

# --- Configurable defaults (override via env) ---
OUTPUT="${SIGNOZ_APPLICATION_OUTPUT:-src/argocd/signoz-application.yaml}"
NAMESPACE="${SIGNOZ_DEST_NAMESPACE:-signoz}"
SIGNOZ_HELM_REPO="${SIGNOZ_CHART_REPO_URL:-https://charts.signoz.io}"
SIGNOZ_CHART="${SIGNOZ_CHART_NAME:-signoz}"
SIGNOZ_CHART_VERSION="${SIGNOZ_CHART_VERSION:-0.120.0}"
CLICKHOUSE_USER="${SIGNOZ_CLICKHOUSE_USER:-admin}"
CLICKHOUSE_PASSWORD="${SIGNOZ_CLICKHOUSE_PASSWORD:-}"
CLICKHOUSE_SECRET_NAME="${SIGNOZ_CLICKHOUSE_SECRET_NAME:-signoz-clickhouse-secret}"
JWT_SECRET="${SIGNOZ_JWT_SECRET:-}"
JWT_SECRET_NAME="${SIGNOZ_JWT_SECRET_NAME:-signoz-jwt-secret}"
CLICKHOUSE_PERSISTENCE_SIZE="${SIGNOZ_CLICKHOUSE_PERSISTENCE_SIZE:-10Gi}"
GIT_PUSH="${GIT_PUSH:-false}"
GIT_COMMIT_MESSAGE="${GIT_COMMIT_MESSAGE:-Add signoz ArgoCD Application (internal ClickHouse)}"

KUBECTL="${KUBECTL:-kubectl}"
HELM="${HELM:-helm}"
OPENSSL="${OPENSSL:-openssl}"
GIT_BIN="${GIT_BIN:-git}"

# Helm release and resource names used by the chart
HELM_RELEASE_NAME="signoz"
OTEL_CLUSTERROLE_NAME="signoz-otel-collector-signoz"

log(){ printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
fatal(){ log "FATAL: $*"; exit 1; }
require(){ command -v "$1" >/dev/null 2>&1 || fatal "$1 not found in PATH"; }

require "$KUBECTL"
require "$HELM"
require "$OPENSSL"
if [[ "$GIT_PUSH" == "true" ]]; then require "$GIT_BIN"; fi

# --- Generate secrets if missing ---
if [[ -z "$CLICKHOUSE_PASSWORD" ]]; then
  CLICKHOUSE_PASSWORD="$($OPENSSL rand -base64 32)"
  log "Generated ClickHouse password"
fi
if [[ -z "$JWT_SECRET" ]]; then
  JWT_SECRET="$($OPENSSL rand -base64 32)"
  log "Generated JWT secret"
fi

# --- Ensure namespace exists ---
kubectl create ns argocd || true
kubectl create ns signoz || true

# --- Create secrets in-cluster (stringData) ---
log "Applying ClickHouse secret ${CLICKHOUSE_SECRET_NAME} in ${NAMESPACE}"
$KUBECTL apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${CLICKHOUSE_SECRET_NAME}
  namespace: ${NAMESPACE}
stringData:
  CLICKHOUSE_USER: "${CLICKHOUSE_USER}"
  CLICKHOUSE_PASSWORD: "${CLICKHOUSE_PASSWORD}"
type: Opaque
EOF

log "Applying JWT secret ${JWT_SECRET_NAME} in ${NAMESPACE}"
$KUBECTL apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${JWT_SECRET_NAME}
  namespace: ${NAMESPACE}
stringData:
  SIGNOZ_TOKENIZER_JWT_SECRET: "${JWT_SECRET}"
type: Opaque
EOF

# --- If apply-secrets mode, render & apply ArgoCD Application YAML and exit ---
if [[ "${APPLY_SECRETS}" == "true" ]]; then
  log "--apply-secrets specified: rendering ArgoCD Application YAML and exiting (no Helm install/wait)."

  log "Rendering ArgoCD Application YAML to ${OUTPUT}"
  mkdir -p "$(dirname "${OUTPUT}")"
  cat > "${OUTPUT}" <<YAML
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: signoz
  namespace: argocd
  labels:
    app.kubernetes.io/name: signoz
    app.kubernetes.io/managed-by: argocd
spec:
  project: default
  source:
    repoURL: ${SIGNOZ_HELM_REPO}
    chart: ${SIGNOZ_CHART}
    targetRevision: "${SIGNOZ_CHART_VERSION}"
    helm:
      values: |
        clickhouse:
          enabled: true
        signoz:
          env:
            SIGNOZ_TOKENIZER_JWT_SECRET:
              valueFrom:
                secretKeyRef:
                  name: "${JWT_SECRET_NAME}"
                  key: SIGNOZ_TOKENIZER_JWT_SECRET
  destination:
    server: https://kubernetes.default.svc
    namespace: ${NAMESPACE}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
YAML

  log "Applying ArgoCD Application resource"
  $KUBECTL apply -f "${OUTPUT}"

  if [[ "${GIT_PUSH}" == "true" ]]; then
    if $GIT_BIN rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      log "Committing ${OUTPUT} to git"
      $GIT_BIN add "${OUTPUT}"
      $GIT_BIN commit -m "${GIT_COMMIT_MESSAGE}" || log "No changes to commit"
      log "Pushing commit to remote"
      $GIT_BIN push || log "git push failed; ensure remote and credentials are configured"
    else
      log "GIT_PUSH=true but current directory is not a git repo; skipping commit"
    fi
  fi

  log "Done. Secrets applied in-cluster and ArgoCD Application created. Exiting without waiting for Helm."
  exit 0
fi

# --- Add helm repo and update ---
log "Adding/updating SigNoz helm repo"
$HELM repo add signoz "${SIGNOZ_HELM_REPO}" >/dev/null 2>&1 || true
$HELM repo update >/dev/null 2>&1 || true

# --- Detect storageclass presence to decide persistence ---
has_sc=false
if $KUBECTL get storageclass >/dev/null 2>&1; then
  if $KUBECTL get storageclass -o jsonpath='{.items[0].metadata.name}' 2>/dev/null | grep -q '.'; then
    has_sc=true
  fi
fi

if [[ "$has_sc" == "true" ]]; then
  CH_PERSISTENCE=true
  log "StorageClass detected: enabling ClickHouse persistence (${CLICKHOUSE_PERSISTENCE_SIZE})"
else
  CH_PERSISTENCE=false
  log "No StorageClass detected: ClickHouse persistence will be disabled (suitable for local/kind)"
fi

# --- Validate helm template for DSN substitution bug ---
log "Rendering helm template to validate no literal \$(CLICKHOUSE_PASSWORD) appears"
TMP_TEMPLATE="$(mktemp -t signoz_helm_template.XXXXXX.yaml)"
trap 'rm -f "${TMP_TEMPLATE}"' EXIT
$HELM template "${HELM_RELEASE_NAME}" signoz/signoz --version "${SIGNOZ_CHART_VERSION}" \
  --set clickhouse.enabled=true \
  --set-string clickhouse.user="${CLICKHOUSE_USER}" \
  --set-string clickhouse.password="${CLICKHOUSE_PASSWORD}" \
  --set signoz.env.SIGNOZ_TOKENIZER_JWT_SECRET.valueFrom.secretKeyRef.name="${JWT_SECRET_NAME}" \
  --set signoz.env.SIGNOZ_TOKENIZER_JWT_SECRET.valueFrom.secretKeyRef.key=SIGNOZ_TOKENIZER_JWT_SECRET \
  > "${TMP_TEMPLATE}"

if grep -q '\$\(CLICKHOUSE_PASSWORD\)' "${TMP_TEMPLATE}"; then
  fatal "Chart template contains literal \$(CLICKHOUSE_PASSWORD). Upgrade chart or use a chart version without this bug."
fi
log "Template validation passed (no literal \$(CLICKHOUSE_PASSWORD) found)."

# --- Fix pre-existing ClusterRole/ClusterRoleBinding ownership conflicts (automated) ---
patch_or_delete_resource() {
  local kind="$1" name="$2" ns="$3"
  if [[ "$kind" == "ClusterRole" || "$kind" == "ClusterRoleBinding" ]]; then
    if $KUBECTL get "$kind" "$name" >/dev/null 2>&1; then
      log "Found existing $kind/$name. Ensuring Helm ownership metadata is present."
      set +e
      $KUBECTL patch "$kind" "$name" --type='merge' -p "{\"metadata\":{\"labels\":{\"app.kubernetes.io/managed-by\":\"Helm\"},\"annotations\":{\"meta.helm.sh/release-name\":\"${HELM_RELEASE_NAME}\",\"meta.helm.sh/release-namespace\":\"${NAMESPACE}\"}}}" >/dev/null 2>&1
      rc=$?
      set -e
      if [[ $rc -eq 0 ]]; then
        log "Patched $kind/$name with Helm ownership metadata."
      else
        log "Failed to patch $kind/$name. Attempting to delete it to allow Helm install (automated)."
        $KUBECTL delete "$kind" "$name" --ignore-not-found || fatal "Failed to delete $kind/$name; manual intervention required."
        log "Deleted $kind/$name."
      fi
    fi
  fi
}

patch_or_delete_resource "ClusterRole" "${OTEL_CLUSTERROLE_NAME}" ""
patch_or_delete_resource "ClusterRoleBinding" "${OTEL_CLUSTERROLE_NAME}" ""

# --- Build helm install args and run install (internal ClickHouse) ---
helm_args=(upgrade --install "${HELM_RELEASE_NAME}" signoz/signoz --namespace "${NAMESPACE}" --create-namespace)
helm_args+=(--version "${SIGNOZ_CHART_VERSION}")
helm_args+=(--set clickhouse.enabled=true --set-string clickhouse.user="${CLICKHOUSE_USER}" --set-string clickhouse.password="${CLICKHOUSE_PASSWORD}")
if [[ "$CH_PERSISTENCE" == "true" ]]; then
  helm_args+=(--set clickhouse.persistence.enabled=true --set clickhouse.persistence.size="${CLICKHOUSE_PERSISTENCE_SIZE}")
else
  helm_args+=(--set clickhouse.persistence.enabled=false)
fi
helm_args+=(--set signoz.env.SIGNOZ_TOKENIZER_JWT_SECRET.valueFrom.secretKeyRef.name="${JWT_SECRET_NAME}" --set signoz.env.SIGNOZ_TOKENIZER_JWT_SECRET.valueFrom.secretKeyRef.key=SIGNOZ_TOKENIZER_JWT_SECRET)
helm_args+=(--wait --timeout 20m)

log "Installing SigNoz (internal ClickHouse) via Helm"
if $HELM "${helm_args[@]}"; then
  log "Helm install/upgrade succeeded"
else
  fatal "Helm install failed even after attempting to fix ownership conflicts. Inspect cluster and Helm state."
fi

# --- Render ArgoCD Application YAML (no plaintext secrets) ---
log "Rendering ArgoCD Application YAML to ${OUTPUT}"
mkdir -p "$(dirname "${OUTPUT}")"
cat > "${OUTPUT}" <<YAML
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: signoz
  namespace: argocd
  labels:
    app.kubernetes.io/name: signoz
    app.kubernetes.io/managed-by: argocd
spec:
  project: e2e-rag-system
  source:
    repoURL: ${SIGNOZ_HELM_REPO}
    chart: ${SIGNOZ_CHART}
    targetRevision: "${SIGNOZ_CHART_VERSION}"
    helm:
      values: |
        clickhouse:
          enabled: true
        signoz:
          env:
            SIGNOZ_TOKENIZER_JWT_SECRET:
              valueFrom:
                secretKeyRef:
                  name: "${JWT_SECRET_NAME}"
                  key: SIGNOZ_TOKENIZER_JWT_SECRET
  destination:
    server: https://kubernetes.default.svc
    namespace: ${NAMESPACE}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
YAML

# Apply Application so ArgoCD sees it immediately
log "Applying ArgoCD Application resource"
$KUBECTL apply -f "${OUTPUT}"

# Optionally commit & push Application YAML to git
if [[ "${GIT_PUSH}" == "true" ]]; then
  if $GIT_BIN rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "Committing ${OUTPUT} to git"
    $GIT_BIN add "${OUTPUT}"
    $GIT_BIN commit -m "${GIT_COMMIT_MESSAGE}" || log "No changes to commit"
    log "Pushing commit to remote"
    $GIT_BIN push || log "git push failed; ensure remote and credentials are configured"
  else
    log "GIT_PUSH=true but current directory is not a git repo; skipping commit"
  fi
fi

# Restart SigNoz pods to pick up env/secret (best-effort)
log "Restarting SigNoz pods"
$KUBECTL -n "${NAMESPACE}" rollout restart statefulset/signoz >/dev/null 2>&1 || true
$KUBECTL -n "${NAMESPACE}" rollout restart deployment/signoz-otel-collector >/dev/null 2>&1 || true

log "Done. SigNoz installed with internal ClickHouse. Secrets exist only in-cluster; Application YAML contains no plaintext secrets."
log "If anything still fails, run: kubectl -n ${NAMESPACE} get pods; kubectl -n ${NAMESPACE} logs statefulset/signoz --tail=200"
exit 0
