// src/infra/terraform/aws/variables.tf
// Root-level contract for the E2E RAG platform.

variable "region" {
  description = "AWS region where resources will be created."
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Logical environment name used for naming and tags."
  type        = string
  default     = "prod"

  validation {
    condition     = length(trimspace(var.environment)) > 0
    error_message = "environment must not be empty."
  }
}

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
  default     = "rag-eks-prod"

  validation {
    condition     = length(trimspace(var.cluster_name)) > 0
    error_message = "cluster_name must not be empty."
  }
}

variable "github_repository" {
  description = "GitHub repository in owner/repo format (injected into all GitHub Actions roles)."
  type        = string
  default     = "Athithya-Sakthivel/E2E-RAG-System"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "github_repository must be in owner/repo format."
  }
}

variable "system_nodegroup_replicas" {
  description = "Number of replicas for the system node group. Min, desired, and max will all be set to this value (no autoscaling)."
  type        = number
  default     = 2

  validation {
    condition     = var.system_nodegroup_replicas >= 1 && var.system_nodegroup_replicas <= 10
    error_message = "system_nodegroup_replicas must be between 1 and 10."
  }
}

variable "create_admin_ec2" {
  description = "Create the private admin EC2 host in this environment."
  type        = bool
  default     = false
}

variable "admin_instance_type" {
  description = "Instance type for the admin EC2 host."
  type        = string
  default     = "t3.micro"
}

variable "admin_ami_parameter" {
  description = "SSM parameter path for the Ubuntu 24.04 AMI."
  type        = string
  default     = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

variable "karpenter_chart_version" {
  description = "Karpenter Helm chart version."
  type        = string
  default     = "1.9.0"
}

variable "cluster_endpoint_public_access" {
  description = "Whether the EKS API server is publicly reachable."
  type        = bool
  default     = false
}

variable "cluster_endpoint_private_access" {
  description = "Whether the EKS API server is privately reachable inside the VPC."
  type        = bool
  default     = true
}

variable "cluster_endpoint_public_access_cidrs" {
  description = "CIDR blocks allowed to reach the public EKS endpoint."
  type        = list(string)
  default     = []

  validation {
    condition = (
      length(var.cluster_endpoint_public_access_cidrs) == 0 ||
      alltrue([for cidr in var.cluster_endpoint_public_access_cidrs : length(trimspace(cidr)) > 0])
    )
    error_message = "cluster_endpoint_public_access_cidrs must be empty or contain only non-empty CIDR strings."
  }
}

variable "vpc_cidr" {
  description = "Primary IPv4 CIDR for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of Availability Zones used by the VPC."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 4
    error_message = "az_count must be between 2 and 4."
  }
}

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs. Must match az_count."
  type        = list(string)
  default = [
    "10.0.32.0/20",
    "10.0.48.0/20",
  ]

  validation {
    condition     = length(var.private_subnet_cidrs) == var.az_count
    error_message = "private_subnet_cidrs must contain exactly az_count values."
  }
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDRs. Must match az_count."
  type        = list(string)
  default = [
    "10.0.0.0/24",
    "10.0.1.0/24",
  ]

  validation {
    condition     = length(var.public_subnet_cidrs) == var.az_count
    error_message = "public_subnet_cidrs must contain exactly az_count values."
  }
}

variable "enable_nat_per_az" {
  description = "Create one NAT Gateway per AZ."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Create a single shared NAT Gateway."
  type        = bool
  default     = false
}

variable "system_nodegroup" {
  description = "Base sizing for the system node group. Instance type is used; actual sizes are overridden by system_nodegroup_replicas."
  type = object({
    instance_type = string
    min_size      = number
    desired_size  = number
    max_size      = number
  })

  default = {
    instance_type = "t3.small"
    min_size      = 2
    desired_size  = 2
    max_size      = 3
  }
}

variable "system_node_labels" {
  description = "Labels applied to system nodes."
  type        = map(string)

  default = {
    node-type = "general"
  }
}

variable "system_node_taints" {
  description = "Taints applied to system nodes."
  type = list(object({
    key    = string
    value  = string
    effect = string
  }))

  default = []

  validation {
    condition = alltrue([
      for t in var.system_node_taints :
      contains(
        ["NO_SCHEDULE", "NO_EXECUTE", "PREFER_NO_SCHEDULE"],
        t.effect
      )
    ])
    error_message = "system_node_taints.effect must be NO_SCHEDULE, NO_EXECUTE, or PREFER_NO_SCHEDULE."
  }
}

variable "s3_buckets" {
  description = "Managed S3 buckets."
  type = map(object({
    name          = string
    versioning    = bool
    force_destroy = bool
  }))

  default = {
    DATA_S3_BUCKET = {
      name          = "rag-prod-data-681802563986"
      versioning    = true
      force_destroy = false
    }

    QDRANT_BACKUPS_BUCKET = {
      name          = "rag-prod-qdrant-backups-681802563986"
      versioning    = true
      force_destroy = false
    }
  }
}

variable "irsa_roles" {
  description = "IAM roles for Kubernetes service accounts."
  type = map(object({
    namespace       = string
    service_account = string
    buckets = optional(list(object({
      key    = string
      access = string
    })), [])
    aws_services = optional(object({
      bedrock = optional(bool, false)
    }), null)
  }))

  default = {
    indexer = {
      namespace       = "indexing"
      service_account = "indexer"
      buckets = [
        {
          key    = "DATA_S3_BUCKET"
          access = "read_write"
        },
        {
          key    = "QDRANT_BACKUPS_BUCKET"
          access = "read_write"
        },
      ]
    }

    frontend = {
      namespace       = "inference"
      service_account = "frontend"
      buckets = [
        {
          key    = "DATA_S3_BUCKET"
          access = "read"
        },
      ]
    }

    retriever = {
      namespace       = "inference"
      service_account = "retriever"
      aws_services = {
        bedrock = true
      }
    }
  }

  validation {
    condition = alltrue([
      for _, v in var.irsa_roles :
      length(trimspace(v.namespace)) > 0 &&
      length(trimspace(v.service_account)) > 0 &&
      (
        try(v.aws_services.bedrock, false) == true ||
        length(try(v.buckets, [])) >= 1
      )
    ])
    error_message = "Each irsa_roles item must define namespace and service_account, and must define either buckets or aws_services."
  }
}

variable "github_actions_roles" {
  description = "GitHub Actions federated IAM roles. The repository field will be overridden at runtime by var.github_repository."
  type = map(object({
    repository = string
    branch     = string
    role_name  = string
    ecr_repo   = string
  }))

  default = {
    frontend = {
      repository = "placeholder"   # replaced by local
      branch     = "main"
      role_name  = "gh-actions-frontend"
      ecr_repo   = "frontend"
    }

    retriever = {
      repository = "placeholder"
      branch     = "main"
      role_name  = "gh-actions-retriever"
      ecr_repo   = "retriever"
    }

    dense_model = {
      repository = "placeholder"
      branch     = "main"
      role_name  = "gh-actions-dense-model"
      ecr_repo   = "dense-model"
    }

    sparse_model = {
      repository = "placeholder"
      branch     = "main"
      role_name  = "gh-actions-sparse-model"
      ecr_repo   = "sparse-model"
    }

    reranker = {
      repository = "placeholder"
      branch     = "main"
      role_name  = "gh-actions-reranker"
      ecr_repo   = "reranker"
    }

    indexer = {
      repository = "placeholder"
      branch     = "main"
      role_name  = "gh-actions-indexer"
      ecr_repo   = "indexer"
    }
  }

  validation {
    condition = alltrue([
      for v in var.github_actions_roles :
      length(trimspace(v.branch)) > 0 &&
      v.branch == "main" &&
      length(trimspace(v.role_name)) > 0 &&
      can(regex("^gh-actions-[a-z0-9-]+$", v.role_name)) &&
      length(trimspace(v.ecr_repo)) > 0 &&
      can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", v.ecr_repo))
    ])
    error_message = "github_actions_roles entries must define branch 'main', a gh-actions-* role_name, and a lowercase hyphenated ecr_repo."
  }
}

variable "ecr_repositories" {
  description = "ECR repositories."
  type = map(object({
    name                 = string
    image_tag_mutability = optional(string, "IMMUTABLE")
    scan_on_push         = optional(bool, true)
    encryption_type      = optional(string, "AES256")
    retain_last_images   = optional(number, 30)
  }))

  default = {
    frontend = {
      name = "frontend"
    }

    retriever = {
      name = "retriever"
    }

    dense_model = {
      name = "dense-model"
    }

    sparse_model = {
      name = "sparse-model"
    }

    reranker = {
      name = "reranker"
    }

    indexer = {
      name = "indexer"
    }
  }

  validation {
    condition = alltrue([
      for _, v in var.ecr_repositories :
      length(trimspace(v.name)) > 0 &&
      try(v.retain_last_images, 30) > 0 &&
      try(upper(v.image_tag_mutability), "IMMUTABLE") == "IMMUTABLE" &&
      try(upper(v.encryption_type), "AES256") == "AES256" &&
      can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", v.name))
    ])
    error_message = "Each ecr_repositories entry must define a lowercase hyphenated name, immutable tags, AES256 encryption, and retain_last_images > 0."
  }
}

variable "tags" {
  description = "Additional tags for all resources."
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------------------
# Locals – combine static & dynamic values for final usage
# ------------------------------------------------------------------------------
locals {
  # Effective system node group: use the replicas value for all sizes
  effective_system_nodegroup = {
    instance_type = var.system_nodegroup.instance_type
    min_size      = var.system_nodegroup_replicas
    desired_size  = var.system_nodegroup_replicas
    max_size      = var.system_nodegroup_replicas
  }

  # GitHub Actions roles with the dynamic repository injected
  effective_github_actions_roles = {
    for k, v in var.github_actions_roles : k => merge(v, {
      repository = var.github_repository
    })
  }
}