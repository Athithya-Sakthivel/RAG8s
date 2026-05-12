#!/usr/bin/env bash
# Stable installer for a single default StorageClass named "default-storage-class".
# Supports: k3s, eks, eks-auto (AWS EKS with Auto Mode).
# This script ensures the named StorageClass exists and is the cluster default.
# For k3s it leverages the built-in local-path provisioner.
# For eks/eks-auto it requires the EBS CSI driver (ebs.csi.aws.com) be installed.

set -euo pipefail

readonly TARGET_SC="default-storage-class"
readonly MANIFEST_DIR="src/manifests/storageclass"
readonly SC_READY_TIMEOUT_SECONDS="${SC_READY_TIMEOUT_SECONDS:-120}"

log(){ printf '[%s] [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "${K8S_CLUSTER:-auto}" "$*" >&2; }
fatal(){ printf '[ERROR] [%s] %s\n' "${K8S_CLUSTER:-auto}" "$*" >&2; exit 1; }
require_bin(){ command -v "$1" >/dev/null 2>&1 || fatal "$1 not found in PATH"; }

# ------------------------------------------------------------------------------
# Detection of cloud/cluster type
# ------------------------------------------------------------------------------
detect_provider(){
  # If user explicitly set K8S_CLUSTER, use that (already set as env var but we still return)
  if [[ -n "${K8S_CLUSTER:-}" ]]; then
    echo "${K8S_CLUSTER}"
    return 0
  fi

  if ! kubectl version --request-timeout='5s' >/dev/null 2>&1; then
    fatal "kubectl cannot reach cluster; ensure kubeconfig is configured"
  fi

  local nodeName providerID
  nodeName="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  providerID="$(kubectl get node "${nodeName}" -o jsonpath='{.spec.providerID}' 2>/dev/null || true)"

  # k3s detection: node name often contains 'k3s' or we check for k3s-specific annotation
  if [[ "${nodeName:-}" =~ k3s ]] || kubectl get nodes -o jsonpath='{.items[0].metadata.labels}' 2>/dev/null | grep -q 'node-role.kubernetes.io/control-plane' && ! kubectl get csidrivers 2>/dev/null | grep -q 'ebs.csi.aws.com' && ! [[ "${providerID}" == aws* ]]; then
    # In k3s, control-plane nodes have that label and no EBS CSI driver.
    # This is a rough heuristic but works in most cases.
    echo "k3s"
    return 0
  fi

  # EKS detection (including Auto Mode)
  if [[ "${providerID}" == aws* || "${providerID}" == aws://* ]] || kubectl get csidrivers 2>/dev/null | grep -q 'ebs.csi.aws.com'; then
    # Distinguish between regular EKS and EKS Auto Mode if possible.
    # EKS Auto Mode sets specific labels on nodes, e.g., 'eks.amazonaws.com/compute-type'
    if kubectl get nodes -o jsonpath='{.items[0].metadata.labels}' 2>/dev/null | grep -q 'eks.amazonaws.com/compute-type'; then
      echo "eks-auto"
    else
      echo "eks"
    fi
    return 0
  fi

  echo "unknown"
}

# ------------------------------------------------------------------------------
# Ensure only our StorageClass bears the default annotation
# ------------------------------------------------------------------------------
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

# ------------------------------------------------------------------------------
# Wait for StorageClass to appear
# ------------------------------------------------------------------------------
wait_for_storageclass(){
  local name="$1" timeout="${2:-${SC_READY_TIMEOUT_SECONDS}}" start now elapsed

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

# ------------------------------------------------------------------------------
# Print details of the StorageClass
# ------------------------------------------------------------------------------
print_storageclass_details(){
  local name="$1"
  log "StorageClass '${name}' details:"
  kubectl get storageclass "${name}" -o wide
  printf "\n"
  kubectl get storageclass "${name}" -o jsonpath='provisioner={.provisioner} | mode={.volumeBindingMode} | default={.metadata.annotations.storageclass\.kubernetes\.io/is-default-class} | expansion={.allowVolumeExpansion}{"\n"}'
}

# ------------------------------------------------------------------------------
# StorageClass creation for k3s (built-in local-path provisioner)
# ------------------------------------------------------------------------------
create_storageclass_k3s(){
  log "creating StorageClass ${TARGET_SC} for k3s (rancher.io/local-path, WaitForFirstConsumer)"
  local out="${MANIFEST_DIR}/${TARGET_SC}-k3s.yaml"
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

# ------------------------------------------------------------------------------
# StorageClass creation for EKS (ebs.csi.aws.com, gp3)
# ------------------------------------------------------------------------------
create_storageclass_eks(){
  log "creating StorageClass ${TARGET_SC} for EKS (ebs.csi.aws.com, gp3, encrypted)"
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
  kubectl patch deployment coredns -n kube-system --type='merge' -p '{
  "spec": {
    "template": {
      "spec": {
        "tolerations": [
          {
            "operator": "Exists"
          }
        ]
      }
    }
  }
}'
  kubectl rollout restart deployment coredns -n kube-system

}

# ------------------------------------------------------------------------------
# StorageClass creation for EKS Auto Mode (identical provisioner, maybe future differentiation)
# ------------------------------------------------------------------------------
create_storageclass_eks_auto(){
  # Currently identical to EKS; kept as separate function for future customisation.
  create_storageclass_eks
  log "StorageClass created for EKS Auto Mode (same spec as EKS)"
}

# ------------------------------------------------------------------------------
# Verify CSI driver (for EKS)
# ------------------------------------------------------------------------------
ensure_csi_driver_present(){
  local driver="ebs.csi.aws.com"

  if kubectl get csidrivers -o name 2>/dev/null | grep -q "${driver}"; then
    log "CSI driver '${driver}' present"
    return 0
  fi

  if kubectl get deployments --all-namespaces -o name 2>/dev/null | grep -q 'ebs-csi-controller'; then
    log "CSI driver pods/deployments for '${driver}' appear present (fallback check)"
    return 0
  fi

  fatal "required CSI driver '${driver}' not found. Install the AWS EBS CSI driver before creating the StorageClass."
}

# ------------------------------------------------------------------------------
# Main logic: ensure the target StorageClass exists and is default
# ------------------------------------------------------------------------------
ensure_storage_class(){
  local cluster="$1"
  log "ensure_storage_class: target cluster type -> ${cluster}"

  # If the StorageClass already exists, validate and possibly fix it.
  if kubectl get storageclass "${TARGET_SC}" >/dev/null 2>&1; then
    log "StorageClass '${TARGET_SC}' already exists. Verifying..."

    local prov mode is_default
    prov="$(kubectl get storageclass "${TARGET_SC}" -o jsonpath='{.provisioner}' 2>/dev/null || true)"
    mode="$(kubectl get storageclass "${TARGET_SC}" -o jsonpath='{.volumeBindingMode}' 2>/dev/null || true)"
    is_default="$(kubectl get storageclass "${TARGET_SC}" -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}' 2>/dev/null || true)"

    [[ -n "${prov}" ]] || fatal "existing StorageClass '${TARGET_SC}' has no provisioner; please inspect"

    log "existing ${TARGET_SC} provisioner: ${prov}"
    log "existing ${TARGET_SC} volumeBindingMode: ${mode}"
    log "existing ${TARGET_SC} default annotation: ${is_default}"

    # For k3s, if provisioner isn't rancher.io/local-path, consider recreating
    if [[ "${cluster}" == "k3s" && "${prov}" != "rancher.io/local-path" ]]; then
      log "k3s cluster but provisioner is '${prov}', recreating StorageClass with rancher.io/local-path"
      kubectl delete storageclass "${TARGET_SC}" --ignore-not-found=true
      wait_for_storageclass "${TARGET_SC}" 5 || true
      ensure_single_default_annotation
      create_storageclass_k3s
    elif [[ "${cluster}" == "eks" || "${cluster}" == "eks-auto" ]] && [[ "${prov}" != "ebs.csi.aws.com" ]]; then
      fatal "existing StorageClass '${TARGET_SC}' has provisioner '${prov}', expected 'ebs.csi.aws.com' for ${cluster}"
    else
      # Provisioner matches; ensure it is default
      if [[ "${is_default}" != "true" ]]; then
        log "marking '${TARGET_SC}' as default and clearing other defaults"
        ensure_single_default_annotation
        kubectl patch storageclass "${TARGET_SC}" \
          -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' \
          >/dev/null || fatal "failed to set default annotation on ${TARGET_SC}"
      fi
    fi

    log "StorageClass '${TARGET_SC}' is present and valid."
    return 0
  fi

  # StorageClass does not exist; create it based on cluster type
  case "${cluster}" in
    k3s)
      # K3s has a built-in local-path provisioner (rancher.io/local-path) in kube-system.
      # No additional installation needed.
      ensure_single_default_annotation
      create_storageclass_k3s
      ;;
    eks)
      ensure_csi_driver_present
      ensure_single_default_annotation
      create_storageclass_eks
      ;;
    eks-auto)
      ensure_csi_driver_present
      ensure_single_default_annotation
      create_storageclass_eks_auto
      ;;
    *)
      fatal "unsupported cluster type '${cluster}'. Supported: k3s, eks, eks-auto"
      ;;
  esac

  wait_for_storageclass "${TARGET_SC}"
  log "StorageClass '${TARGET_SC}' created and verified."
}

# ------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------
main(){
  require_bin kubectl

  local detected
  detected="$(detect_provider)"

  # If K8S_CLUSTER is set, override detection; else use detected
  if [[ -n "${K8S_CLUSTER:-}" ]]; then
    log "K8S_CLUSTER explicitly set to '${K8S_CLUSTER}' (detection returned: ${detected})"
    cluster="${K8S_CLUSTER}"
  else
    cluster="${detected}"
    log "auto-detected cluster type: ${cluster}"
  fi

  if [[ "${cluster}" == "unknown" ]]; then
    fatal "cluster provider could not be detected. Set K8S_CLUSTER to one of: k3s, eks, eks-auto"
  fi

  log "starting storage-class setup for cluster=${cluster}"
  mkdir -p "${MANIFEST_DIR}"
  ensure_storage_class "${cluster}"
  wait_for_storageclass "${TARGET_SC}"
  print_storageclass_details "${TARGET_SC}"
  log "storage-class setup complete"
}

# Allow sourcing or direct execution
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi