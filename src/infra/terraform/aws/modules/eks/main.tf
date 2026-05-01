// src/terraform/aws/modules/eks/main.tf
// EKS cluster + managed nodegroups.
//
// This version fixes the nodegroup bootstrap path by making the worker
// security group explicit in a module-owned launch template.
// Managed nodegroups are always created with a launch template; when custom
// security groups are used there, EKS does not add the cluster security group
// automatically, so the worker SG must be attached deliberately.
//
// Source of truth:
// - private cluster endpoint
// - EKS cluster security group
// - worker node security group attached through launch template
// - nodegroup -> control-plane ingress rule on TCP/443

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "ap-south-1"
}

variable "vpc_id" {
  description = "VPC ID."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the managed nodegroups."
  type        = list(string)
}

variable "node_security_group_id" {
  description = "Security group ID used by worker nodes."
  type        = string

  validation {
    condition     = length(trimspace(var.node_security_group_id)) > 0
    error_message = "node_security_group_id must be provided."
  }
}

variable "cluster_role_arn" {
  description = "IAM role ARN for the EKS control plane."
  type        = string
}

variable "node_role_arn" {
  description = "IAM role ARN for EKS worker nodes."
  type        = string
}

variable "system_nodegroup" {
  description = "Sizing for the system nodegroup."
  type = object({
    instance_type = string
    min_size      = number
    desired_size  = number
    max_size      = number
  })
}

variable "workloads_nodegroup" {
  description = "Sizing for the workloads nodegroup."
  type = object({
    instance_type = string
    min_size      = number
    desired_size  = number
    max_size      = number
  })
}

variable "system_node_taints" {
  description = "Structured taints for the system nodegroup."
  type = list(object({
    key    = string
    value  = string
    effect = string
  }))
  default = [
    { key = "node-type", value = "general", effect = "NO_SCHEDULE" }
  ]

  validation {
    condition = alltrue([
      for t in var.system_node_taints :
      contains(["NO_SCHEDULE", "NO_EXECUTE", "PREFER_NO_SCHEDULE"], t.effect)
    ])
    error_message = "Each system_node_taints[].effect must be one of NO_SCHEDULE, NO_EXECUTE, or PREFER_NO_SCHEDULE."
  }
}

variable "workloads_node_taints" {
  description = "Structured taints for the workloads nodegroup."
  type = list(object({
    key    = string
    value  = string
    effect = string
  }))
  default = [
    { key = "node-type", value = "compute", effect = "NO_SCHEDULE" }
  ]

  validation {
    condition = alltrue([
      for t in var.workloads_node_taints :
      contains(["NO_SCHEDULE", "NO_EXECUTE", "PREFER_NO_SCHEDULE"], t.effect)
    ])
    error_message = "Each workloads_node_taints[].effect must be one of NO_SCHEDULE, NO_EXECUTE, or PREFER_NO_SCHEDULE."
  }
}

variable "system_node_labels" {
  description = "Labels applied to system nodes."
  type        = map(string)
  default     = { "node-type" = "general" }
}

variable "workloads_node_labels" {
  description = "Labels applied to workloads nodes."
  type        = map(string)
  default     = { "node-type" = "compute" }
}

variable "enabled_cluster_log_types" {
  description = "EKS control-plane log types. Keep empty to avoid a CloudWatch dependency."
  type        = list(string)
  default     = []
}

variable "launch_template_id" {
  description = "Retained for backward compatibility. Internal module-owned launch templates are used."
  type        = string
  default     = ""
}

variable "launch_template_version" {
  description = "Retained for backward compatibility. Internal module-owned launch templates are used."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to resources."
  type        = map(string)
  default     = {}
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  env_tag = lookup(var.tags, "Environment", "prod")

  common_tags = merge(
    {
      ManagedBy   = "opentofu"
      Platform    = "mlops"
      Environment = local.env_tag
      Name        = var.cluster_name
    },
    var.tags
  )
}

data "aws_iam_policy_document" "eks_secrets_encryption" {
  statement {
    sid     = "EnableAccountRootPermissions"
    effect  = "Allow"
    actions = ["kms:*"]

    principals {
      type = "AWS"
      identifiers = [
        "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
      ]
    }

    resources = ["*"]
  }

  statement {
    sid    = "AllowEKSClusterRoleToUseKey"
    effect = "Allow"

    actions = [
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*"
    ]

    principals {
      type        = "AWS"
      identifiers = [var.cluster_role_arn]
    }

    resources = ["*"]
  }

  statement {
    sid    = "AllowEKSClusterRoleToManageGrants"
    effect = "Allow"

    actions = [
      "kms:CreateGrant",
      "kms:ListGrants",
      "kms:RevokeGrant"
    ]

    principals {
      type        = "AWS"
      identifiers = [var.cluster_role_arn]
    }

    resources = ["*"]

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "eks_secrets" {
  description             = "EKS secrets encryption key for ${var.cluster_name}"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.eks_secrets_encryption.json

  tags = local.common_tags
}

resource "aws_kms_alias" "eks_secrets" {
  name          = "alias/${var.cluster_name}-eks-secrets"
  target_key_id = aws_kms_key.eks_secrets.key_id
}

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  role_arn = var.cluster_role_arn

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_public_access  = false
    endpoint_private_access = true
  }

  encryption_config {
    resources = ["secrets"]

    provider {
      key_arn = aws_kms_key.eks_secrets.arn
    }
  }

  enabled_cluster_log_types = var.enabled_cluster_log_types

  tags = local.common_tags
}

output "cluster_security_group_id" {
  description = "Control-plane security group ID created by EKS."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

resource "aws_security_group_rule" "allow_nodes_to_control_plane" {
  description              = "Allow worker nodes to contact the EKS API server on TCP/443."
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  source_security_group_id = var.node_security_group_id
}

resource "aws_launch_template" "nodes" {
  name_prefix             = "${var.cluster_name}-nodes-"
  update_default_version  = true
  disable_api_termination = false

  network_interfaces {
    associate_public_ip_address = false
    delete_on_termination       = true
    device_index                = 0
    security_groups = [
      var.node_security_group_id,
      aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
    ]
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = local.common_tags
  }

  tag_specifications {
    resource_type = "volume"
    tags          = local.common_tags
  }

  tags = local.common_tags
}

resource "aws_eks_node_group" "system" {
  depends_on = [aws_security_group_rule.allow_nodes_to_control_plane]

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.cluster_name}-system"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.subnet_ids

  scaling_config {
    desired_size = var.system_nodegroup.desired_size
    min_size     = var.system_nodegroup.min_size
    max_size     = var.system_nodegroup.max_size
  }

  instance_types = [var.system_nodegroup.instance_type]

  dynamic "taint" {
    for_each = var.system_node_taints
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  labels = var.system_node_labels

  launch_template {
    id      = aws_launch_template.nodes.id
    version = "$Latest"
  }

  update_config {
    max_unavailable = 1
  }

  tags = local.common_tags
}

resource "aws_eks_node_group" "workloads" {
  depends_on = [aws_security_group_rule.allow_nodes_to_control_plane]

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.cluster_name}-workloads"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.subnet_ids

  scaling_config {
    desired_size = var.workloads_nodegroup.desired_size
    min_size     = var.workloads_nodegroup.min_size
    max_size     = var.workloads_nodegroup.max_size
  }

  instance_types = [var.workloads_nodegroup.instance_type]

  dynamic "taint" {
    for_each = var.workloads_node_taints
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  labels = var.workloads_node_labels

  launch_template {
    id      = aws_launch_template.nodes.id
    version = "$Latest"
  }

  update_config {
    max_unavailable = 1
  }

  tags = local.common_tags
}

resource "aws_iam_openid_connect_provider" "this" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer

  client_id_list = ["sts.amazonaws.com"]

  thumbprint_list = [
    data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint
  ]

  tags = local.common_tags
}

data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint."
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_ca_data" {
  description = "Base64-encoded CA data for the cluster."
  value       = aws_eks_cluster.this.certificate_authority[0].data
}

output "oidc_provider_arn" {
  description = "ARN of the EKS OIDC provider."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "oidc_provider_issuer" {
  description = "OIDC issuer host/path without https:// prefix."
  value       = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
}

output "secrets_encryption_kms_key_arn" {
  description = "KMS key ARN used for EKS secrets encryption."
  value       = aws_kms_key.eks_secrets.arn
}