make lc
make deploy-qdrant
make deploy-models

make run-indexing-cronjob-kind:

export BACKUP_ID="20260104T113111Z-0d243e54" # Optional explicit backup identifier to restore; leave empty to auto-select the latest backup manifest under the Azure prefix
make qdrant-restore


make fix-dns && python3 infra/generators/monitoring.py --delete || true && python3 infra/generators/monitoring.py --apply

python3 infra/generators/retriever.py --delete || true && python3 infra/generators/retriever.py --apply

make fix-dns
bash infra/tests/monitoring/qdrant_metrics_e2e.sh
make fix-dns
bash infra/tests/monitoring/retriever_metrics_e2e.sh

make fix-dns && python3 infra/generators/dashboards.py --delete && \
python3 infra/generators/dashboards.py --apply

kubectl -n monitoring port-forward svc/grafana 3000:3000 >/tmp/grafana-portforward.log 2>&1 & \
echo "Grafana port-forward PID=$!" && \
kubectl -n monitoring logs -f deployment/grafana --tail 50
