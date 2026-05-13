// src/infra/terraform/aws/main.tf
// Root composition for the E2E RAG platform.
//
// This keeps the bootstrap AWS-only, uses EKS add-ons instead of
// Helm/Kubernetes providers, installs kubectl on the admin EC2 at boot,
// and relies on the VPC module for the required private-network endpoints.

terraform {
  backend "s3" {}
}

data "aws_caller_identity" "current" {}

module "vpc" {
  source = "./modules/vpc"

  cluster_name         = var.cluster_name
  vpc_cidr             = var.vpc_cidr
  az_count             = var.az_count
  private_subnet_cidrs = var.private_subnet_cidrs
  public_subnet_cidrs  = var.public_subnet_cidrs
  enable_nat_per_az    = var.enable_nat_per_az
  single_nat_gateway   = var.single_nat_gateway
  tags                 = var.tags
}

module "security" {
  source = "./modules/security"

  vpc_id      = module.vpc.vpc_id
  vpc_cidr    = var.vpc_cidr
  name_prefix = "rag"
  tags        = var.tags
}

module "ecr" {
  source = "./modules/ecr"

  repositories = var.ecr_repositories
  tags         = var.tags
}

module "iam_pre_eks" {
  source = "./modules/iam_pre_eks"

  name_prefix = "rag"
  tags        = var.tags
}

module "eks" {
  source = "./modules/eks"

  cluster_name = var.cluster_name
  region       = var.region

  vpc_id                 = module.vpc.vpc_id
  subnet_ids             = module.vpc.private_subnet_ids
  node_security_group_id = module.security.node_security_group_id
  cluster_role_arn       = module.iam_pre_eks.cluster_role_arn
  node_role_arn          = module.iam_pre_eks.node_role_arn

  system_nodegroup      = var.system_nodegroup
  workloads_nodegroup   = var.workloads_nodegroup
  system_node_taints    = []
  workloads_node_taints = var.workloads_node_taints
  system_node_labels    = var.system_node_labels
  workloads_node_labels = var.workloads_node_labels

  enabled_cluster_log_types = []

  tags = var.tags

  depends_on = [
    module.vpc,
    module.security,
    module.iam_pre_eks
  ]
}

module "s3" {
  source = "./modules/s3"

  buckets = var.s3_buckets
  tags    = var.tags
}

module "iam_post_eks" {
  source = "./modules/iam_post_eks"

  name_prefix = "rag"
  tags        = var.tags

  oidc_provider_arn    = module.eks.oidc_provider_arn
  oidc_provider_issuer = module.eks.oidc_provider_issuer

  s3_bucket_name_map = module.s3.bucket_name_map
  s3_bucket_arn_map  = module.s3.bucket_arn_map

  irsa_roles           = var.irsa_roles
  github_actions_roles = var.github_actions_roles

  depends_on = [
    module.eks,
    module.s3
  ]
}

###############################################################################
# PRIVATE ADMIN EC2 FOR EKS ACCESS
###############################################################################

data "aws_ssm_parameter" "al2023_x86_64_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_security_group" "eks_admin" {
  name        = "${var.cluster_name}-eks-admin-sg"
  description = "Private SSM-only admin host for EKS access"
  vpc_id      = module.vpc.vpc_id

  egress {
    description = "Allow HTTPS only inside the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(var.tags, {
    Name    = "${var.cluster_name}-eks-admin-sg"
    Purpose = "eks-admin"
  })
}

resource "aws_iam_role" "eks_admin" {
  name = "${var.cluster_name}-eks-admin-role"

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

  tags = merge(var.tags, {
    Name    = "${var.cluster_name}-eks-admin-role"
    Purpose = "eks-admin"
  })
}

resource "aws_iam_role_policy_attachment" "eks_admin_ssm" {
  role       = aws_iam_role.eks_admin.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "eks_admin_describe_cluster" {
  name = "${var.cluster_name}-eks-admin-describe-cluster"
  role = aws_iam_role.eks_admin.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster"
        ]
        Resource = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${module.eks.cluster_name}"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "eks_admin" {
  name = "${var.cluster_name}-eks-admin-profile"
  role = aws_iam_role.eks_admin.name
}

resource "aws_instance" "eks_admin" {
  ami                         = data.aws_ssm_parameter.al2023_x86_64_ami.value
  instance_type               = "t3.micro"
  subnet_id                   = module.vpc.private_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.eks_admin.id]
  iam_instance_profile        = aws_iam_instance_profile.eks_admin.name
  associate_public_ip_address = false
  user_data_replace_on_change = true

  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail

    KUBECTL_VERSION="1.35.3"
    ARCH="amd64"

    if [ "$(uname -m)" = "aarch64" ]; then
      ARCH="arm64"
    fi

    curl -fsSLo /usr/local/bin/kubectl \
      https://s3.us-west-2.amazonaws.com/amazon-eks/$${KUBECTL_VERSION}/2026-04-08/bin/linux/$${ARCH}/kubectl

    chmod +x /usr/local/bin/kubectl
  EOF

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "disabled"
  }

  tags = merge(var.tags, {
    Name    = "${var.cluster_name}-eks-admin"
    Purpose = "eks-admin"
  })
}

resource "aws_security_group_rule" "eks_admin_to_cluster" {
  description              = "Allow the admin host to reach the EKS API server"
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = module.eks.cluster_security_group_id
  source_security_group_id = aws_security_group.eks_admin.id
}

###############################################################################
# EBS CSI DRIVER AS AN EKS ADD-ON
###############################################################################

resource "aws_eks_addon" "aws_ebs_csi_driver" {
  cluster_name             = module.eks.cluster_name
  addon_name               = "aws-ebs-csi-driver"
  service_account_role_arn = module.iam_post_eks.ebs_csi_driver_role_arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    module.vpc,
    module.eks,
    module.iam_post_eks
  ]

  tags = var.tags
}