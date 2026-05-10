# Cloudflare Terraform Stack

This stack manages only the Cloudflare pieces needed for the current deployment.

It does not use Cloudflare Pages.

It does:

- one named Cloudflare Tunnel
- DNS CNAME records to that tunnel
- zone SSL/TLS set to `strict`
- bot protection at the zone level

Cloudflare Tunnel routing works by pointing each hostname at the tunnel’s `<UUID>.cfargotunnel.com` CNAME target. Cloudflare’s bot-management resource supports `fight_mode` and `enable_js`, which is what this stack uses.

## Hostnames

- `https://athithya.site`
- `https://api.athithya.site`
- `https://auth.athithya.site`

All three are routed through the same tunnel.

## Tunnel model

The tunnel is named:

- `default-tunnel-1`

Cloudflare generates a tunnel subdomain at `<UUID>.cfargotunnel.com`, and the public hostnames are routed to that target with CNAME records. 

## What this stack creates

- DNS CNAME records for:
  - root host
  - API host
  - auth host

- Zone settings:
  - `ssl = strict`
  - `always_use_https = on`
  - `tls_1_3 = on`

- Bot protection:
  - `fight_mode = true`
  - `enable_js = true`

## What it does not create

- Cloudflare Pages
- nginx certificates
- Kubernetes resources
- public load balancers

The tunnel token and tunnel name are the important outputs for Kubernetes. Cloudflare’s tunnel docs use the tunnel token to run `cloudflared`, and the DNS record is just a CNAME to the tunnel subdomain.

## Inputs

Required:

- `DOMAIN`
- `CLOUDFLARE_ACCOUNT_ID`

Required for auth:

- `CLOUDFLARE_API_TOKEN`, or
- `CLOUDFLARE_GLOBAL_API_KEY` + `CLOUDFLARE_EMAIL`

Optional:

- `CLOUDFLARE_TUNNEL_NAME`  
  Default: `default-tunnel-1`

- `ROOT_HOST`  
  Default: `athithya.site`

- `API_HOST`  
  Default: `api.athithya.site`

- `AUTH_HOST`  
  Default: `auth.athithya.site`

## Run

Plan:

```bash
bash src/infra/terraform/cloudflare/run.sh --plan
````

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
* `root_url`
* `api_url`
* `auth_url`

## Runtime exports

Use these values for the cloudflared deployment:

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

Safe to rerun:

```bash
bash src/infra/terraform/cloudflare/run.sh --apply
```

Existing DNS records and zone settings are imported into state if present, and the named tunnel is reused when it already exists.

## Notes

Bot Fight Mode is a free bot-mitigation feature, and the Terraform resource also exposes JavaScript detections and additional bot-management toggles. This stack keeps only the minimal settings needed for your current setup. ([Cloudflare Docs][2])


[1]: https://developers.cloudflare.com/api/terraform/resources/bot_management/ "Bot Management | Cloudflare API"
[2]: https://developers.cloudflare.com/bots/get-started/bot-fight-mode/ "Get started with Bot Fight Mode"
