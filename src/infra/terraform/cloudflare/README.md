# Cloudflare Terraform Stack

This stack manages the Cloudflare-side infrastructure for this deployment. It creates one named Cloudflare Tunnel, explicit DNS CNAME records for the published hostnames, and a small set of zone settings. It does not create any Kubernetes resources, Cloudflare Pages, public load balancers, or origin certificates.

## Architecture

GitHub to Cloudflare to Tunnel to Argo CD (ClusterIP). No Kubernetes ingress or LoadBalancer is required, and TLS terminates at Cloudflare.

## Public Hostnames

The deployment is exposed under the following namespace:

- `https://rag.athithya.site`
- `https://argocd.rag.athithya.site`
- `https://grafana.rag.athithya.site`

All three hostnames point to the same tunnel, but each hostname has its own explicit DNS record. There is no wildcard DNS.

## Tunnel Model

The tunnel name is `default-tunnel-1`. Cloudflare Tunnel resolves to a tunnel target of the form `<UUID>.cfargotunnel.com`. This stack creates DNS CNAME records that point the published hostnames at that tunnel target. The actual routing to backend services happens in the Kubernetes `cloudflared` configuration, not in Terraform.

## What This Stack Creates

### DNS Records

Explicit CNAMEs for:

- the root app hostname: `rag.athithya.site`
- Argo CD: `argocd.rag.athithya.site`
- Grafana: `grafana.rag.athithya.site`

### Zone Settings

- `ssl = full`  
  Cloudflare uses Full SSL behavior for the zone. Do not use Strict mode, as Argo CD runs behind Cloudflare Tunnel with HTTP internally, and Strict mode requires a valid origin certificate, which will cause an `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`.
- `always_use_https = on`  
  HTTP requests are redirected to HTTPS.
- `tls_1_3 = on`  
  TLS 1.3 is enabled for the zone.

### Bot Protections

This stack can enable Cloudflare zone-level bot controls:

- `enable_bot_fight_mode`
- `enable_js_detections`

These are zone-wide settings that apply to the entire zone. Bot Fight Mode may block webhook requests and other non-browser traffic, and it cannot be bypassed per endpoint on free plans. The recommended configuration is:

```
enable_bot_fight_mode = false
enable_js_detections  = true
```

## What It Does Not Create

This stack does not create:

- Cloudflare Pages
- Wildcard DNS records
- Kubernetes objects
- Public load balancers
- Origin certificates

## Runtime Model

The outputs from this stack are used by the `cloudflared` deployment in Kubernetes. The Kubernetes-side `cloudflared` configuration must route:

- `rag.athithya.site` to the frontend service
- `argocd.rag.athithya.site` to Argo CD
- `grafana.rag.athithya.site` to Grafana

The ingress list must end with a catch-all rule that returns a 404 response for unmatched requests.

## Inputs

### Required

- `CLOUDFLARE_ACCOUNT_ID`
- Cloudflare zone apex provided via one of:
  - `TF_VAR_zone_name`
  - `CLOUDFLARE_ZONE_NAME`
  - `CLOUDFLARE_ZONE`
  - `DOMAIN`

### Required for Authentication

Use one of:

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_GLOBAL_API_KEY` with `CLOUDFLARE_EMAIL`

### Optional

- `CLOUDFLARE_TUNNEL_NAME` (default: `default-tunnel-1`)
- `TF_VAR_root_subdomain` (default: `rag`)
- `TF_VAR_enable_always_use_https` (default: `true`)
- `TF_VAR_enable_tls_1_3` (default: `true`)
- `TF_VAR_enable_bot_fight_mode` (default: `false`)
- `TF_VAR_enable_js_detections` (default: `false`)

## Execution

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

- `cloudflare_tunnel_id`
- `cloudflare_tunnel_name`
- `cloudflare_tunnel_token`
- `rag_url`
- `argocd_url`
- `grafana_url`

## Runtime Exports

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

This stack is intended to be safely rerun. Existing DNS records and zone settings are imported into state if they already exist, and the named tunnel is reused when it already exists. Because the stack uses explicit DNS records only, adding a new public hostname requires creating a new DNS record in Terraform and adding a matching ingress rule in `cloudflared`.

## SSL Configuration

Ensure the Cloudflare SSL mode is set to Full. Do not use Strict mode, as Argo CD runs behind Cloudflare Tunnel with HTTP internally. Strict mode requires a valid origin certificate and will cause an `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`.

## Webhook Behavior

A webhook will show as "never triggered" until a real push occurs. GitHub ping events do not count as triggers.

## Notes

This is the production-oriented model for this stack: one tunnel, explicit hostnames only, no wildcard DNS, a narrow public surface area, and optional zone-wide bot mitigation.