// src/terraform/aws/modules/vpc/main.tf
// Multi-AZ VPC module for the MLOps platform.
//
// Goals:
// - private subnets for worker nodes
// - public subnets only for NAT gateways
// - one NAT gateway per AZ by default
// - deterministic AZ selection
// - subnet tags required by EKS
// - no IPv6, no VPC endpoints, no public worker exposure

variable "cluster_name" {
  description = "EKS cluster name used for Kubernetes subnet discovery tags."
  type        = string
}

variable "vpc_cidr" {
  description = "Primary IPv4 CIDR for the VPC."
  type        = string
}

variable "az_count" {
  description = "Number of Availability Zones to use."
  type        = number

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 4
    error_message = "az_count must be between 2 and 4."
  }
}

variable "private_subnet_cidrs" {
  description = "IPv4 CIDRs for private subnets, one per AZ."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_cidrs) == var.az_count
    error_message = "private_subnet_cidrs must contain exactly az_count CIDRs."
  }
}

variable "public_subnet_cidrs" {
  description = "IPv4 CIDRs for public subnets used by NAT gateways, one per AZ."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_cidrs) == var.az_count
    error_message = "public_subnet_cidrs must contain exactly az_count CIDRs."
  }
}

variable "enable_nat_per_az" {
  description = "Create one NAT gateway per AZ when true."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Compatibility escape hatch. Keep false for production."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to all resources created by this module."
  type        = map(string)
  default     = {}
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  env_tag = lookup(var.tags, "Environment", "prod")

  common_tags = merge(
    {
      Name        = "mlops-vpc"
      Environment = local.env_tag
      ManagedBy   = "mlops-platform-terraform"
    },
    var.tags
  )

  use_nat_per_az = var.enable_nat_per_az && !var.single_nat_gateway
  nat_count      = local.use_nat_per_az ? var.az_count : 1

  eks_subnet_cluster_tag = {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = local.common_tags
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "mlops-igw"
  })
}

resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = var.public_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  map_public_ip_on_launch = false

  tags = merge(
    local.common_tags,
    local.eks_subnet_cluster_tag,
    {
      Name                     = "mlops-public-${local.azs[count.index]}"
      Tier                     = "public"
      AZ                       = local.azs[count.index]
      "kubernetes.io/role/elb" = "1"
    }
  )
}

resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  map_public_ip_on_launch = false

  tags = merge(
    local.common_tags,
    local.eks_subnet_cluster_tag,
    {
      Name                              = "mlops-private-${local.azs[count.index]}"
      Tier                              = "private"
      AZ                                = local.azs[count.index]
      "kubernetes.io/role/internal-elb" = "1"
    }
  )
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "mlops-public-rt"
    Tier = "public"
  })
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  count = local.nat_count

  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = local.use_nat_per_az ? "mlops-nat-eip-${local.azs[count.index]}" : "mlops-nat-eip-primary"
  })

  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  count = local.nat_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[local.use_nat_per_az ? count.index : 0].id

  tags = merge(local.common_tags, {
    Name = local.use_nat_per_az ? "mlops-natgw-${local.azs[count.index]}" : "mlops-natgw-primary"
  })

  depends_on = [aws_internet_gateway.this, aws_subnet.public]
}

resource "aws_route_table" "private" {
  count = var.az_count

  vpc_id = aws_vpc.this.id

  tags = merge(local.common_tags, {
    Name = "mlops-private-rt-${local.azs[count.index]}"
    Tier = "private"
    AZ   = local.azs[count.index]
  })
}

resource "aws_route" "private_default" {
  count = var.az_count

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[local.use_nat_per_az ? count.index : 0].id

  depends_on = [aws_nat_gateway.this]
}

resource "aws_route_table_association" "private" {
  count = var.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

output "vpc_id" {
  description = "VPC ID."
  value       = aws_vpc.this.id
}

output "availability_zones" {
  description = "Selected Availability Zones."
  value       = local.azs
}

output "public_subnet_ids" {
  description = "Public subnet IDs, one per AZ."
  value       = [for s in aws_subnet.public : s.id]
}

output "private_subnet_ids" {
  description = "Private subnet IDs, one per AZ."
  value       = [for s in aws_subnet.private : s.id]
}

output "public_route_table_id" {
  description = "Shared public route table ID."
  value       = aws_route_table.public.id
}

output "private_route_table_ids" {
  description = "Private route table IDs, one per AZ."
  value       = [for rt in aws_route_table.private : rt.id]
}

output "nat_gateway_ids" {
  description = "NAT gateway IDs."
  value       = [for nat in aws_nat_gateway.this : nat.id]
}

output "internet_gateway_id" {
  description = "Internet gateway ID."
  value       = aws_internet_gateway.this.id
}