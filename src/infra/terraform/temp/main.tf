terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.43.0"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "guardrail_name" {
  type    = string
  default = "rag-llm-guardrail"
}

variable "environment" {
  type    = string
  default = "prod"
}

provider "aws" {
  region = var.aws_region
}

locals {
  blocked_input_message  = "Your request was blocked by guardrail policy."
  blocked_output_message = "The model response was blocked by guardrail policy."

  tags = {
    Name        = var.guardrail_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "aws_bedrock_guardrail" "rag" {
  name                      = var.guardrail_name
  description               = "Production guardrail for the retriever/RAG service."
  blocked_input_messaging   = local.blocked_input_message
  blocked_outputs_messaging = local.blocked_output_message
  tags                      = local.tags

  content_policy_config {
    filters_config {
      type           = "HATE"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }

    filters_config {
      type           = "INSULTS"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }

    filters_config {
      type           = "SEXUAL"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }

    filters_config {
      type           = "VIOLENCE"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }

    filters_config {
      type           = "MISCONDUCT"
      input_strength  = "MEDIUM"
      output_strength = "MEDIUM"
    }

    filters_config {
      type           = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }
}

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