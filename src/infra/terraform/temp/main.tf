terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.44.0"         # Latest stable, May 2026
    }
  }
}

variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "guardrail_name" {
  type    = string
  default = "rag-essential-guardrail"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "pii_mask_enabled" {
  type    = bool
  default = false                  # Override in shell with TF_VAR_pii_mask_enabled=true
}

provider "aws" {
  region = var.aws_region
}

locals {
  blocked_input_message  = "Your request was blocked by security policy."
  blocked_output_message = "The model response was blocked by security policy."

  tags = {
    Name        = var.guardrail_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_bedrock_guardrail" "rag" {
  name                      = var.guardrail_name
  description               = "Production guardrail for RAG – prompt‑attack defence (MEDIUM) and PII masking."
  blocked_input_messaging   = local.blocked_input_message
  blocked_outputs_messaging = local.blocked_output_message
  tags                      = local.tags

  # Single content filter: prompt‑attack with medium sensitivity
  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "MEDIUM"   # blocks high/medium confidence attacks; avoids false positives on security docs
      output_strength = "NONE"     # no need to inspect model responses for prompt attacks
    }
  }

  # Conditional PII masking – only EMAIL and PHONE, controlled by TF_VAR_pii_mask_enabled
  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = var.pii_mask_enabled ? ["EMAIL", "PHONE"] : []
      content {
        type   = pii_entities_config.value
        action = "ANONYMIZE"
      }
    }
  }

  # No denied topics, no word filters, no extra content filters – each would risk false positives
}

# Immutable version for safe production deployments and rollbacks
resource "aws_bedrock_guardrail_version" "rag" {
  description   = "Production snapshot for ${var.guardrail_name}"
  guardrail_arn = aws_bedrock_guardrail.rag.guardrail_arn
}

output "bedrock_guardrail_arn" {
  value = aws_bedrock_guardrail.rag.guardrail_arn
}

output "bedrock_guardrail_version_id" {
  value = aws_bedrock_guardrail_version.rag.version
}