#!/usr/bin/env bash
set -euo pipefail

TF_DIR="src/infra/terraform/aws"

REGION="$(cd "$TF_DIR" && tofu output -raw aws_region)"
INSTANCE_ID="$(cd "$TF_DIR" && tofu output -raw eks_admin_instance_id)"
CLUSTER_NAME="$(cd "$TF_DIR" && tofu output -raw cluster_name)"

REMOTE_CMD="aws eks update-kubeconfig --region ${REGION} --name ${CLUSTER_NAME} && kubectl get nodes -o wide && kubectl get pods -A -o wide && bash"
SESSION_PARAMS=$(printf '{"command":["%s"]}' "$REMOTE_CMD")

aws ssm start-session \
  --region "$REGION" \
  --target "$INSTANCE_ID" \
  --document-name AWS-StartInteractiveCommand \
  --parameters "$SESSION_PARAMS"