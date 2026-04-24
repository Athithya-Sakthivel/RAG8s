#!/usr/bin/env bash
# cd apps/reranker && bash test_reranker_locally.sh
set -euo pipefail

GREEN="\033[0;32m"
NC="\033[0m"

echo -e "${GREEN}=== Local reranker smoke test ===${NC}"

#############################################
# 1. Install CPU dependencies (quiet)
#############################################
echo "[1/5] Install CPU dependencies (quiet)"
pip install -q --upgrade pip
pip install -q numpy==2.2.6 fastembed==0.7.3 fastapi==0.122.0 uvicorn==0.38.0 prometheus_client==0.23.1

#############################################
# 2. Start server (unbuffered logs)
#############################################
echo "[2/5] Start server in background (logs -> reranker.log)"

pkill -f "host_reranker.py" >/dev/null 2>&1 || true
rm -f reranker.log

# Force unbuffered logging: PYTHONUNBUFFERED=1 python3 -u
nohup env PYTHONUNBUFFERED=1 python3 -u host_reranker.py > reranker.log 2>&1 &

#############################################
# 3. Poll until server is ready or fail
#############################################
echo -n "[3/5] Waiting for server to become ready"

timeout=40
count=0

while ! curl -sf http://localhost:8202/health >/dev/null 2>&1; do
    sleep 1
    count=$((count+1))
    echo -n "."

    if (( count >= timeout )); then
        echo ""
        echo " Server did not become ready within ${timeout}s"
        echo "---- Last 80 lines of reranker.log ----"
        tail -n 80 reranker.log || true
        exit 1
    fi
done

echo ""
echo "Server is ready."
echo "---- Server startup logs ----"
tail -n 40 reranker.log

#############################################
# 4. Rerank request
#############################################
echo "[4/5] Rerank request"
curl -s -X POST http://localhost:8202/rerank \
  -H "Content-Type: application/json" \
  -d '{"query":"what is retrieval?","documents":["doc one","doc two"]}' \
  | jq

#############################################
# 5. Metrics snapshot
#############################################
echo "[5/5] Metrics snapshot"
curl -s http://localhost:8202/metrics | grep -E "requests_total|request_duration_seconds_count|rerank_doc_count" || true

echo -e "${GREEN}Local reranker tests complete${NC}"
