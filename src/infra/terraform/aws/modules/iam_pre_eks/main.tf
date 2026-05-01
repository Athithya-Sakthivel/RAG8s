// src/terraform/aws/modules/iam_pre_eks/main.tf
// Pre-EKS IAM for the platform.
//
// Responsibilities:
// - EKS control plane role
// - EKS node role
// - Cluster Autoscaler policy
// - CI ECR push policy (legacy/backward compatibility)
// - EBS CSI managed policy ARN output
//
// The worker node role includes AmazonEC2ContainerRegistryPullOnly so managed
// nodes can bootstrap and pull images successfully.

variable "name_prefix" {
  description = "Prefix used for IAM role and policy names."
  type        = string
  default     = "mlops"
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}

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
}

resource "aws_iam_role" "cluster" {
  name = "${var.name_prefix}-eks-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "cluster_attach_eks_cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role" "node" {
  name = "${var.name_prefix}-eks-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "node_attach_eks_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_attach_ecr_pull" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
}

resource "aws_iam_role_policy_attachment" "node_attach_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_policy" "cluster_autoscaler" {
  name = "${var.name_prefix}-cluster-autoscaler-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeScalingActivities",
          "ec2:DescribeImages",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplateVersions",
          "ec2:GetInstanceTypesFromInstanceRequirements",
          "eks:DescribeNodegroup"
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "autoscaling:SetDesiredCapacity",
          "autoscaling:TerminateInstanceInAutoScalingGroup"
        ]
        Resource = ["*"]
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_policy" "ci_ecr_push" {
  name = "${var.name_prefix}-ci-ecr-push-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage"
        ]
        Resource = ["*"]
      }
    ]
  })

  tags = local.common_tags
}

output "cluster_role_arn" {
  description = "ARN of the EKS control plane IAM role."
  value       = aws_iam_role.cluster.arn
}

output "node_role_arn" {
  description = "ARN of the EKS worker node IAM role."
  value       = aws_iam_role.node.arn
}

output "cluster_autoscaler_policy_arn" {
  description = "ARN of the Cluster Autoscaler policy."
  value       = aws_iam_policy.cluster_autoscaler.arn
}

output "ci_ecr_push_policy_arn" {
  description = "ARN of the CI ECR push policy."
  value       = aws_iam_policy.ci_ecr_push.arn
}

output "ebs_csi_managed_policy_arn" {
  description = "AWS-managed EBS CSI driver policy ARN."
  value       = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}