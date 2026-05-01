// src/terraform/aws/modules/s3/main.tf
// Managed S3 buckets for the MLOps platform.
//
// Responsibilities:
// - create named buckets from a root-provided map
// - enforce public access blocking
// - enforce bucket-owner-enforced ownership controls
// - enable server-side encryption
// - enable or suspend versioning per bucket
//
// This module intentionally avoids:
// - bucket policies for public access
// - replication
// - lifecycle rules unless explicitly needed later

variable "buckets" {
  description = "Map of logical bucket key -> bucket configuration."
  type = map(object({
    name          = string
    versioning    = bool
    force_destroy = bool
  }))

  validation {
    condition = alltrue([
      for _, b in var.buckets :
      length(trimspace(b.name)) > 0 &&
      can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", b.name))
    ])
    error_message = "Each bucket name must be a valid lowercase S3 bucket name."
  }
}

variable "tags" {
  description = "Tags applied to all buckets created by this module."
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

  bucket_names = [for _, b in var.buckets : b.name]
}

# Prevent accidental duplicate names in the same configuration.
# This does not solve global S3 namespace collisions, so env tfvars still
# must use globally unique bucket names.
resource "null_resource" "bucket_name_uniqueness_guard" {
  lifecycle {
    precondition {
      condition     = length(distinct(local.bucket_names)) == length(local.bucket_names)
      error_message = "Bucket names within var.buckets must be unique."
    }
  }
}

resource "aws_s3_bucket" "this" {
  for_each = var.buckets

  bucket        = each.value.name
  force_destroy = each.value.force_destroy

  tags = merge(local.common_tags, {
    Name = each.value.name
    Role = each.key
  })
}

resource "aws_s3_bucket_ownership_controls" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = var.buckets

  bucket = aws_s3_bucket.this[each.key].id

  versioning_configuration {
    status = each.value.versioning ? "Enabled" : "Suspended"
  }
}

output "bucket_name_map" {
  description = "Logical bucket key -> bucket name."
  value = {
    for k, b in aws_s3_bucket.this : k => b.bucket
  }
}

output "bucket_arn_map" {
  description = "Logical bucket key -> bucket ARN."
  value = {
    for k, b in aws_s3_bucket.this : k => b.arn
  }
}

output "bucket_id_map" {
  description = "Logical bucket key -> bucket ID."
  value = {
    for k, b in aws_s3_bucket.this : k => b.id
  }
}