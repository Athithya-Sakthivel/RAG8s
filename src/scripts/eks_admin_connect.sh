#!/usr/bin/env bash
set -euo pipefail

TF_DIR="src/infra/terraform/aws"
REGION="$(cd "$TF_DIR" && tofu output -raw aws_region)"
INSTANCE_ID="$(cd "$TF_DIR" && tofu output -raw eks_admin_instance_id)"
CLUSTER_NAME="$(cd "$TF_DIR" && tofu output -raw cluster_name)"
aws ssm send-command \
  --region "$REGION" \
  --document-name "AWS-RunShellScript" \
  --instance-ids "$INSTANCE_ID" \
  --parameters commands="aws eks update-kubeconfig --region $REGION --name $CLUSTER_NAME","kubectl get nodes -o wide","kubectl get pods -A -o wide" \
  --comment "EKS admin quick check"

aws ssm start-session --region "$REGION" --target "$INSTANCE_ID"

# run inside shell

# aws eks update-kubeconfig --region ap-south-1 --name rag-eks-staging
# sudo curl -L -o /usr/local/bin/kubectl https://s3.us-west-2.amazonaws.com/amazon-eks/1.35.3/2026-04-08/bin/linux/amd64/kubectl
# sudo chmod +x /usr/local/bin/kubectl && kubectl version --client && sudo dnf install git -y && cd ~