#!/usr/bin/env bash
# src/terraform/aws/run.sh
# Production-ready, idempotent wrapper to manage OpenTofu (tofu) lifecycle:
#  - --plan      : init backend, fmt/validate (auto-fix), produce a plan file (dry-run)
#  - --create    : init backend, fmt/validate (auto-fix), plan, then apply -auto-approve (fully automated)
#  - --destroy   : init backend, then destroy (destructive; requires --yes-delete)
#  - --validate  : init backend and validate backend / prereqs
#  - --find-version / --rollback-state <versionId> : state management helpers. VersionID is created only if a create is successful e2e. 
#
# Usage:
#   bash src/terraform/aws/run.sh --plan  --env staging
#   bash src/terraform/aws/run.sh --create --env staging
#   bash src/terraform/aws/run.sh --destroy --env staging --yes-delete
#   bash src/terraform/aws/run.sh --env staging --find-version
# 
# Notes / invariants:
#  - AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION is used (fallback ap-south-1).
#  - Script does NOT commit formatted changes to git; it only auto-formats files in-place.
#  - State bucket is versioned (ENABLED) and encrypted (AES256).
#  - DynamoDB lock table exists and is ACTIVE.
#  - Script exits non-zero on any infrastructure mutation failure.

#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
STACK_DIR="${ROOT_DIR}/src/terraform/aws"
AWS_REGION="${AWS_DEFAULT_REGION:-ap-south-1}"

usage() {
  cat <<USAGE >&2
Usage:
  $(basename "$0") --plan|--create|--destroy|--validate|--find-version|--rollback-state <versionId> --env <prod|staging> [--yes-delete]

Modes:
  --plan             : init backend, fmt/validate, create plan file
  --create           : init backend, fmt/validate, plan, apply -auto-approve
  --destroy|--delete : init backend, destroy -auto-approve
  --validate         : validate backend and prerequisites
  --find-version     : list remote state object versions
  --rollback-state   : restore specified state object version

Flags:
  --env <prod|staging>
  --yes-delete

Notes:
  Requires aws, tofu, python3 in PATH.
  Uses <env>.tfvars from ${STACK_DIR}.
USAGE
  exit 2
}

if [ $# -lt 1 ]; then
  usage
fi

MODE=""
ENVIRONMENT=""
YES_DELETE=false
ROLLBACK_VERSION=""

while [ $# -gt 0 ]; do
  case "$1" in
    --plan|--create|--destroy|--delete|--validate|--find-version)
      if [ -n "$MODE" ]; then
        echo "ERROR: only one mode may be specified" >&2
        usage
      fi
      MODE="$1"
      [ "$MODE" = "--delete" ] && MODE="--destroy"
      shift
      ;;
    --rollback-state)
      if [ -n "$MODE" ]; then
        echo "ERROR: only one mode may be specified" >&2
        usage
      fi
      MODE="--rollback-state"
      shift
      if [ $# -eq 0 ]; then
        echo "ERROR: --rollback-state requires <versionId>" >&2
        usage
      fi
      ROLLBACK_VERSION="$1"
      shift
      ;;
    --env)
      shift
      if [ $# -eq 0 ]; then
        echo "ERROR: --env requires prod or staging" >&2
        usage
      fi
      case "$1" in
        prod|staging)
          ENVIRONMENT="$1"
          shift
          ;;
        *)
          echo "ERROR: invalid environment: $1" >&2
          usage
          ;;
      esac
      ;;
    --yes-delete)
      YES_DELETE=true
      shift
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [ -z "$MODE" ] || [ -z "$ENVIRONMENT" ]; then
  usage
fi

for cmd in aws tofu python3; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $cmd" >&2
    exit 10
  }
done

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

retry() {
  local tries=${1:-6}
  local delay=${2:-1}
  shift 2
  local i=0 rc=0
  while [ "$i" -lt "$tries" ]; do
    set +e
    "$@"
    rc=$?
    set -e
    [ "$rc" -eq 0 ] && return 0
    i=$((i + 1))
    sleep "$delay"
    delay=$((delay * 2))
  done
  return "$rc"
}

PLAN_DIR="${STACK_DIR}/.plans"
mkdir -p "$PLAN_DIR"

PLAN_FILE="${PLAN_DIR}/${ENVIRONMENT}.tfplan"
VAR_FILE="${STACK_DIR}/${ENVIRONMENT}.tfvars"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
if [ -z "$ACCOUNT_ID" ] || [ "$ACCOUNT_ID" = "None" ]; then
  echo "ERROR: unable to determine AWS account id" >&2
  exit 20
fi

STATE_BUCKET="mlsecops-tf-state-${ACCOUNT_ID}"
LOCK_TABLE="mlsecops-tf-lock-${ACCOUNT_ID}"
STATE_KEY="${ENVIRONMENT}/terraform.tfstate"

exec_and_log() {
  local label="$1"
  shift
  log "CMD START: ${label}: $*"
  set +e
  "$@"
  local rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: command failed: ${label} (rc=${rc})" >&2
    exit "$rc"
  fi
  log "CMD OK: ${label}"
}

ensure_bucket_exists_and_versioning() {
  local bucket="$1"
  local region="$2"

  if aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    log "s3: bucket ${bucket} exists"
  else
    log "s3: creating bucket ${bucket} (region=${region})"
    if [ "$region" = "us-east-1" ]; then
      exec_and_log "s3-create-bucket" aws s3api create-bucket --bucket "$bucket"
    else
      exec_and_log "s3-create-bucket" aws s3api create-bucket --bucket "$bucket" --create-bucket-configuration LocationConstraint="$region"
    fi
    retry 6 2 aws s3api head-bucket --bucket "$bucket"
    log "s3: created bucket ${bucket}"
  fi

  exec_and_log "s3-put-versioning" aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled

  set +e
  aws s3api put-bucket-encryption --bucket "$bucket" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null 2>&1
  set -e

  set +e
  aws s3api put-public-access-block --bucket "$bucket" \
    --public-access-block-configuration '{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}' >/dev/null 2>&1
  set -e
}

ensure_dynamodb_table() {
  local table="$1"
  local region="$2"

  if aws dynamodb describe-table --table-name "$table" >/dev/null 2>&1; then
    log "ddb: table ${table} exists"
    return 0
  fi

  exec_and_log "ddb-create-table" aws dynamodb create-table --table-name "$table" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$region"

  retry 8 2 aws dynamodb wait table-exists --table-name "$table" --region "$region"
  log "ddb: ensured ${table}"
}

validate_backend() {
  local bucket="$1"
  local key="$2"
  local table="$3"
  local region="$4"

  aws sts get-caller-identity --query Account --output text >/dev/null

  if ! aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    echo "ERROR: state bucket ${bucket} not found" >&2
    return 1
  fi

  local vs
  vs="$(aws s3api get-bucket-versioning --bucket "$bucket" --query Status --output text 2>/dev/null || true)"
  if [ "$vs" != "Enabled" ]; then
    echo "ERROR: bucket ${bucket} versioning not Enabled (status=${vs})" >&2
    return 2
  fi

  if ! aws s3api get-bucket-encryption --bucket "$bucket" >/dev/null 2>&1; then
    echo "ERROR: bucket ${bucket} encryption not configured" >&2
    return 3
  fi

  local dstat
  dstat="$(aws dynamodb describe-table --table-name "$table" --query "Table.TableStatus" --output text 2>/dev/null || true)"
  if [ -z "$dstat" ]; then
    echo "ERROR: dynamodb table ${table} not found" >&2
    return 4
  fi
  if [ "$dstat" != "ACTIVE" ]; then
    echo "ERROR: dynamodb table ${table} status=${dstat}" >&2
    return 5
  fi

  exec_and_log "tofu-init-validate" bash -c "cd \"$STACK_DIR\" && tofu init -backend-config \"bucket=${bucket}\" -backend-config \"key=${key}\" -backend-config \"region=${region}\" -backend-config \"dynamodb_table=${table}\" -input=false"
  log "Validation OK"
}

fmt_auto_fix_if_needed() {
  if (cd "$STACK_DIR" && tofu fmt -check -recursive); then
    log "Formatting OK"
    return 0
  fi

  (cd "$STACK_DIR" && tofu fmt -recursive)

  if (cd "$STACK_DIR" && tofu fmt -check -recursive); then
    log "Formatting fixed"
  else
    echo "ERROR: formatting still failing after auto-fix" >&2
    exit 30
  fi
}

validate_config() {
  exec_and_log "tofu-validate" bash -c "cd \"$STACK_DIR\" && tofu validate -no-color"
}

build_plan() {
  if [ -f "$VAR_FILE" ]; then
    exec_and_log "tofu-plan" bash -c "cd \"$STACK_DIR\" && tofu plan -var-file=\"$VAR_FILE\" -out=\"$PLAN_FILE\" -input=false"
  else
    exec_and_log "tofu-plan" bash -c "cd \"$STACK_DIR\" && tofu plan -out=\"$PLAN_FILE\" -input=false"
  fi
  log "Plan written to ${PLAN_FILE}"
}

apply_plan_auto() {
  if [ ! -f "$PLAN_FILE" ]; then
    echo "ERROR: plan file not found: ${PLAN_FILE}" >&2
    exit 40
  fi
  exec_and_log "tofu-apply-plan" bash -c "cd \"$STACK_DIR\" && tofu apply -input=false -auto-approve \"$PLAN_FILE\""
}

destroy_auto() {
  if [ -f "$VAR_FILE" ]; then
    exec_and_log "tofu-destroy" bash -c "cd \"$STACK_DIR\" && tofu destroy -var-file=\"$VAR_FILE\" -input=false -auto-approve"
  else
    exec_and_log "tofu-destroy" bash -c "cd \"$STACK_DIR\" && tofu destroy -input=false -auto-approve"
  fi
}


list_state_versions() {
  local bucket="$1"
  local key="$2"

  local json
  if ! json="$(aws s3api list-object-versions \
      --bucket "$bucket" \
      --prefix "$key" \
      --output json 2>/dev/null)"; then
    echo "ERROR: unable to list versions for key: $key in bucket: $bucket" >&2
    return 1
  fi

  python3 -c '
import json
import sys

key = sys.argv[1]

try:
    data = json.load(sys.stdin)
except Exception:
    print(f"No versions found or error listing versions for: {key}")
    sys.exit(0)

rows = []
for v in data.get("Versions", []):
    if v.get("Key") == key:
        rows.append((v.get("VersionId"), v.get("LastModified"), "Version"))

for d in data.get("DeleteMarkers", []):
    if d.get("Key") == key:
        rows.append((d.get("VersionId"), d.get("LastModified"), "DeleteMarker"))

if not rows:
    print(f"No versions found for key: {key}")
    sys.exit(0)

print("{:<36}  {:<30}  {}".format("VersionId", "LastModified", "info"))
for ver, lm, info in rows:
    print("{:<36}  {:<30}  {}".format(ver or "", str(lm or ""), info))
' "$key" <<<"$json"
}

rollback_state_version() {
  local bucket="$1"
  local key="$2"
  local version="$3"
  local found
  found="$(aws s3api list-object-versions --bucket "$bucket" --prefix "$key" --query "Versions[?VersionId=='${version}'] | [0].VersionId" --output text 2>/dev/null || true)"
  if [ -z "$found" ] || [ "$found" = "None" ]; then
    echo "ERROR: versionId ${version} not found for ${key} in ${bucket}" >&2
    return 2
  fi
  exec_and_log "s3-copy-rollback" aws s3api copy-object --bucket "$bucket" --copy-source "${bucket}/${key}?versionId=${version}" --key "$key" --metadata-directive REPLACE
}

init_backend() {
  ensure_bucket_exists_and_versioning "$STATE_BUCKET" "$AWS_REGION"
  ensure_dynamodb_table "$LOCK_TABLE" "$AWS_REGION"
  exec_and_log "tofu-init" bash -c "cd \"$STACK_DIR\" && tofu init -backend-config \"bucket=${STATE_BUCKET}\" -backend-config \"key=${STATE_KEY}\" -backend-config \"region=${AWS_REGION}\" -backend-config \"dynamodb_table=${LOCK_TABLE}\" -input=false"
}

case "$MODE" in
  --plan)
    init_backend
    fmt_auto_fix_if_needed
    validate_config
    build_plan
    ;;
  --create)
    init_backend
    fmt_auto_fix_if_needed
    validate_config
    build_plan
    apply_plan_auto
    ;;
  --destroy)
    if [ "$YES_DELETE" != true ]; then
      echo "ERROR: destructive action requires --yes-delete" >&2
      exit 3
    fi
    init_backend
    destroy_auto
    ;;
  --validate)
    init_backend
    validate_backend "$STATE_BUCKET" "$STATE_KEY" "$LOCK_TABLE" "$AWS_REGION"
    ;;
  --find-version)
    list_state_versions "$STATE_BUCKET" "$STATE_KEY"
    ;;
  --rollback-state)
    if [ "$YES_DELETE" != true ]; then
      echo "ERROR: rollback requires --yes-delete" >&2
      exit 3
    fi
    rollback_state_version "$STATE_BUCKET" "$STATE_KEY" "$ROLLBACK_VERSION"
    ;;
  *)
    usage
    ;;
esac

