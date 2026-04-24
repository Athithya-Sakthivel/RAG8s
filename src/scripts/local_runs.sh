rm -rf src/manifests
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

sleep 800

# ghcr.io/athithya-sakthivel/indexing-pipeline:2026-04-24-11-24--324996b@sha256:8a7ed61a4441cb274724e8b26eca7b5c7011f3035961baeec679f8434a70f6dc
kubectl delete jobs indexing-backup-manual -n indexing
python3 src/infra/rag/indexing_cronjob.py

kubectl create job --from=cronjob/indexing-backup-cronjob indexing-backup-manual -n indexing
kubectl get jobs -n indexing
kubectl get pods -n indexing --watch
