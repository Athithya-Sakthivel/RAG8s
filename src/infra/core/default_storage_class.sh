#!/usr/bin/env bash
# Stable installer for a single default StorageClass named "default-storage-class". Removes existing defaults if exists.
# Supports: kind (local-path), AWS (ebs.csi.aws.com)
# Additionally: renders the StorageClass YAML into src/manifests/storageclass/

set -euo pipefail

readonly K8S_CLUSTER="${K8S_CLUSTER:-kind}"
readonly MANIFEST_DIR="src/manifests/storageclass"
readonly TARGET_SC="default-storage-class"
readonly LOCAL_PATH_PROVISIONER_TAG="${LOCAL_PATH_PROVISIONER_TAG:-v0.0.35}"
readonly SC_READY_TIMEOUT_SECONDS="${SC_READY_TIMEOUT_SECONDS:-120}"

log(){ printf '[%s] [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "${K8S_CLUSTER:-auto}" "$*" >&2; }
fatal(){ printf '[ERROR] [%s] %s\n' "${K8S_CLUSTER:-auto}" "$*" >&2; exit 1; }
require_bin(){ command -v "$1" >/dev/null 2>&1 || fatal "$1 not found in PATH"; }

kubectl_wait_rollout(){
  local ns="$1" deployment="$2" timeout="${3:-180s}"
  if kubectl -n "${ns}" rollout status "deployment/${deployment}" --timeout="${timeout}" >/dev/null 2>&1; then
    log "deployment/${deployment} in namespace ${ns} is rolled out"
    return 0
  else
    log "warning: rollout for ${deployment} in ${ns} did not reach ready state within ${timeout}"
    return 1
  fi
}

detect_provider(){
  if [[ -n "${K8S_CLUSTER:-}" ]]; then
    echo "${K8S_CLUSTER}"
    return 0
  fi

  if ! kubectl version --request-timeout='5s' >/dev/null 2>&1; then
    fatal "kubectl cannot reach cluster; ensure kubeconfig is configured"
  fi

  local providerID nodeName
  nodeName="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  providerID="$(kubectl get node "${nodeName}" -o jsonpath='{.spec.providerID}' 2>/dev/null || true)"

  if [[ "${providerID}" == aws* || "${providerID}" == aws://* ]] || kubectl get csidrivers 2>/dev/null | grep -q 'ebs.csi.aws.com'; then
    echo "eks"
    return 0
  fi
  if [[ "${providerID}" == gce* || "${providerID}" == gce://* ]] || kubectl get csidrivers 2>/dev/null | grep -q 'pd.csi.storage.gke.io'; then
    echo "gke"
    return 0
  fi
  if [[ "${providerID}" == azure* || "${providerID}" == azure://* ]] || kubectl get csidrivers 2>/dev/null | grep -q 'disk.csi.azure.com'; then
    echo "aks"
    return 0
  fi
  if [[ -z "${providerID}" ]]; then
    if [[ "${nodeName:-}" =~ kind- ]] || kubectl get ns local-path-storage >/dev/null 2>&1; then
      echo "kind"
      return 0
    fi
  fi

  echo "unknown"
}

ensure_single_default_annotation(){
  mapfile -t defaults < <(
    kubectl get storageclass -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true
  )

  for sc in "${defaults[@]:-}"; do
    if [[ "${sc}" != "${TARGET_SC}" ]]; then
      log "removing default annotation from existing StorageClass '${sc}'"
      kubectl patch storageclass "${sc}" \
        -p '{"metadata": {"annotations": {"storageclass.kubernetes.io/is-default-class": null}}}' \
        >/dev/null || fatal "failed to remove default annotation from '${sc}'"
    fi
  done
}

wait_for_storageclass(){
  local name="$1"
  local timeout="${2:-${SC_READY_TIMEOUT_SECONDS}}"
  local start now elapsed

  log "waiting for StorageClass '${name}' to be created (timeout ${timeout}s)"
  start="$(date +%s)"

  while true; do
    if kubectl get storageclass "${name}" >/dev/null 2>&1; then
      log "StorageClass '${name}' is present"
      return 0
    fi

    now="$(date +%s)"
    elapsed="$((now - start))"
    if [[ "${elapsed}" -ge "${timeout}" ]]; then
      fatal "timed out waiting for StorageClass '${name}'"
    fi

    sleep 2
  done
}

print_storageclass_details(){
  local name="$1"

  log "storageclass '${name}' details"

  kubectl get storageclass "${name}" -o wide

  printf "\n"
  kubectl get storageclass "${name}" \
    -o jsonpath='provisioner={.provisioner} | mode={.volumeBindingMode} | default={.metadata.annotations.storageclass\.kubernetes\.io/is-default-class} | expansion={.allowVolumeExpansion}{"\n"}'
}

create_storageclass_kind(){
  log "creating StorageClass ${TARGET_SC} for kind (local-path, WaitForFirstConsumer)"
  local out="${MANIFEST_DIR}/${TARGET_SC}-kind.yaml"
  mkdir -p "${MANIFEST_DIR}"

  cat > "${out}" <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ${TARGET_SC}
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rancher.io/local-path
mountOptions:
  - noatime
  - nodiratime
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF

  log "saved StorageClass manifest to ${out}"
  kubectl apply -f "${out}" >/dev/null
}

create_storageclass_eks(){
  log "creating StorageClass ${TARGET_SC} for EKS (AWS EBS CSI)"
  local out="${MANIFEST_DIR}/${TARGET_SC}-eks.yaml"
  mkdir -p "${MANIFEST_DIR}"

  cat > "${out}" <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ${TARGET_SC}
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
  csi.storage.k8s.io/fstype: ext4
mountOptions:
  - noatime
  - nodiratime
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF

  log "saved StorageClass manifest to ${out}"
  kubectl apply -f "${out}" >/dev/null
}

create_storageclass_gke(){
  log "creating StorageClass ${TARGET_SC} for GKE (GCE PD CSI)"
  local out="${MANIFEST_DIR}/${TARGET_SC}-gke.yaml"
  mkdir -p "${MANIFEST_DIR}"

  cat > "${out}" <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ${TARGET_SC}
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: pd.csi.storage.gke.io
parameters:
  type: pd-balanced
  csi.storage.k8s.io/fstype: ext4
mountOptions:
  - noatime
  - nodiratime
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF

  log "saved StorageClass manifest to ${out}"
  kubectl apply -f "${out}" >/dev/null
}

create_storageclass_aks(){
  log "creating StorageClass ${TARGET_SC} for AKS (Azure Disk CSI)"
  local out="${MANIFEST_DIR}/${TARGET_SC}-aks.yaml"
  mkdir -p "${MANIFEST_DIR}"

  cat > "${out}" <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ${TARGET_SC}
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: disk.csi.azure.com
parameters:
  skuName: Premium_LRS
  fsType: ext4
mountOptions:
  - noatime
  - nodiratime
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF

  log "saved StorageClass manifest to ${out}"
  kubectl apply -f "${out}" >/dev/null
}

install_local_path_provisioner(){
  if kubectl -n local-path-storage get deploy local-path-provisioner >/dev/null 2>&1; then
    log "local-path-provisioner already installed"
    return 0
  fi

  log "installing local-path-provisioner ${LOCAL_PATH_PROVISIONER_TAG}"
  kubectl apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_PROVISIONER_TAG}/deploy/local-path-storage.yaml" >/dev/null 2>&1 \
    || fatal "failed to install local-path-provisioner"

  kubectl_wait_rollout local-path-storage local-path-provisioner 180s \
    || log "continuing despite local-path-provisioner not being fully ready"
}

ensure_csi_driver_present(){
  local prov="$1"

  if kubectl get csidrivers -o name 2>/dev/null | grep -q "${prov}"; then
    log "CSI driver '${prov}' present"
    return 0
  fi

  if kubectl get deployments --all-namespaces -o name 2>/dev/null | grep -E "$(echo "${prov}" | sed 's/\./\\./g' | sed 's/-csi//g')" >/dev/null 2>&1; then
    log "CSI driver pods/deployments for '${prov}' appear present (fallback check)"
    return 0
  fi

  fatal "required CSI driver '${prov}' not found in cluster. Install the provider CSI driver before creating the StorageClass."
}

ensure_storage_class(){
  local cluster="$1"
  log "ensure_storage_class: target cluster type -> ${cluster}"

  if kubectl get storageclass "${TARGET_SC}" >/dev/null 2>&1; then
    log "StorageClass '${TARGET_SC}' already exists. Verifying it's valid..."

    local prov mode is_default
    prov="$(kubectl get storageclass "${TARGET_SC}" -o jsonpath='{.provisioner}' 2>/dev/null || true)"
    mode="$(kubectl get storageclass "${TARGET_SC}" -o jsonpath='{.volumeBindingMode}' 2>/dev/null || true)"
    is_default="$(kubectl get storageclass "${TARGET_SC}" -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}' 2>/dev/null || true)"

    [[ -n "${prov}" ]] || fatal "existing StorageClass '${TARGET_SC}' has no provisioner; please inspect"

    log "existing ${TARGET_SC} provisioner: ${prov}"
    log "existing ${TARGET_SC} volumeBindingMode: ${mode}"
    log "existing ${TARGET_SC} default annotation: ${is_default}"

    if [[ "${cluster}" == "kind" && "${mode}" != "WaitForFirstConsumer" ]]; then
      log "kind cluster requires WaitForFirstConsumer; recreating '${TARGET_SC}'"
      kubectl delete storageclass "${TARGET_SC}" >/dev/null 2>&1 || true
      wait_for_storageclass "${TARGET_SC}" 5 || true
      install_local_path_provisioner
      ensure_single_default_annotation
      create_storageclass_kind
    elif [[ "${cluster}" == "eks" && "${prov}" != "ebs.csi.aws.com" ]]; then
      fatal "existing StorageClass '${TARGET_SC}' has provisioner '${prov}', expected 'ebs.csi.aws.com'"
    elif [[ "${cluster}" == "gke" && "${prov}" != "pd.csi.storage.gke.io" ]]; then
      fatal "existing StorageClass '${TARGET_SC}' has provisioner '${prov}', expected 'pd.csi.storage.gke.io'"
    elif [[ "${cluster}" == "aks" && "${prov}" != "disk.csi.azure.com" ]]; then
      fatal "existing StorageClass '${TARGET_SC}' has provisioner '${prov}', expected 'disk.csi.azure.com'"
    fi

    if [[ "${is_default}" != "true" ]]; then
      log "marking '${TARGET_SC}' as default and clearing other defaults"
      ensure_single_default_annotation
      kubectl patch storageclass "${TARGET_SC}" \
        -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' \
        >/dev/null || fatal "failed to set default annotation on ${TARGET_SC}"
    fi

    log "StorageClass '${TARGET_SC}' is present and valid. Skipping creation."
    return 0
  fi

  case "${cluster}" in
    kind)
      install_local_path_provisioner
      ensure_single_default_annotation
      create_storageclass_kind
      ;;
    eks)
      ensure_csi_driver_present "ebs.csi.aws.com"
      ensure_single_default_annotation
      create_storageclass_eks
      ;;
    gke)
      ensure_csi_driver_present "pd.csi.storage.gke.io"
      ensure_single_default_annotation
      create_storageclass_gke
      ;;
    aks)
      ensure_csi_driver_present "disk.csi.azure.com"
      ensure_single_default_annotation
      create_storageclass_aks
      ;;
    *)
      fatal "unsupported/unknown cluster type '${cluster}'. Supported: kind, eks, gke, aks"
      ;;
  esac

  wait_for_storageclass "${TARGET_SC}"
  log "StorageClass '${TARGET_SC}' created and verified."
}

main(){
  require_bin kubectl

  local cluster
  cluster="$(detect_provider)"

  if [[ "${cluster}" == "unknown" ]]; then
    fatal "cluster provider could not be detected; set K8S_CLUSTER to one of: kind, eks, gke, aks"
  fi

  if [[ -n "${K8S_CLUSTER:-}" ]]; then
    log "K8S_CLUSTER explicitly set to '${K8S_CLUSTER}' (detection result: ${cluster})"
    cluster="${K8S_CLUSTER}"
  else
    log "auto-detected cluster type: ${cluster}"
  fi

  log "starting storage-class setup for cluster=${cluster}"
  mkdir -p "${MANIFEST_DIR}"
  ensure_storage_class "${cluster}"
  wait_for_storageclass "${TARGET_SC}"
  print_storageclass_details "${TARGET_SC}"
  log "storage-class setup complete"
}

case "${1:-}" in
  --setup) main ;;
  --help|-h) printf "Usage: %s [--setup]\n" "$0" ; exit 0 ;;
  *) main ;;
esac
