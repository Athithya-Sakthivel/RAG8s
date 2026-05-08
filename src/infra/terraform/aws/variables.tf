// src/terraform/aws/variables.tf
// Root-level contract for the MLOps platform.
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
  default     = "mlops-eks-prod"

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

  default = [
    {
      key    = "node-type"
      value  = "general"
      effect = "NO_SCHEDULE"
    }
  ]

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
    S3_BUCKET = {
      name          = "mlops-prod-data"
      versioning    = true
      force_destroy = false
    }

    PG_BACKUPS_S3_BUCKET = {
      name          = "mlops-prod-pg-backups"
      versioning    = true
      force_destroy = false
    }

    MLFLOW_S3_BUCKET = {
      name          = "mlops-prod-mlflow"
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
    bucket_key      = string
    access          = string
  }))

  default = {
    cnpg = {
      namespace       = "database"
      service_account = "cnpg-sa"
      bucket_key      = "PG_BACKUPS_S3_BUCKET"
      access          = "read_write"
    }

    flyte_task = {
      namespace       = "flyte"
      service_account = "flyte-task"
      bucket_key      = "S3_BUCKET"
      access          = "read_write"
    }

    iceberg = {
      namespace       = "iceberg"
      service_account = "iceberg-rest"
      bucket_key      = "S3_BUCKET"
      access          = "read_write"
    }

    ray_inference = {
      namespace       = "inference"
      service_account = "ray-inference-sa"
      bucket_key      = "MLFLOW_S3_BUCKET"
      access          = "read"
    }

    mlflow = {
      namespace       = "mlflow"
      service_account = "mlflow-sa"
      bucket_key      = "MLFLOW_S3_BUCKET"
      access          = "read_write"
    }
  }

  validation {
    condition = alltrue([
      for _, v in var.irsa_roles :
      length(trimspace(v.namespace)) > 0 &&
      length(trimspace(v.service_account)) > 0 &&
      length(trimspace(v.bucket_key)) > 0 &&
      contains(["read", "read_write"], v.access)
    ])
    error_message = "Each irsa_roles item must define namespace, service_account, bucket_key and access."
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
  }))

  default = {
    flyte_elt_task = {
      repository = "athithya-sakthivel/flyte-elt-task"
      branch     = "main"
      role_name  = "gh-actions-flyte-elt-task"
    }

    flyte_train_task = {
      repository = "athithya-sakthivel/flyte-train-task"
      branch     = "main"
      role_name  = "gh-actions-flyte-train-task"
    }

    tabular_inference_service = {
      repository = "athithya-sakthivel/mlsecops-tabular"
      branch     = "main"
      role_name  = "gh-actions-tabular-inference-service"
    }
  }

  validation {
    condition = alltrue([
      for _, v in var.github_actions_roles :
      length(trimspace(v.repository)) > 0 &&
      length(trimspace(v.branch)) > 0 &&
      length(trimspace(v.role_name)) > 0 &&
      can(regex("^[a-z0-9._-]+/[a-z0-9._-]+$", v.repository))
    ])
    error_message = "github_actions_roles entries must define lowercase repository as owner/repo, branch, and role_name."
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
    flyte_elt_task = {
      name = "flyte-elt-task"
    }

    flyte_train_task = {
      name = "flyte-train-task"
    }

    tabular_inference_service = {
      name = "tabular-inference-service"
    }
  }

  validation {
    condition = alltrue([
      for _, v in var.ecr_repositories :
      length(trimspace(v.name)) > 0 &&
      v.retain_last_images > 0 &&
      contains(["IMMUTABLE"], upper(v.image_tag_mutability)) &&
      contains(["AES256"], upper(v.encryption_type))
    ])
    error_message = "Each ecr_repositories entry must define name, immutable tags, AES256 encryption, and retain_last_images > 0."
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

###############################################################################
# TAGS
###############################################################################

variable "tags" {
  description = "Additional tags for all resources."
  type        = map(string)
  default     = {}
}