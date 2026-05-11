# Cloudflare Terraform Stack

This stack manages the **Cloudflare-side infrastructure** for this deployment.

It creates **one named Cloudflare Tunnel**, **explicit DNS CNAME records** for the published hostnames, and a small set of **zone settings**. It does **not** create any Kubernetes resources, Cloudflare Pages, public load balancers, or origin certificates.

## Public hostnames

The deployment is exposed under this namespace:

* `https://rag.athithya.site`
* `https://argocd.rag.athithya.site`
* `https://grafana.rag.athithya.site`

All three hostnames point to the **same tunnel**, but each hostname has its **own explicit DNS record**. There is **no wildcard DNS**.

## Tunnel model

The tunnel name is:

* `default-tunnel-1`

Cloudflare Tunnel resolves to a tunnel target of the form:

* `<UUID>.cfargotunnel.com`

This stack creates DNS CNAME records that point the published hostnames at that tunnel target. The actual routing to backend services happens in the **Kubernetes `cloudflared` configuration**, not in Terraform.

## What this stack creates

### DNS records

Explicit CNAMEs for:

* the root app hostname: `rag.athithya.site`
* Argo CD: `argocd.rag.athithya.site`
* Grafana: `grafana.rag.athithya.site`

### Zone settings

* `ssl = strict`
  Cloudflare uses Full (strict) SSL behavior for the zone.
* `always_use_https = on`
  HTTP requests are redirected to HTTPS.
* `tls_1_3 = on`
  TLS 1.3 is enabled for the zone.

### Bot protections

This stack can enable Cloudflare zone-level bot controls:

* `enable_bot_fight_mode`
* `enable_js_detections`

These are **zone-wide** settings. When enabled, they apply to the whole zone, not just one hostname. That means they are appropriate for browser-facing UI hostnames, but they can interfere with machine-to-machine traffic such as webhooks or API clients if used too broadly.

## What it does not create

This stack does **not** create:

* Cloudflare Pages
* wildcard DNS records
* Kubernetes objects
* public load balancers
* origin certificates

## Runtime model

The outputs from this stack are used by the `cloudflared` deployment in Kubernetes.

The Kubernetes-side `cloudflared` config must route:

* `rag.athithya.site` → frontend
* `argocd.rag.athithya.site` → Argo CD
* `grafana.rag.athithya.site` → Grafana

The ingress list must end with a catch-all rule that returns `404` for unmatched requests.

## Inputs

### Required

* `CLOUDFLARE_ACCOUNT_ID`
* Cloudflare zone apex via one of:

  * `TF_VAR_zone_name`, or
  * `CLOUDFLARE_ZONE_NAME`, or
  * `CLOUDFLARE_ZONE`, or
  * `DOMAIN`

### Required for authentication

Use one of:

* `CLOUDFLARE_API_TOKEN`, or
* `CLOUDFLARE_GLOBAL_API_KEY` + `CLOUDFLARE_EMAIL`

### Optional

* `CLOUDFLARE_TUNNEL_NAME`
  Default: `default-tunnel-1`

* `TF_VAR_root_subdomain`
  Default: `rag`

* `TF_VAR_enable_always_use_https`
  Default: `true`

* `TF_VAR_enable_tls_1_3`
  Default: `true`

* `TF_VAR_enable_bot_fight_mode`
  Default: `false`

* `TF_VAR_enable_js_detections`
  Default: `false`

## Run

Plan:

```bash
bash src/infra/terraform/cloudflare/run.sh --plan
```

Apply:

```bash
bash src/infra/terraform/cloudflare/run.sh --apply
```

Destroy:

```bash
bash src/infra/terraform/cloudflare/run.sh --destroy
```

## Outputs

* `cloudflare_tunnel_id`
* `cloudflare_tunnel_name`
* `cloudflare_tunnel_token`
* `rag_url`
* `argocd_url`
* `grafana_url`

## Runtime exports

Use these values for the `cloudflared` deployment:

```bash
export CLOUDFLARE_TUNNEL_TOKEN="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_token)"
export CLOUDFLARE_TUNNEL_NAME="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_name)"
export CLOUDFLARE_TUNNEL_ID="$(tofu -chdir=src/infra/terraform/cloudflare output -raw cloudflare_tunnel_id)"
export CLOUDFLARE_SECRET_NAME="cloudflared-token"
export CLOUDFLARE_SECRET_KEY="token"
export DOMAIN="athithya.site"

python3 src/infra/core/cloudflared.py --rollout
```

## Idempotency

This stack is intended to be safely rerun.

* Existing DNS records are imported into state if they already exist.
* Existing zone settings are imported into state if they already exist.
* The named tunnel is reused when it already exists.

Because the stack uses **explicit DNS records only**, adding a new public hostname means:

1. creating a new DNS record in Terraform, and
2. adding a matching ingress rule in `cloudflared`.

## Bot protection guidance

Bot Fight Mode and JS Detections are zone-wide controls. For this deployment:

* they are reasonable for browser-facing UIs such as:

  * `rag.athithya.site`
  * `grafana.rag.athithya.site`

## Notes

This is the production-oriented model for this stack:

* one tunnel
* explicit hostnames only
* no wildcard DNS
* narrow public surface area
* optional zone-wide bot mitigation
