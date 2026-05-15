// src/infra/terraform/aws/modules/karpenter/iam.tf
// AWS-side Karpenter infrastructure.
//
// Terraform owns ONLY:
// - controller IRSA role
// - controller IAM permissions
// - Karpenter node IAM role
// - EKS access entry
//
// ArgoCD owns:
// - Helm chart install
// - EC2NodeClass
// - NodePool

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
}

variable "region" {
  description = "AWS region."
  type        = string
}

variable "tags" {
  description = "Tags applied to resources."
  type        = map(string)
  default     = {}
}

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN."
  type        = string
}

variable "oidc_provider_issuer" {
  description = "OIDC issuer without https:// prefix."
  type        = string
}

variable "karpenter_namespace" {
  description = "Namespace where Karpenter runs."
  type        = string
  default     = "karpenter"
}

variable "controller_service_account_name" {
  description = "Karpenter controller service account name."
  type        = string
  default     = "karpenter"
}

variable "controller_role_name" {
  description = "IAM role name for Karpenter controller."
  type        = string
  default     = ""
}

variable "node_role_name" {
  description = "IAM role name for Karpenter nodes."
  type        = string
  default     = ""
}

variable "node_pool_name" {
  description = "Default NodePool name exposed as output."
  type        = string
  default     = "compute"
}

variable "node_class_name" {
  description = "Default EC2NodeClass name exposed as output."
  type        = string
  default     = "compute"
}

data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

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

  controller_role_name = (
    var.controller_role_name != ""
    ? var.controller_role_name
    : "${var.cluster_name}-karpenter"
  )

  node_role_name = (
    var.node_role_name != ""
    ? var.node_role_name
    : "KarpenterNodeRole-${var.cluster_name}"
  )

  controller_sa_subject = (
    "system:serviceaccount:${var.karpenter_namespace}:${var.controller_service_account_name}"
  )

  cluster_arn = (
    "arn:${data.aws_partition.current.partition}:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"
  )
}

###############################################################################
# CONTROLLER IRSA ROLE
###############################################################################

data "aws_iam_policy_document" "controller_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_issuer}:sub"
      values   = [local.controller_sa_subject]
    }
  }
}

resource "aws_iam_role" "controller" {
  name               = local.controller_role_name
  assume_role_policy = data.aws_iam_policy_document.controller_assume_role.json

  tags = merge(local.common_tags, {
    Name    = local.controller_role_name
    Purpose = "karpenter-controller"
  })
}

###############################################################################
# KARPENTER NODE ROLE
###############################################################################

data "aws_iam_policy_document" "node_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = local.node_role_name
  assume_role_policy = data.aws_iam_policy_document.node_assume_role.json

  tags = merge(local.common_tags, {
    Name    = local.node_role_name
    Purpose = "karpenter-node"
  })
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "node_ecr_pull" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
}

resource "aws_iam_role_policy_attachment" "node_ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

###############################################################################
# CONTROLLER POLICY
###############################################################################

data "aws_iam_policy_document" "controller" {
  statement {
    sid    = "AllowRegionalReadActions"
    effect = "Allow"

    actions = [
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeCapacityReservations",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribePlacementGroups",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeSubnets",
      "pricing:GetProducts",
      "ssm:GetParameter",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "AllowEC2LifecycleActions"
    effect = "Allow"

    actions = [
      "ec2:RunInstances",
      "ec2:CreateFleet",
      "ec2:CreateLaunchTemplate",
      "ec2:DeleteLaunchTemplate",
      "ec2:CreateTags",
      "ec2:TerminateInstances",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "AllowIAMInstanceProfileActions"
    effect = "Allow"

    actions = [
      "iam:PassRole",
      "iam:CreateInstanceProfile",
      "iam:TagInstanceProfile",
      "iam:AddRoleToInstanceProfile",
      "iam:RemoveRoleFromInstanceProfile",
      "iam:DeleteInstanceProfile",
      "iam:GetInstanceProfile",
      "iam:ListInstanceProfiles",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "AllowEKSDiscovery"
    effect = "Allow"

    actions = [
      "eks:DescribeCluster",
    ]

    resources = [
      local.cluster_arn,
    ]
  }
}

resource "aws_iam_role_policy" "controller" {
  name   = "karpenter-controller-${var.cluster_name}"
  role   = aws_iam_role.controller.name
  policy = data.aws_iam_policy_document.controller.json
}

###############################################################################
# NODE ACCESS ENTRY
###############################################################################

resource "aws_eks_access_entry" "karpenter_node" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.node.arn
  type          = "EC2_LINUX"
}

###############################################################################
# OUTPUTS
###############################################################################

output "controller_role_arn" {
  description = "Karpenter controller IAM role ARN."
  value       = aws_iam_role.controller.arn
}

output "controller_role_name" {
  description = "Karpenter controller IAM role name."
  value       = aws_iam_role.controller.name
}

output "node_role_arn" {
  description = "Karpenter node IAM role ARN."
  value       = aws_iam_role.node.arn
}

output "node_role_name" {
  description = "Karpenter node IAM role name."
  value       = aws_iam_role.node.name
}

output "node_pool_name" {
  description = "Suggested NodePool name for GitOps manifests."
  value       = var.node_pool_name
}

output "node_class_name" {
  description = "Suggested EC2NodeClass name for GitOps manifests."
  value       = var.node_class_name
}