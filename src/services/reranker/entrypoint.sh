#!/bin/sh
set -eu

: "${RERANKER_HOST:=0.0.0.0}"
: "${RERANKER_PORT:=8202}"
: "${UVICORN_WORKERS:=1}"

exec uvicorn host_reranker:app \
  --host "${RERANKER_HOST}" \
  --port "${RERANKER_PORT}" \
  --lifespan on \
  --loop uvloop \
  --http httptools \
  --workers "${UVICORN_WORKERS}"