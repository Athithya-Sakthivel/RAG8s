# python3 src/offline_eval/view_chunks.py

python3 src/infra/rag/qdrant_service.py --rollout

export DENSE_MODEL_NAME=BAAI/bge-small-en-v1.5
export DENSE_DIM=384
export DENSE_BATCH_SIZE=64 # upper bound
python3 src/infra/rag/dense_service.py --rollout

export SPARSE_MODEL_NAME=Qdrant/minicoil-v1
export SPARSE_BATCH_SIZE=64 # upper bound
python3 src/infra/rag/sparse_service.py --rollout


kubectl delete ns qdrant
python3 src/infra/rag/qdrant_service.py --rollout
export PER_POD=true
export QDRANT_BACKUP_S3_PREFIX=qdrant/backups/
export BACKUP_S3_BUCKET=$DATA_S3_BUCKET
bash src/scripts/backups_and_restore.sh restore


export RERANKER_MODEL_NAME=Xenova/ms-marco-MiniLM-L-6-v2
export RERANKER_MAX_DOCS=20 # upper bound
python3 src/infra/rag/reranker_service.py --rollout
kubectl patch deployment reranker-deployment -n models \
  --type='json' \
  -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"6"},
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"4Gi"},
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/cpu","value":"2"},
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"2Gi"}
  ]'


kubectl port-forward -n models svc/dense-svc 8200:8200 &
kubectl port-forward -n models svc/sparse-svc 8201:8201 &
kubectl port-forward -n models svc/reranker-svc 8202:8202 &
kubectl port-forward -n qdrant svc/qdrant 6333:6333 &

curl -X DELETE http://localhost:6333/collections/default_rag_collection1__semantic_cache


source .venv/bin/activate && cd /workspace/src/services/retriever && export PYTHONPATH=$(pwd)

export DENSE_URL="http://localhost:8200"
export SPARSE_URL="http://localhost:8201"
export RERANKER_URL="http://localhost:8202"
export QDRANT_URL="http://localhost:6333"
uvicorn main:app \
  --host 0.0.0.0 \
  --port 8203 \
  --loop uvloop \
  --http httptools \
  --proxy-headers \
  --forwarded-allow-ips "*"


cd /workspace/src/offline_eval && export PYTHONPATH=$(pwd) && \
python3 offline_eval.py \
  --dataset /workspace/src/offline_eval/golden_dataset.json \
  --base-url http://127.0.0.1:8203 \
  --experiment retriever-offline-eval \
  --max-records 5
