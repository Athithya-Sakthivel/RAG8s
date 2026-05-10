#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="grafana"
RELEASE="grafana"
CHART_REF="oci://ghcr.io/grafana-community/helm-charts/grafana"
CHART_VERSION="12.3.0"
ADMIN_SECRET="grafana-admin"
DASHBOARD_CM="grafana-dashboard-observability-health"
DATASOURCE_CM="grafana-datasource-prometheus"
ADMIN_USER="admin"
ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://prometheus-server.monitoring.svc.cluster.local:9090}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_bin() {
  command -v "$1" >/dev/null 2>&1 || die "Required binary not found: $1"
}

require_bin kubectl
require_bin helm

[[ -n "$ADMIN_PASSWORD" ]] || die "Set GRAFANA_ADMIN_PASSWORD before running this script."

log "==> Checking cluster access..."
kubectl version --client >/dev/null
kubectl cluster-info >/dev/null

log "==> Removing any previous Grafana Helm releases..."
helm uninstall "$RELEASE" -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
helm uninstall "$RELEASE" -n monitoring --ignore-not-found >/dev/null 2>&1 || true

log "==> Removing stale cluster-scoped Grafana RBAC objects..."
kubectl delete clusterrole grafana-clusterrole --ignore-not-found=true >/dev/null 2>&1 || true
kubectl delete clusterrolebinding grafana-clusterrolebinding --ignore-not-found=true >/dev/null 2>&1 || true

log "==> Deleting namespace ${NAMESPACE} (if it exists)..."
kubectl delete namespace "$NAMESPACE" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true

log "==> Waiting for namespace ${NAMESPACE} to be fully deleted..."
while kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; do
  sleep 2
done

log "==> Recreating namespace ${NAMESPACE}..."
kubectl create namespace "$NAMESPACE"

log "==> Writing dashboard and datasource manifests..."
cat > "$TMP_DIR/dashboard.json" <<'JSON'
{
  "uid": "observability-health",
  "title": "Observability Health",
  "tags": ["observability", "prometheus"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "30s",
  "editable": true,
  "panels": [
    {
      "type": "stat",
      "title": "Prometheus",
      "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "1", "text": "UP"},
            {"type": "value", "value": "0", "text": "DOWN"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "red", "value": null},
              {"color": "red", "value": 0},
              {"color": "green", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "up{job=\"prometheus\"}", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "Alertmanager",
      "gridPos": {"x": 6, "y": 0, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "1", "text": "UP"},
            {"type": "value", "value": "0", "text": "DOWN"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "red", "value": null},
              {"color": "red", "value": 0},
              {"color": "green", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "up{job=\"alertmanager\"}", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "ClickHouse",
      "gridPos": {"x": 12, "y": 0, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "1", "text": "UP"},
            {"type": "value", "value": "0", "text": "DOWN"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "red", "value": null},
              {"color": "red", "value": 0},
              {"color": "green", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "up{job=\"clickhouse\"}", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "Vector",
      "gridPos": {"x": 18, "y": 0, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "1", "text": "UP"},
            {"type": "value", "value": "0", "text": "DOWN"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "red", "value": null},
              {"color": "red", "value": 0},
              {"color": "green", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "up{job=\"vector\"}", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "ClickHouse Read-Only",
      "gridPos": {"x": 0, "y": 4, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "0", "text": "Writable"},
            {"type": "range", "from": "1", "to": "999", "text": "READONLY"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green", "value": null},
              {"color": "red", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "ClickHouseMetrics_ReadonlyReplica{job=\"clickhouse\"}", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "Prometheus Storage",
      "gridPos": {"x": 6, "y": 4, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "unit": "bytes",
          "decimals": 2,
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green", "value": null},
              {"color": "orange", "value": 40000000000},
              {"color": "red", "value": 50000000000}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "prometheus_tsdb_storage_blocks_bytes", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "Firing Alerts",
      "gridPos": {"x": 12, "y": 4, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "0", "text": "None"},
            {"type": "range", "from": "1", "to": "999", "text": "FIRING"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green", "value": null},
              {"color": "red", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "sum(ALERTS{alertstate=\"firing\"})", "refId": "A"}]
    },
    {
      "type": "stat",
      "title": "Rule Evaluation Failures",
      "gridPos": {"x": 18, "y": 4, "w": 6, "h": 4},
      "fieldConfig": {
        "defaults": {
          "mappings": [
            {"type": "value", "value": "0", "text": "OK"},
            {"type": "range", "from": "1", "to": "999", "text": "FAILURES"}
          ],
          "thresholds": {
            "mode": "absolute",
            "steps": [
              {"color": "green", "value": null},
              {"color": "red", "value": 1}
            ]
          },
          "color": {"mode": "thresholds"}
        }
      },
      "options": {"reduceOptions": {"values": false, "calcs": ["lastNotNull"]}, "textMode": "auto"},
      "targets": [{"expr": "prometheus_rule_evaluation_failures_total", "refId": "A"}]
    }
  ],
  "time": {"from": "now-15m", "to": "now"}
}
JSON

cat > "$TMP_DIR/datasource.yaml" <<EOF
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: ${PROMETHEUS_URL}
    isDefault: true
    editable: false
EOF

log "==> Creating admin secret..."
kubectl -n "$NAMESPACE" create secret generic "$ADMIN_SECRET" \
  --from-literal=admin-user="$ADMIN_USER" \
  --from-literal=admin-password="$ADMIN_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

log "==> Creating dashboard ConfigMap..."
kubectl -n "$NAMESPACE" create configmap "$DASHBOARD_CM" \
  --from-file=observability-health.json="$TMP_DIR/dashboard.json" \
  --dry-run=client -o yaml | kubectl label --local -f - grafana_dashboard="1" -o yaml | kubectl apply -f -

log "==> Creating datasource ConfigMap..."
kubectl -n "$NAMESPACE" create configmap "$DATASOURCE_CM" \
  --from-file=prometheus.yaml="$TMP_DIR/datasource.yaml" \
  --dry-run=client -o yaml | kubectl label --local -f - grafana_datasource="1" -o yaml | kubectl apply -f -

VALUES_FILE="$TMP_DIR/grafana-values.yaml"
cat > "$VALUES_FILE" <<EOF
rbac:
  create: true
  namespaced: true
serviceAccount:
  create: true
  automountServiceAccountToken: true
admin:
  existingSecret: ${ADMIN_SECRET}
service:
  type: ClusterIP
  port: 80
sidecar:
  dashboards:
    enabled: true
    searchNamespace: ${NAMESPACE}
    resource: both
    label: grafana_dashboard
    provider:
      name: sidecarProvider
      orgid: 1
      folder: ''
      type: file
      disableDelete: false
      allowUiUpdates: false
      foldersFromFilesStructure: false
  datasources:
    enabled: true
    searchNamespace: ${NAMESPACE}
    resource: both
    label: grafana_datasource
imageRenderer:
  enabled: false
testFramework:
  enabled: false
ingress:
  enabled: false
serviceMonitor:
  enabled: false
EOF

log "==> Installing Grafana ${CHART_VERSION} from ${CHART_REF}..."
helm upgrade --install "$RELEASE" "$CHART_REF" \
  --version "$CHART_VERSION" \
  --namespace "$NAMESPACE" \
  --create-namespace \
  --values "$VALUES_FILE" \
  --wait \
  --timeout 10m \
  --atomic

log ""
log "Grafana deployed in namespace '${NAMESPACE}'."
log "Dashboard: Observability Health"
log "Login: ${ADMIN_USER} / ${ADMIN_PASSWORD}"
log "Port-forward: kubectl -n ${NAMESPACE} port-forward svc/${RELEASE} 3000:80"