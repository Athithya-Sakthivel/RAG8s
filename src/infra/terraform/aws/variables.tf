// src/infra/terraform/aws/variables.tf
// Root-level contract for the E2E RAG platform.
// Finalized for OpenTofu/Terraform 1.x compatibility.

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
    "10.0.48.0/20"
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
    "10.0.1.0/24"
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

###############################################################################
# NODE GROUPS
###############################################################################

variable "system_nodegroup" {
  description = "Sizing for the system node group."
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

variable "workloads_nodegroup" {
  description = "Sizing for the workloads node group."
  type = object({
    instance_type = string
    min_size      = number
    desired_size  = number
    max_size      = number
  })

  default = {
    instance_type = "m7i-flex.large"
    min_size      = 2
    desired_size  = 2
    max_size      = 6
  }
}

variable "system_node_labels" {
  description = "Labels applied to system nodes."
  type        = map(string)

  default = {
    node-type = "general"
  }
}

variable "workloads_node_labels" {
  description = "Labels applied to workload nodes."
  type        = map(string)

  default = {
    node-type = "compute"
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

variable "workloads_node_taints" {
  description = "Taints applied to workloads nodes."
  type = list(object({
    key    = string
    value  = string
    effect = string
  }))

  default = [
    {
      key    = "node-type"
      value  = "compute"
      effect = "NO_SCHEDULE"
    }
  ]

  validation {
    condition = alltrue([
      for t in var.workloads_node_taints :
      contains(
        ["NO_SCHEDULE", "NO_EXECUTE", "PREFER_NO_SCHEDULE"],
        t.effect
      )
    ])
    error_message = "workloads_node_taints.effect must be NO_SCHEDULE, NO_EXECUTE, or PREFER_NO_SCHEDULE."
  }
}

###############################################################################
# AUTOSCALER
###############################################################################

variable "cluster_autoscaler" {
  description = "Cluster Autoscaler feature configuration."
  type = object({
    enabled                    = bool
    scan_interval_seconds      = number
    max_node_provision_time    = number
    expander                   = string
    balance_similar_nodegroups = bool
  })

  default = {
    enabled                    = true
    scan_interval_seconds      = 10
    max_node_provision_time    = 600
    expander                   = "least-waste"
    balance_similar_nodegroups = true
  }

  validation {
    condition = (
      var.cluster_autoscaler.scan_interval_seconds > 0 &&
      var.cluster_autoscaler.max_node_provision_time > 0 &&
      contains(
        ["least-waste", "most-pods", "random"],
        var.cluster_autoscaler.expander
      )
    )
    error_message = "cluster_autoscaler must use positive timing values and a valid expander."
  }
}

###############################################################################
# S3
###############################################################################

variable "s3_buckets" {
  description = "Managed S3 buckets."
  type = map(object({
    name          = string
    versioning    = bool
    force_destroy = bool
  }))

  default = {
    DATA_S3_BUCKET = {
      name          = "rag-prod-data"
      versioning    = true
      force_destroy = false
    }

    QDRANT_BACKUPS_BUCKET = {
      name          = "rag-prod-qdrant-backups"
      versioning    = true
      force_destroy = false
    }
  }
}

###############################################################################
# IRSA
###############################################################################

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
        }
      ]
    }

    frontend = {
      namespace       = "inference"
      service_account = "frontend"
      buckets = [
        {
          key    = "DATA_S3_BUCKET"
          access = "read"
        }
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

###############################################################################
# GITHUB ACTIONS OIDC ROLES
###############################################################################

variable "github_actions_roles" {
  description = "GitHub Actions federated IAM roles."
  type = map(object({
    repository = string
    branch     = string
    role_name  = string
    ecr_repo   = string
  }))

  default = {
    frontend = {
      repository = "Athithya-Sakthivel/E2E-RAG-System"
      branch     = "main"
      role_name  = "gh-actions-frontend"
      ecr_repo   = "frontend"
    }

    retriever = {
      repository = "Athithya-Sakthivel/E2E-RAG-System"
      branch     = "main"
      role_name  = "gh-actions-retriever"
      ecr_repo   = "retriever"
    }

    dense_model = {
      repository = "Athithya-Sakthivel/E2E-RAG-System"
      branch     = "main"
      role_name  = "gh-actions-dense-model"
      ecr_repo   = "dense-model"
    }

    sparse_model = {
      repository = "Athithya-Sakthivel/E2E-RAG-System"
      branch     = "main"
      role_name  = "gh-actions-sparse-model"
      ecr_repo   = "sparse-model"
    }

    reranker = {
      repository = "Athithya-Sakthivel/E2E-RAG-System"
      branch     = "main"
      role_name  = "gh-actions-reranker"
      ecr_repo   = "reranker"
    }

    indexer = {
      repository = "Athithya-Sakthivel/E2E-RAG-System"
      branch     = "main"
      role_name  = "gh-actions-indexer"
      ecr_repo   = "indexer"
    }
  }

  validation {
    condition = alltrue([
      for v in var.github_actions_roles :
      strcontains(v.repository, "/") &&
      can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", v.repository)) &&
      length(trimspace(v.branch)) > 0 &&
      v.branch == "main" &&
      length(trimspace(v.role_name)) > 0 &&
      can(regex("^gh-actions-[a-z0-9-]+$", v.role_name)) &&
      length(trimspace(v.ecr_repo)) > 0 &&
      can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", v.ecr_repo))
    ])
    error_message = "github_actions_roles entries must define repository in owner/repo format, branch 'main', a gh-actions-* role_name, and a lowercase hyphenated ecr_repo."
  }
}

###############################################################################
# ECR
###############################################################################

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
      v.retain_last_images > 0 &&
      contains(["IMMUTABLE"], upper(v.image_tag_mutability)) &&
      contains(["AES256"], upper(v.encryption_type)) &&
      can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", v.name))
    ])
    error_message = "Each ecr_repositories entry must define a lowercase hyphenated name, immutable tags, AES256 encryption, and retain_last_images > 0."
  }
}

###############################################################################
# OPTIONAL LAUNCH TEMPLATE INPUTS
###############################################################################

variable "launch_template_id" {
  description = "Optional EC2 Launch Template ID."
  type        = string
  default     = ""
}

variable "launch_template_version" {
  description = "Optional Launch Template version."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags for all resources."
  type        = map(string)
  default     = {}
}