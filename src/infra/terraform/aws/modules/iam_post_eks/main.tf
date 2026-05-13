// src/infra/terraform/aws/modules/iam_post_eks/main.tf
// Post-EKS IAM identities for the RAG platform:
// - IRSA roles for Kubernetes service accounts
// - GitHub Actions OIDC roles for ECR push/pull
// - EBS CSI driver IRSA role

variable "name_prefix" {
  description = "Prefix used for IAM role and policy names."
  type        = string
  default     = "rag"
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
    buckets = optional(list(object({
      key    = string
      access = string
    })), [])
    aws_services = optional(object({
      bedrock = optional(bool, false)
    }), null)
  }))

  validation {
    condition = alltrue([
      for _, role in var.irsa_roles :
      length(trimspace(role.namespace)) > 0 &&
      length(trimspace(role.service_account)) > 0 &&
      (
        length(try(role.buckets, [])) > 0 ||
        try(role.aws_services.bedrock, false) == true
      ) &&
      alltrue([
        for bucket in try(role.buckets, []) :
        contains(keys(var.s3_bucket_name_map), bucket.key) &&
        contains(keys(var.s3_bucket_arn_map), bucket.key) &&
        contains(["read", "read_write"], bucket.access)
      ])
    ])
    error_message = "Each irsa_roles item must define namespace/service_account and either S3 buckets or aws_services.bedrock, with valid bucket keys and access modes."
  }
}

variable "github_actions_roles" {
  description = "GitHub Actions OIDC role definitions."
  type = map(object({
    repository = string
    branch     = string
    role_name  = string
    ecr_repo   = string
  }))

  validation {
    condition = alltrue([
      for _, role in var.github_actions_roles :
      strcontains(role.repository, "/") &&
      can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", role.repository)) &&
      role.branch == "main" &&
      can(regex("^gh-actions-[a-z0-9-]+$", role.role_name)) &&
      can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", role.ecr_repo))
    ])
    error_message = "GitHub Actions roles must use repo owner/repo format, branch main, a gh-actions-* role name, and a lowercase hyphenated ecr_repo."
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
      Platform    = "rag"
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
    for k, v in var.github_actions_roles :
    k => v.ecr_repo
  }

  list_bucket_actions       = ["s3:ListBucket"]
  read_object_actions       = ["s3:GetObject"]
  read_write_object_actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]

  irsa_s3_roles = {
    for k, role in var.irsa_roles :
    k => role if length(try(role.buckets, [])) > 0
  }

  irsa_s3_statements = {
    for role_key, role in local.irsa_s3_roles :
    role_key => flatten([
      for bucket in role.buckets : [
        {
          actions   = local.list_bucket_actions
          resources = [var.s3_bucket_arn_map[bucket.key]]
        },
        {
          actions = bucket.access == "read" ? local.read_object_actions : local.read_write_object_actions
          resources = [
            "${var.s3_bucket_arn_map[bucket.key]}/*"
          ]
        }
      ]
    ])
  }
}

###############################################################################
# IRSA trust policies
###############################################################################

data "aws_iam_policy_document" "irsa_assume_role" {
  for_each = var.irsa_roles

  statement {
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
  for_each = local.irsa_s3_roles

  dynamic "statement" {
    for_each = local.irsa_s3_statements[each.key]
    content {
      effect    = "Allow"
      actions   = statement.value.actions
      resources = statement.value.resources
    }
  }
}

###############################################################################
# IRSA BEDROCK POLICY FOR RETRIEVER (LEAST PRIVILEGE)
###############################################################################

data "aws_iam_policy_document" "irsa_bedrock_access" {
  for_each = {
    for k, role in var.irsa_roles :
    k => role if try(role.aws_services.bedrock, false)
  }

  statement {
    sid    = "AllowInvokeSpecificModel"
    effect = "Allow"

    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream"
    ]

    resources = [
      "arn:aws:bedrock:${data.aws_region.current.region}::foundation-model/meta.llama3-8b-instruct-v1:0"
    ]
  }
}

resource "aws_iam_policy" "irsa_bedrock" {
  for_each = data.aws_iam_policy_document.irsa_bedrock_access

  name        = "${var.name_prefix}-${each.key}-bedrock-policy"
  description = "IRSA Bedrock policy for ${each.key}"
  policy      = each.value.json
  tags        = local.common_tags
}

resource "aws_iam_role_policy_attachment" "irsa_bedrock" {
  for_each = aws_iam_policy.irsa_bedrock

  role       = aws_iam_role.irsa[each.key].name
  policy_arn = each.value.arn
}

###############################################################################
# EBS CSI driver IRSA policy
###############################################################################

data "aws_iam_policy_document" "ebs_csi_assume_role" {
  statement {
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
        "system:serviceaccount:kube-system:ebs-csi-controller-sa"
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi_driver" {
  name               = "${var.name_prefix}-ebs-csi-driver-role"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi_driver" {
  role       = aws_iam_role.ebs_csi_driver.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

###############################################################################
# IRSA roles
###############################################################################

resource "aws_iam_role" "irsa" {
  for_each = var.irsa_roles

  name               = local.irsa_role_names[each.key]
  assume_role_policy = data.aws_iam_policy_document.irsa_assume_role[each.key].json
  tags               = local.common_tags
}

resource "aws_iam_policy" "irsa_s3" {
  for_each = local.irsa_s3_roles

  name        = local.irsa_policy_names[each.key]
  description = "IRSA S3 policy for ${each.key}"
  policy      = data.aws_iam_policy_document.irsa_s3_access[each.key].json
  tags        = local.common_tags
}

resource "aws_iam_role_policy_attachment" "irsa_s3" {
  for_each = local.irsa_s3_roles

  role       = aws_iam_role.irsa[each.key].name
  policy_arn = aws_iam_policy.irsa_s3[each.key].arn
}

###############################################################################
# GitHub Actions trust policies
###############################################################################

data "aws_iam_policy_document" "github_assume_role" {
  for_each = var.github_actions_roles

  statement {
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
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
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
      "arn:aws:ecr:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:repository/${local.github_ecr_repository_names[each.key]}"
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
  value = merge(
    { for k, v in aws_iam_policy.irsa_s3 : k => v.arn },
    { for k, v in aws_iam_policy.irsa_bedrock : k => v.arn }
  )
}

output "ebs_csi_driver_role_arn" {
  description = "EBS CSI driver IAM role ARN."
  value       = aws_iam_role.ebs_csi_driver.arn
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