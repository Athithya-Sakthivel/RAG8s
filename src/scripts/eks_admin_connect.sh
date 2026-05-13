#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT_DIR}/infra/terraform/aws"

cd "$TF_DIR"

AWS_REGION="$(tofu output -raw aws_region)"
CLUSTER_NAME="$(tofu output -raw cluster_name)"
INSTANCE_ID="$(tofu output -raw eks_admin_instance_id)"

REMOTE_CMD=$(cat <<EOF
set -euo pipefail

export AWS_REGION="${AWS_REGION}"
export CLUSTER_NAME="${CLUSTER_NAME}"

if ! command -v kubectl >/dev/null 2>&1; then
  KUBECTL_VERSION="1.35.3"
  ARCH="\$(uname -m)"
  case "\$ARCH" in
    x86_64) KUBECTL_ARCH="amd64" ;;
    aarch64|arm64) KUBECTL_ARCH="arm64" ;;
    *)
      echo "Unsupported architecture: \$ARCH" >&2
      exit 1
      ;;
  esac

  curl -fsSLo /tmp/kubectl "https://s3.us-west-2.amazonaws.com/amazon-eks/\${KUBECTL_VERSION}/2026-04-08/bin/linux/\${KUBECTL_ARCH}/kubectl"
  chmod +x /tmp/kubectl
  mkdir -p "\$HOME/bin"
  mv /tmp/kubectl "\$HOME/bin/kubectl"
  export PATH="\$HOME/bin:\$PATH"
fi

aws eks update-kubeconfig --region "\$AWS_REGION" --name "\$CLUSTER_NAME"

kubectl get nodes -o wide
echo "--------------------------------------------------"
kubectl get pods -A -o wide
exec bash
EOF
)

export REMOTE_CMD
SESSION_PARAMS="$(
python3 - <<'PY'
import json, os
print(json.dumps({"command": [os.environ["REMOTE_CMD"]]}))
PY
)"

aws ssm start-session \
  --region "$AWS_REGION" \
  --target "$INSTANCE_ID" \
  --document-name AWS-StartInteractiveCommand \
  --parameters "$SESSION_PARAMS"