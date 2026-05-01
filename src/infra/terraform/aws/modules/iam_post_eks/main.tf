// src/terraform/aws/modules/iam_post_eks/main.tf
// Post-EKS IAM identities:
// - IRSA roles for Kubernetes service accounts (S3 access)
// - GitHub Actions OIDC roles (ECR push/pull access)

variable "name_prefix" {
  description = "Prefix used for IAM role and policy names."
  type        = string
  default     = "mlops"
}

variable "tags" {
  description = "Tags applied to resources."
  type        = map(string)
  default     = {}
}

variable "oidc_provider_arn" {
  description = "ARN of the EKS OIDC provider."
  type        = string
}

variable "oidc_provider_issuer" {
  description = "OIDC issuer host/path without https:// prefix."
  type        = string
}

variable "s3_bucket_name_map" {
  description = "Logical bucket key -> bucket name."
  type        = map(string)
}

variable "s3_bucket_arn_map" {
  description = "Logical bucket key -> bucket ARN."
  type        = map(string)
}

variable "irsa_roles" {
  description = "IRSA role definitions."
  type = map(object({
    namespace       = string
    service_account = string
    bucket_key      = string
    access          = string
  }))

  validation {
    condition = alltrue([
      for _, role in var.irsa_roles :
      contains(keys(var.s3_bucket_name_map), role.bucket_key) &&
      contains(keys(var.s3_bucket_arn_map), role.bucket_key) &&
      contains(["read", "read_write"], role.access) &&
      length(trimspace(role.namespace)) > 0 &&
      length(trimspace(role.service_account)) > 0
    ])
    error_message = "Each irsa_roles item must reference a valid bucket_key, valid access mode, namespace, and service_account."
  }
}

variable "github_actions_roles" {
  description = "GitHub Actions OIDC role definitions."
  type = map(object({
    repository = string
    branch     = string
    role_name  = string
  }))

  validation {
    condition = alltrue([
      for role_key, role in var.github_actions_roles :
      contains(
        [
          "flyte_elt_task",
          "flyte_train_task",
          "tabular_inference_service"
        ],
        role_key
      ) &&
      can(regex("^[a-z0-9._-]+/[a-z0-9._-]+$", role.repository)) &&
      length(trimspace(role.branch)) > 0 &&
      length(trimspace(role.role_name)) > 0
    ])
    error_message = "GitHub Actions roles must use approved keys and repository format owner/repo in lowercase."
  }
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  env_tag = lookup(var.tags, "Environment", "prod")

  common_tags = merge(
    {
      ManagedBy   = "opentofu"
      Platform    = "mlops"
      Environment = local.env_tag
    },
    var.tags
  )

  irsa_role_names = {
    for k, _ in var.irsa_roles :
    k => "${var.name_prefix}-${k}-irsa-role"
  }

  irsa_policy_names = {
    for k, _ in var.irsa_roles :
    k => "${var.name_prefix}-${k}-irsa-policy"
  }

  github_role_names = {
    for k, v in var.github_actions_roles :
    k => v.role_name
  }

  github_policy_names = {
    for k, v in var.github_actions_roles :
    k => "${v.role_name}-policy"
  }

  github_ecr_repository_names = {
    flyte_elt_task            = "flyte-elt-task"
    flyte_train_task          = "flyte-train-task"
    tabular_inference_service = "tabular-inference-service"
  }

  list_bucket_actions = ["s3:ListBucket"]

  read_object_actions = ["s3:GetObject"]

  read_write_object_actions = [
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject"
  ]
}

###############################################################################
# IRSA trust policies
###############################################################################

data "aws_iam_policy_document" "irsa_assume_role" {
  for_each = var.irsa_roles

  statement {
    sid     = "AllowAssumeRoleWithWebIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_issuer}:sub"
      values = [
        "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

###############################################################################
# IRSA S3 access policies
###############################################################################

data "aws_iam_policy_document" "irsa_s3_access" {
  for_each = var.irsa_roles

  statement {
    sid       = "AllowBucketListing"
    effect    = "Allow"
    actions   = local.list_bucket_actions
    resources = [var.s3_bucket_arn_map[each.value.bucket_key]]
  }

  statement {
    sid    = "AllowObjectAccess"
    effect = "Allow"

    actions = each.value.access == "read" ? local.read_object_actions : local.read_write_object_actions

    resources = [
      "${var.s3_bucket_arn_map[each.value.bucket_key]}/*"
    ]
  }
}

resource "aws_iam_role" "irsa" {
  for_each = var.irsa_roles

  name               = local.irsa_role_names[each.key]
  assume_role_policy = data.aws_iam_policy_document.irsa_assume_role[each.key].json
  tags               = local.common_tags
}

resource "aws_iam_policy" "irsa" {
  for_each = var.irsa_roles

  name        = local.irsa_policy_names[each.key]
  description = "IRSA S3 policy for ${each.key}"
  policy      = data.aws_iam_policy_document.irsa_s3_access[each.key].json
  tags        = local.common_tags
}

resource "aws_iam_role_policy_attachment" "irsa" {
  for_each = var.irsa_roles

  role       = aws_iam_role.irsa[each.key].name
  policy_arn = aws_iam_policy.irsa[each.key].arn
}

###############################################################################
# GitHub Actions trust policies
###############################################################################

data "aws_iam_policy_document" "github_assume_role" {
  for_each = var.github_actions_roles

  statement {
    sid     = "AllowGitHubActionsOIDC"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type = "Federated"
      identifiers = [
        "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${each.value.repository}:ref:refs/heads/${each.value.branch}"
      ]
    }
  }
}

###############################################################################
# GitHub Actions ECR access policies
###############################################################################

data "aws_iam_policy_document" "github_ecr_push" {
  for_each = var.github_actions_roles

  statement {
    sid       = "AllowECRAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "AllowECRPushPull"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart"
    ]

    resources = [
      "arn:${data.aws_partition.current.partition}:ecr:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:repository/${local.github_ecr_repository_names[each.key]}"
    ]
  }
}

resource "aws_iam_role" "github_actions" {
  for_each = var.github_actions_roles

  name               = local.github_role_names[each.key]
  assume_role_policy = data.aws_iam_policy_document.github_assume_role[each.key].json
  tags               = local.common_tags
}

resource "aws_iam_policy" "github_actions" {
  for_each = var.github_actions_roles

  name        = local.github_policy_names[each.key]
  description = "GitHub Actions ECR policy for ${each.key}"
  policy      = data.aws_iam_policy_document.github_ecr_push[each.key].json
  tags        = local.common_tags
}

resource "aws_iam_role_policy_attachment" "github_actions" {
  for_each = var.github_actions_roles

  role       = aws_iam_role.github_actions[each.key].name
  policy_arn = aws_iam_policy.github_actions[each.key].arn
}

###############################################################################
# Outputs
###############################################################################

output "irsa_role_arns" {
  description = "IRSA role ARNs."
  value = {
    for k, v in aws_iam_role.irsa :
    k => v.arn
  }
}

output "irsa_role_names" {
  description = "IRSA role names."
  value = {
    for k, v in aws_iam_role.irsa :
    k => v.name
  }
}

output "irsa_policy_arns" {
  description = "IRSA policy ARNs."
  value = {
    for k, v in aws_iam_policy.irsa :
    k => v.arn
  }
}

output "github_actions_role_arns" {
  description = "GitHub Actions role ARNs."
  value = {
    for k, v in aws_iam_role.github_actions :
    k => v.arn
  }
}

output "github_actions_role_names" {
  description = "GitHub Actions role names."
  value = {
    for k, v in aws_iam_role.github_actions :
    k => v.name
  }
}

output "github_actions_policy_arns" {
  description = "GitHub Actions policy ARNs."
  value = {
    for k, v in aws_iam_policy.github_actions :
    k => v.arn
  }
}