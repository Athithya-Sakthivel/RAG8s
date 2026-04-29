#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-}"
if [[ -z "$MODE" ]]; then
  echo "Usage: backup_and_restore.sh <backup|restore>" >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
BACKUP_RUNNER="${BACKUP_RUNNER:-src/scripts/run_qdrant_backup.py}"
RESTORE_RUNNER="${RESTORE_RUNNER:-src/scripts/qdrant_restore.py}"

QDRANT_NAMESPACE="${QDRANT_NAMESPACE:-${NAMESPACE:-qdrant}}"
QDRANT_URL="${QDRANT_URL:-http://qdrant.qdrant.svc.cluster.local:6333}"
QDRANT_API_KEY="${QDRANT_API_KEY:-${QDRANT__SERVICE__API_KEY:-}}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
BACKUP_ID="${BACKUP_ID:-}"
PER_POD="${PER_POD:-false}"
PORT_BASE="${PORT_BASE:-7000}"

QDRANT_BACKUP_S3_BUCKET="${QDRANT_BACKUP_S3_BUCKET:-${BACKUP_S3_BUCKET:-${BACKUP_BUCKET:-${DATA_S3_BUCKET:-}}}}"
QDRANT_BACKUP_S3_PREFIX="${QDRANT_BACKUP_S3_PREFIX:-${BACKUP_S3_PREFIX:-${BACKUP_PREFIX:-data/backups/qdrant/}}}"
QDRANT_BACKUP_S3_PREFIX="${QDRANT_BACKUP_S3_PREFIX#/}"
QDRANT_BACKUP_S3_PREFIX="${QDRANT_BACKUP_S3_PREFIX%/}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found" >&2; exit 1; }
}

require_nonempty() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "$value" ]]; then
    echo "ERROR: $name is required" >&2
    exit 1
  fi
}

require_cmd "$PYTHON"

if [[ -z "$QDRANT_BACKUP_S3_BUCKET" ]]; then
  echo "ERROR: QDRANT_BACKUP_S3_BUCKET (or BACKUP_S3_BUCKET/BACKUP_BUCKET/DATA_S3_BUCKET) is required" >&2
  exit 1
fi
require_nonempty AWS_REGION

case "$MODE" in
  backup)
    echo "==> Qdrant backup started (bucket=$QDRANT_BACKUP_S3_BUCKET prefix=$QDRANT_BACKUP_S3_PREFIX namespace=$QDRANT_NAMESPACE)" >&2
    env \
      AWS_REGION="$AWS_REGION" \
      AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}" \
      DATA_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
      DATA_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
      BACKUP_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
      BACKUP_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
      BACKUP_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
      BACKUP_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
      QDRANT_BACKUP_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
      QDRANT_BACKUP_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
      QDRANT_URL="$QDRANT_URL" \
      QDRANT_API_KEY="$QDRANT_API_KEY" \
      QDRANT_NAMESPACE="$QDRANT_NAMESPACE" \
      "$PYTHON" "$BACKUP_RUNNER" --backup
    ;;
  restore)
    echo "==> Qdrant restore started (bucket=$QDRANT_BACKUP_S3_BUCKET prefix=$QDRANT_BACKUP_S3_PREFIX namespace=$QDRANT_NAMESPACE)" >&2
    if [[ -n "$BACKUP_ID" ]]; then
      env \
        AWS_REGION="$AWS_REGION" \
        AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}" \
        BACKUP_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
        BACKUP_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
        BACKUP_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
        BACKUP_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
        QDRANT_BACKUP_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
        QDRANT_BACKUP_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
        QDRANT_URL="$QDRANT_URL" \
        QDRANT_API_KEY="$QDRANT_API_KEY" \
        QDRANT_NAMESPACE="$QDRANT_NAMESPACE" \
        PER_POD="$PER_POD" \
        PORT_BASE="$PORT_BASE" \
        BACKUP_ID="$BACKUP_ID" \
        "$PYTHON" "$RESTORE_RUNNER"
    else
      env \
        AWS_REGION="$AWS_REGION" \
        AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}" \
        BACKUP_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
        BACKUP_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
        BACKUP_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
        BACKUP_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
        QDRANT_BACKUP_S3_BUCKET="$QDRANT_BACKUP_S3_BUCKET" \
        QDRANT_BACKUP_S3_PREFIX="$QDRANT_BACKUP_S3_PREFIX" \
        QDRANT_URL="$QDRANT_URL" \
        QDRANT_API_KEY="$QDRANT_API_KEY" \
        QDRANT_NAMESPACE="$QDRANT_NAMESPACE" \
        PER_POD="$PER_POD" \
        PORT_BASE="$PORT_BASE" \
        "$PYTHON" "$RESTORE_RUNNER"
    fi
    ;;
  *)
    echo "ERROR: unknown mode '$MODE' (expected backup|restore)" >&2
    exit 1
    ;;
esac
