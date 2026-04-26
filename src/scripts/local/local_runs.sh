rm -rf src/manifests
aws s3 rm s3://s3-temp-bucket-mlsecops-681802563986/ --recursive

aws s3 ls s3://s3-temp-bucket-mlsecops-681802563986/ --recursive


python3 src/scripts/local/force_sync_s3_local_fs.py --upload


python3 src/infra/rag/qdrant_service.py --rollout

export DENSE_MODEL_NAME=BAAI/bge-small-en-v1.5
export DENSE_DIM=384
export DENSE_BATCH_SIZE=64 # upper bound
python3 src/infra/rag/dense_service.py --rollout

export SPARSE_MODEL_NAME=Qdrant/minicoil-v1
export SPARSE_BATCH_SIZE=64 # upper bound
python3 src/infra/rag/sparse_service.py --rollout



export FORCE_QDRANT_BACKUP=true
export MIN_INDEXED_POINTS_FOR_BACKUP=50
kubectl delete jobs indexing-backup-manual -n indexing || true
python3 src/infra/rag/indexing_cronjob.py
kubectl create job --from=cronjob/indexing-backup-cronjob indexing-backup-manual -n indexing
kubectl get jobs -n indexing

sleep 3600

kubectl delete ns qdrant
python3 src/infra/rag/qdrant_service.py --rollout
export PER_POD=true
export QDRANT_BACKUP_S3_PREFIX=qdrant/backups/
export BACKUP_S3_BUCKET=$DATA_S3_BUCKET
bash src/scripts/backups_and_restore.sh restore


export RERANKER_MODEL_NAME=Xenova/ms-marco-MiniLM-L-6-v2
export RERANKER_MAX_DOCS=20 # upper bound
python3 src/infra/rag/reranker_service.py --rollout

kubectl port-forward -n models svc/dense-svc 8200:8200 &
kubectl port-forward -n models svc/sparse-svc 8201:8201 &
kubectl port-forward -n models svc/reranker-svc 8202:8202 &
kubectl port-forward -n qdrant svc/qdrant 6333:6333 &

source .venv/bin/activate && cd /workspace/src/services/retriever
export DENSE_URL="http://localhost:8200"
export SPARSE_URL="http://localhost:8201"
export RERANKER_URL="http://localhost:8202"
export QDRANT_URL="http://localhost:6333"
uvicorn retriever:app \
  --host 0.0.0.0 \
  --port 8203 \
  --loop uvloop \
  --http httptools \
  --proxy-headers \
  --forwarded-allow-ips "*"

curl -s -X POST http://localhost:8203/generate   -H "Content-Type: application/json"   -d '{"query":"what are the agent governance standards?"}' | jq

curl -s -X POST http://localhost:8203/generate   -H "Content-Type: application/json"   -d '{"query":"why AI agents become popular now?"}' | jq


curl -s -X POST http://localhost:8203/generate   -H "Content-Type: application/json"   -d '{"query":"what is parsing in RAG?"}' | jq
