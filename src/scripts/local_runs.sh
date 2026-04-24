make core

python3 src/infra/rag/qdrant_service.py --rollout

export DENSE_MODEL_NAME=BAAI/bge-small-en-v1.5
export DENSE_DIM=384
export DENSE_BATCH_SIZE=16
python3 src/infra/rag/dense_service.py --rollout

export SPARSE_MODEL_NAME=Qdrant/minicoil-v1
export SPARSE_BATCH_SIZE=8
python3 src/infra/rag/sparse_service.py --rollout

export RERANKER_MODEL_NAME=Xenova/ms-marco-MiniLM-L-6-v2
python3 src/infra/rag/reranker_service.py --rollout

kubectl port-forward svc/qdrant -n qdrant 6333:6333
kubectl port-forward svc/reranker-svc -n models 8200:8200
kubectl port-forward svc/sparse-svc -n models 8201:8201
kubectl port-forward svc/dense-svc -n models 8200:8200
