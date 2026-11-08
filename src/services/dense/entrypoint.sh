#!/bin/sh
set -eu

# Ensure sensible defaults if not provided
: "${DENSE_HOST:=0.0.0.0}"
: "${DENSE_PORT:=8200}"
: "${UVICORN_WORKERS:=1}"

# Note: this script runs as non-root (appuser). It intentionally does not
# attempt privileged operations. If you need to bind to ports <1024 or write
# to protected paths, run the container with appropriate capabilities or as root.

exec uvicorn host_dense:app \
  --host "${DENSE_HOST}" \
  --port "${DENSE_PORT}" \
  --lifespan on \
  --loop uvloop \
  --http httptools \
  --workers "${UVICORN_WORKERS}"