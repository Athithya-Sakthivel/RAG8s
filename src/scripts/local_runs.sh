

kubectl port-forward svc/dense-svc -n models 8200:8200
kubectl port-forward svc/qdrant -n qdrant 6333:6333
