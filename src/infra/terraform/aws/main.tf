// src/infra/terraform/aws/main.tf
// Root composition for the E2E RAG platform.
// This file wires module contracts together and installs the EBS CSI add-on
// through the EKS API rather than through Kubernetes/Helm providers.

terraform {
  backend "s3" {}
}

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
# VPC INTERFACE ENDPOINTS FOR PRIVATE EKS / SSM / IRSA / IMAGE PULLS
###############################################################################

resource "aws_security_group" "vpce" {
  name        = "${var.cluster_name}-vpce-sg"
  description = "Security group for interface VPC endpoints"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "HTTPS from admin EC2 and worker nodes"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    security_groups = [
      aws_security_group.eks_admin.id,
      module.security.node_security_group_id
    ]
  }

  egress {
    description = "Allow endpoint traffic within the VPC"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-vpce-sg"
  })
}

resource "aws_vpc_endpoint" "ssm" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-ssm-vpce"
  })
}

resource "aws_vpc_endpoint" "ssmmessages" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-ssmmessages-vpce"
  })
}

resource "aws_vpc_endpoint" "ec2messages" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ec2messages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-ec2messages-vpce"
  })
}

resource "aws_vpc_endpoint" "ec2" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ec2"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-ec2-vpce"
  })
}

resource "aws_vpc_endpoint" "eks" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.eks"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-eks-vpce"
  })
}

resource "aws_vpc_endpoint" "sts" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-sts-vpce"
  })
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-ecr-api-vpce"
  })
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-ecr-dkr-vpce"
  })
}

resource "aws_vpc_endpoint" "autoscaling" {
  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.autoscaling"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-autoscaling-vpce"
  })
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-s3-gateway-endpoint"
  })
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
    module.eks,
    module.iam_post_eks,
    aws_vpc_endpoint.ec2,
    aws_vpc_endpoint.ec2messages,
    aws_vpc_endpoint.ecr_api,
    aws_vpc_endpoint.ecr_dkr,
    aws_vpc_endpoint.eks,
    aws_vpc_endpoint.s3,
    aws_vpc_endpoint.ssm,
    aws_vpc_endpoint.ssmmessages,
    aws_vpc_endpoint.sts
  ]

  tags = var.tags
}