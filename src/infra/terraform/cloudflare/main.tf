terraform {
  required_version = ">= 1.6.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      # Use 4.x series – last stable version that correctly fetches tunnel tokens
      version = "~> 4.0"
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

variable "domain" {
  type = string
}

variable "tunnel_name" {
  description = "Cloudflare Tunnel name used by cloudflared"
  type        = string
  default     = "default-tunnel-1"
}

variable "root_host" {
  type    = string
  default = null
}

variable "api_host" {
  type    = string
  default = null
}

variable "auth_host" {
  type    = string
  default = null
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
  domain       = trim(var.domain, ".")
  root_host    = coalesce(var.root_host, local.domain)
  api_host     = coalesce(var.api_host, "api.${local.domain}")
  auth_host    = coalesce(var.auth_host, "auth.${local.domain}")
  tunnel_cname = "${data.cloudflare_zero_trust_tunnel_cloudflared.default.id}.cfargotunnel.com"
}

data "cloudflare_zero_trust_tunnel_cloudflared" "default" {
  account_id = var.account_id

  filter = {
    name       = var.tunnel_name
    is_deleted = false
  }
}

# NOTE: This data source works correctly only with provider version ~> 4.0
data "cloudflare_zero_trust_tunnel_cloudflared_token" "default" {
  account_id = var.account_id
  tunnel_id  = data.cloudflare_zero_trust_tunnel_cloudflared.default.id
}

resource "cloudflare_dns_record" "root_cname" {
  zone_id = var.zone_id
  name    = local.root_host
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

resource "cloudflare_dns_record" "api_cname" {
  zone_id = var.zone_id
  name    = local.api_host
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

resource "cloudflare_dns_record" "auth_cname" {
  zone_id = var.zone_id
  name    = local.auth_host
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
  zone_id = var.zone_id

  fight_mode = var.enable_bot_fight_mode
  enable_js  = var.enable_js_detections

  ai_bots_protection = "block"
  crawler_protection = "enabled"

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

output "root_url" {
  value = "https://${local.root_host}"
}

output "api_url" {
  value = "https://${local.api_host}"
}

output "auth_url" {
  value = "https://${local.auth_host}"
}