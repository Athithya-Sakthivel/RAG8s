# src/infra/terraform/aws/prod.tfvars
environment  = "prod"
region       = "ap-south-1"
cluster_name = "rag-eks-prod"

create_admin_ec2 = true

cluster_endpoint_public_access       = false
cluster_endpoint_private_access      = true
cluster_endpoint_public_access_cidrs = []

vpc_cidr = "10.0.0.0/16"

az_count = 2

private_subnet_cidrs = [
  "10.0.32.0/20",
  "10.0.48.0/20",
]

public_subnet_cidrs = [
  "10.0.0.0/24",
  "10.0.1.0/24",
]

enable_nat_per_az  = true
single_nat_gateway = false

system_nodegroup = {
  instance_type = "t3.small"
  min_size      = 3
  desired_size  = 3
  max_size      = 3
}

workloads_nodegroup = {
  instance_type = "m7i-flex.large"
  min_size      = 2
  desired_size  = 2
  max_size      = 6
}

system_node_taints = []

workloads_node_taints = [
  {
    key    = "node-type"
    value  = "compute"
    effect = "NO_SCHEDULE"
  }
]

system_node_labels = {
  "node-type" = "general"
}

workloads_node_labels = {
  "node-type" = "compute"
}

s3_buckets = {
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

github_actions_roles = {
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

cluster_autoscaler = {
  enabled                    = true
  scan_interval_seconds      = 10
  max_node_provision_time    = 600
  expander                   = "least-waste"
  balance_similar_nodegroups = true
}

tags = {
  Platform    = "rag"
  Environment = "prod"
}