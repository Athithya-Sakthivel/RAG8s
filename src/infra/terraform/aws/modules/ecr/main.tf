// src/terraform/aws/modules/ecr/main.tf
// ECR repository module for the MLOps platform.
//
// Responsibilities:
// - create repositories from root-provided input
// - enforce immutable tags
// - enable scan-on-push by default
// - enforce AES256 encryption by default
// - attach exactly one lifecycle policy per repository
//
// This module is production-owned, tfvars-driven, and contains no AgentOps names.

variable "repositories" {
  description = "Map of logical repository key -> repository configuration."
  type = map(object({
    name                 = string
    image_tag_mutability = optional(string, "IMMUTABLE")
    scan_on_push         = optional(bool, true)
    encryption_type      = optional(string, "AES256")
    retain_last_images   = optional(number, 30)
  }))

  validation {
    condition = alltrue([
      for _, v in var.repositories :
      length(trimspace(v.name)) > 0 &&
      v.retain_last_images > 0 &&
      try(upper(v.image_tag_mutability), "IMMUTABLE") == "IMMUTABLE" &&
      try(v.scan_on_push, true) == true &&
      try(upper(v.encryption_type), "AES256") == "AES256"
    ])
    error_message = "Each repositories entry must define a non-empty name, retain_last_images > 0, immutable tags, scan_on_push = true, and AES256 encryption."
  }
}

variable "tags" {
  description = "Tags applied to all ECR repositories created by this module."
  type        = map(string)
  default     = {}
}

locals {
  env_tag = lookup(var.tags, "Environment", "prod")

  common_tags = merge(
    {
      ManagedBy   = "mlops-platform-terraform"
      Environment = local.env_tag
    },
    var.tags
  )
}

resource "aws_ecr_repository" "this" {
  for_each = var.repositories

  name = each.value.name

  # Hard-enforced for supply-chain security.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = try(each.value.scan_on_push, true)
  }

  encryption_configuration {
    encryption_type = try(each.value.encryption_type, "AES256")
  }

  tags = merge(local.common_tags, {
    Name = each.value.name
    Role = each.key
  })
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each = var.repositories

  repository = aws_ecr_repository.this[each.key].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last ${each.value.retain_last_images} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = each.value.retain_last_images
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "repository_url_map" {
  description = "Logical repository key -> repository URL."
  value = {
    for k, repo in aws_ecr_repository.this : k => repo.repository_url
  }
}

output "repository_arn_map" {
  description = "Logical repository key -> repository ARN."
  value = {
    for k, repo in aws_ecr_repository.this : k => repo.arn
  }
}

output "repository_name_map" {
  description = "Logical repository key -> repository name."
  value = {
    for k, repo in aws_ecr_repository.this : k => repo.name
  }
}