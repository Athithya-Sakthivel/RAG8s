#!/usr/bin/env bash
set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────
TF_DIR="$(cd "$(dirname "$0")" && pwd)"
export TF_VAR_pii_mask_enabled="${TF_VAR_pii_mask_enabled:-true}"

# ─── Deploy ──────────────────────────────────────────────────────
cd "$TF_DIR"

echo "=== Initialising OpenTofu ==="
tofu init

echo "=== Planning ==="
tofu plan

echo "=== Applying ==="
tofu apply --auto-approve

# ─── Export outputs ──────────────────────────────────────────────
export BEDROCK_GUARDRAIL_IDENTIFIER="$(tofu output -raw bedrock_guardrail_arn)"
export BEDROCK_GUARDRAIL_VERSION="$(tofu output -raw bedrock_guardrail_version_id)"

echo ""
echo "=== Guardrail deployed ==="
echo "ARN:     $BEDROCK_GUARDRAIL_IDENTIFIER"
echo "Version: $BEDROCK_GUARDRAIL_VERSION"
echo "PII mask: $TF_VAR_pii_mask_enabled"

# ─── Optional: destroy (uncomment to use) ────────────────────────
# cd src/infra/terraform/temp && tofu destroy --auto-approve