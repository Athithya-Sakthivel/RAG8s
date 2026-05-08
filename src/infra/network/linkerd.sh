#!/usr/bin/env bash
set -euo pipefail

# -------------------------------------------------------------------
# Linkerd one-go bootstrap for your cluster.
#
# What it does:
# 1) Renders all manifests into src/manifests/linkerd/
# 2) Installs Gateway API CRDs
# 3) Validates the cluster with linkerd check --pre
# 4) Installs Linkerd control plane
# 5) Installs Linkerd viz
# 6) Injects namespaces/workloads
# 7) Applies current policy manifests (AuthorizationPolicy)
# 8) Restarts workloads so the proxy sidecar is actually injected
# 9) Verifies meshing with linkerd check --proxy
#
# Defaults assume:
#   - edge namespace holds cloudflared
#   - auth namespace holds auth-svc
#   - predict namespace holds predict workload
#
# Override as needed via env vars:
#   EDGE_NAMESPACE=edge
#   AUTH_NAMESPACE=auth
#   PREDICT_NAMESPACE=predict
#   CLOUDFLARED_DEPLOYMENT=cloudflared
#   AUTH_WORKLOAD_KIND=deployment
#   AUTH_WORKLOAD_NAME=auth-svc
#   PREDICT_WORKLOAD_KIND=deployment
#   PREDICT_WORKLOAD_NAME=predict-svc
#   LINKERD2_VERSION=edge-26.4.3
# -------------------------------------------------------------------

ROOT="$(pwd)"
OUT_DIR="${ROOT}/src/manifests/linkerd"

# Install source/version pins
GATEWAY_API_URL="${GATEWAY_API_URL:-https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml}"
LINKERD2_VERSION="${LINKERD2_VERSION:-edge-26.4.3}"

# Namespaces
EDGE_NAMESPACE="${EDGE_NAMESPACE:-edge}"
AUTH_NAMESPACE="${AUTH_NAMESPACE:-auth}"
PREDICT_NAMESPACE="${PREDICT_NAMESPACE:-predict}"

# Workload names
CLOUDFLARED_DEPLOYMENT="${CLOUDFLARED_DEPLOYMENT:-cloudflared}"
CLOUDFLARED_SA="${CLOUDFLARED_SA:-cloudflared}"

AUTH_WORKLOAD_KIND="${AUTH_WORKLOAD_KIND:-deployment}"
AUTH_WORKLOAD_NAME="${AUTH_WORKLOAD_NAME:-auth-svc}"

PREDICT_WORKLOAD_KIND="${PREDICT_WORKLOAD_KIND:-deployment}"
PREDICT_WORKLOAD_NAME="${PREDICT_WORKLOAD_NAME:-predict-svc}"

mkdir -p "${OUT_DIR}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

download_linkerd_cli_if_needed() {
  if command -v linkerd >/dev/null 2>&1; then
    return
  fi

  echo "INFO: linkerd CLI not found; installing ${LINKERD2_VERSION}..."
  curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install-edge \
    | LINKERD2_VERSION="${LINKERD2_VERSION}" sh

  export PATH="${HOME}/.linkerd2/bin:${PATH}"
  command -v linkerd >/dev/null 2>&1 || {
    echo "ERROR: linkerd CLI install failed" >&2
    exit 1
  }
}

render_gateway_api_crds() {
  echo "INFO: rendering Gateway API CRDs"
  curl -fsSL "${GATEWAY_API_URL}" -o "${OUT_DIR}/00-gateway-api.yaml"
}

render_linkerd_manifests() {
  echo "INFO: rendering Linkerd control plane manifests"
  linkerd install --crds > "${OUT_DIR}/01-linkerd-crds.yaml"
  linkerd install > "${OUT_DIR}/02-linkerd-control-plane.yaml"
  linkerd viz install > "${OUT_DIR}/03-linkerd-viz.yaml"
}

render_namespace_manifests() {
  cat > "${OUT_DIR}/10-namespace-edge.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${EDGE_NAMESPACE}
  annotations:
    linkerd.io/inject: enabled
EOF

  cat > "${OUT_DIR}/11-namespace-auth.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${AUTH_NAMESPACE}
  annotations:
    linkerd.io/inject: enabled
    config.linkerd.io/default-inbound-policy: deny
EOF

  cat > "${OUT_DIR}/12-namespace-predict.yaml" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${PREDICT_NAMESPACE}
  annotations:
    linkerd.io/inject: enabled
    config.linkerd.io/default-inbound-policy: deny
EOF

  cat > "${OUT_DIR}/13-serviceaccount-cloudflared.yaml" <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${CLOUDFLARED_SA}
  namespace: ${EDGE_NAMESPACE}
EOF
}

render_policy_manifests() {
  # Namespace-level AuthorizationPolicy:
  # only the cloudflared service account from the edge namespace may talk to
  # any policy target in the auth / predict namespaces.
  cat > "${OUT_DIR}/20-auth-authorizationpolicy.yaml" <<EOF
apiVersion: policy.linkerd.io/v1alpha1
kind: AuthorizationPolicy
metadata:
  name: allow-cloudflared
  namespace: ${AUTH_NAMESPACE}
spec:
  targetRef:
    kind: Namespace
    name: ${AUTH_NAMESPACE}
  requiredAuthenticationRefs:
    - kind: ServiceAccount
      name: ${CLOUDFLARED_SA}
      namespace: ${EDGE_NAMESPACE}
EOF

  cat > "${OUT_DIR}/21-predict-authorizationpolicy.yaml" <<EOF
apiVersion: policy.linkerd.io/v1alpha1
kind: AuthorizationPolicy
metadata:
  name: allow-cloudflared
  namespace: ${PREDICT_NAMESPACE}
spec:
  targetRef:
    kind: Namespace
    name: ${PREDICT_NAMESPACE}
  requiredAuthenticationRefs:
    - kind: ServiceAccount
      name: ${CLOUDFLARED_SA}
      namespace: ${EDGE_NAMESPACE}
EOF
}

apply_file() {
  local file="$1"
  echo "INFO: applying $(basename "${file}")"
  kubectl apply -f "${file}"
}

annotate_namespace() {
  local ns="$1"
  kubectl annotate ns "${ns}" linkerd.io/inject=enabled --overwrite
}

set_namespace_default_deny() {
  local ns="$1"
  kubectl annotate ns "${ns}" config.linkerd.io/default-inbound-policy=deny --overwrite
}

restart_workload() {
  local kind="$1"
  local name="$2"
  local ns="$3"

  case "${kind}" in
    deployment|deploy|deployments)
      kubectl -n "${ns}" rollout restart deployment/"${name}"
      kubectl -n "${ns}" rollout status deployment/"${name}" --timeout=300s
      ;;
    statefulset|sts)
      kubectl -n "${ns}" rollout restart statefulset/"${name}"
      kubectl -n "${ns}" rollout status statefulset/"${name}" --timeout=300s
      ;;
    daemonset|ds)
      kubectl -n "${ns}" rollout restart daemonset/"${name}"
      kubectl -n "${ns}" rollout status daemonset/"${name}" --timeout=300s
      ;;
    job)
      echo "WARN: job restart is not handled automatically for ${name} in ${ns}"
      ;;
    none|"")
      echo "INFO: skipping restart for ${name} (${kind}) in ${ns}"
      ;;
    *)
      echo "WARN: unsupported workload kind '${kind}' for ${name} in ${ns}; skipping automatic restart"
      ;;
  esac
}

verify_meshing() {
  echo "INFO: checking Linkerd data plane"
  linkerd check --proxy
}

main() {
  need_cmd curl
  need_cmd kubectl
  download_linkerd_cli_if_needed

  # 1) Install Gateway API CRDs first, then verify the cluster.
  render_gateway_api_crds
  apply_file "${OUT_DIR}/00-gateway-api.yaml"

  echo "INFO: validating cluster before Linkerd install"
  linkerd check --pre

  # 2) Install Linkerd control plane and viz.
  render_linkerd_manifests
  apply_file "${OUT_DIR}/01-linkerd-crds.yaml"
  apply_file "${OUT_DIR}/02-linkerd-control-plane.yaml"
  apply_file "${OUT_DIR}/03-linkerd-viz.yaml"

  echo "INFO: checking Linkerd control plane"
  linkerd check

  # 3) Render the namespace and policy manifests for your system.
  render_namespace_manifests
  render_policy_manifests

  apply_file "${OUT_DIR}/10-namespace-edge.yaml"
  apply_file "${OUT_DIR}/11-namespace-auth.yaml"
  apply_file "${OUT_DIR}/12-namespace-predict.yaml"
  apply_file "${OUT_DIR}/13-serviceaccount-cloudflared.yaml"
  apply_file "${OUT_DIR}/20-auth-authorizationpolicy.yaml"
  apply_file "${OUT_DIR}/21-predict-authorizationpolicy.yaml"

  # 4) Ensure namespaces are annotated for injection.
  annotate_namespace "${EDGE_NAMESPACE}"
  annotate_namespace "${AUTH_NAMESPACE}"
  annotate_namespace "${PREDICT_NAMESPACE}"

  set_namespace_default_deny "${AUTH_NAMESPACE}"
  set_namespace_default_deny "${PREDICT_NAMESPACE}"

  # 5) Restart workloads so they actually get injected.
  restart_workload "${AUTH_WORKLOAD_KIND}" "${AUTH_WORKLOAD_NAME}" "${AUTH_NAMESPACE}"
  restart_workload "${PREDICT_WORKLOAD_KIND}" "${PREDICT_WORKLOAD_NAME}" "${PREDICT_NAMESPACE}"
  restart_workload deployment "${CLOUDFLARED_DEPLOYMENT}" "${EDGE_NAMESPACE}"

  # 6) Final verification.
  verify_meshing

  echo "INFO: done"
  echo "INFO: manifests rendered under ${OUT_DIR}"
}

main "$@"