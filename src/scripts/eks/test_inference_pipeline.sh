#!/bin/bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_step()    { echo -e "\n${BLUE}[STEP]${NC} ${YELLOW}$*${NC}"; }
log_info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_success() { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
TOFU_AWS_DIR="$REPO_ROOT/src/infra/terraform/aws"
TOFU_CF_DIR="$REPO_ROOT/src/infra/terraform/cloudflare"
ARGOCD_APP_DIR="$REPO_ROOT/src/infra/argocd"
INFERENCE_APP_YAML="${ARGOCD_APP_DIR}/inference-application.yaml"
GH_REPO="${GH_REPO:-https://github.com/Athithya-Sakthivel/E2E-RAG-System.git}"
NAMESPACE="inference"

for cmd in tofu aws kubectl jq openssl; do
    command -v "$cmd" >/dev/null 2>&1 || { log_error "$cmd required"; exit 1; }
done

log_step "1/5 Fetching infrastructure outputs"
AWS_REGION="$(tofu -chdir="$TOFU_AWS_DIR" output -raw aws_region)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

FRONTEND_IAM_ROLE="$(tofu -chdir="$TOFU_AWS_DIR" output -raw frontend_irsa_role_arn 2>/dev/null || echo "arn:aws:iam::${ACCOUNT_ID}:role/rag-frontend-irsa-role")"
RETRIEVER_IAM_ROLE="$(tofu -chdir="$TOFU_AWS_DIR" output -raw retriever_irsa_role_arn 2>/dev/null || echo "arn:aws:iam::${ACCOUNT_ID}:role/rag-retriever-irsa-role")"
CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir="$TOFU_CF_DIR" output -raw cloudflare_tunnel_token)"
CLOUDFLARE_TUNNEL_NAME="$(tofu -chdir="$TOFU_CF_DIR" output -raw cloudflare_tunnel_name)"
DOMAIN="${DOMAIN:-rag.athithya.site}"

ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_TAG="${IMAGE_TAG:-staging}"
FRONTEND_IMAGE="${ECR_URL}/frontend:${IMAGE_TAG}"
RETRIEVER_IMAGE="${ECR_URL}/retriever:${IMAGE_TAG}"

log_info "Region:      $AWS_REGION"
log_info "ECR base:    $ECR_URL"
log_info "Frontend:    $FRONTEND_IMAGE"
log_info "Retriever:   $RETRIEVER_IMAGE"
log_info "Domain:      $DOMAIN"
log_info "Tunnel:      $CLOUDFLARE_TUNNEL_NAME"

log_step "2/5 Creating required secrets"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -


PRIVATE_KEY_PEM=$(openssl ecparam -genkey -name prime256v1 -noout 2>/dev/null)

kubectl create secret generic frontend-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=MICROSOFT_CLIENT_ID="${MICROSOFT_CLIENT_ID:-}" \
  --from-literal=MICROSOFT_CLIENT_SECRET="${MICROSOFT_CLIENT_SECRET:-}" \
  --from-literal=JWT_SECRET="$(openssl rand -base64 32)" \
  --from-literal=SESSION_SECRET="$(openssl rand -base64 32)" \
  --from-literal=JWT_PRIVATE_KEY_PEM="$PRIVATE_KEY_PEM" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic retriever-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=QDRANT_URL="${QDRANT_URL:-http://qdrant.qdrant.svc.cluster.local:6333}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic cloudflared-token \
  --namespace="$NAMESPACE" \
  --from-literal=token="$CLOUDFLARE_TUNNEL_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

log_success "Secrets created/updated"

log_step "3/5 Generating ArgoCD Application YAML"
mkdir -p "$ARGOCD_APP_DIR"

cat > "$INFERENCE_APP_YAML" <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: inference
  namespace: argocd
  labels:
    app.kubernetes.io/name: inference
    app.kubernetes.io/managed-by: argocd
    app.kubernetes.io/part-of: e2e-rag-system
  annotations:
    description: Inference services (frontend, retriever, cloudflared)
    deployed-at: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    aws-region: "${AWS_REGION}"
spec:
  project: default
  source:
    repoURL: ${GH_REPO}
    targetRevision: main
    path: src/infra/helm/inference_svc
    helm:
      values: |
        frontend:
          image: "${FRONTEND_IMAGE}"
          serviceAccount:
            annotations:
              eks.amazonaws.com/role-arn: "${FRONTEND_IAM_ROLE}"
          env:
            FRONTEND_HOSTNAME: "${DOMAIN}"
            MS_TENANT_ID: "${MICROSOFT_ALLOWED_TENANT_IDS:-}"
            GOOGLE_ALLOWED_DOMAINS: "${GOOGLE_ALLOWED_DOMAINS:-gmail.com}"
            MICROSOFT_ALLOWED_DOMAINS: "${MICROSOFT_ALLOWED_DOMAINS:-outlook.com}"
        retriever:
          image: "${RETRIEVER_IMAGE}"
          serviceAccount:
            annotations:
              eks.amazonaws.com/role-arn: "${RETRIEVER_IAM_ROLE}"
        cloudflared:
          tunnel:
            name: "${CLOUDFLARE_TUNNEL_NAME}"
            tokenSecretName: cloudflared-token
          ingress:
            rules:
              - hostname: ${DOMAIN}
                path: /metrics
                service: http_status:403
              - hostname: ${DOMAIN}
                path: /healthz
                service: http_status:403
              - hostname: ${DOMAIN}
                path: /readyz
                service: http_status:403
              - hostname: ${DOMAIN}
                service: http://frontend.inference.svc.cluster.local:8000
                originRequest:
                  connectTimeout: 10s
                  keepAliveTimeout: 30s
                  disableChunkedEncoding: false
              - hostname: argocd.athithya.site
                service: http://argocd-server.argocd.svc.cluster.local:80
                originRequest:
                  connectTimeout: 10s
                  keepAliveTimeout: 30s
              - hostname: grafana.athithya.site
                service: http://grafana.grafana.svc.cluster.local:80
                originRequest:
                  connectTimeout: 10s
                  keepAliveTimeout: 30s
            catchAll: http_status:404
  destination:
    server: https://kubernetes.default.svc
    namespace: ${NAMESPACE}
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
        duration: 10s
        factor: 2
        maxDuration: 3m
EOF

log_success "Generated $INFERENCE_APP_YAML"

log_step "4/5 Applying ArgoCD Application"
kubectl delete application inference -n argocd --ignore-not-found
sleep 3
kubectl apply -f "$INFERENCE_APP_YAML"
log_info "Inference application submitted to ArgoCD"

log_step "5/5 Waiting for sync (max 10 min)"
sleep 5
kubectl wait --for=condition=ready pod \
  -l "app.kubernetes.io/component in (frontend,retriever)" \
  -n "$NAMESPACE" \
  --timeout=600s 2>/dev/null || log_warn "Timeout – check ArgoCD UI"

echo ""
log_success "Deployment complete"
kubectl get pods -n "$NAMESPACE"

echo -e "\n${CYAN}ArgoCD application status:${NC}"
kubectl get application inference -n argocd