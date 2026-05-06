#!/usr/bin/env bash
# Bulletproof Valkey deployment script.
# Delegates storage class management to src/infra/core/default_storage_class.sh.
# Idempotent, observable, and failure-resistant.

set -euo pipefail

# --- Configuration ---
K8S_CLUSTER="${K8S_CLUSTER:-kind}"
NAMESPACE="${NAMESPACE:-valkey-prod}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-valkey-sa}"
SECRET_NAME="${SECRET_NAME:-valkey-auth}"
HEADLESS_SVC="${HEADLESS_SVC:-valkey-headless}"
CLIENT_SVC="${CLIENT_SVC:-valkey}"
VALKEY_PORT="${VALKEY_PORT:-6379}"
BUS_PORT="${BUS_PORT:-16379}"
IMAGE="${IMAGE:-valkey/valkey:9.0.3-alpine3.23@sha256:e1095c6c76ee982cb2d1e07edbb7fb2a53606630a1d810d5a47c9f646b708bf5}"
REPLICAS="${REPLICAS:-1}"
CPU_REQUEST="${CPU_REQUEST:-500m}"
MEMORY_REQUEST="${MEMORY_REQUEST:-1Gi}"
CPU_LIMIT="${CPU_LIMIT:-2}"
MEMORY_LIMIT="${MEMORY_LIMIT:-4Gi}"
TERMINATION_GRACE="${TERMINATION_GRACE:-120}"
ENABLE_PERSISTENCE="${ENABLE_PERSISTENCE:-1}"
PVC_SIZE="${PVC_SIZE:-10Gi}"
MANIFEST_DIR="${MANIFEST_DIR:-src/manifests/valkey}"
CLUSTER_FILE="${MANIFEST_DIR}/valkey_statefulset.yaml"

# Timeouts
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-600}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"

# Annotations for idempotency
ANNOTATION_KEY="mlsecops.valkey.checksum"

# --- Logging ---
timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log_info()  { printf '[%s] [INFO] %s\n' "$(timestamp)" "$*"; }
log_step()  { printf '[%s] [STEP] %s\n' "$(timestamp)" "$*"; }
log_success(){ printf '[%s] [SUCCESS] %s\n' "$(timestamp)" "$*"; }
log_error() { printf '[%s] [ERROR] %s\n' "$(timestamp)" "$*" >&2; }
log_warn()  { printf '[%s] [WARN] %s\n' "$(timestamp)" "$*" >&2; }
log_debug() { [[ "${VERBOSE:-0}" == "1" ]] && printf '[%s] [DEBUG] %s\n' "$(timestamp)" "$*" >&2 || true; }

fatal() { log_error "$*"; exit 1; }

# --- Prerequisites ---
require_bin() {
  local missing=()
  for bin in "$@"; do
    command -v "$bin" >/dev/null 2>&1 || missing+=("$bin")
  done
  [[ ${#missing[@]} -eq 0 ]] || fatal "Missing required binaries: ${missing[*]}"
}

trap 'rc=$?; log_error "Script failed with exit code ${rc}"; \
  log_info "Debug: kubectl context=$(kubectl config current-context 2>/dev/null || echo N/A)"; \
  log_info "Debug: pods in ${NAMESPACE}:"; kubectl -n "${NAMESPACE}" get pods -l app=valkey -o wide 2>/dev/null || true; \
  log_info "Debug: recent events:"; kubectl -n "${NAMESPACE}" get events --sort-by=.lastTimestamp 2>/dev/null | tail -10 || true; \
  exit ${rc}' ERR

# --- Storage Delegation ---
ensure_storage_infrastructure() {
  log_step "Delegating storage class verification to default_storage_class.sh"
  
  local script_path="src/infra/core/default_storage_class.sh"
  if [[ ! -f "${script_path}" ]]; then
    fatal "Storage helper script not found at ${script_path}"
  fi

  if ! K8S_CLUSTER="${K8S_CLUSTER}" bash "${script_path}" --setup; then
    fatal "Failed to ensure storage infrastructure"
  fi
  
  if ! kubectl get storageclass "default-storage-class" >/dev/null 2>&1; then
    fatal "StorageClass 'default-storage-class' missing after delegation"
  fi
  log_success "StorageClass 'default-storage-class' verified ready"
}

# --- Manifest Rendering ---
render_manifest() {
  log_step "Rendering Valkey StatefulSet manifest"
  mkdir -p "$(dirname "${CLUSTER_FILE}")"

  local storage_block=""
  local volume_mounts=""
  local volumes=""
  
  if [[ "${ENABLE_PERSISTENCE}" == "1" ]]; then
    storage_block=$(cat <<EOF
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: "default-storage-class"
        resources:
          requests:
            storage: ${PVC_SIZE}
EOF
)
    volume_mounts="- name: data
              mountPath: /data"
    volumes="- name: run
          emptyDir: {}"
  else
    volume_mounts="- name: data
              mountPath: /data
          - name: run
              mountPath: /run"
    volumes="- name: data
          emptyDir: {}
        - name: run
          emptyDir: {}"
  fi

  cat > "${CLUSTER_FILE}" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: valkey
    app.kubernetes.io/managed-by: valkey-platform-script

---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SERVICE_ACCOUNT}
  namespace: ${NAMESPACE}
automountServiceAccountToken: false

---
apiVersion: v1
kind: Service
metadata:
  name: ${HEADLESS_SVC}
  namespace: ${NAMESPACE}
  labels:
    app: valkey
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: valkey
  ports:
    - name: client
      port: ${VALKEY_PORT}
      targetPort: ${VALKEY_PORT}
      protocol: TCP
    - name: cluster-bus
      port: ${BUS_PORT}
      targetPort: ${BUS_PORT}
      protocol: TCP

---
apiVersion: v1
kind: Service
metadata:
  name: ${CLIENT_SVC}
  namespace: ${NAMESPACE}
  labels:
    app: valkey
spec:
  type: ClusterIP
  selector:
    app: valkey
  ports:
    - name: client
      port: ${VALKEY_PORT}
      targetPort: ${VALKEY_PORT}
      protocol: TCP

---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: valkey-allow-same-namespace
  namespace: ${NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app: valkey
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector: {}
      ports:
        - protocol: TCP
          port: ${VALKEY_PORT}
    - from:
        - podSelector: {}
      ports:
        - protocol: TCP
          port: ${BUS_PORT}

---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: valkey-pdb
  namespace: ${NAMESPACE}
spec:
  minAvailable: $( [[ ${REPLICAS} -ge 3 ]] && echo 2 || echo 1 )
  selector:
    matchLabels:
      app: valkey

---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: valkey
  namespace: ${NAMESPACE}
  labels:
    app: valkey
spec:
  serviceName: "${HEADLESS_SVC}"
  replicas: ${REPLICAS}
  selector:
    matchLabels:
      app: valkey
  template:
    metadata:
      labels:
        app: valkey
    spec:
      serviceAccountName: ${SERVICE_ACCOUNT}
      automountServiceAccountToken: false
      securityContext:
        fsGroup: 1000
        runAsUser: 1000
        runAsGroup: 1000
      terminationGracePeriodSeconds: ${TERMINATION_GRACE}
      containers:
        - name: valkey
          image: "${IMAGE}"
          imagePullPolicy: IfNotPresent
          ports:
            - name: client
              containerPort: ${VALKEY_PORT}
              protocol: TCP
            - name: cluster-bus
              containerPort: ${BUS_PORT}
              protocol: TCP
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            runAsNonRoot: true
            runAsUser: 1000
          resources:
            requests:
              cpu: "${CPU_REQUEST}"
              memory: "${MEMORY_REQUEST}"
            limits:
              cpu: "${CPU_LIMIT}"
              memory: "${MEMORY_LIMIT}"
          env:
            - name: POD_IP
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: VALKEY_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: ${SECRET_NAME}
                  key: VALKEY_PASSWORD
            - name: VALKEY_PORT
              value: "${VALKEY_PORT}"
            - name: VALKEY_BUS_PORT
              value: "${BUS_PORT}"
          startupProbe:
            tcpSocket:
              port: ${VALKEY_PORT}
            failureThreshold: 60
            periodSeconds: 5
            timeoutSeconds: 3
          readinessProbe:
            tcpSocket:
              port: ${VALKEY_PORT}
            initialDelaySeconds: 8
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            tcpSocket:
              port: ${VALKEY_PORT}
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 6
          lifecycle:
            preStop:
              exec:
                command:
                  - /bin/sh
                  - -c
                  - |
                    if command -v valkey-cli >/dev/null 2>&1; then
                      valkey-cli -a "\${VALKEY_PASSWORD}" --no-auth-warning shutdown || true
                    else
                      echo "valkey-cli not found; continuing shutdown"
                    fi
          volumeMounts:
            ${volume_mounts}
      volumes:
        ${volumes}
${storage_block}
EOF

  log_debug "Manifest rendered to ${CLUSTER_FILE}"
  
  # Validate manifest
  if ! kubectl apply --dry-run=client -f "${CLUSTER_FILE}" >/dev/null 2>&1; then
    fatal "Manifest failed kubectl dry-run validation"
  fi
  log_success "Manifest validated and saved to ${CLUSTER_FILE}"
}

# --- Apply with Idempotency ---
manifest_hash() {
  sha256sum "$1" | awk '{print $1}'
}

get_annotation() {
  kubectl -n "${NAMESPACE}" get statefulset valkey -o "jsonpath={.metadata.annotations['${ANNOTATION_KEY}']}" 2>/dev/null || true
}

set_annotation() {
  local hash="$1"
  kubectl -n "${NAMESPACE}" patch statefulset valkey --type=merge \
    -p "{\"metadata\":{\"annotations\":{\"${ANNOTATION_KEY}\":\"${hash}\"}}}" >/dev/null 2>&1 || true
}

apply_manifest() {
  log_step "Applying Valkey manifest (idempotent)"
  
  local current_hash new_hash
  new_hash=$(manifest_hash "${CLUSTER_FILE}")
  current_hash=$(get_annotation)
  
  if [[ -n "${current_hash}" && "${current_hash}" == "${new_hash}" ]]; then
    log_info "Manifest unchanged (hash match); skipping apply"
    return 0
  fi
  
  log_info "Applying manifest (hash changed: ${current_hash:0:8} -> ${new_hash:0:8})"
  kubectl apply --server-side --force-conflicts --field-manager=valkey-platform -f "${CLUSTER_FILE}" >/dev/null || \
    fatal "Failed to apply manifest"
  
  set_annotation "${new_hash}"
  log_success "Manifest applied and annotation updated"
}

# --- Wait Logic ---
wait_for_rollout() {
  log_step "Waiting for StatefulSet rollout (timeout: ${ROLLOUT_TIMEOUT}s)"
  
  local start elapsed
  start=$(date +%s)
  
  if ! kubectl -n "${NAMESPACE}" rollout status statefulset/valkey --timeout="${ROLLOUT_TIMEOUT}s" >/dev/null 2>&1; then
    log_error "Rollout status command failed"
    kubectl -n "${NAMESPACE}" get pods -l app=valkey -o wide || true
    fatal "StatefulSet rollout failed or timed out"
  fi
  
  log_step "Waiting for all pods to be Ready (timeout: ${READY_TIMEOUT}s)"
  if ! kubectl -n "${NAMESPACE}" wait --for=condition=ready pod -l app=valkey --timeout="${READY_TIMEOUT}s" >/dev/null 2>&1; then
    log_error "Pod readiness wait failed"
    kubectl -n "${NAMESPACE}" get pods -l app=valkey -o wide || true
    fatal "Pods failed to become ready"
  fi
  
  local ready_count
  ready_count=$(kubectl -n "${NAMESPACE}" get pods -l app=valkey --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
  log_success "All ${ready_count} pod(s) are Ready"
}

# --- Pre-flight & Setup ---
preflight_checks() {
  log_step "Running pre-flight checks"
  
  kubectl cluster-info >/dev/null 2>&1 || fatal "Cannot reach Kubernetes cluster"
  log_info "Cluster connectivity: OK"
  
  if [[ -z "${VALKEY_PASSWORD:-}" ]]; then
    fatal "VALKEY_PASSWORD environment variable must be set"
  fi
  log_info "Password: configured"
  
  if ! [[ "${REPLICAS}" =~ ^[0-9]+$ && "${REPLICAS}" -ge 1 ]]; then
    fatal "REPLICAS must be a positive integer (got: ${REPLICAS})"
  fi
  log_info "Replicas: ${REPLICAS}"
  
  if [[ "${K8S_CLUSTER}" == "kind" && "${REPLICAS}" -gt 1 ]]; then
    log_warn "Kind clusters may struggle with multi-replica anti-affinity. Consider REPLICAS=1."
  fi
  
  log_success "Pre-flight checks passed"
}

setup_namespace_and_secret() {
  log_step "Ensuring namespace ${NAMESPACE} exists"
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  log_info "Namespace: ready"
  
  log_step "Ensuring secret ${SECRET_NAME} exists"
  kubectl -n "${NAMESPACE}" create secret generic "${SECRET_NAME}" \
    --from-literal=VALKEY_PASSWORD="${VALKEY_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  log_info "Secret: ready"
}

# --- Output ---
print_connection_info() {
  log_step "Connection information"
  cat <<EOF

========================================
       VALKEY CONNECTION INFO
========================================
NAMESPACE      : ${NAMESPACE}
SERVICE        : ${CLIENT_SVC}
HEADLESS       : ${HEADLESS_SVC}
PORT           : ${VALKEY_PORT}
SECRET         : ${NAMESPACE}/${SECRET_NAME}
IN_CLUSTER_URL : valkey://:<password>@${CLIENT_SVC}.${NAMESPACE}.svc.cluster.local:${VALKEY_PORT}
PORT_FORWARD   : kubectl -n ${NAMESPACE} port-forward svc/${CLIENT_SVC} 6379:6379
========================================

EOF
}

# --- Delete ---
delete_resources() {
  log_info "Deleting Valkey resources in namespace ${NAMESPACE}"
  kubectl -n "${NAMESPACE}" delete statefulset,service,secret,serviceaccount,networkpolicy,poddisruptionbudget -l app=valkey --ignore-not-found >/dev/null 2>&1 || true
  log_info "Resources deleted (PVCs preserved)"
}

# --- Main ---
main() {
  require_bin kubectl
  
  log_info "Starting Valkey deployment workflow"
  log_info "Cluster: ${K8S_CLUSTER} | Namespace: ${NAMESPACE} | Replicas: ${REPLICAS}"
  
  preflight_checks
  ensure_storage_infrastructure
  setup_namespace_and_secret
  render_manifest
  apply_manifest
  wait_for_rollout
  print_connection_info
  
  log_success "Valkey deployment complete"
  log_info "To validate, run: bash src/tests/infra/validate_valkey.sh"
}

case "${1:-}" in
  --rollout) main ;;
  --delete) delete_resources ;;
  --render-only) render_manifest; log_info "Rendered to ${CLUSTER_FILE}" ;;
  --help|-h)
    echo "Usage: $0 [--rollout|--delete|--render-only]"
    echo "Env vars: K8S_CLUSTER, NAMESPACE, REPLICAS, VALKEY_PASSWORD, ENABLE_PERSISTENCE, PVC_SIZE"
    exit 0 ;;
  *) main ;;
esac