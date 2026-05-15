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
  default     = "rag"
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}

variable "cluster_name" {
  description = "EKS cluster name used for Karpenter discovery tag."
  type        = string
}