

kubectl port-forward svc/dense-svc -n models 8200:8200
kubectl port-forward svc/sparse-svc -n models 8201:8201
kubectl port-forward svc/dense-svc -n models 8200:8200
kubectl port-forward svc/qdrant -n qdrant 6333:6333


python3 src/infra/rag/dense_service.py --rollout
