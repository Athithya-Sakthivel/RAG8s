// src/terraform/aws/modules/security/main.tf
// Security module for the MLOps platform.
//
// Responsibilities:
// - create the worker-node security group
// - allow node-to-node and intra-VPC traffic
// - allow required outbound internet access through NAT
// - stay intentionally free of endpoint SGs and control-plane rules
//
// The EKS module owns the control-plane <-> node SG rule.

variable "vpc_id" {
  description = "VPC ID where the node security group will be created."
  type        = string
}

variable "vpc_cidr" {
  description = "Primary IPv4 CIDR block for the VPC."
  type        = string
}

variable "name_prefix" {
  description = "Prefix used for security group names."
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

  merged_tags = merge(
    {
      Name        = "${var.name_prefix}-nodes-sg"
      Environment = local.env_tag
      ManagedBy   = "mlops-platform-terraform"
    },
    var.tags
  )
}

# trivy:ignore:AWS-0104
resource "aws_security_group" "node" {
  name        = "${var.name_prefix}-nodes-sg"
  description = "Worker node security group for the MLOps platform."
  vpc_id      = var.vpc_id

  ingress {
    description = "Allow all traffic within the VPC CIDR"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow outbound traffic to the internet via NAT"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
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