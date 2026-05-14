#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TOFU_AWS_DIR="$REPO_ROOT/src/infra/terraform/aws"
TOFU_CF_DIR="$REPO_ROOT/src/infra/terraform/cloudflare"
HELM_DIR="$REPO_ROOT/src/infra/helm/inference_svc"
NAMESPACE="inference"
RELEASE_NAME="inference"

export AWS_REGION="$(tofu -chdir="$TOFU_AWS_DIR" output -raw aws_region)"
export AWS_DEFAULT_REGION="$AWS_REGION"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

FRONTEND_IAM_ROLE="$(tofu -chdir="$TOFU_AWS_DIR" output -raw frontend_irsa_role_arn 2>/dev/null || echo "arn:aws:iam::${ACCOUNT_ID}:role/rag-frontend-irsa-role")"
RETRIEVER_IAM_ROLE="$(tofu -chdir="$TOFU_AWS_DIR" output -raw retriever_irsa_role_arn 2>/dev/null || echo "arn:aws:iam::${ACCOUNT_ID}:role/rag-retriever-irsa-role")"
CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir="$TOFU_CF_DIR" output -raw cloudflare_tunnel_token)"
CLOUDFLARE_TUNNEL_NAME="$(tofu -chdir="$TOFU_CF_DIR" output -raw cloudflare_tunnel_name)"
DOMAIN="${DOMAIN:-rag.athithya.site}"

ECR_URL="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_TAG="${IMAGE_TAG:-staging}"
FRONTEND_IMAGE="${ECR_URL}/frontend:${IMAGE_TAG}"
RETRIEVER_IMAGE="${ECR_URL}/retriever:${IMAGE_TAG}"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic frontend-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}" \
  --from-literal=GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-}" \
  --from-literal=MICROSOFT_CLIENT_ID="${MICROSOFT_CLIENT_ID:-}" \
  --from-literal=MICROSOFT_CLIENT_SECRET="${MICROSOFT_CLIENT_SECRET:-}" \
  --from-literal=JWT_SECRET="${JWT_SECRET:-$(openssl rand -base64 32)}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic retriever-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=QDRANT_URL="${QDRANT_URL:-http://qdrant.qdrant.svc.cluster.local:6333}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic cloudflared-token \
  --namespace="$NAMESPACE" \
  --from-literal=token="$CLOUDFLARE_TUNNEL_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap frontend-image \
  --namespace="$NAMESPACE" \
  --from-literal=image="$FRONTEND_IMAGE" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap retriever-image \
  --namespace="$NAMESPACE" \
  --from-literal=image="$RETRIEVER_IMAGE" \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install "$RELEASE_NAME" "$HELM_DIR" \
  --namespace="$NAMESPACE" \
  --set frontend.image.tag="$FRONTEND_IMAGE" \
  --set retriever.image.tag="$RETRIEVER_IMAGE" \
  --set frontend.serviceAccount.annotations.eks\.amazonaws\.com/role-arn="$FRONTEND_IAM_ROLE" \
  --set retriever.serviceAccount.annotations.eks\.amazonaws\.com/role-arn="$RETRIEVER_IAM_ROLE" \
  --set cloudflared.tunnel.name="$CLOUDFLARE_TUNNEL_NAME" \
  --set cloudflared.tunnel.tokenSecretName="cloudflared-token" \
  --set cloudflared.ingress.rules[0].hostname="$DOMAIN" \
  --set cloudflared.ingress.rules[1].hostname="$DOMAIN" \
  --set cloudflared.ingress.rules[2].hostname="$DOMAIN" \
  --set cloudflared.ingress.rules[3].hostname="$DOMAIN" \
  --wait --timeout 10m

kubectl get pods -n "$NAMESPACE"