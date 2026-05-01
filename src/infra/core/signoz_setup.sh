#!/usr/bin/env bash
# signoz_bootstrap.sh
#
# Fully automated bootstrap for a fresh Kubernetes cluster (no manual steps).
# - Creates namespaces (signoz, clickhouse)
# - Generates strong secrets and applies them to the cluster (kubectl)
# - Attempts to install a ClickHouse Helm release (best-effort)
# - Renders an ArgoCD Application YAML that references the pre-created Secrets
#   (no plaintext secrets in the Application YAML or committed files)
#
# Usage:
#   chmod +x signoz_bootstrap.sh
#   ./signoz_bootstrap.sh
#
# Optional environment variables (examples):
#   SIGNOZ_APPLICATION_OUTPUT (default: src/argocd/signoz-application.yaml)
#   SIGNOZ_APP_NAME (default: signoz)
#   SIGNOZ_APP_NAMESPACE (default: argocd)
#   SIGNOZ_PROJECT (default: e2e-rag-system)
#   SIGNOZ_DEST_NAMESPACE (default: signoz)
#   SIGNOZ_CLICKHOUSE_NS (default: clickhouse)
#   SIGNOZ_CLICKHOUSE_RELEASE (default: clickhouse)
#   SIGNOZ_CLICKHOUSE_CHART (default: clickhouse/clickhouse)
#   SIGNOZ_CHART_REPO_URL (default: https://charts.signoz.io)
#   SIGNOZ_CHART_NAME (default: signoz)
#   SIGNOZ_CHART_VERSION (default: 0.120.0)
#   SIGNOZ_CLICKHOUSE_USER (default: admin)
#   SIGNOZ_CLICKHOUSE_PASSWORD (auto-generated if empty)
#   SIGNOZ_CLICKHOUSE_SECRET (default: signoz-clickhouse-auth)
#   SIGNOZ_JWT_SECRET (auto-generated if empty)
#   SIGNOZ_JWT_SECRET_NAME (default: signoz-jwt-secret)
#   SIGNOZ_STORAGE_CLASS (default: default-storage-class)
#   GIT_PUSH (if "true", attempt to git add/commit/push the rendered Application YAML)
#   GIT_COMMIT_MESSAGE (commit message when GIT_PUSH=true)
#
set -euo pipefail
IFS=$'\n\t'
umask 077

# --- Defaults and env ---
REPO_ROOT="$(pwd)"
OUTPUT="${SIGNOZ_APPLICATION_OUTPUT:-${REPO_ROOT}/src/argocd/signoz-application.yaml}"
APP_NAME="${SIGNOZ_APP_NAME:-signoz}"
APP_NAMESPACE="${SIGNOZ_APP_NAMESPACE:-argocd}"
PROJECT="${SIGNOZ_PROJECT:-e2e-rag-system}"
DEST_NAMESPACE="${SIGNOZ_DEST_NAMESPACE:-signoz}"
CLICKHOUSE_NS="${SIGNOZ_CLICKHOUSE_NS:-clickhouse}"
CLICKHOUSE_RELEASE="${SIGNOZ_CLICKHOUSE_RELEASE:-clickhouse}"
CLICKHOUSE_CHART="${SIGNOZ_CLICKHOUSE_CHART:-clickhouse/clickhouse}"
CLICKHOUSE_CHART_VERSION="${SIGNOZ_CLICKHOUSE_CHART_VERSION:-}"
SIGNOZ_HELM_REPO="${SIGNOZ_CHART_REPO_URL:-https://charts.signoz.io}"
SIGNOZ_CHART="${SIGNOZ_CHART_NAME:-signoz}"
SIGNOZ_CHART_VERSION="${SIGNOZ_CHART_VERSION:-0.120.0}"
CLUSTER_NAME="${SIGNOZ_CLUSTER_NAME:-production-cluster}"
CLUSTER_DOMAIN="${SIGNOZ_CLUSTER_DOMAIN:-cluster.local}"
STORAGE_CLASS="${SIGNOZ_STORAGE_CLASS:-default-storage-class}"
CLICKHOUSE_USER="${SIGNOZ_CLICKHOUSE_USER:-admin}"
CLICKHOUSE_PASSWORD="${SIGNOZ_CLICKHOUSE_PASSWORD:-}"
CLICKHOUSE_SECRET="${SIGNOZ_CLICKHOUSE_SECRET:-signoz-clickhouse-auth}"
JWT_SECRET="${SIGNOZ_JWT_SECRET:-}"
JWT_SECRET_NAME="${SIGNOZ_JWT_SECRET_NAME:-signoz-jwt-secret}"
KUBECTL="${KUBECTL:-kubectl}"
HELM="${HELM:-helm}"
OPENSSL="${OPENSSL:-openssl}"
GIT_PUSH="${GIT_PUSH:-false}"
GIT_COMMIT_MESSAGE="${GIT_COMMIT_MESSAGE:-Add signoz ArgoCD Application}"

# --- Helpers ---
log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
fatal() { log "FATAL: $*"; exit 1; }
require_bin() { command -v "$1" >/dev/null 2>&1 || fatal "$1 not found in PATH"; }
atomic_write() {
  local dest="$1"
  local tmp
  tmp="$(mktemp "$(dirname "$dest")/.tmp.XXXXXX")"
  cat > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$dest"
}

# --- Validate tools ---
require_bin "$KUBECTL"
require_bin "$HELM"
require_bin "$OPENSSL"
if [[ "$GIT_PUSH" == "true" ]]; then require_bin git; fi

# --- Generate secrets if missing (kept only in-cluster) ---
if [[ -z "$CLICKHOUSE_PASSWORD" ]]; then
  CLICKHOUSE_PASSWORD="$($OPENSSL rand -base64 32)"
  log "Generated ClickHouse password"
fi
if [[ -z "$JWT_SECRET" ]]; then
  JWT_SECRET="$($OPENSSL rand -base64 32)"
  log "Generated JWT secret"
fi

# --- Ensure namespaces exist ---
if ! $KUBECTL get ns "$DEST_NAMESPACE" >/dev/null 2>&1; then
  log "Creating namespace: $DEST_NAMESPACE"
  $KUBECTL create namespace "$DEST_NAMESPACE" >/dev/null
else
  log "Namespace $DEST_NAMESPACE already exists"
fi

if ! $KUBECTL get ns "$CLICKHOUSE_NS" >/dev/null 2>&1; then
  log "Creating ClickHouse namespace: $CLICKHOUSE_NS"
  $KUBECTL create namespace "$CLICKHOUSE_NS" >/dev/null
else
  log "Namespace $CLICKHOUSE_NS already exists"
fi

# --- Apply Kubernetes Secrets (stringData) into the cluster only ---
apply_secret() {
  local name="$1"; local ns="$2"; declare -n kv="$3"
  # Build manifest on stdout and apply via kubectl
  $KUBECTL apply -f - <<EOF >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: ${name}
  namespace: ${ns}
  labels:
    app.kubernetes.io/managed-by: signoz-bootstrap
stringData:
$(for k in "${!kv[@]}"; do v="${kv[$k]//\"/\\\"}"; printf '  %s: "%s"\n' "$k" "$v"; done)
type: Opaque
EOF
  log "Applied secret ${name} in namespace ${ns}"
}

declare -A clickhouse_kv=( [user]="$CLICKHOUSE_USER" [password]="$CLICKHOUSE_PASSWORD" )
declare -A jwt_kv=( [SIGNOZ_TOKENIZER_JWT_SECRET]="$JWT_SECRET" )

apply_secret "$CLICKHOUSE_SECRET" "$DEST_NAMESPACE" clickhouse_kv
apply_secret "$JWT_SECRET_NAME" "$DEST_NAMESPACE" jwt_kv

# --- Add Helm repos and update ---
log "Adding/updating Helm repos"
$HELM repo add clickhouse https://clickhouse.github.io/helm-charts >/dev/null 2>&1 || true
$HELM repo add signoz "$SIGNOZ_HELM_REPO" >/dev/null 2>&1 || true
$HELM repo update >/dev/null

# --- Detect kind/local cluster and StorageClass presence to choose persistence ---
is_kind=false
if $KUBECTL get nodes -o name 2>/dev/null | grep -qi kind; then is_kind=true; fi
if $KUBECTL get nodes -o wide 2>/dev/null | grep -qi kind; then is_kind=true; fi

has_sc=false
if $KUBECTL get storageclass >/dev/null 2>&1; then
  if $KUBECTL get storageclass -o jsonpath='{.items[0].metadata.name}' 2>/dev/null | grep -q '.'; then
    has_sc=true
  fi
fi

if [[ "$is_kind" == "true" ]] || [[ "$has_sc" == "false" ]]; then
  CH_PERSISTENCE=false
  log "Detected kind/local cluster or no StorageClass; ClickHouse persistence will be disabled for install"
else
  CH_PERSISTENCE=true
  log "StorageClass present; ClickHouse persistence will be enabled for install"
fi

# --- Attempt ClickHouse install (best-effort). secrets are passed to helm via --set-string only ---
install_clickhouse() {
  local persistence="$1"
  local args=(upgrade --install "$CLICKHOUSE_RELEASE" "$CLICKHOUSE_CHART" --namespace "$CLICKHOUSE_NS" --create-namespace)
  if [[ -n "$CLICKHOUSE_CHART_VERSION" ]]; then args+=(--version "$CLICKHOUSE_CHART_VERSION"); fi
  args+=(--set-string clickhouse.user="$CLICKHOUSE_USER" --set-string clickhouse.password="$CLICKHOUSE_PASSWORD")
  if [[ "$persistence" == "true" ]]; then
    args+=(--set clickhouse.persistentVolume.enabled=true --set clickhouse.persistentVolume.size=10Gi)
  else
    args+=(--set clickhouse.persistentVolume.enabled=false)
  fi
  args+=(--wait --timeout 10m)
  log "Running helm: ${HELM} ${args[*]}"
  if $HELM "${args[@]}" >/dev/null 2>&1; then
    log "ClickHouse helm install succeeded (persistence=${persistence})"
    return 0
  else
    log "ClickHouse helm install failed (persistence=${persistence})"
    return 1
  fi
}

CH_INSTALL_OK=false
if install_clickhouse "$CH_PERSISTENCE"; then
  CH_INSTALL_OK=true
else
  log "Retrying ClickHouse install with persistence disabled"
  if install_clickhouse "false"; then
    CH_INSTALL_OK=true
  else
    CH_INSTALL_OK=false
    log "ClickHouse install failed after retries; continuing. Secrets remain only in-cluster."
  fi
fi

# --- Resolve ClickHouse service name (best-effort) ---
CLICKHOUSE_SVC="$($KUBECTL -n "$CLICKHOUSE_NS" get svc -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{range .spec.ports[*]}{.port}{";"}{end}{"\n"}{end}' 2>/dev/null | awk -F'|' -v port=9000 '{
  split($2, arr, ";");
  for (i in arr) if (arr[i]==port) { print $1; exit }
}')"
if [[ -z "$CLICKHOUSE_SVC" ]]; then
  CLICKHOUSE_SVC="${CLICKHOUSE_RELEASE}-clickhouse"
fi
CLICKHOUSE_FQDN="${CLICKHOUSE_SVC}.${CLICKHOUSE_NS}.svc.cluster.local"

# --- Build Helm values block for ArgoCD Application (externalClickhouse pattern) ---
# IMPORTANT: This Application YAML does NOT contain any plaintext secret values.
read -r -d '' HELM_VALUES <<'EOF' || true
global:
  storageClass: "__STORAGE_CLASS__"
  clusterDomain: "__CLUSTER_DOMAIN__"
  clusterName: "__CLUSTER_NAME__"

clusterName: "__CLUSTER_NAME__"

clickhouse:
  enabled: false

externalClickhouse:
  host: "__CLICKHOUSE_FQDN__:9000"
  user: "__CLICKHOUSE_USER__"
  existingSecret: "__CLICKHOUSE_SECRET__"
  existingSecretPasswordKey: password

signoz:
  name: "signoz"
  replicaCount: 1
  env:
    signoz_telemetrystore_provider: "clickhouse"
    signoz_include_only_log_namespaces: "inference"
    SIGNOZ_TOKENIZER_JWT_SECRET:
      valueFrom:
        secretKeyRef:
          name: "__JWT_SECRET_NAME__"
          key: SIGNOZ_TOKENIZER_JWT_SECRET
  podSecurityContext:
    fsGroup: 1000
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    readOnlyRootFilesystem: true
    runAsNonRoot: true
    runAsUser: 1000
  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "500m"
      memory: "512Mi"
  persistence:
    enabled: true
    existingClaim: ""
    storageClass: "__STORAGE_CLASS__"
    accessModes:
      - ReadWriteOnce
    size: "1Gi"
EOF

HELM_VALUES_RENDERED="$(printf '%s' "$HELM_VALUES" \
  | sed -e "s|__STORAGE_CLASS__|${STORAGE_CLASS}|g" \
        -e "s|__CLUSTER_DOMAIN__|${CLUSTER_DOMAIN}|g" \
        -e "s|__CLUSTER_NAME__|${CLUSTER_NAME}|g" \
        -e "s|__CLICKHOUSE_FQDN__|${CLICKHOUSE_FQDN}|g" \
        -e "s|__CLICKHOUSE_USER__|${CLICKHOUSE_USER}|g" \
        -e "s|__CLICKHOUSE_SECRET__|${CLICKHOUSE_SECRET}|g" \
        -e "s|__JWT_SECRET_NAME__|${JWT_SECRET_NAME}|g" )"

# Insert configChecksum under global (best-effort)
CONFIG_CHECKSUM="$(printf '%s' "$HELM_VALUES_RENDERED" | sha256sum | awk '{print $1}')"
HELM_VALUES_WITH_CHECKSUM="$(printf '%s\n' "$HELM_VALUES_RENDERED" | awk -v cs="$CONFIG_CHECKSUM" '
  BEGIN { added=0 }
  {
    print $0
    if ($0 ~ /^global:/ && added==0) {
      getline; print $0; print "  configChecksum: " cs; added=1; next
    }
  }')"
if [[ -z "$HELM_VALUES_WITH_CHECKSUM" ]]; then HELM_VALUES_WITH_CHECKSUM="$HELM_VALUES_RENDERED"; fi

# --- Build ArgoCD Application YAML (references secrets by name; no plaintext) ---
APPLICATION_YAML="$(cat <<-YAML
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ${APP_NAME}
  namespace: ${APP_NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_NAME}
    app.kubernetes.io/managed-by: argocd
  annotations:
    description: SigNoz Helm deployment (external ClickHouse; secrets created at bootstrap)
    signoz.argoproj.io/environment: prod
spec:
  project: ${PROJECT}
  source:
    repoURL: ${SIGNOZ_HELM_REPO}
    chart: ${SIGNOZ_CHART}
    targetRevision: "${SIGNOZ_CHART_VERSION}"
    helm:
      values: |
$(printf '%s' "$HELM_VALUES_WITH_CHECKSUM" | sed 's/^/        /')
  destination:
    server: https://kubernetes.default.svc
    namespace: ${DEST_NAMESPACE}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - PrunePropagationPolicy=foreground
      - PruneLast=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
  ignoreDifferences:
    - group: ""
      kind: Secret
      name: ${CLICKHOUSE_SECRET}
      jsonPointers:
        - /data
    - group: ""
      kind: Secret
      name: ${JWT_SECRET_NAME}
      jsonPointers:
        - /data
YAML
)"

# --- Write Application YAML atomically (no secrets inside) ---
mkdir -p "$(dirname "$OUTPUT")"
atomic_write "$OUTPUT" <<EOF
$APPLICATION_YAML
EOF
log "Wrote ArgoCD Application YAML to: $OUTPUT"

# --- Optionally commit & push the Application YAML to Git (if requested) ---
if [[ "$GIT_PUSH" == "true" ]]; then
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git add "$OUTPUT"
    git commit -m "${GIT_COMMIT_MESSAGE}" || log "No changes to commit"
    git push || log "Git push failed; please push manually"
    log "Committed and pushed $OUTPUT"
  else
    log "GIT_PUSH=true but current directory is not a git repo; skipping push"
  fi
fi

# --- Final status ---
log "Bootstrap complete."
log "Secrets created in namespace ${DEST_NAMESPACE}: ${CLICKHOUSE_SECRET}, ${JWT_SECRET_NAME} (kept only in-cluster)"
if [[ "$CH_INSTALL_OK" == "true" ]]; then
  log "ClickHouse install succeeded (namespace: ${CLICKHOUSE_NS}, service: ${CLICKHOUSE_SVC}.${CLICKHOUSE_NS}.svc.cluster.local:9000)"
else
  log "ClickHouse install did not succeed; ArgoCD Application references the pre-created secrets and will retry when ClickHouse becomes available."
fi
log "ArgoCD Application rendered to: ${OUTPUT} (no plaintext secrets inside)."

exit 0
