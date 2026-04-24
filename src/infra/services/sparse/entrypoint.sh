#!/bin/sh
set -eu

# verify baked model exists
if [ ! -f /app/.baked_sparse_model ]; then
  echo "ERROR: baked sparse model file missing" >&2
  exit 1
fi

BAKED_MODEL=$(cat /app/.baked_sparse_model)
CUR_MODEL="${SPARSE_MODEL_NAME:-}"

# If runtime env not set, assume baked (common for no-override runs)
if [ -z "$CUR_MODEL" ]; then CUR_MODEL="$BAKED_MODEL"; fi

if [ "$CUR_MODEL" != "$BAKED_MODEL" ]; then
  echo "ERROR: runtime SPARSE_MODEL_NAME must match the baked image value" >&2
  echo "  Baked:   SPARSE_MODEL_NAME=${BAKED_MODEL}" >&2
  echo "  Runtime: SPARSE_MODEL_NAME=${CUR_MODEL:-<unset>}" >&2
  exit 1
fi

exec uvicorn host_sparse:app --host "${SPARSE_HOST:-0.0.0.0}" --port "${SPARSE_PORT:-8201}" --lifespan on
