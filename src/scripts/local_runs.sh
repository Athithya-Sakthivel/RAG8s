rm -rf src/manifests
python3 src/scripts/force_sync_s3_local_fs.py --upload
aws s3 ls s3://s3-temp-bucket-mlsecops-681802563986/ --recursive

make core

python3 src/infra/rag/qdrant_service.py --rollout

export DENSE_MODEL_NAME=BAAI/bge-small-en-v1.5
export DENSE_DIM=384
export DENSE_BATCH_SIZE=64 # upper bound
python3 src/infra/rag/dense_service.py --rollout

export SPARSE_MODEL_NAME=Qdrant/minicoil-v1
export SPARSE_BATCH_SIZE=64 # upper bound
python3 src/infra/rag/sparse_service.py --rollout

export RERANKER_MODEL_NAME=Xenova/ms-marco-MiniLM-L-6-v2
export RERANKER_MAX_DOCS=50 # upper bound
python3 src/infra/rag/reranker_service.py --rollout


kubectl delete jobs indexing-backup-manual -n indexing
python3 src/infra/rag/indexing_cronjob.py

kubectl create job --from=cronjob/indexing-backup-cronjob indexing-backup-manual -n indexing
kubectl get jobs -n indexing
kubectl get pods -n indexing --watch
