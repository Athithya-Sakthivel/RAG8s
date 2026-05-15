// src/infra/terraform/aws/main.tf
// Root composition for the E2E RAG platform.
//
// Architecture:
// - Terraform/OpenTofu owns AWS infrastructure.
// - ArgoCD/GitOps owns Kubernetes applications.
// - Karpenter controller + NodePool manifests are deployed by GitOps.
// - Terraform only creates IAM + access primitives required by Karpenter.
//
// Important invariants:
// - Never taint or recreate the system nodegroup.
// - System nodegroup remains stable baseline capacity.
// - Karpenter handles burst/stateless workloads separately.
// - No kubectl/helm/local-exec inside Terraform.
// - No null provider.
// - No terraform_data provisioners.

terraform {
  backend "s3" {}
}

data "aws_caller_identity" "current" {}

###############################################################################
# NETWORK
###############################################################################

module "vpc" {
  source = "./modules/vpc"

  cluster_name         = var.cluster_name
  vpc_cidr             = var.vpc_cidr
  az_count             = var.az_count
  private_subnet_cidrs = var.private_subnet_cidrs
  public_subnet_cidrs  = var.public_subnet_cidrs

  enable_nat_per_az  = var.enable_nat_per_az
  single_nat_gateway = var.single_nat_gateway

  tags = var.tags
}

###############################################################################
# SECURITY GROUPS
###############################################################################

module "security" {
  source = "./modules/security"

  vpc_id      = module.vpc.vpc_id
  vpc_cidr    = var.vpc_cidr
  name_prefix = "rag"

  tags = var.tags
}

###############################################################################
# ECR
###############################################################################

module "ecr" {
  source = "./modules/ecr"

  repositories = var.ecr_repositories
  tags         = var.tags
}

###############################################################################
# IAM BEFORE EKS
###############################################################################

module "iam_pre_eks" {
  source = "./modules/iam_pre_eks"

  name_prefix = "rag"
  tags        = var.tags
}

###############################################################################
# EKS
###############################################################################

module "eks" {
  source = "./modules/eks"

  cluster_name = var.cluster_name
  region       = var.region

  vpc_id                 = module.vpc.vpc_id
  subnet_ids             = module.vpc.private_subnet_ids
  node_security_group_id = module.security.node_security_group_id

  cluster_role_arn = module.iam_pre_eks.cluster_role_arn
  node_role_arn    = module.iam_pre_eks.node_role_arn

  cluster_endpoint_public_access       = var.cluster_endpoint_public_access
  cluster_endpoint_private_access      = var.cluster_endpoint_private_access
  cluster_endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs

  system_nodegroup   = var.system_nodegroup
  system_node_taints = var.system_node_taints
  system_node_labels = var.system_node_labels

  enabled_cluster_log_types = []

  tags = var.tags

  depends_on = [
    module.vpc,
    module.security,
    module.iam_pre_eks,
  ]
}

###############################################################################
# S3
###############################################################################

module "s3" {
  source = "./modules/s3"

  buckets = var.s3_buckets
  tags    = var.tags
}

###############################################################################
# IAM AFTER EKS (IRSA / GITHUB ACTIONS / CSI)
###############################################################################

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
    module.s3,
  ]
}

###############################################################################
# KARPENTER IAM ONLY
#
# Controller install + NodePool manifests are GitOps-managed via ArgoCD.
###############################################################################

module "karpenter" {
  source = "./modules/karpenter"

  cluster_name         = var.cluster_name
  region               = var.region
  tags                 = var.tags
  oidc_provider_arn    = module.eks.oidc_provider_arn
  oidc_provider_issuer = module.eks.oidc_provider_issuer

  depends_on = [
    module.eks,
  ]
}

###############################################################################
# PRIVATE ADMIN EC2 FOR PRODUCTION ONLY
###############################################################################

data "aws_ssm_parameter" "ubuntu_2404_ami" {
  count = var.create_admin_ec2 ? 1 : 0

  name = var.admin_ami_parameter
}

resource "aws_security_group" "eks_admin" {
  count = var.create_admin_ec2 ? 1 : 0

  name        = "${var.cluster_name}-eks-admin-sg"
  description = "Private SSM-only admin host for EKS access"
  vpc_id      = module.vpc.vpc_id

  #tfsec:ignore:aws-ec2-no-public-egress-sgr
  #trivy:ignore:AVD-AWS-0104
  egress {
    description = "Allow outbound HTTPS"

    from_port = 443
    to_port   = 443
    protocol  = "tcp"

    cidr_blocks = ["0.0.0.0/0"]
  }

  #tfsec:ignore:aws-ec2-no-public-egress-sgr
  #trivy:ignore:AVD-AWS-0104
  egress {
    description = "Allow outbound DNS UDP"

    from_port = 53
    to_port   = 53
    protocol  = "udp"

    cidr_blocks = ["0.0.0.0/0"]
  }

  #tfsec:ignore:aws-ec2-no-public-egress-sgr
  #trivy:ignore:AVD-AWS-0104
  egress {
    description = "Allow outbound DNS TCP"

    from_port = 53
    to_port   = 53
    protocol  = "tcp"

    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name    = "${var.cluster_name}-eks-admin-sg"
    Purpose = "eks-admin"
  })
}

resource "aws_iam_role" "eks_admin" {
  count = var.create_admin_ec2 ? 1 : 0

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
  count = var.create_admin_ec2 ? 1 : 0

  role       = aws_iam_role.eks_admin[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "eks_admin_describe_cluster" {
  count = var.create_admin_ec2 ? 1 : 0

  name = "${var.cluster_name}-eks-admin-describe-cluster"
  role = aws_iam_role.eks_admin[0].id

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
  count = var.create_admin_ec2 ? 1 : 0

  name = "${var.cluster_name}-eks-admin-profile"
  role = aws_iam_role.eks_admin[0].name
}

resource "aws_instance" "eks_admin" {
  count = var.create_admin_ec2 ? 1 : 0

  ami                    = data.aws_ssm_parameter.ubuntu_2404_ami[0].value
  instance_type          = var.admin_instance_type
  subnet_id              = module.vpc.private_subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.eks_admin[0].id]

  iam_instance_profile = aws_iam_instance_profile.eks_admin[0].name

  associate_public_ip_address = false
  user_data_replace_on_change = true

  user_data = <<-EOF
    #!/usr/bin/env bash
    set -euxo pipefail

    exec > >(tee /var/log/eks-admin-bootstrap.log) 2>&1

    export DEBIAN_FRONTEND=noninteractive

    apt-get update
    apt-get install -y ca-certificates curl git jq unzip awscli

    KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"

    curl -fsSLo /tmp/kubectl \
      "https://dl.k8s.io/release/$${KUBECTL_VERSION}/bin/linux/amd64/kubectl"

    install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl

    rm -f /tmp/kubectl

    cat >/usr/local/bin/eks-admin-connect <<'EOS'
    #!/usr/bin/env bash
    set -euo pipefail

    aws eks update-kubeconfig \
      --region ${var.region} \
      --name ${var.cluster_name}

    kubectl get nodes -o wide
    kubectl get pods -A -o wide
    EOS

    chmod +x /usr/local/bin/eks-admin-connect
  EOF

  root_block_device {
    volume_type = "gp3"
    volume_size = 12

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
  count = var.create_admin_ec2 ? 1 : 0

  description = "Allow admin host to reach EKS API"

  type = "ingress"

  from_port = 443
  to_port   = 443
  protocol  = "tcp"

  security_group_id        = module.eks.cluster_security_group_id
  source_security_group_id = aws_security_group.eks_admin[0].id
}

###############################################################################
# EKS ACCESS ENTRY FOR ADMIN EC2
###############################################################################

resource "aws_eks_access_entry" "eks_admin" {
  count = var.create_admin_ec2 ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.eks_admin[0].arn

  type = "STANDARD"

  depends_on = [
    module.eks,
    aws_iam_role.eks_admin,
  ]
}

resource "aws_eks_access_policy_association" "eks_admin_cluster_admin" {
  count = var.create_admin_ec2 ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.eks_admin[0].arn

  policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [
    aws_eks_access_entry.eks_admin,
  ]
}

###############################################################################
# EBS CSI DRIVER
###############################################################################

resource "aws_eks_addon" "aws_ebs_csi_driver" {
  cluster_name = module.eks.cluster_name
  addon_name   = "aws-ebs-csi-driver"

  service_account_role_arn = module.iam_post_eks.ebs_csi_driver_role_arn

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [
    module.vpc,
    module.eks,
    module.iam_post_eks,
  ]

  tags = var.tags
}