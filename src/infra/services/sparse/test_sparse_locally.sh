#!/usr/bin/env bash
# bash apps/sparse/test_sparse_locally.sh

set -euo pipefail
cd apps/sparse
GREEN="\033[0;32m"
NC="\033[0m"

echo -e "${GREEN}=== Local sparse smoke test ===${NC}"

echo "[1/5] Install CPU deps (quiet)"
pip install -q --upgrade pip
pip install -q numpy==2.2.6 fastembed==0.7.3 fastapi==0.122.0 uvicorn==0.38.0 prometheus_client==0.23.1

echo "[2/5] Start server in background (logs -> sparse.log)"
pkill -f "host_sparse.py" >/dev/null 2>&1 || true
nohup python3 host_sparse.py > sparse.log 2>&1 &
sleep 4
echo "Server log tail:"
tail -n 12 sparse.log || true

echo "[3/5] Healthcheck"
curl -s http://localhost:8201/health | jq || true

echo "[4/5] Embed test"
curl -s -X POST http://localhost:8201/embed \
  -H "Content-Type: application/json" \
  -d '{"texts":["hello sparse world"]}' | jq || true

echo "[5/5] Metrics snapshot"
curl -s http://localhost:8201/metrics | grep -E "requests_total|request_duration_seconds_count|embed_batch_size_count" || true

echo -e "${GREEN}Local sparse tests complete${NC}"
