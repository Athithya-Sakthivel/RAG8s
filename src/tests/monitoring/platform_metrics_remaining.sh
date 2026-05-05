NS_OBS=observability
NS_MON=monitoring

echo "1) Raw vector /metrics (full grep for event-like counters)"
kubectl -n $NS_OBS run --rm -i vec-metrics-check --image=curlimages/curl --restart=Never --command -- \
  sh -c "curl -sS --max-time 5 http://vector-agent.$NS_OBS.svc.cluster.local:8687/metrics || true" | egrep -i 'event|processed|received|processed_events|events|sink_sent|sink_received' -n || true

echo; echo "2) full prefix list from vector /metrics (first 300 lines)"
kubectl -n $NS_OBS run --rm -i vec-metrics-head --image=curlimages/curl --restart=Never --command -- \
  sh -c "curl -sS --max-time 5 http://vector-agent.$NS_OBS.svc.cluster.local:8687/metrics | sed -n '1,300p' || true"

echo; echo "3) vmagent scrape target summary from Victoria (raw series)"
kubectl -n $NS_MON port-forward svc/victoria-metrics 8428:8428 >/tmp/_pf_vic.$$ 2>&1 & PF=$!
sleep 1
curl -sG --data-urlencode 'query=vm_promscrape_scrape_pool_targets{scrape_job="vector-prometheus-exporter"}' 'http://127.0.0.1:8428/api/v1/query' | jq -C . || true
curl -sG --data-urlencode 'query=vm_promscrape_scrape_errors_total{scrape_job="vector-prometheus-exporter"}' 'http://127.0.0.1:8428/api/v1/query' | jq -C . || true
curl -sG --data-urlencode 'query=vm_promscrape_scrape_pool_targets' 'http://127.0.0.1:8428/api/v1/query' | jq -C . || true
kill $PF || true

echo; echo "4) Check vmagent logs for vector scrape errors (tail recent 400 lines)"
for V in $(kubectl -n $NS_MON get pods -l app=vmagent -o jsonpath='{range .items[*]}{.metadata.name} {end}'); do
  echo "--- vmagent pod: $V ---"
  kubectl -n $NS_MON logs $V --tail=400 | egrep -i 'vector|vector-prometheus|vector-agent|scrape|promscrape|error|failed' -n || true
done

echo; echo "5) Optionally restart vmagent to force re-discovery (uncomment to run)"
echo "# kubectl -n $NS_MON rollout restart deployment vmagent || kubectl -n $NS_MON rollout restart statefulset vmagent || true"

echo; echo "END"
