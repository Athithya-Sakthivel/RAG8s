output "availability_zones" {
  description = "Selected Availability Zones."
  value       = module.vpc.availability_zones
}

output "aws_region" {
  description = "AWS region."
  value       = var.region
}

output "s3_bucket_name_map" {
  description = "Logical S3 bucket key -> bucket name."
  value       = module.s3.bucket_name_map
}

output "s3_bucket_arn_map" {
  description = "Logical S3 bucket key -> bucket ARN."
  value       = module.s3.bucket_arn_map
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "EKS cluster API endpoint."
  value       = module.eks.cluster_endpoint
}

output "eks_cluster_ca_data" {
  description = "Base64-encoded CA data for the cluster."
  value       = module.eks.cluster_ca_data
}

output "eks_cluster_security_group_id" {
  description = "EKS cluster security group ID."
  value       = module.eks.cluster_security_group_id
}

output "eks_oidc_provider_arn" {
  description = "EKS OIDC provider ARN."
  value       = module.eks.oidc_provider_arn
}

output "eks_oidc_provider_issuer" {
  description = "EKS OIDC issuer host/path without https:// prefix."
  value       = module.eks.oidc_provider_issuer
}

output "eks_secrets_encryption_kms_key_arn" {
  description = "KMS key ARN used for EKS secrets encryption."
  value       = module.eks.secrets_encryption_kms_key_arn
}

output "iam_cluster_role_arn" {
  description = "EKS control plane IAM role ARN."
  value       = module.iam_pre_eks.cluster_role_arn
}

output "iam_node_role_arn" {
  description = "EKS node IAM role ARN."
  value       = module.iam_pre_eks.node_role_arn
}

output "ebs_csi_driver_role_arn" {
  description = "EBS CSI driver IAM role ARN."
  value       = module.iam_post_eks.ebs_csi_driver_role_arn
}

output "github_actions_role_arns" {
  description = "GitHub Actions IAM role ARNs."
  value       = module.iam_post_eks.github_actions_role_arns
}

output "github_actions_role_names" {
  description = "GitHub Actions IAM role names."
  value       = module.iam_post_eks.github_actions_role_names
}

output "github_actions_policy_arns" {
  description = "GitHub Actions IAM policy ARNs."
  value       = module.iam_post_eks.github_actions_policy_arns
}

output "irsa_role_arns" {
  description = "IRSA role ARNs."
  value       = module.iam_post_eks.irsa_role_arns
}

output "irsa_role_names" {
  description = "IRSA role names."
  value       = module.iam_post_eks.irsa_role_names
}

output "irsa_policy_arns" {
  description = "IRSA policy ARNs."
  value       = module.iam_post_eks.irsa_policy_arns
}

output "karpenter_controller_role_arn" {
  description = "Karpenter controller IAM role ARN."
  value       = module.karpenter.controller_role_arn
}

output "karpenter_node_role_arn" {
  description = "Karpenter node IAM role ARN."
  value       = module.karpenter.node_role_arn
}

output "karpenter_node_pool_name" {
  description = "Karpenter NodePool name."
  value       = module.karpenter.node_pool_name
}

output "karpenter_node_class_name" {
  description = "Karpenter EC2NodeClass name."
  value       = module.karpenter.node_class_name
}

output "eks_admin_instance_id" {
  description = "Admin EC2 instance ID if enabled."
  value       = try(aws_instance.eks_admin[0].id, null)
}

output "eks_admin_private_ip" {
  description = "Admin EC2 private IP if enabled."
  value       = try(aws_instance.eks_admin[0].private_ip, null)
}

output "eks_admin_security_group_id" {
  description = "Admin EC2 security group ID if enabled."
  value       = try(aws_security_group.eks_admin[0].id, null)
}

output "eks_admin_kubeconfig_command" {
  description = "Command to configure kubeconfig."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "eks_admin_ssm_command" {
  description = "SSM start-session command for the admin EC2 if enabled."
  value       = var.create_admin_ec2 ? "aws ssm start-session --region ${var.region} --target ${aws_instance.eks_admin[0].id}" : null
}

output "eks_admin_quick_check_command" {
  description = "Quick kubectl check command."
  value       = <<EOT
aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}
kubectl get nodes -o wide
kubectl get pods -A -o wide
EOT
}