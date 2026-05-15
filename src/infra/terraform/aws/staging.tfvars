# src/infra/terraform/aws/staging.tfvars
# Staging environment overrides.
#
# The five primary variables (region, environment, cluster_name, github_repository,
# system_nodegroup_replicas) are NOT defined here – they are expected to be injected
# via TF_VAR_ environment variables at runtime (or via -var CLI flags).
#
# Example:
#   export TF_VAR_region="ap-south-1"
#   export TF_VAR_environment="staging"
#   export TF_VAR_cluster_name="rag-eks-staging"
#   export TF_VAR_github_repository="Athithya-Sakthivel/E2E-RAG-System"
#   export TF_VAR_system_nodegroup_replicas=4

# region, environment, cluster_name – intentionally omitted (use TF_VAR_)

create_admin_ec2 = false

cluster_endpoint_public_access       = true
cluster_endpoint_private_access      = true
cluster_endpoint_public_access_cidrs = ["183.82.177.77/32"]

vpc_cidr = "10.1.0.0/16"

az_count = 2

private_subnet_cidrs = [
  "10.1.32.0/20",
  "10.1.48.0/20",
]

public_subnet_cidrs = [
  "10.1.0.0/24",
  "10.1.1.0/24",
]

enable_nat_per_az  = true
single_nat_gateway = false

# system_nodegroup – removed. Node sizes come from TF_VAR_system_nodegroup_replicas.
# Instance type is picked from the variable's default (t3.small). Override with -var
# if you need a different instance type (e.g., -var='system_nodegroup={"instance_type":"t3.medium"}').

system_node_taints = []

system_node_labels = {
  "node-type" = "general"
}

s3_buckets = {
  DATA_S3_BUCKET = {
    name          = "rag-staging-data-681802563986"
    versioning    = true
    force_destroy = false
  }

  QDRANT_BACKUPS_BUCKET = {
    name          = "rag-staging-qdrant-backups-681802563986"
    versioning    = true
    force_destroy = false
  }
}

irsa_roles = {
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
        access = "read_write"
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

# github_actions_roles – fully removed. The dynamic repository will be injected
# automatically from TF_VAR_github_repository.

ecr_repositories = {
  frontend = {
    name               = "frontend"
    scan_on_push       = true
    encryption_type    = "AES256"
    retain_last_images = 30
  }

  retriever = {
    name               = "retriever"
    scan_on_push       = true
    encryption_type    = "AES256"
    retain_last_images = 30
  }

  dense_model = {
    name               = "dense-model"
    scan_on_push       = true
    encryption_type    = "AES256"
    retain_last_images = 30
  }

  sparse_model = {
    name               = "sparse-model"
    scan_on_push       = true
    encryption_type    = "AES256"
    retain_last_images = 30
  }

  reranker = {
    name               = "reranker"
    scan_on_push       = true
    encryption_type    = "AES256"
    retain_last_images = 30
  }

  indexer = {
    name               = "indexer"
    scan_on_push       = true
    encryption_type    = "AES256"
    retain_last_images = 30
  }
}

tags = {
  Platform    = "rag"
  Environment = "staging"
}