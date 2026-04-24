#!/bin/sh
set -eu

# Ensure baked file exists
if [ ! -f /app/.baked_reranker_model ]; then
  echo "ERROR: baked reranker model file missing from image" >&2
  exit 1
fi

BAKED_MODEL=$(cat /app/.baked_reranker_model)
CUR_MODEL="${RERANKER_MODEL_NAME:-}"

# If runtime variables are empty, treat them as the baked values (common when no env override)
if [ -z "$CUR_MODEL" ]; then CUR_MODEL="$BAKED_MODEL"; fi

if [ "$CUR_MODEL" != "$BAKED_MODEL" ]; then
  echo "ERROR: runtime RERANKER_MODEL_NAME must match the baked image value" >&2
  echo "  Baked:   RERANKER_MODEL_NAME=${BAKED_MODEL}" >&2
  echo "  Runtime: RERANKER_MODEL_NAME=${CUR_MODEL:-<unset>}" >&2
  exit 1
fi

exec uvicorn host_reranker:app --host "${RERANKER_HOST:-0.0.0.0}" --port "${RERANKER_PORT:-8202}" --lifespan on
