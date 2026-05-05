python3 infra/generators/gen_reranker.py --apply
python3 infra/generators/gen_dense.py --apply
python3 infra/generators/gen_sparse.py --apply

# ---- PORT-FORWARDS (nohup) ----
nohup kubectl port-forward -n models deploy/dense-deployment    8200:8200 > dense.pf.log 2>&1 &
nohup kubectl port-forward -n models deploy/sparse-deployment   8201:8201 > sparse.pf.log 2>&1 &
nohup kubectl port-forward -n models deploy/reranker-deployment 8202:8202 > reranker.pf.log 2>&1 &

# ---- HEALTHCHECKS ----
curl -s http://localhost:8200/health | jq .
curl -s http://localhost:8201/health | jq .
curl -s http://localhost:8202/health | jq .

# ---- TEST CALLS ----
curl -s -X POST http://localhost:8200/embed \
  -H "Content-Type: application/json" \
  -d '{"texts":["hello dense"]}' | jq .

curl -s -X POST http://localhost:8201/embed \
  -H "Content-Type: application/json" \
  -d '{"texts":["hello sparse"]}' | jq .

curl -s -X POST http://localhost:8202/rerank \
  -H "Content-Type: application/json" \
  -d '{"query":"hello","documents":["doc1","doc2"]}' | jq .
