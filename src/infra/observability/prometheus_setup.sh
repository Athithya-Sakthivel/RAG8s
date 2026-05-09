

export PAGERDUTY_ROUTING_KEY="placeholder-for-now"
kubectl create secret generic alertmanager-secrets \
  -n monitoring \
  --from-literal="slack-api-url=${SLACK_WEBHOOK_URL}" \
  --from-literal="pagerduty-routing-key=${PAGERDUTY_ROUTING_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f src/argocd/prometheus-application.yaml
