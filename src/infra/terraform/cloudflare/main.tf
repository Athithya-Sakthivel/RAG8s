terraform {
  required_version = ">= 1.6.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = ">= 5.18.0, < 6.0.0"
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
  description = "Cloudflare Tunnel name used by the downstream tunneling agent"
  type        = string
  default     = "tabular-api-tunnel"
}

variable "pages_project_name" {
  type    = string
  default = "tabular-ui"
}

variable "pages_branch" {
  type    = string
  default = "main"
}

variable "pages_repo_owner" {
  type = string
}

variable "pages_repo_name" {
  type = string
}

variable "pages_repo_id" {
  type    = string
  default = null
}

variable "pages_root_dir" {
  type    = string
  default = "src/frontend"
}

variable "pages_destination_dir" {
  type    = string
  default = "dist"
}

variable "rate_limit_enabled" {
  type    = bool
  default = true
}

variable "rate_limit_action" {
  type    = string
  default = "block"

  validation {
    condition     = contains(["block", "js_challenge", "managed_challenge", "challenge", "log"], var.rate_limit_action)
    error_message = "rate_limit_action must be one of: block, js_challenge, managed_challenge, challenge, log."
  }
}

variable "rate_limit_requests" {
  type    = number
  default = 60

  validation {
    condition     = var.rate_limit_requests > 0
    error_message = "rate_limit_requests must be greater than 0."
  }
}

variable "rate_limit_period" {
  type    = number
  default = 10

  validation {
    condition     = contains([10, 60, 120, 300, 600, 3600], var.rate_limit_period)
    error_message = "rate_limit_period must be one of: 10, 60, 120, 300, 600, 3600."
  }
}

variable "rate_limit_mitigation_timeout" {
  type    = number
  default = 10

  validation {
    condition     = contains([0, 10, 60, 120, 300, 600, 3600, 86400], var.rate_limit_mitigation_timeout)
    error_message = "rate_limit_mitigation_timeout must be one of: 0, 10, 60, 120, 300, 600, 3600, 86400."
  }
}

locals {
  app_hostname     = "app.${var.domain}"
  auth_hostname    = "auth.${var.domain}"
  predict_hostname = "predict.${var.domain}"
  tunnel_cname     = "${data.cloudflare_zero_trust_tunnel_cloudflared.api.id}.cfargotunnel.com"
}

resource "cloudflare_pages_project" "frontend" {
  account_id        = var.account_id
  name              = var.pages_project_name
  production_branch = var.pages_branch

  build_config = {
    build_caching   = true
    build_command   = "npm ci && npm run build"
    destination_dir = "dist"
    root_dir        = "src/frontend"
  }

  source = {
    type = "github"

    config = {
      owner                          = var.pages_repo_owner
      repo_id                        = var.pages_repo_id
      repo_name                      = var.pages_repo_name
      path_includes                  = ["src/frontend/**"]
      preview_deployment_setting     = "all"
      production_branch              = var.pages_branch
      production_deployments_enabled = true
      pr_comments_enabled            = true
    }
  }
}


resource "cloudflare_dns_record" "frontend_cname" {
  zone_id = var.zone_id
  name    = local.app_hostname
  type    = "CNAME"
  content = cloudflare_pages_project.frontend.subdomain
  proxied = true
  ttl     = 1
}

resource "cloudflare_pages_domain" "frontend_domain" {
  account_id   = var.account_id
  project_name = cloudflare_pages_project.frontend.name
  name         = local.app_hostname

  depends_on = [
    cloudflare_pages_project.frontend,
    cloudflare_dns_record.frontend_cname,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared" "api" {
  account_id = var.account_id

  filter = {
    name       = var.tunnel_name
    is_deleted = false
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "api" {
  account_id = var.account_id
  tunnel_id  = data.cloudflare_zero_trust_tunnel_cloudflared.api.id
}

resource "cloudflare_dns_record" "auth_api_cname" {
  zone_id = var.zone_id
  name    = local.auth_hostname
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

resource "cloudflare_dns_record" "predict_api_cname" {
  zone_id = var.zone_id
  name    = local.predict_hostname
  type    = "CNAME"
  content = local.tunnel_cname
  proxied = true
  ttl     = 1
}

resource "cloudflare_ruleset" "zone_custom_firewall" {
  zone_id     = var.zone_id
  name        = "zone-custom-firewall"
  description = "Block non-standard HTTP(S) ports"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules = [
    {
      ref         = "block_non_default_ports"
      description = "Block ports other than 80 and 443"
      enabled     = true
      expression  = "not (cf.edge.server_port in {80 443})"
      action      = "block"
    }
  ]
}

resource "cloudflare_ruleset" "zone_rate_limit" {
  count       = var.rate_limit_enabled ? 1 : 0
  zone_id     = var.zone_id
  name        = "zone-rate-limit"
  description = "Rate limiting for API hosts"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules = [
    {
      ref         = "rate_limit_api_hosts"
      description = "Rate limit auth and predict API hosts by IP"
      enabled     = true
      expression  = "(http.host in {\"${local.auth_hostname}\" \"${local.predict_hostname}\"})"
      action      = var.rate_limit_action
      ratelimit = {
        characteristics     = ["cf.colo.id", "ip.src"]
        period              = var.rate_limit_period
        requests_per_period = var.rate_limit_requests
        mitigation_timeout   = var.rate_limit_mitigation_timeout
      }
    }
  ]
}

output "frontend_url" {
  value = "https://${local.app_hostname}"
}

output "auth_url" {
  value = "https://${local.auth_hostname}"
}

output "predict_url" {
  value = "https://${local.predict_hostname}"
}

output "pages_project_name" {
  value = cloudflare_pages_project.frontend.name
}

output "cloudflare_tunnel_id" {
  value = data.cloudflare_zero_trust_tunnel_cloudflared.api.id
}

output "cloudflare_tunnel_name" {
  value = data.cloudflare_zero_trust_tunnel_cloudflared.api.name
}

output "cloudflare_tunnel_token" {
  value     = data.cloudflare_zero_trust_tunnel_cloudflared_token.api.token
  sensitive = true
}