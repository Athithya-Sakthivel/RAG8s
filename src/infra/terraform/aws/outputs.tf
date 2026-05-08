// src/terraform/aws/outputs.tf
// Stable outputs for automation, kubeconfig generation, and downstream consumers.

output "vpc_id" {
  description = "VPC ID created by the VPC module."
  value       = module.vpc.vpc_id
}

output "availability_zones" {
  description = "Availability zones selected by the VPC module."
  value       = module.vpc.availability_zones
}

output "public_subnet_ids" {
  description = "Public subnet IDs, one per AZ."
  value       = module.vpc.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs, one per AZ."
  value       = module.vpc.private_subnet_ids
}

output "public_route_table_id" {
  description = "Shared public route table ID."
  value       = module.vpc.public_route_table_id
}

output "private_route_table_ids" {
  description = "Private route table IDs, one per AZ."
  value       = module.vpc.private_route_table_ids
}

output "nat_gateway_ids" {
  description = "NAT gateway IDs."
  value       = module.vpc.nat_gateway_ids
}

output "internet_gateway_id" {
  description = "Internet gateway ID."
  value       = module.vpc.internet_gateway_id
}

output "node_security_group_id" {
  description = "Worker node security group ID."
  value       = module.security.node_security_group_id
}

output "node_security_group_arn" {
  description = "Worker node security group ARN."
  value       = module.security.node_security_group_arn
}

output "node_security_group_name" {
  description = "Worker node security group name."
  value       = module.security.node_security_group_name
}

output "iam_cluster_role_arn" {
  description = "EKS control plane IAM role ARN."
  value       = module.iam_pre_eks.cluster_role_arn
}

output "iam_node_role_arn" {
  description = "EKS worker node IAM role ARN."
  value       = module.iam_pre_eks.node_role_arn
}

output "cluster_autoscaler_policy_arn" {
  description = "Cluster Autoscaler IAM policy ARN."
  value       = module.iam_pre_eks.cluster_autoscaler_policy_arn
}

output "ebs_csi_managed_policy_arn" {
  description = "AWS-managed EBS CSI driver policy ARN."
  value       = module.iam_pre_eks.ebs_csi_managed_policy_arn
}

output "eks_cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "EKS cluster API endpoint."
  value       = module.eks.cluster_endpoint
}

output "eks_cluster_ca_data" {
  description = "Base64-encoded cluster CA data."
  value       = module.eks.cluster_ca_data
}

output "eks_cluster_security_group_id" {
  description = "EKS control-plane security group ID."
  value       = module.eks.cluster_security_group_id
}

output "eks_oidc_provider_arn" {
  description = "EKS OIDC provider ARN."
  value       = module.eks.oidc_provider_arn
}

output "eks_oidc_provider_issuer" {
  description = "EKS OIDC issuer without the https:// prefix."
  value       = module.eks.oidc_provider_issuer
}

output "eks_secrets_encryption_kms_key_arn" {
  description = "KMS key ARN used for EKS secrets encryption."
  value       = module.eks.secrets_encryption_kms_key_arn
}

output "kubeconfig_data" {
  description = "Cluster connection data for downstream automation."
  value = {
    cluster_name = module.eks.cluster_name
    endpoint     = module.eks.cluster_endpoint
    ca_data      = module.eks.cluster_ca_data
  }
}

output "s3_bucket_names" {
  description = "Logical bucket key -> bucket name."
  value       = module.s3.bucket_name_map
}

output "s3_bucket_arns" {
  description = "Logical bucket key -> bucket ARN."
  value       = module.s3.bucket_arn_map
}

output "s3_bucket_ids" {
  description = "Logical bucket key -> bucket ID."
  value       = module.s3.bucket_id_map
}

output "irsa_role_arns" {
  description = "Logical IRSA role key -> IAM role ARN."
  value       = module.iam_post_eks.irsa_role_arns
}

output "irsa_role_names" {
  description = "Logical IRSA role key -> IAM role name."
  value       = module.iam_post_eks.irsa_role_names
}

output "irsa_policy_arns" {
  description = "Logical IRSA role key -> IAM policy ARN."
  value       = module.iam_post_eks.irsa_policy_arns
}

output "github_actions_role_arns" {
  description = "Logical GitHub Actions role key -> IAM role ARN."
  value       = module.iam_post_eks.github_actions_role_arns
}

output "github_actions_role_names" {
  description = "Logical GitHub Actions role key -> IAM role name."
  value       = module.iam_post_eks.github_actions_role_names
}

output "github_actions_policy_arns" {
  description = "Logical GitHub Actions role key -> IAM policy ARN."
  value       = module.iam_post_eks.github_actions_policy_arns
}

output "ecr_repository_urls" {
  description = "Logical repository key -> ECR repository URL."
  value       = module.ecr.repository_url_map
}

output "ecr_repository_arns" {
  description = "Logical repository key -> ECR repository ARN."
  value       = module.ecr.repository_arn_map
}

output "ecr_repository_names" {
  description = "Logical repository key -> ECR repository name."
  value       = module.ecr.repository_name_map
}