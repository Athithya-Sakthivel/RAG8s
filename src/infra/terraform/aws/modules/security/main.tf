// src/infra/terraform/aws/modules/security/main.tf
// Security module for the E2E RAG platform.
//
// Responsibilities:
// - create the worker-node security group
// - allow node-to-node and intra-VPC traffic
// - restrict outbound traffic to the VPC CIDR
// - stay intentionally free of endpoint SGs and control-plane rules
//
// The EKS module owns the control-plane <-> node SG rule.

locals {
  env_tag = lookup(var.tags, "Environment", "prod")

  merged_tags = merge(
    {
      Name        = "${var.name_prefix}-nodes-sg"
      Environment = local.env_tag
      ManagedBy   = "opentofu"
      Platform    = "rag"
    },
    var.tags
  )
}

# trivy:ignore:AWS-0104
resource "aws_security_group" "node" {
  name        = "${var.name_prefix}-nodes-sg"
  description = "Worker node security group for the RAG platform."
  vpc_id      = var.vpc_id

  ingress {
    description = "Allow all traffic within the VPC CIDR"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow outbound traffic within the VPC CIDR only"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = local.merged_tags
}

output "node_security_group_id" {
  description = "Security Group ID for worker nodes."
  value       = aws_security_group.node.id
}

output "node_security_group_arn" {
  description = "Security Group ARN for worker nodes."
  value       = aws_security_group.node.arn
}

output "node_security_group_name" {
  description = "Security Group name for worker nodes."
  value       = aws_security_group.node.name
}

# ============================================
# Karpenter discovery tag for node security group
# ============================================
resource "aws_ec2_tag" "karpenter_discovery" {
  resource_id = aws_security_group.node.id
  key         = "karpenter.sh/discovery"
  value       = var.cluster_name
}