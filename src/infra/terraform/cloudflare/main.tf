terraform {
  required_version = ">= 1.6.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = ">= 5.19.0, < 6.0.0"
    }
  }
}

provider "cloudflare" {}

variable "account_id" {
  type = string
}

variable "zone_id" {
  type    = string
  default = null
}

# Cloudflare zone apex, e.g. "athithya.site"
variable "zone_name" {
  type    = string
  default = null
}

# Deprecated alias kept for compatibility with older callers.
variable "domain" {
  type        = string
  default     = null
  description = "Deprecated alias for zone_name."
}

# Public app namespace under the zone, e.g. "rag" -> rag.athithya.site
variable "root_subdomain" {
  type    = string
  default = "rag"

  validation {
    condition     = trimspace(var.root_subdomain) != ""
    error_message = "root_subdomain must not be empty."
  }
}

variable "tunnel_name" {
  description = "Cloudflare Tunnel name used by cloudflared"
  type        = string
  default     = "default-tunnel-1"

  validation {
    condition     = trimspace(var.tunnel_name) != ""
    error_message = "tunnel_name must not be empty."
  }
}

variable "enable_always_use_https" {
  type    = bool
  default = true
}

variable "enable_tls_1_3" {
  type    = bool
  default = true
}

variable "enable_bot_fight_mode" {
  type    = bool
  default = true
}

variable "enable_js_detections" {
  type    = bool
  default = true
}

locals {
  zone_name_raw  = try(coalesce(var.zone_name, var.domain), null)
  zone_name      = local.zone_name_raw != null ? trim(local.zone_name_raw, ".") : null
  root_subdomain = trim(var.root_subdomain, ".")

  root_hostname   = local.zone_name != null ? "${local.root_subdomain}.${local.zone_name}" : null
  argocd_hostname  = local.root_hostname != null ? "argocd.${local.root_hostname}" : null
  grafana_hostname = local.root_hostname != null ? "grafana.${local.root_hostname}" : null

  tunnel_cname = "${data.cloudflare_zero_trust_tunnel_cloudflared.default.id}.cfargotunnel.com"
}

check "zone_name_present" {
  assert {
    condition     = local.zone_name != null && local.zone_name != ""
    error_message = "Set TF_VAR_zone_name (preferred) or TF_VAR_domain to the Cloudflare zone apex, e.g. athithya.site."
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared" "default" {
  account_id = var.account_id

  filter = {
    name       = var.tunnel_name
    is_deleted = false
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "default" {
  account_id = var.account_id
  tunnel_id  = data.cloudflare_zero_trust_tunnel_cloudflared.default.id
}

# Root app hostname -> tunnel
resource "cloudflare_dns_record" "root_cname" {
  zone_id = var.zone_id
  name    = local.root_subdomain
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

# Argo CD -> tunnel
resource "cloudflare_dns_record" "argocd_cname" {
  zone_id = var.zone_id
  name    = "argocd.${local.root_subdomain}"
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

# Grafana -> tunnel
resource "cloudflare_dns_record" "grafana_cname" {
  zone_id = var.zone_id
  name    = "grafana.${local.root_subdomain}"
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

resource "cloudflare_zone_setting" "ssl" {
  zone_id    = var.zone_id
  setting_id = "ssl"
  value      = "strict"
}

resource "cloudflare_zone_setting" "always_use_https" {
  count      = var.enable_always_use_https ? 1 : 0
  zone_id    = var.zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_zone_setting" "tls_1_3" {
  count      = var.enable_tls_1_3 ? 1 : 0
  zone_id    = var.zone_id
  setting_id = "tls_1_3"
  value      = "on"
}

resource "cloudflare_bot_management" "zone" {
  count   = (var.enable_bot_fight_mode || var.enable_js_detections) ? 1 : 0
  zone_id = var.zone_id

  fight_mode = var.enable_bot_fight_mode
  enable_js  = var.enable_js_detections

  ai_bots_protection = "block"
  crawler_protection  = "enabled"

  lifecycle {
    ignore_changes = [
      auto_update_model
    ]
  }
}

output "cloudflare_tunnel_id" {
  value = data.cloudflare_zero_trust_tunnel_cloudflared.default.id
}

output "cloudflare_tunnel_name" {
  value = data.cloudflare_zero_trust_tunnel_cloudflared.default.name
}

output "cloudflare_tunnel_token" {
  value     = data.cloudflare_zero_trust_tunnel_cloudflared_token.default.token
  sensitive = true
}

output "rag_url" {
  value = "https://${local.root_hostname}"
}

output "argocd_url" {
  value = "https://${local.argocd_hostname}"
}

output "grafana_url" {
  value = "https://${local.grafana_hostname}"
}