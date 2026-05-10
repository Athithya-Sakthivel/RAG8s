#!/usr/bin/env bash
set -euo pipefail

K8S_CLUSTER="${K8S_CLUSTER:-kind}"
NAMESPACE="${NAMESPACE:-valkey}"
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
MEMORY_LIMIT="${MEMORY_LIMIT:-2Gi}"
TERMINATION_GRACE="${TERMINATION_GRACE:-120}"
ENABLE_PERSISTENCE="${ENABLE_PERSISTENCE:-0}"
PVC_SIZE="${PVC_SIZE:-10Gi}"
MANIFEST_DIR="${MANIFEST_DIR:-src/manifests/valkey}"

ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-600}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log_info()  { printf '[%s] [INFO] %s\n' "$(timestamp)" "$*"; }
log_step()  { printf '[%s] [STEP] %s\n' "$(timestamp)" "$*"; }
log_success(){ printf '[%s] [SUCCESS] %s\n' "$(timestamp)" "$*"; }
log_error() { printf '[%s] [ERROR] %s\n' "$(timestamp)" "$*" >&2; }
log_warn()  { printf '[%s] [WARN] %s\n' "$(timestamp)" "$*" >&2; }

fatal() { log_error "$*"; exit 1; }

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

ensure_storage_infrastructure() {
  log_step "Delegating storage class verification to default_storage_class.sh"
  local script_path="src/infra/core/default_storage_class.sh"
  if [[ ! -f "${script_path}" ]]; then
    fatal "Storage helper script not found at ${script_path}"
  fi
  if ! K8S_CLUSTER="${K8S_CLUSTER}" bash "${script_path}" --setup; then
    fatal "Failed to ensure storage infrastructure"
  fi
  kubectl get storageclass "default-storage-class" >/dev/null 2>&1 || fatal "StorageClass 'default-storage-class' missing after delegation"
  log_success "StorageClass 'default-storage-class' verified ready"
}

setup_namespace_and_secret() {
  log_step "Ensuring namespace ${NAMESPACE} exists"
  kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  log_info "Namespace: ready"

  log_step "Ensuring secret ${SECRET_NAME} exists"
  kubectl -n "${NAMESPACE}" create secret generic "${SECRET_NAME}" \
    --from-literal=VALKEY_PASSWORD=valkey \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  log_info "Secret: ready"
}

render_manifests() {
  log_step "Rendering Valkey manifests into ${MANIFEST_DIR}"
  mkdir -p "${MANIFEST_DIR}"

  # 00-namespace.yaml
  cat > "${MANIFEST_DIR}/00-namespace.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: valkey
    app.kubernetes.io/managed-by: valkey-platform-script
EOF

  # 01-serviceaccount.yaml
  cat > "${MANIFEST_DIR}/01-serviceaccount.yaml" <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SERVICE_ACCOUNT}
  namespace: ${NAMESPACE}
automountServiceAccountToken: false
EOF

  # 02-headless-service.yaml
  cat > "${MANIFEST_DIR}/02-headless-service.yaml" <<EOF
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
EOF

  # 03-client-service.yaml
  cat > "${MANIFEST_DIR}/03-client-service.yaml" <<EOF
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
EOF

  # 04-network-policy.yaml (final policy: ingress from inference+monitoring, egress to DNS)
  cat > "${MANIFEST_DIR}/04-network-policy.yaml" <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: valkey-allow-ingress
  namespace: ${NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app: valkey
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: inference
      ports:
        - protocol: TCP
          port: ${VALKEY_PORT}
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - protocol: TCP
          port: ${VALKEY_PORT}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: valkey-allow-egress-dns
  namespace: ${NAMESPACE}
spec:
  podSelector:
    matchLabels:
      app: valkey
  policyTypes:
    - Egress
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
EOF

  # 05-pdb.yaml
  cat > "${MANIFEST_DIR}/05-pdb.yaml" <<EOF
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: valkey-pdb
  namespace: ${NAMESPACE}
spec:
  minAvailable: $([[ ${REPLICAS} -ge 3 ]] && echo 2 || echo 1)
  selector:
    matchLabels:
      app: valkey
EOF

  # 06-statefulset.yaml
  cat > "${MANIFEST_DIR}/06-statefulset.yaml" <<EOF
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
            readOnlyRootFilesystem: true
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
            - name: VALKEY_EXTRA_FLAGS
              value: "--save '' --appendonly no"
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
                      valkey-cli shutdown || true
                    else
                      echo "valkey-cli not found; continuing shutdown"
                    fi
          volumeMounts:
            - name: tmp
              mountPath: /tmp
EOF

  if [[ "${ENABLE_PERSISTENCE}" == "1" ]]; then
    cat >> "${MANIFEST_DIR}/06-statefulset.yaml" <<EOF
            - name: data
              mountPath: /data
EOF
  fi

  cat >> "${MANIFEST_DIR}/06-statefulset.yaml" <<EOF
      volumes:
        - name: tmp
          emptyDir:
            medium: Memory
            sizeLimit: 128Mi
EOF

  if [[ "${ENABLE_PERSISTENCE}" == "1" ]]; then
    cat >> "${MANIFEST_DIR}/06-statefulset.yaml" <<EOF
        - name: data
          emptyDir: {}
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
  fi

  log_success "Manifests rendered to ${MANIFEST_DIR}"
}

apply_manifests() {
  log_step "Applying Valkey manifests"
  kubectl apply -f "${MANIFEST_DIR}" >/dev/null || fatal "Failed to apply manifests"
  log_success "Manifests applied"
}

wait_for_rollout() {
  log_step "Waiting for StatefulSet rollout (timeout: ${ROLLOUT_TIMEOUT}s)"
  kubectl -n "${NAMESPACE}" rollout status statefulset/valkey --timeout="${ROLLOUT_TIMEOUT}s" >/dev/null || {
    log_error "Rollout status failed"
    kubectl -n "${NAMESPACE}" get pods -l app=valkey -o wide || true
    fatal "StatefulSet rollout failed or timed out"
  }
  log_step "Waiting for all pods Ready (timeout: ${READY_TIMEOUT}s)"
  kubectl -n "${NAMESPACE}" wait --for=condition=ready pod -l app=valkey --timeout="${READY_TIMEOUT}s" >/dev/null || {
    log_error "Pods readiness wait failed"
    kubectl -n "${NAMESPACE}" get pods -l app=valkey -o wide || true
    fatal "Pods failed to become ready"
  }
  local ready_count=$(kubectl -n "${NAMESPACE}" get pods -l app=valkey --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
  log_success "All ${ready_count} pod(s) are Ready"
}

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
AUTH           : none (NetworkPolicy enforced)
IN_CLUSTER_URL : redis://${CLIENT_SVC}.${NAMESPACE}.svc.cluster.local:${VALKEY_PORT}
PORT_FORWARD   : kubectl -n ${NAMESPACE} port-forward svc/${CLIENT_SVC} 6379:6379
========================================

To export VALKEY_URL, run:

export VALKEY_URL="redis://${CLIENT_SVC:-valkey}.${NAMESPACE:-valkey}.svc.cluster.local:${VALKEY_PORT:-6379}"

EOF
}

delete_resources() {
  log_info "Deleting Valkey resources in namespace ${NAMESPACE}"
  kubectl -n "${NAMESPACE}" delete statefulset valkey --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" delete service "${HEADLESS_SVC}" "${CLIENT_SVC}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" delete secret "${SECRET_NAME}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" delete serviceaccount "${SERVICE_ACCOUNT}" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" delete networkpolicy valkey-allow-ingress valkey-allow-egress-dns --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "${NAMESPACE}" delete poddisruptionbudget valkey-pdb --ignore-not-found >/dev/null 2>&1 || true
  log_info "Resources deleted (PVCs preserved)"
}

main() {
  require_bin kubectl
  log_info "Starting Valkey deployment workflow"
  log_info "Cluster: ${K8S_CLUSTER} | Namespace: ${NAMESPACE} | Replicas: ${REPLICAS}"
  ensure_storage_infrastructure
  setup_namespace_and_secret
  render_manifests
  apply_manifests
  wait_for_rollout
  print_connection_info
  log_success "Valkey deployment complete"
}

case "${1:-}" in
  --rollout) main ;;
  --delete) delete_resources ;;
  --render-only) render_manifests; log_info "Manifests rendered to ${MANIFEST_DIR}" ;;
  --help|-h)
    echo "Usage: $0 [--rollout|--delete|--render-only]"
    echo "Env vars: K8S_CLUSTER, NAMESPACE, REPLICAS, IMAGE, ENABLE_PERSISTENCE, PVC_SIZE"
    exit 0 ;;
  *) main ;;
esac