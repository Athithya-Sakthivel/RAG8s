// src/terraform/aws/main.tf
// Root composition for the MLOps platform.
// This file only wires module contracts together.

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
  name_prefix = "mlops"
  tags        = var.tags
}

module "ecr" {
  source = "./modules/ecr"

  repositories = var.ecr_repositories
  tags         = var.tags
}

module "iam_pre_eks" {
  source = "./modules/iam_pre_eks"

  name_prefix = "mlops"
  tags        = var.tags
}

module "s3" {
  source = "./modules/s3"

  buckets = var.s3_buckets
  tags    = var.tags
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
  system_node_taints    = var.system_node_taints
  workloads_node_taints = var.workloads_node_taints
  system_node_labels    = var.system_node_labels
  workloads_node_labels = var.workloads_node_labels

  launch_template_id      = var.launch_template_id
  launch_template_version = var.launch_template_version

  tags = var.tags

  depends_on = [
    module.vpc,
    module.security,
    module.iam_pre_eks
  ]
}

module "iam_post_eks" {
  source = "./modules/iam_post_eks"

  name_prefix = "mlops"
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