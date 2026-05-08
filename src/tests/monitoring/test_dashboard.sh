make deploy-qdrant 
make deploy-retriever
make deploy-vm

export PAGERDUTY_INTEGRATION_KEY=$PAGERDUTY_INTEGRATION_KEY   # Set when PagerDuty receiver required; empty fully disables PD in build_alertmanager_cm()
export ALERTMANAGER_SLACK_WEBHOOK=$ALERTMANAGER_SLACK_WEBHOOK     # Set when Slack receiver required; empty disables Slack receiver entirely
export ALERT_DEFAULT_CHANNEL="#all-new-workspace"                       # (Optional) Change per environment/team; used by Slack templates downstream 
export RUNBOOK_BASE_URL=$RUNBOOK_BASE_URL                         # Set to absolute http(s) URL to enable per-alert runbook links; 
export ALERTING_GROUP_WAIT="30s"                                  # Change to reduce initial fanout latency; wired to Alertmanager global+route group_wait
export ALERTING_GROUP_INTERVAL="5m"                               # Increase to reduce noise for flappy alerts; Alertmanager route group_interval
export ALERTING_REPEAT_INTERVAL="3h"                              # Increase for less reminder spam; decrease for stricter paging policies
export VMALERT_EVAL_INTERVAL="30s"                                # Increase if CPU-bound; decrease for faster detection; passed directly to vmalert
export VMALERT_REPLICAS="2"                                       # Set to 1 for k3s/dev; >=2 for AKS HA; parsed as int with safe fallback
export SLO_SUCCESS_TARGET="0.999"                                 # Change ONLY when SLO policy changes; must be 0<value<1 or validation fails
export SLO_LATENCY_QUANTILE="0.95"                                # Allowed values ONLY: 0.95 or 0.99; controls histogram_quantile in SLO alerts
export SLO_FAST_BURN_MULTIPLIER="2.0"                             # Increase to reduce pages; decrease for aggressive paging; used in fast-burn PromQL
export SLO_SLOW_BURN_MULTIPLIER="1.2"                             # Increase to tolerate long-term degradation; used in slow-burn PromQL
export ALERTMANAGER_REPLICAS="2"                                  # Set >=2 to enable HA gossip; parsed as int with fallback to 1
export ALERTMANAGER_RES_CPU="200m"                                # Increase with high route/template count; no validation, pure manifest pass-through
export ALERTMANAGER_RES_MEM="256Mi"                               # Increase when many alerts or receivers; Alertmanager memory-bound first
export VMALERT_RES_CPU="200m"                                     # Increase with rule count and eval interval; affects vmalert stability
export VMALERT_RES_MEM="256Mi"                                    # Increase with complex PromQL or large rule files

bash infra/tests/monitoring/test_alerts.sh

python3 infra/generators/dashboards.py --delete && python3 infra/generators/dashboards.py --apply