# src/terraform/aws/staging.tfvars
environment  = "staging"
region       = "ap-south-1"
cluster_name = "mlsecops-eks-staging"

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

system_nodegroup = {
  instance_type = "t3.small"
  min_size      = 2
  desired_size  = 2
  max_size      = 2
}

workloads_nodegroup = {
  instance_type = "m7i-flex.large"
  min_size      = 2
  desired_size  = 2
  max_size      = 4
}

system_node_taints = [
  {
    key    = "node-type"
    value  = "general"
    effect = "NO_SCHEDULE"
  }
]

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
  S3_BUCKET = {
    name          = "mlops-staging-data-681802563986"
    versioning    = true
    force_destroy = false
  }

  PG_BACKUPS_S3_BUCKET = {
    name          = "mlops-staging-pg-backups-681802563986"
    versioning    = true
    force_destroy = false
  }

  MLFLOW_S3_BUCKET = {
    name          = "mlops-staging-mlflow-681802563986"
    versioning    = true
    force_destroy = false
  }
}

irsa_roles = {
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

github_actions_roles = {
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

ecr_repositories = {
  flyte_elt_task = {
    name                 = "flyte-elt-task"
    image_tag_mutability = "IMMUTABLE"
    scan_on_push         = true
    encryption_type      = "AES256"
    retain_last_images   = 30
  }

  flyte_train_task = {
    name                 = "flyte-train-task"
    image_tag_mutability = "IMMUTABLE"
    scan_on_push         = true
    encryption_type      = "AES256"
    retain_last_images   = 30
  }

  tabular_inference_service = {
    name                 = "tabular-inference-service"
    image_tag_mutability = "IMMUTABLE"
    scan_on_push         = true
    encryption_type      = "AES256"
    retain_last_images   = 30
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
  Platform    = "mlsecops"
  Environment = "staging"
}