#!/usr/bin/env bash
# CloudNativePG lifecycle script (idempotent, declarative, restore-safe)
# Works for both kind and EKS.
#
# Commands:
#   bash src/infra/core/postgres_cluster.sh deploy
#   bash src/infra/core/postgres_cluster.sh backup --wait
#   bash src/infra/core/postgres_cluster.sh deploy --restore latest --force-recreate
#   bash src/infra/core/postgres_cluster.sh deploy --restore time --target-time <RFC3339> --force-recreate
#   bash src/infra/core/postgres_cluster.sh destroy
#   bash src/infra/core/postgres_cluster.sh status
#
# Stable identity:
#   PG_CLUSTER_ID              S3 namespace / environment scope
#   PG_SERVER_NAME             Stable backup lineage name
#   RESTORE_SOURCE_SERVER_NAME Source lineage for restore (defaults to PG_SERVER_NAME)
#
# When K8S_CLUSTER=eks:
#   - cluster and pooler pods are pinned to the general nodegroup
#   - the cluster gets an IRSA-backed serviceAccountTemplate when IRSA_ROLE_ARN is set
#   - a PodDisruptionBudget is rendered for the CNPG cluster
#
# When K8S_CLUSTER=kind:
#   - no hard node scheduling is injected by default
#   - AWS access keys are required for S3 backup/restore access

IFS=$'\n\t'

log() { printf '[%s] [cnpg] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
fatal() { printf '[%s] [cnpg][FATAL] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }
require_bin() { command -v "$1" >/dev/null 2>&1 || fatal "$1 required in PATH"; }
shq() { printf '%q' "$1"; }

safe_dns_name() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

mask_uri() {
  echo "$1" | sed -E 's#(:)[^:@]+(@)#:\*\*\*\*@#'
}

manifest_hash() {
  sha256sum "$1" | awk '{print $1}'
}

K8S_CLUSTER="${K8S_CLUSTER:-kind}"
TARGET_NS="${TARGET_NS:-default}"
ARCHIVE_DIR="${ARCHIVE_DIR:-src/scripts/archive}"
MANIFEST_DIR="${MANIFEST_DIR:-src/manifests/postgres}"

CLUSTER_NAME="${CLUSTER_NAME:-postgres-cluster}"
POOLER_NAME="${POOLER_NAME:-postgres-pooler}"
CLUSTER_FILE="${CLUSTER_FILE:-${MANIFEST_DIR}/postgres_cluster.yaml}"
POOLER_FILE="${POOLER_FILE:-${MANIFEST_DIR}/postgres_pooler.yaml}"
PDB_FILE="${PDB_FILE:-${MANIFEST_DIR}/postgres_pdb.yaml}"
SCHEDULED_BACKUP_FILE="${SCHEDULED_BACKUP_FILE:-${MANIFEST_DIR}/postgres_scheduled_backup.yaml}"
MANUAL_BACKUP_FILE="${MANUAL_BACKUP_FILE:-${MANIFEST_DIR}/postgres_backup.yaml}"

CNPG_VERSION="${CNPG_VERSION:-1.28.2}"
CNPG_NAMESPACE="${CNPG_NAMESPACE:-cnpg-system}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-ghcr.io/cloudnative-pg/postgresql:18.3-system-trixie}"

PG_BACKUPS_S3_BUCKET="${PG_BACKUPS_S3_BUCKET:-}"
PG_CLUSTER_ID="${PG_CLUSTER_ID:-cnpg-cluster-kind}"
PG_SERVER_NAME="${PG_SERVER_NAME:-mlsecops}"
RESTORE_SOURCE_SERVER_NAME="${RESTORE_SOURCE_SERVER_NAME:-${PG_SERVER_NAME}}"
RESTORE_SERVER_NAME="${RESTORE_SERVER_NAME:-}"
RESTORE_MODE="${RESTORE_MODE:-none}"
RESTORE_TARGET_TIME="${RESTORE_TARGET_TIME:-}"
FORCE_RECREATE="${FORCE_RECREATE:-false}"
WAIT_FOR_BACKUP="${WAIT_FOR_BACKUP:-true}"
CREATE_INITIAL_BACKUP="${CREATE_INITIAL_BACKUP:-false}"

OPERATOR_TIMEOUT="${OPERATOR_TIMEOUT:-300}"
POD_TIMEOUT="${POD_TIMEOUT:-900}"
SECRET_TIMEOUT="${SECRET_TIMEOUT:-180}"
BACKUP_TIMEOUT="${BACKUP_TIMEOUT:-1800}"

STORAGE_CLASS_NAME="${STORAGE_CLASS_NAME:-}"
INITDB_DB="${INITDB_DB:-zitadel_db}"
ADDITIONAL_DBS_RAW="${ADDITIONAL_DBS_RAW:-}"

BACKUP_PREFIX="${BACKUP_PREFIX:-postgres_backups/}"
BACKUP_DESTINATION_PATH="${BACKUP_DESTINATION_PATH:-}"
BACKUP_ENDPOINT_URL="${BACKUP_ENDPOINT_URL:-}"
BACKUP_RETENTION_POLICY="${BACKUP_RETENTION_POLICY:-30d}"
BACKUP_SCHEDULE="${BACKUP_SCHEDULE:-0 0 0 * * *}"

IRSA_ROLE_ARN="${PG_IRSA_ROLE_ARN:-}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}"
AWS_SESSION_TOKEN="${AWS_SESSION_TOKEN:-}"
AWS_CREDS_SECRET_NAME="${AWS_CREDS_SECRET_NAME:-aws-creds}"

RUN_TIMESTAMP="${RUN_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
BACKUP_ACTIVE_SERVER_NAME=""
RESTORE_ACTIVE_SERVER_NAME=""

is_kind_cluster() { [[ "$K8S_CLUSTER" == kind* ]]; }
is_eks_cluster() { [[ "$K8S_CLUSTER" == eks* ]]; }

if is_kind_cluster; then
  INSTANCES="${INSTANCES:-1}"
  CPU_REQUEST="${CPU_REQUEST:-250m}"
  CPU_LIMIT="${CPU_LIMIT:-1000m}"
  MEM_REQUEST="${MEM_REQUEST:-512Mi}"
  MEM_LIMIT="${MEM_LIMIT:-1Gi}"
  STORAGE_SIZE="${STORAGE_SIZE:-5Gi}"
  WAL_SIZE="${WAL_SIZE:-2Gi}"
  POOLER_INSTANCES="${POOLER_INSTANCES:-1}"
  POOLER_CPU_REQUEST="${POOLER_CPU_REQUEST:-50m}"
  POOLER_MEM_REQUEST="${POOLER_MEM_REQUEST:-64Mi}"
  POOLER_CPU_LIMIT="${POOLER_CPU_LIMIT:-200m}"
  POOLER_MEM_LIMIT="${POOLER_MEM_LIMIT:-256Mi}"
else
  INSTANCES="${INSTANCES:-3}"
  CPU_REQUEST="${CPU_REQUEST:-500m}"
  CPU_LIMIT="${CPU_LIMIT:-2000m}"
  MEM_REQUEST="${MEM_REQUEST:-1Gi}"
  MEM_LIMIT="${MEM_LIMIT:-4Gi}"
  STORAGE_SIZE="${STORAGE_SIZE:-20Gi}"
  WAL_SIZE="${WAL_SIZE:-10Gi}"
  POOLER_INSTANCES="${POOLER_INSTANCES:-2}"
  POOLER_CPU_REQUEST="${POOLER_CPU_REQUEST:-100m}"
  POOLER_MEM_REQUEST="${POOLER_MEM_REQUEST:-128Mi}"
  POOLER_CPU_LIMIT="${POOLER_CPU_LIMIT:-500m}"
  POOLER_MEM_LIMIT="${POOLER_MEM_LIMIT:-512Mi}"
fi

parse_additional_dbs() {
  local raw="$1" old_ifs="$IFS"
  local -a items=()
  IFS=' ' read -r -a items <<< "$raw"
  IFS="$old_ifs"
  for i in "${items[@]}"; do
    [[ -n "$i" ]] && printf '%s\n' "$i"
  done
}

mapfile -t ADDITIONAL_DBS < <(parse_additional_dbs "$ADDITIONAL_DBS_RAW")
ALL_DBS=("$INITDB_DB" "${ADDITIONAL_DBS[@]}")

trap 'rc=$?; echo; echo "[DIAG] exit_code=$rc"; echo "[DIAG] kubectl context: $(kubectl config current-context 2>/dev/null || true)"; echo "[DIAG] pods (ns ${TARGET_NS}):"; kubectl -n "${TARGET_NS}" get pods -o wide || true; echo "[DIAG] pvc (ns ${TARGET_NS}):"; kubectl -n "${TARGET_NS}" get pvc || true; exit $rc' ERR

set_cluster_id() {
  PG_CLUSTER_ID="$(safe_dns_name "$1")"
  BACKUP_DESTINATION_PATH=""
}

source_lineage_server_name() {
  safe_dns_name "${RESTORE_SOURCE_SERVER_NAME:-$PG_SERVER_NAME}"
}

compose_backup_destination_path() {
  if [[ -n "$BACKUP_DESTINATION_PATH" ]]; then
    BACKUP_DESTINATION_PATH="${BACKUP_DESTINATION_PATH%/}/"
    return 0
  fi
  [[ -n "$PG_BACKUPS_S3_BUCKET" ]] || fatal "set PG_BACKUPS_S3_BUCKET or BACKUP_DESTINATION_PATH"
  BACKUP_PREFIX="${BACKUP_PREFIX#/}"
  BACKUP_PREFIX="${BACKUP_PREFIX%/}/"
  BACKUP_DESTINATION_PATH="s3://${PG_BACKUPS_S3_BUCKET%/}/${BACKUP_PREFIX}${PG_CLUSTER_ID}/"
}

lineage_prefix_for() {
  printf '%s%s/' "${BACKUP_DESTINATION_PATH%/}/" "$(safe_dns_name "$1")"
}

cluster_exists() {
  kubectl -n "$TARGET_NS" get cluster "$CLUSTER_NAME" >/dev/null 2>&1
}

live_cluster_backup_server_name() {
  kubectl -n "$TARGET_NS" get cluster "$CLUSTER_NAME" -o jsonpath='{.spec.backup.barmanObjectStore.serverName}' 2>/dev/null || true
}

live_cluster_backup_destination_path() {
  kubectl -n "$TARGET_NS" get cluster "$CLUSTER_NAME" -o jsonpath='{.spec.backup.barmanObjectStore.destinationPath}' 2>/dev/null || true
}

resolve_fresh_target_server_name() {
  BACKUP_ACTIVE_SERVER_NAME="$(safe_dns_name "$PG_SERVER_NAME")"
}

resolve_restore_target_server_name() {
  if [[ -n "$RESTORE_SERVER_NAME" && "$RESTORE_SERVER_NAME" != "auto" ]]; then
    RESTORE_ACTIVE_SERVER_NAME="$(safe_dns_name "$RESTORE_SERVER_NAME")"
  else
    RESTORE_ACTIVE_SERVER_NAME="$(safe_dns_name "$(source_lineage_server_name)-restore-${RUN_TIMESTAMP}")"
  fi
}

require_prereqs() {
  require_bin kubectl
  require_bin curl
  require_bin sha256sum
  require_bin awk
  require_bin sed
  require_bin base64
  require_bin grep
  if [[ -n "$PG_BACKUPS_S3_BUCKET" || -n "$BACKUP_DESTINATION_PATH" ]]; then
    require_bin aws
  fi
  kubectl version --client >/dev/null 2>&1 || fatal "kubectl client unavailable"
  mkdir -p "$ARCHIVE_DIR" "$MANIFEST_DIR"
}

ensure_namespace() {
  kubectl get ns "$TARGET_NS" >/dev/null 2>&1 || kubectl create ns "$TARGET_NS" >/dev/null
}

install_cnpg_operator() {
  log "installing CloudNativePG operator ${CNPG_VERSION}"
  kubectl get ns "$CNPG_NAMESPACE" >/dev/null 2>&1 || kubectl create ns "$CNPG_NAMESPACE" >/dev/null
  local url archive
  url="https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.28/releases/cnpg-${CNPG_VERSION}.yaml"
  archive="$ARCHIVE_DIR/cnpg-${CNPG_VERSION}.yaml"
  curl -fsSL -o "$archive" "$url" || fatal "failed to download operator manifest"
  kubectl apply --server-side --force-conflicts -f "$archive" >/dev/null || fatal "failed to apply operator manifest"
  kubectl -n "$CNPG_NAMESPACE" rollout status deployment/cnpg-controller-manager --timeout="${OPERATOR_TIMEOUT}s" >/dev/null || fatal "operator rollout failed"
}

ensure_cnpg_operator() {
  if kubectl get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 &&
     kubectl get crd poolers.postgresql.cnpg.io >/dev/null 2>&1 &&
     kubectl get crd backups.postgresql.cnpg.io >/dev/null 2>&1 &&
     kubectl get crd scheduledbackups.postgresql.cnpg.io >/dev/null 2>&1; then
    log "CloudNativePG CRDs already present"
    return 0
  fi
  install_cnpg_operator
}

detect_default_storage_class() {
  if [[ -n "$STORAGE_CLASS_NAME" ]]; then
    return 0
  fi

  local sc
  sc="$(kubectl get sc -o jsonpath='{range .items[*]}{.metadata.name}|{.metadata.annotations.kubernetes\.io/is-default-class}|{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null | awk -F'|' '$2=="true" || $3=="true" {print $1; exit}')"

  if [[ -n "$sc" ]]; then
    STORAGE_CLASS_NAME="$sc"
    return 0
  fi

  if kubectl get sc standard >/dev/null 2>&1; then
    STORAGE_CLASS_NAME="standard"
    return 0
  fi

  fatal "no default storage class found; set STORAGE_CLASS_NAME explicitly"
}

validate_backup_inputs() {
  compose_backup_destination_path

  if is_kind_cluster && [[ -n "$IRSA_ROLE_ARN" ]]; then
    fatal "IRSA_ROLE_ARN is not supported on kind; use AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
  fi

  if [[ -n "$IRSA_ROLE_ARN" && -n "$AWS_ACCESS_KEY_ID" ]]; then
    fatal "use either IRSA_ROLE_ARN or AWS access keys, not both"
  fi

  if [[ -z "$IRSA_ROLE_ARN" ]]; then
    [[ -n "$AWS_ACCESS_KEY_ID" && -n "$AWS_SECRET_ACCESS_KEY" ]] || fatal "set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY when not using IRSA"
  fi
}

validate_restore_inputs() {
  case "$RESTORE_MODE" in
    latest) ;;
    time) [[ -n "$RESTORE_TARGET_TIME" ]] || fatal "restore time requires --target-time <RFC3339>" ;;
    none) fatal "restore mode not selected" ;;
    *) fatal "invalid restore mode: $RESTORE_MODE" ;;
  esac
}

ensure_aws_secret() {
  [[ -n "$IRSA_ROLE_ARN" ]] && return 0
  log "creating/updating AWS credentials secret $AWS_CREDS_SECRET_NAME"
  kubectl -n "$TARGET_NS" create secret generic "$AWS_CREDS_SECRET_NAME" \
    --from-literal=ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    --from-literal=ACCESS_SECRET_KEY="$AWS_SECRET_ACCESS_KEY" \
    ${AWS_SESSION_TOKEN:+--from-literal=ACCESS_SESSION_TOKEN="$AWS_SESSION_TOKEN"} \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

cluster_scheduling_block() {
  if ! is_eks_cluster; then
    return 0
  fi
  cat <<EOF
  affinity:
    enablePodAntiAffinity: true
    topologyKey: kubernetes.io/hostname
    nodeSelector:
      node-type: general
    tolerations:
      - key: node-type
        operator: Equal
        value: general
        effect: NoSchedule
EOF
}

pooler_scheduling_block() {
  if ! is_eks_cluster; then
    return 0
  fi
  cat <<EOF
      nodeSelector:
        node-type: general
      tolerations:
        - key: node-type
          operator: Equal
          value: general
          effect: NoSchedule
EOF
}

emit_irsa_block() {
  [[ -n "$IRSA_ROLE_ARN" ]] && cat <<EOF
  serviceAccountTemplate:
    metadata:
      annotations:
        eks.amazonaws.com/role-arn: $IRSA_ROLE_ARN
EOF
}

emit_credentials_block() {
  local indent="$1"
  if [[ -z "$IRSA_ROLE_ARN" ]]; then
    printf '%ss3Credentials:\n' "$indent"
    printf '%s  accessKeyId:\n' "$indent"
    printf '%s    name: %s\n' "$indent" "$AWS_CREDS_SECRET_NAME"
    printf '%s    key: ACCESS_KEY_ID\n' "$indent"
    printf '%s  secretAccessKey:\n' "$indent"
    printf '%s    name: %s\n' "$indent" "$AWS_CREDS_SECRET_NAME"
    printf '%s    key: ACCESS_SECRET_KEY\n' "$indent"
    [[ -n "$AWS_SESSION_TOKEN" ]] && {
      printf '%s  sessionToken:\n' "$indent"
      printf '%s    name: %s\n' "$indent" "$AWS_CREDS_SECRET_NAME"
      printf '%s    key: ACCESS_SESSION_TOKEN\n' "$indent"
    }
  fi
}

emit_backup_store_block() {
  local indent="$1" server_name="$2"
  printf '%sbarmanObjectStore:\n' "$indent"
  printf '%s  destinationPath: %s\n' "$indent" "$BACKUP_DESTINATION_PATH"
  printf '%s  serverName: %s\n' "$indent" "$server_name"
  [[ -n "$BACKUP_ENDPOINT_URL" ]] && printf '%s  endpointURL: %s\n' "$indent" "$BACKUP_ENDPOINT_URL"
  emit_credentials_block "${indent}  "
  printf '%s  wal:\n' "$indent"
  printf '%s    compression: gzip\n' "$indent"
  printf '%s  data:\n' "$indent"
  printf '%s    compression: gzip\n' "$indent"
}

cluster_pdb_min_available() {
  if (( INSTANCES <= 1 )); then
    printf '1'
    return 0
  fi
  printf '%s' "$((INSTANCES - 1))"
}

render_cluster_manifest() {
  local file="$1" mode="$2" restore_target_time="${3:-}" source_server_name="${4:-}" target_server_name="${5:-}"
  mkdir -p "$(dirname "$file")"

  {
    cat <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${CLUSTER_NAME}
  namespace: ${TARGET_NS}
spec:
  instances: ${INSTANCES}
  imageName: ${POSTGRES_IMAGE}
EOF
    emit_irsa_block
    cat <<EOF
  backup:
    retentionPolicy: ${BACKUP_RETENTION_POLICY}
EOF
    emit_backup_store_block "    " "${target_server_name}"
    cluster_scheduling_block

    if [[ "$mode" == "fresh" ]]; then
      cat <<EOF
  bootstrap:
    initdb:
      database: ${INITDB_DB}
      owner: app
  storage:
    storageClass: ${STORAGE_CLASS_NAME}
    size: ${STORAGE_SIZE}
  walStorage:
    storageClass: ${STORAGE_CLASS_NAME}
    size: ${WAL_SIZE}
  postgresql:
    parameters:
      shared_buffers: "256MB"
      max_connections: "200"
      wal_compression: "on"
      effective_cache_size: "1GB"
  resources:
    requests:
      cpu: ${CPU_REQUEST}
      memory: ${MEM_REQUEST}
    limits:
      cpu: ${CPU_LIMIT}
      memory: ${MEM_LIMIT}
EOF
    else
      cat <<EOF
  bootstrap:
    recovery:
      source: origin
EOF
      [[ -n "$restore_target_time" ]] && cat <<EOF
      recoveryTarget:
        targetTime: ${restore_target_time}
EOF
      cat <<EOF
  externalClusters:
    - name: origin
      barmanObjectStore:
        destinationPath: ${BACKUP_DESTINATION_PATH}
        serverName: ${source_server_name}
EOF
      [[ -n "$BACKUP_ENDPOINT_URL" ]] && cat <<EOF
        endpointURL: ${BACKUP_ENDPOINT_URL}
EOF
      if [[ -z "$IRSA_ROLE_ARN" ]]; then
        cat <<EOF
        s3Credentials:
          accessKeyId:
            name: ${AWS_CREDS_SECRET_NAME}
            key: ACCESS_KEY_ID
          secretAccessKey:
            name: ${AWS_CREDS_SECRET_NAME}
            key: ACCESS_SECRET_KEY
EOF
        [[ -n "$AWS_SESSION_TOKEN" ]] && cat <<EOF
          sessionToken:
            name: ${AWS_CREDS_SECRET_NAME}
            key: ACCESS_SESSION_TOKEN
EOF
      fi
      cat <<EOF
        wal:
          compression: gzip
        data:
          compression: gzip
  storage:
    storageClass: ${STORAGE_CLASS_NAME}
    size: ${STORAGE_SIZE}
  walStorage:
    storageClass: ${STORAGE_CLASS_NAME}
    size: ${WAL_SIZE}
  postgresql:
    parameters:
      shared_buffers: "256MB"
      max_connections: "200"
      wal_compression: "on"
      effective_cache_size: "1GB"
  resources:
    requests:
      cpu: ${CPU_REQUEST}
      memory: ${MEM_REQUEST}
    limits:
      cpu: ${CPU_LIMIT}
      memory: ${MEM_LIMIT}
EOF
    fi
  } > "$file"

  log "wrote $file"
}

render_cluster_pdb_manifest() {
  local file="$1" cluster_name="$2"
  mkdir -p "$(dirname "$file")"
  cat > "$file" <<EOF
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ${cluster_name}-pdb
  namespace: ${TARGET_NS}
spec:
  minAvailable: $(cluster_pdb_min_available)
  selector:
    matchLabels:
      cnpg.io/cluster: ${cluster_name}
EOF
  log "wrote $file"
}

render_pooler_manifest() {
  local file="$1" cluster_name="$2"
  mkdir -p "$(dirname "$file")"
  {
    cat <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Pooler
metadata:
  name: ${POOLER_NAME}
  namespace: ${TARGET_NS}
spec:
  cluster:
    name: ${cluster_name}
  instances: ${POOLER_INSTANCES}
  type: rw
  pgbouncer:
    poolMode: transaction
    parameters:
      max_client_conn: "1000"
      default_pool_size: "25"
      min_pool_size: "5"
      reserve_pool_size: "10"
      server_idle_timeout: "600"
  template:
    spec:
      securityContext:
        runAsNonRoot: true
EOF
    pooler_scheduling_block
    cat <<EOF
      containers:
        - name: pgbouncer
          securityContext:
            runAsNonRoot: true
            allowPrivilegeEscalation: false
          resources:
            requests:
              cpu: ${POOLER_CPU_REQUEST}
              memory: ${POOLER_MEM_REQUEST}
            limits:
              cpu: ${POOLER_CPU_LIMIT}
              memory: ${POOLER_MEM_LIMIT}
EOF
  } > "$file"
  log "wrote $file"
}

render_scheduled_backup_manifest() {
  local file="$1" cluster_name="$2"
  mkdir -p "$(dirname "$file")"
  cat > "$file" <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: ScheduledBackup
metadata:
  name: ${cluster_name}-backup
  namespace: ${TARGET_NS}
spec:
  cluster:
    name: ${cluster_name}
  schedule: "${BACKUP_SCHEDULE}"
  backupOwnerReference: self
  method: barmanObjectStore
EOF
  log "wrote $file"
}

render_manual_backup_manifest() {
  local file="$1" cluster_name="$2" backup_name="$3"
  mkdir -p "$(dirname "$file")"
  cat > "$file" <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: ${backup_name}
  namespace: ${TARGET_NS}
spec:
  method: barmanObjectStore
  cluster:
    name: ${cluster_name}
EOF
  log "wrote $file"
}

apply_if_changed() {
  local file="$1" kind="$2" name="$3" ann_key="$4"
  local h existing
  h="$(manifest_hash "$file")"
  existing="$(kubectl -n "$TARGET_NS" get "$kind" "$name" -o "jsonpath={.metadata.annotations['${ann_key}']}" 2>/dev/null || true)"
  if [[ -n "$existing" && "$existing" == "$h" ]]; then
    log "$kind/$name unchanged; skipping"
    return 0
  fi
  kubectl apply --server-side --force-conflicts -f "$file" >/dev/null || fatal "failed to apply $kind/$name"
  kubectl -n "$TARGET_NS" patch "$kind" "$name" --type=merge -p "{\"metadata\":{\"annotations\":{\"${ann_key}\":\"$h\"}}}" >/dev/null 2>&1 || true
  log "applied $kind/$name"
}

jsonpath_condition() {
  local cluster_name="$1" condition_type="$2"
  kubectl -n "$TARGET_NS" get cluster "$cluster_name" -o jsonpath="{range .status.conditions[?(@.type==\"$condition_type\")]}{.status}|{.reason}|{.message}{end}" 2>/dev/null || true
}

wait_for_cluster_ready() {
  local cluster_name="$1" timeout="${2:-$POD_TIMEOUT}"
  log "waiting for cluster readiness (${cluster_name})"
  local start now elapsed ready expected
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - start))
    [[ "$elapsed" -ge "$timeout" ]] && fatal "timeout waiting for cluster readiness: ${cluster_name}"
    ready=$(kubectl -n "$TARGET_NS" get cluster "$cluster_name" -o jsonpath='{.status.readyInstances}' 2>/dev/null || echo 0)
    expected=$(kubectl -n "$TARGET_NS" get cluster "$cluster_name" -o jsonpath='{.spec.instances}' 2>/dev/null || echo 1)
    if [[ -n "$ready" && -n "$expected" && "$ready" -ge "$expected" ]]; then
      log "cluster ready ${ready}/${expected}: ${cluster_name}"
      return 0
    fi
    sleep 3
  done
}

wait_for_continuous_archiving() {
  local cluster_name="$1" timeout="${2:-$POD_TIMEOUT}"
  log "waiting for ContinuousArchiving=True (${cluster_name})"
  local start now elapsed cond status reason_message reason message
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - start))
    if [[ "$elapsed" -ge "$timeout" ]]; then
      cond="$(jsonpath_condition "$cluster_name" "ContinuousArchiving")"
      fatal "timeout waiting for ContinuousArchiving; current=${cond}"
    fi
    cond="$(jsonpath_condition "$cluster_name" "ContinuousArchiving")"
    status="${cond%%|*}"
    reason_message="${cond#*|}"
    reason="${reason_message%%|*}"
    message="${reason_message#*|}"
    if [[ "$status" == "True" ]]; then
      log "ContinuousArchiving ready: ${cluster_name}"
      return 0
    fi
    [[ -n "$status" && "$status" == "False" ]] && log "ContinuousArchiving still failing: ${reason} - ${message}"
    sleep 3
  done
}

wait_for_app_secret() {
  local cluster_name="$1"
  log "waiting for app secret (${cluster_name})"
  local start now elapsed secret
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - start))
    secret=$(kubectl -n "$TARGET_NS" get secret -l "cnpg.io/cluster=${cluster_name},cnpg.io/userType=app" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -n "$secret" ]]; then
      log "found app secret ${secret}"
      return 0
    fi
    [[ "$elapsed" -ge "$SECRET_TIMEOUT" ]] && fatal "timeout waiting for app secret: ${cluster_name}"
    sleep 2
  done
}

wait_for_backup_completed() {
  local backup_name="$1"
  log "waiting for backup ${backup_name} to complete"
  local start now elapsed phase
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - start))
    if [[ "$elapsed" -ge "$BACKUP_TIMEOUT" ]]; then
      kubectl -n "$TARGET_NS" describe backup "$backup_name" || true
      fatal "timeout waiting for backup completion: ${backup_name}"
    fi
    phase=$(kubectl -n "$TARGET_NS" get backup "$backup_name" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    case "$phase" in
      completed) log "backup completed: ${backup_name}"; return 0 ;;
      failed) kubectl -n "$TARGET_NS" describe backup "$backup_name" || true; fatal "backup failed: ${backup_name}" ;;
      running|started|"") sleep 5 ;;
      *) sleep 5 ;;
    esac
  done
}

latest_completed_backup_name() {
  kubectl -n "$TARGET_NS" get backup -l "cnpg.io/cluster=${CLUSTER_NAME}" \
    -o jsonpath='{range .items[?(@.status.phase=="completed")]}{.status.stoppedAt}|{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | sort | tail -n 1 | cut -d'|' -f2- || true
}

get_primary_pod() {
  kubectl -n "$TARGET_NS" get pods -l 'cnpg.io/instanceRole=primary' -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
}

ensure_database_exists() {
  local primary db exists
  primary="$(get_primary_pod)"
  [[ -n "$primary" ]] || fatal "primary pod not found"
  for db in "${ALL_DBS[@]}"; do
    exists=$(kubectl -n "$TARGET_NS" exec "$primary" -- psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${db}';" 2>/dev/null || echo "")
    if [[ "$exists" =~ 1 ]]; then
      log "database ${db} already exists"
    else
      log "creating database ${db}"
      kubectl -n "$TARGET_NS" exec "$primary" -- psql -U postgres -c "CREATE DATABASE ${db} OWNER app;" >/dev/null
      log "created ${db}"
    fi
  done
}

fix_database_schema_ownership() {
  log "ensuring schema ownership for target DBs"
  local primary db
  primary="$(get_primary_pod)"
  [[ -n "$primary" ]] || fatal "primary pod not found"
  for db in "${ALL_DBS[@]}"; do
    kubectl -n "$TARGET_NS" exec "$primary" -- psql -U postgres -d "$db" -c "ALTER SCHEMA public OWNER TO app;" >/dev/null 2>&1 || true
  done
}

print_connection_uris() {
  local cluster_name="$1" pooler_name="$2"
  log "printing masked pooler URIs (${cluster_name})"
  local secret user pw port host raw masked
  secret=$(kubectl -n "$TARGET_NS" get secret -l "cnpg.io/cluster=${cluster_name},cnpg.io/userType=app" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$secret" ]] || fatal "app secret not found for connection URI output: ${cluster_name}"
  user=$(kubectl -n "$TARGET_NS" get secret "$secret" -o jsonpath='{.data.username}' 2>/dev/null | base64 -d)
  pw=$(kubectl -n "$TARGET_NS" get secret "$secret" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)
  port=$(kubectl -n "$TARGET_NS" get secret "$secret" -o jsonpath='{.data.port}' 2>/dev/null | base64 -d || echo 5432)
  host="${pooler_name}.${TARGET_NS}"
  raw="postgresql://${user}:${pw}@${host}:${port}"
  masked="$(mask_uri "$raw")"
  printf '\nConnection URIs (masked):\n\n'
  for db in "${ALL_DBS[@]}"; do
    printf '%s/%s\n' "$masked" "$db"
  done
}

s3_lineage_has_any_objects() {
  local server_name="$1" prefix
  prefix="$(lineage_prefix_for "$server_name")"
  aws s3 ls "$prefix" --recursive 2>/dev/null | grep -q .
}

s3_lineage_has_base_backup() {
  local server_name="$1" prefix
  prefix="$(lineage_prefix_for "$server_name")"
  prefix="${prefix%/}"
  aws s3 ls "${prefix}/base/" --recursive 2>/dev/null | grep -q 'backup.info'
}

check_fresh_archive_empty() {
  local prefix
  prefix="$(lineage_prefix_for "$BACKUP_ACTIVE_SERVER_NAME")"
  s3_lineage_has_any_objects "$BACKUP_ACTIVE_SERVER_NAME" && fatal "S3 lineage is not empty for fresh deploy: ${prefix} (choose a new PG_CLUSTER_ID or purge that lineage if this is intentional)"
}

check_restore_source_present() {
  local prefix src
  src="$(source_lineage_server_name)"
  prefix="$(lineage_prefix_for "$src")"
  prefix="${prefix%/}"
  s3_lineage_has_base_backup "$src" || fatal "no completed base backup found for restore in ${prefix}/base/"
}

check_restore_target_empty() {
  local prefix
  prefix="$(lineage_prefix_for "$RESTORE_ACTIVE_SERVER_NAME")"
  s3_lineage_has_any_objects "$RESTORE_ACTIVE_SERVER_NAME" && fatal "restore target lineage is not empty: ${prefix} (choose a new restore target or purge that target lineage if intentional)"
}

ensure_fresh_lineage() {
  resolve_fresh_target_server_name
  check_fresh_archive_empty
}

ensure_restore_lineage() {
  resolve_restore_target_server_name
  check_restore_source_present
  check_restore_target_empty
}

create_initial_backup() {
  local backup_name
  [[ "$CREATE_INITIAL_BACKUP" != "true" ]] && { log "initial backup skipped"; return 0; }
  [[ -n "$(latest_completed_backup_name)" ]] && { log "completed backup already exists; skipping initial backup"; return 0; }
  backup_name="${CLUSTER_NAME}-manual-$(date -u +%Y%m%d%H%M%S)-$$"
  render_manual_backup_manifest "$MANUAL_BACKUP_FILE" "$CLUSTER_NAME" "$backup_name"
  apply_if_changed "$MANUAL_BACKUP_FILE" backup "$backup_name" "mlsecops.cnpg.backup-checksum"
  wait_for_backup_completed "$backup_name"
}

render_cluster_pdb_and_apply() {
  render_cluster_pdb_manifest "$PDB_FILE" "$CLUSTER_NAME"
  apply_if_changed "$PDB_FILE" poddisruptionbudget "${CLUSTER_NAME}-pdb" "mlsecops.cnpg.pdb-checksum"
}

deploy_pooler_and_wait() {
  local cluster_name="$1" pooler_name="$2"
  render_pooler_manifest "$POOLER_FILE" "$cluster_name"
  apply_if_changed "$POOLER_FILE" pooler "$pooler_name" "mlsecops.cnpg.pooler-checksum"
  log "waiting for pooler pods to be ready (${pooler_name})"
  local start now elapsed
  start=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - start))
    local pods ready need svc
    pods=$(kubectl -n "$TARGET_NS" get pods -l "cnpg.io/poolerName=${pooler_name}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
    if [[ -n "$pods" ]]; then
      ready=$(for p in $pods; do kubectl -n "$TARGET_NS" get pod "$p" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo false; done | grep -c true || true)
      need=$(kubectl -n "$TARGET_NS" get pooler "$pooler_name" -o jsonpath='{.spec.instances}' 2>/dev/null || echo "$POOLER_INSTANCES")
      if [[ "$ready" -ge "$need" && "$need" -gt 0 ]]; then
        svc=$(kubectl -n "$TARGET_NS" get svc "$pooler_name" -o jsonpath='{.metadata.name}' 2>/dev/null || true)
        [[ -n "$svc" ]] && { log "pooler ready: ${pooler_name}"; return 0; }
      fi
    fi
    [[ "$elapsed" -ge "$OPERATOR_TIMEOUT" ]] && fatal "timeout waiting for pooler readiness: ${pooler_name}"
    sleep 3
  done
}

show_runtime_context() {
  local mode="$1"
  log "runtime context:"
  log "  mode=${mode}"
  log "  k8s_cluster=${K8S_CLUSTER}"
  log "  namespace=${TARGET_NS}"
  log "  cluster_name=${CLUSTER_NAME}"
  log "  pooler_name=${POOLER_NAME}"
  log "  cluster_id=${PG_CLUSTER_ID}"
  log "  source_server_name=$(source_lineage_server_name)"
  log "  backup_destination_path=${BACKUP_DESTINATION_PATH}"
  log "  storage_class=${STORAGE_CLASS_NAME}"
  if is_eks_cluster; then
    log "  scheduling=node-type=general"
    log "  irsa=${IRSA_ROLE_ARN:-<none>}"
  fi
  if [[ "$mode" == "restore" ]]; then
    log "  restore_target_server_name=${RESTORE_ACTIVE_SERVER_NAME}"
    log "  restore_target_time=${RESTORE_TARGET_TIME:-<none>}"
  else
    log "  active_server_name=${BACKUP_ACTIVE_SERVER_NAME}"
  fi
}

print_next_steps() {
  local src
  src="$(source_lineage_server_name)"
  printf '\nNext commands:\n\n'
  printf 'Backup later:\n'
  printf '  K8S_CLUSTER=%s PG_BACKUPS_S3_BUCKET=%s PG_CLUSTER_ID=%s PG_SERVER_NAME=%s bash src/infra/core/postgres_cluster.sh backup --wait\n' \
    "$(shq "$K8S_CLUSTER")" "$(shq "$PG_BACKUPS_S3_BUCKET")" "$(shq "$PG_CLUSTER_ID")" "$(shq "$src")"
  printf '\nRestore latest into a new cluster:\n'
  printf '  K8S_CLUSTER=%s PG_BACKUPS_S3_BUCKET=%s PG_CLUSTER_ID=%s RESTORE_SOURCE_SERVER_NAME=%s bash src/infra/core/postgres_cluster.sh deploy --restore latest --force-recreate\n' \
    "$(shq "$K8S_CLUSTER")" "$(shq "$PG_BACKUPS_S3_BUCKET")" "$(shq "$PG_CLUSTER_ID")" "$(shq "$src")"
  printf '\nRestore to time:\n'
  printf '  K8S_CLUSTER=%s PG_BACKUPS_S3_BUCKET=%s PG_CLUSTER_ID=%s RESTORE_SOURCE_SERVER_NAME=%s bash src/infra/core/postgres_cluster.sh deploy --restore time --target-time <RFC3339> --force-recreate\n' \
    "$(shq "$K8S_CLUSTER")" "$(shq "$PG_BACKUPS_S3_BUCKET")" "$(shq "$PG_CLUSTER_ID")" "$(shq "$src")"
}

persist_artifacts() {
  cp "$CLUSTER_FILE" "${MANIFEST_DIR}/postgres_cluster.yaml" 2>/dev/null || true
  cp "$POOLER_FILE" "${MANIFEST_DIR}/postgres_pooler.yaml" 2>/dev/null || true
  cp "$PDB_FILE" "${MANIFEST_DIR}/postgres_pdb.yaml" 2>/dev/null || true
  cp "$SCHEDULED_BACKUP_FILE" "${MANIFEST_DIR}/postgres_scheduled_backup.yaml" 2>/dev/null || true
  log "artifacts persisted to ${MANIFEST_DIR}"
}

deploy_fresh() {
  ensure_cnpg_operator
  detect_default_storage_class
  validate_backup_inputs
  ensure_aws_secret
  ensure_fresh_lineage
  compose_backup_destination_path
  show_runtime_context "deploy"

  render_cluster_manifest "$CLUSTER_FILE" "fresh" "" "" "$BACKUP_ACTIVE_SERVER_NAME"
  apply_if_changed "$CLUSTER_FILE" cluster "$CLUSTER_NAME" "mlsecops.cnpg.cluster-checksum"
  render_cluster_pdb_and_apply
  wait_for_cluster_ready "$CLUSTER_NAME"
  wait_for_continuous_archiving "$CLUSTER_NAME"
  wait_for_app_secret "$CLUSTER_NAME"
  ensure_database_exists
  fix_database_schema_ownership
  create_initial_backup
  render_scheduled_backup_manifest "$SCHEDULED_BACKUP_FILE" "$CLUSTER_NAME"
  apply_if_changed "$SCHEDULED_BACKUP_FILE" scheduledbackup "${CLUSTER_NAME}-backup" "mlsecops.cnpg.scheduled-backup-checksum"
  deploy_pooler_and_wait "$CLUSTER_NAME" "$POOLER_NAME"
  persist_artifacts
  print_connection_uris "$CLUSTER_NAME" "$POOLER_NAME"
  print_next_steps
  printf '\n[SUCCESS] deployed CNPG cluster %s\n' "$CLUSTER_NAME"
  printf '[SUCCESS] source lineage: serverName=%s prefix=%s\n' "$(source_lineage_server_name)" "$(lineage_prefix_for "$(source_lineage_server_name)")"
  printf '[SUCCESS] target lineage: serverName=%s prefix=%s\n' "$BACKUP_ACTIVE_SERVER_NAME" "$(lineage_prefix_for "$BACKUP_ACTIVE_SERVER_NAME")"
}

deploy_restore() {
  ensure_cnpg_operator
  detect_default_storage_class
  validate_backup_inputs
  ensure_aws_secret
  validate_restore_inputs
  compose_backup_destination_path
  ensure_restore_lineage
  show_runtime_context "restore"

  if cluster_exists; then
    [[ "$FORCE_RECREATE" == "true" ]] || fatal "cluster ${CLUSTER_NAME} already exists; use --force-recreate to replace it"
    kubectl -n "$TARGET_NS" delete scheduledbackup "${CLUSTER_NAME}-backup" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$TARGET_NS" delete backup -l "cnpg.io/cluster=${CLUSTER_NAME}" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$TARGET_NS" delete pooler "$POOLER_NAME" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$TARGET_NS" delete poddisruptionbudget "${CLUSTER_NAME}-pdb" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$TARGET_NS" delete cluster "$CLUSTER_NAME" --ignore-not-found >/dev/null 2>&1 || true
  fi

  if [[ "$RESTORE_MODE" == "time" ]]; then
    render_cluster_manifest "$CLUSTER_FILE" "restore" "$RESTORE_TARGET_TIME" "$(source_lineage_server_name)" "$RESTORE_ACTIVE_SERVER_NAME"
  else
    render_cluster_manifest "$CLUSTER_FILE" "restore" "" "$(source_lineage_server_name)" "$RESTORE_ACTIVE_SERVER_NAME"
  fi

  apply_if_changed "$CLUSTER_FILE" cluster "$CLUSTER_NAME" "mlsecops.cnpg.cluster-checksum"
  render_cluster_pdb_and_apply
  wait_for_cluster_ready "$CLUSTER_NAME"
  wait_for_continuous_archiving "$CLUSTER_NAME"
  wait_for_app_secret "$CLUSTER_NAME"
  render_scheduled_backup_manifest "$SCHEDULED_BACKUP_FILE" "$CLUSTER_NAME"
  apply_if_changed "$SCHEDULED_BACKUP_FILE" scheduledbackup "${CLUSTER_NAME}-backup" "mlsecops.cnpg.scheduled-backup-checksum"
  deploy_pooler_and_wait "$CLUSTER_NAME" "$POOLER_NAME"
  persist_artifacts
  print_connection_uris "$CLUSTER_NAME" "$POOLER_NAME"
  print_next_steps
  printf '\n[SUCCESS] restored CNPG cluster %s\n' "$CLUSTER_NAME"
  printf '[SUCCESS] restore source lineage: serverName=%s prefix=%s\n' "$(source_lineage_server_name)" "$(lineage_prefix_for "$(source_lineage_server_name)")"
  printf '[SUCCESS] restore target lineage: serverName=%s prefix=%s\n' "$RESTORE_ACTIVE_SERVER_NAME" "$(lineage_prefix_for "$RESTORE_ACTIVE_SERVER_NAME")"
}

cmd_backup() {
  ensure_cnpg_operator
  detect_default_storage_class
  validate_backup_inputs
  ensure_aws_secret
  if cluster_exists; then
    BACKUP_ACTIVE_SERVER_NAME="$(safe_dns_name "$(live_cluster_backup_server_name)")"
    [[ -n "$BACKUP_ACTIVE_SERVER_NAME" ]] || BACKUP_ACTIVE_SERVER_NAME="$(safe_dns_name "$PG_SERVER_NAME")"
    [[ -n "$(live_cluster_backup_destination_path)" ]] && BACKUP_DESTINATION_PATH="$(live_cluster_backup_destination_path)"
    BACKUP_DESTINATION_PATH="${BACKUP_DESTINATION_PATH%/}/"
  else
    resolve_fresh_target_server_name
  fi
  compose_backup_destination_path
  show_runtime_context "backup"
  cluster_exists || fatal "cluster not found: ${CLUSTER_NAME}"
  wait_for_cluster_ready "$CLUSTER_NAME"
  wait_for_continuous_archiving "$CLUSTER_NAME"

  local backup_name
  backup_name="${CLUSTER_NAME}-manual-$(date -u +%Y%m%d%H%M%S)-$$"
  render_manual_backup_manifest "$MANUAL_BACKUP_FILE" "$CLUSTER_NAME" "$backup_name"
  apply_if_changed "$MANUAL_BACKUP_FILE" backup "$backup_name" "mlsecops.cnpg.backup-checksum"
  [[ "$WAIT_FOR_BACKUP" == "true" ]] && wait_for_backup_completed "$backup_name"

  printf '\n[SUCCESS] backup started for cluster %s: %s\n' "$CLUSTER_NAME" "$backup_name"
  printf '[SUCCESS] active lineage: serverName=%s prefix=%s\n' "$BACKUP_ACTIVE_SERVER_NAME" "$(lineage_prefix_for "$BACKUP_ACTIVE_SERVER_NAME")"
  print_next_steps
}

cmd_destroy() {
  log "destroy requested for cluster=${CLUSTER_NAME} namespace=${TARGET_NS}"
  kubectl -n "$TARGET_NS" delete scheduledbackup "${CLUSTER_NAME}-backup" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$TARGET_NS" delete backup -l "cnpg.io/cluster=${CLUSTER_NAME}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$TARGET_NS" delete pooler "$POOLER_NAME" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$TARGET_NS" delete poddisruptionbudget "${CLUSTER_NAME}-pdb" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$TARGET_NS" delete cluster "$CLUSTER_NAME" --ignore-not-found >/dev/null 2>&1 || true
  log "deleted CNPG resources for ${CLUSTER_NAME}"
  log "preserved PV data and S3 backups"
}

status_cluster() {
  compose_backup_destination_path
  echo
  echo "=== ${CLUSTER_NAME} ==="
  if ! cluster_exists; then
    echo "not found"
    echo "lineage:"
    echo "  cluster-id: $(safe_dns_name "$PG_CLUSTER_ID")"
    echo "  source-serverName: $(source_lineage_server_name)"
    echo "  source-prefix: $(lineage_prefix_for "$(source_lineage_server_name)")"
    echo "  backup-prefix: ${BACKUP_DESTINATION_PATH}"
    return 0
  fi
  resolve_fresh_target_server_name
  kubectl -n "$TARGET_NS" get cluster "$CLUSTER_NAME" -o wide || true
  echo
  echo "conditions:"
  kubectl -n "$TARGET_NS" get cluster "$CLUSTER_NAME" -o jsonpath='{range .status.conditions[*]}{.type}={" "}{.status}{" "}{.reason}{" "}{.message}{"\n"}{end}' 2>/dev/null || true
  echo
  echo "pooler:"
  kubectl -n "$TARGET_NS" get pooler "$POOLER_NAME" -o wide 2>/dev/null || true
  echo
  echo "pods:"
  kubectl -n "$TARGET_NS" get pods -l "cnpg.io/cluster=${CLUSTER_NAME}" -o wide 2>/dev/null || true
  echo
  echo "lineage:"
  echo "  cluster-id: $(safe_dns_name "$PG_CLUSTER_ID")"
  echo "  source-serverName: $(source_lineage_server_name)"
  echo "  source-prefix: $(lineage_prefix_for "$(source_lineage_server_name)")"
  echo "  backup-destination-path: ${BACKUP_DESTINATION_PATH}"
}

show_help() {
  cat <<EOF
Usage:
  $0 deploy [--cluster-id <id>] [--restore latest|time] [--target-time <RFC3339>] [--force-recreate] [--create-initial-backup true|false]
  $0 backup [--cluster-id <id>] [--wait]
  $0 destroy [--cluster-id <id>]
  $0 status [--cluster-id <id>]
  $0 help

Required:
  PG_BACKUPS_S3_BUCKET=<bucket-name>

Stable lineage:
  PG_SERVER_NAME=<stable-lineage-name>
  PG_CLUSTER_ID=<environment-id>

Restore:
  RESTORE_SOURCE_SERVER_NAME defaults to PG_SERVER_NAME
  restore target lineage is generated internally when omitted

No backup_and_restore_commands.sh is generated.
EOF
}

main() {
  require_prereqs
  ensure_namespace
  local action="${1:-help}"
  shift || true

  case "$action" in
    deploy)
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --cluster-id)
            shift
            [[ $# -gt 0 ]] || fatal "--cluster-id requires a value"
            set_cluster_id "$1"
            ;;
          --cluster-id=*)
            set_cluster_id "${1#*=}"
            ;;
          --restore)
            shift
            [[ $# -gt 0 ]] || fatal "--restore requires latest|time"
            RESTORE_MODE="$1"
            ;;
          --restore=*)
            RESTORE_MODE="${1#*=}"
            ;;
          --restore-source-server-name)
            shift
            [[ $# -gt 0 ]] || fatal "--restore-source-server-name requires a value"
            RESTORE_SOURCE_SERVER_NAME="$1"
            ;;
          --restore-source-server-name=*)
            RESTORE_SOURCE_SERVER_NAME="${1#*=}"
            ;;
          --restore-server-name)
            shift
            [[ $# -gt 0 ]] || fatal "--restore-server-name requires a value"
            RESTORE_SERVER_NAME="$1"
            ;;
          --restore-server-name=*)
            RESTORE_SERVER_NAME="${1#*=}"
            ;;
          --target-time)
            shift
            [[ $# -gt 0 ]] || fatal "--target-time requires a value"
            RESTORE_TARGET_TIME="$1"
            ;;
          --target-time=*)
            RESTORE_TARGET_TIME="${1#*=}"
            ;;
          --force-recreate)
            FORCE_RECREATE=true
            ;;
          --create-initial-backup)
            shift
            [[ $# -gt 0 ]] || fatal "--create-initial-backup requires true|false"
            CREATE_INITIAL_BACKUP="$1"
            ;;
          --create-initial-backup=*)
            CREATE_INITIAL_BACKUP="${1#*=}"
            ;;
          *)
            fatal "unknown deploy argument: $1"
            ;;
        esac
        shift
      done

      case "$RESTORE_MODE" in
        none) deploy_fresh ;;
        latest|time) deploy_restore ;;
        *) fatal "restore requires --restore latest|time" ;;
      esac
      ;;

    backup)
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --cluster-id)
            shift
            [[ $# -gt 0 ]] || fatal "--cluster-id requires a value"
            set_cluster_id "$1"
            ;;
          --cluster-id=*)
            set_cluster_id "${1#*=}"
            ;;
          --wait)
            WAIT_FOR_BACKUP=true
            ;;
          *)
            fatal "unknown backup argument: $1"
            ;;
        esac
        shift
      done
      cmd_backup
      ;;

    destroy)
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --cluster-id)
            shift
            [[ $# -gt 0 ]] || fatal "--cluster-id requires a value"
            set_cluster_id "$1"
            ;;
          --cluster-id=*)
            set_cluster_id "${1#*=}"
            ;;
          *)
            fatal "unknown destroy argument: $1"
            ;;
        esac
        shift
      done
      cmd_destroy
      ;;

    status)
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --cluster-id)
            shift
            [[ $# -gt 0 ]] || fatal "--cluster-id requires a value"
            set_cluster_id "$1"
            ;;
          --cluster-id=*)
            set_cluster_id "${1#*=}"
            ;;
          *)
            fatal "unknown status argument: $1"
            ;;
        esac
        shift
      done
      status_cluster
      ;;

    help|-h|--help)
      show_help
      ;;

    *)
      fatal "unknown command: ${action}"
      ;;
  esac
}

main "$@"
