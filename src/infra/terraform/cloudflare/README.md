# Cloudflare Infrastructure Stack — Deployment Guide

## Overview

This repository uses **OpenTofu** to provision and manage Cloudflare infrastructure as code. The stack is designed for repeatable deployments, Kubernetes tunnel integration, and minimal manual dashboard operations.

It manages four primary layers:

1. **Frontend Delivery** via Cloudflare Pages
2. **Backend Exposure** via Cloudflare Tunnel
3. **Edge Security** via Firewall Rules + Rate Limiting
4. **Automation Outputs** for downstream workloads

---

# Architecture

## Public Endpoints

After deployment:

| Service     | URL                                 | Purpose                 |
| ----------- | ----------------------------------- | ----------------------- |
| Frontend    | `https://app.athithya.site`         | Static web UI           |
| Auth API    | `https://auth.athithya.site`        | Authentication backend  |
| Predict API | `https://predict.athithya.site`     | Inference / API backend |

---

## Traffic Flow

```text
Users
  ↓
Cloudflare Edge
  ├── Pages → app.athithya.site
  ├── Tunnel → auth.api.athithya.site
  └── Tunnel → predict.api.athithya.site
```

No inbound ports need to be opened on servers or Kubernetes nodes.

---

# Why This Design

## Cloudflare Pages

Used for:

* Static frontend hosting
* Automatic HTTPS
* CDN acceleration
* GitHub-based deployments
* Global edge delivery

## Cloudflare Tunnel

Used for:

* Private origin services
* Outbound-only connectivity
* No public ingress exposure
* Kubernetes-friendly backend publishing

## OpenTofu

Used for:

* Infrastructure as code
* Versioned deployments
* Drift detection
* Reproducible rebuilds
* CI/CD automation

---

# Required Inputs

| Input             | Example Value              |
| ----------------- | ------------------ |
| Domain            | `athithya.site`    |
| Pages Project     | `tabular-ui`       |
| GitHub Repository | `MLSecOps-tabular` |
| Branch            | `main`             |

---

# Authentication Model

Use **Cloudflare Global API Key** bootstrap authentication.

Required:

* Cloudflare Account ID
* Cloudflare Email
* Global API Key

Used for:

* Zone discovery
* DNS management
* Pages provisioning
* Ruleset provisioning

---

# Canonical Environment Configuration

Use one consistent export block.

```bash
unset CLOUDFLARE_API_TOKEN
unset CLOUDFLARE_API_KEY

export CLOUDFLARE_ACCOUNT_ID="YOUR_ACCOUNT_ID"
export CLOUDFLARE_GLOBAL_API_KEY="YOUR_GLOBAL_API_KEY"
export CLOUDFLARE_EMAIL="YOUR_EMAIL"

export TF_VAR_account_id="$CLOUDFLARE_ACCOUNT_ID"
export TF_VAR_domain="athithya.site"
```

---

# Resolve Zone ID Automatically

```bash
export TF_VAR_zone_id="$(
curl -s \
  -H "X-Auth-Key: $CLOUDFLARE_GLOBAL_API_KEY" \
  -H "X-Auth-Email: $CLOUDFLARE_EMAIL" \
  "https://api.cloudflare.com/client/v4/zones?name=$TF_VAR_domain" \
| jq -r '.result[0].id'
)"
```

---

# Pages Configuration

```bash
export TF_VAR_pages_project_name="tabular-ui"
export TF_VAR_pages_branch="main"
export TF_VAR_pages_repo_owner="YOUR_GITHUB_OWNER"
export TF_VAR_pages_repo_name="MLSecOps-tabular"
export TF_VAR_pages_root_dir="."
export TF_VAR_pages_destination_dir="dist"
```

---

# Resolve GitHub Repository ID

```bash
export TF_VAR_pages_repo_id="$(
curl -s \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$TF_VAR_pages_repo_owner/$TF_VAR_pages_repo_name" \
| jq -r '.id'
)"
```

---

# Rate Limiting Configuration

```bash
export TF_VAR_rate_limit_enabled="true"
export TF_VAR_rate_limit_action="block"
export TF_VAR_rate_limit_requests="60"
export TF_VAR_rate_limit_period="10"
export TF_VAR_rate_limit_mitigation_timeout="10"
```

## Meaning

| Variable           | Description       |
| ------------------ | ----------------- |
| requests           | Allowed requests  |
| period             | Window in seconds |
| action             | Response action   |
| mitigation_timeout | Block duration    |

---

# Provision Infrastructure

## Apply

```bash
bash src/terraform/cloudflare/run.sh --apply
```

This performs:

1. Resolve zone / repo IDs
2. Reuse or create tunnel
3. Bind API DNS records
4. Initialize OpenTofu
5. Import existing resources if needed
6. Apply infrastructure changes
7. Print outputs

---

# Resources Created

## Pages

* Project `tabular-ui`
* Domain `app.athithya.site`

## DNS

* `app` → Pages
* `auth.api` → Tunnel
* `predict.api` → Tunnel

## Security

* Firewall rules
* Rate limiting ruleset

## Outputs

* Tunnel ID
* Tunnel Name
* Tunnel Token
* Frontend URL
* API URLs

---

# Terraform Outputs

Show outputs:

```bash
tofu -chdir=src/terraform/cloudflare output
```

Expected:

```text
frontend_url
auth_url
predict_url
pages_project_name
cloudflare_tunnel_id
cloudflare_tunnel_name
cloudflare_tunnel_token
```

---

# Export Runtime Tunnel Variables

Use the exact same variable names everywhere.

```bash
export CLOUDFLARE_TUNNEL_TOKEN="$(
tofu -chdir=src/terraform/cloudflare output -raw cloudflare_tunnel_token
)"

export CLOUDFLARE_TUNNEL_NAME="$(
tofu -chdir=src/terraform/cloudflare output -raw cloudflare_tunnel_name
)"

export CLOUDFLARE_SECRET_NAME="cloudflared-token"
export CLOUDFLARE_SECRET_KEY="token"
```

---

# Idempotent Operations

Safe to rerun:

```bash
bash src/terraform/cloudflare/run.sh --apply
```

Behavior:

* Existing resources imported if unmanaged
* Managed resources updated only when drift exists
* Existing tunnel reused
* DNS corrected automatically

---

# Destroy Infrastructure

```bash
bash src/terraform/cloudflare/run.sh --destroy
```

Removes:

* Pages project
* Custom domain
* DNS records
* Firewall rules
* Rate limits
* Tunnel
* Tunnel DNS routes

---

# Operational Best Practices

## Secrets

Never commit:

* API keys
* Tunnel tokens
* Terraform state with secrets

Use:

* CI secret stores
* Kubernetes Secrets
* Vault / secret managers

---

# CI/CD Model

```text
git push
  → Cloudflare Pages build
  → OpenTofu apply
  → Kubernetes deploy
  → cloudflared consumes token
```

---

# Drift Detection

Run periodically:

```bash
bash src/terraform/cloudflare/run.sh --plan
```

Detects:

* DNS changes
* Dashboard edits
* Ruleset drift
* Pages config drift

---

# Troubleshooting

## Pages Domain Pending

```bash
dig app.athithya.site
```

Usually DNS propagation.

---

## Tunnel Not Routing

```bash
cloudflared tunnel list
kubectl logs <pod>
```

---

## Rate Limit Validation Errors

Use supported values:

```bash
period=10
mitigation_timeout=10
```

---

# Security Posture

This architecture is stronger than exposing public LoadBalancers because:

* No inbound ports exposed
* TLS terminates at Cloudflare edge
* Rate limiting at edge
* Bot / WAF controls available
* Private cluster services remain internal

---

# Summary

This stack provides:

* Production frontend CDN
* Private backend exposure
* Infrastructure as code
* Kubernetes-ready tunnel auth
* Deterministic rebuilds
* Cloudflare-native security controls

Suitable for production deployments and automated CI/CD workflows.
