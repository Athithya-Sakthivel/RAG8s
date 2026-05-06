#!/bin/sh
set -eu

: "${SPARSE_HOST:=0.0.0.0}"
: "${SPARSE_PORT:=8201}"
: "${UVICORN_WORKERS:=1}"

exec uvicorn host_sparse:app \
  --host "${SPARSE_HOST}" \
  --port "${SPARSE_PORT}" \
  --lifespan on \
  --loop uvloop \
  --http httptools \
  --workers "${UVICORN_WORKERS}"