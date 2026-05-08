# === QUICK DIAGNOSTIC BLOCK (run this entire block) ===
# 1) namespaces & important pods
kubectl get ns -o=custom-columns=NAME:.metadata.name --no-headers | xargs -I{} echo NS:{} || true
kubectl get pods -A --no-headers | sed -n '1,200p' || true

# 2) vmagent & victoria basic endpoints (port-forward short-lived)
VM_NS=monitoring
VMAGENT_SVC=vmagent
VICTORIA_SVC=victoria-metrics
VMAGENT_PORT=8429
VICTORIA_PORT=8428
PF_VMAG_LOCAL=$(python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('',0)); p=s.getsockname()[1]; s.close(); print(p)
PY)
PF_VIC_LOCAL=$(python3 - <<'PY'
import socket
s=socket.socket(); s.bind(('',0)); p=s.getsockname()[1]; s.close(); print(p)
PY)
# start background port-forwards (will be killed by ctrl-c by you; safe short use)
kubectl -n ${VM_NS} port-forward svc/${VMAGENT_SVC} ${PF_VMAG_LOCAL}:${VMAGENT_PORT} >/tmp/_pf_vmagent.log 2>&1 & echo "vmagent pf pid:$!" && sleep 0.5
kubectl -n ${VM_NS} port-forward svc/${VICTORIA_SVC} ${PF_VIC_LOCAL}:${VICTORIA_PORT} >/tmp/_pf_victoria.log 2>&1 & echo "victoria pf pid:$!" && sleep 0.5

# quick probes (local forwarded)
timeout 5 curl -sS --fail http://127.0.0.1:${PF_VMAG_LOCAL}/metrics | egrep -i 'vm_promscrape|vmagent_remotewrite' | sed -n '1,40p' || echo "vmagent /metrics unreachable or empty"
timeout 5 curl -sS --fail http://127.0.0.1:${PF_VIC_LOCAL}/metrics | sed -n '1,10p' || echo "victoria /metrics unreachable"

# 3) vmagent-config (scrape.yml) snippet (monitoring namespace)
kubectl -n monitoring get configmap vmagent-config -o jsonpath='{.data.scrape\.yml}' 2>/dev/null | sed -n '1,200p' || echo "vmagent-config missing or empty"

# 4) Verify vmagent queue & remote-write metrics (local port-forward used above)
timeout 5 curl -sS http://127.0.0.1:${PF_VMAG_LOCAL}/metrics | egrep -i 'vm_persistentqueue|vmagent_remotewrite|vm_promscrape' | sed -n '1,200p' || true

# 5) Verify Victoria query access (PromQL) using example queries (q= up and scrape sample)
VMQ="http://127.0.0.1:${PF_VIC_LOCAL}/api/v1/query"
Q() { timeout 8 curl -sS -G --data-urlencode "query=$1" "$VMQ"; }
echo "PROMQL: vmagent up"
Q 'max(up{job="vmagent-self"})' | sed -n '1,200p'
echo "PROMQL: victoria up (job-based)"
Q 'max by(job) (up)' | sed -n '1,200p'

# 6) Check Vector exporter & internal metrics (if present)
kubectl -n observability get ds,deploy -l app=vector -o wide --no-headers 2>/dev/null || true
# check ConfigMap (if exists)
kubectl -n observability get configmap vector-config -o yaml 2>/dev/null | sed -n '1,200p' || echo "vector-config missing"

# 7) ClickHouse pod state + exporter logs (observability)
kubectl -n observability get pod -l app=clickhouse -o wide --no-headers || true
# tail exporter logs (truncated)
kubectl -n observability logs -l app=clickhouse -c clickhouse-exporter --tail=200 2>/dev/null | sed -n '1,200p' || echo "clickhouse-exporter logs not available"

# 8) Lightweight test: create a temporary Prom metrics target that vmagent should scrape.
# NOTE: this creates a short-lived pod running hashicorp/http-echo to serve a metrics page on :9100/metrics.
kubectl -n observability run tmp-metric-server --image=hashicorp/http-echo --restart=Never --labels=app=tmp-metric-server --command -- /http-echo -text="test_generated_metric 1" -listen=:9100 >/dev/null 2>&1 || true
echo "tmp metric pod created (if supported). Sleeping 5s to let scheduler start..."
sleep 5
kubectl -n observability get pods -l app=tmp-metric-server -o wide --no-headers || echo "tmp-metric-server pod not scheduled"
# clean up test pod
kubectl -n observability delete pod -l app=tmp-metric-server --ignore-not-found >/dev/null 2>&1 || true

# 9) short summary (grep for common issues)
echo "=== SUMMARY LINES ==="
kubectl -n monitoring get pods -l app=vmagent --no-headers -o wide || true
kubectl -n monitoring get pods -l app=victoria-metrics --no-headers -o wide || true
kubectl -n observability get pods -l app=clickhouse --no-headers -o wide || true
echo "Logs and metrics collected to /tmp/_pf_vmagent.log & /tmp/_pf_victoria.log for port-forward processes (inspect if you started them)."
