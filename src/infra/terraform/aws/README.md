This repository defines AWS infrastructure using **OpenTofu (Terraform-compatible)**. It provisions a VPC, security boundaries, IAM, S3 storage, and a production-ready **EKS cluster** with strict workload isolation.

Ingress is expected to be handled externally (Cloudflare). This layer only provisions the underlying platform.

---

# What this infrastructure provisions

Single AWS account environment with:

* VPC with configurable CIDR
* Multi-AZ private networking
* NAT gateway(s) for outbound access
* EKS cluster with two isolated nodegroups
* IAM roles (pre- and post-cluster)
* S3 buckets for platform storage
* ECR repositories
* Security groups for cluster nodes
* Remote state (S3 + DynamoDB via `run.sh`)

All resources are environment-scoped via `.tfvars`.

---

# Architecture

## Networking (VPC)

The VPC module creates:

* VPC with configurable CIDR
* Public + private subnets across `az_count`
* Internet Gateway
* NAT configuration:

  * **one NAT per AZ** (default)
  * or **single NAT** (cost mode)
* Route tables:

  * public → IGW
  * private → NAT

Worker nodes run **only in private subnets**.

VPC endpoints were removed.

---

## EKS Cluster

The EKS module provisions:

* EKS control plane
* OIDC provider (required for IRSA)
* Managed nodegroups
* Cluster security group
* Secrets encryption (KMS)

### Nodegroups (strict contract)

Two nodegroups exist:

### 1. `system` (general)

Purpose: long-running services

Runs:

* Flyte control plane
* Postgres (CNPG)
* MLflow
* Iceberg REST
* Operators (Ray, Spark, etc.)

Characteristics:

* On-demand instances
* Stable capacity
* No batch workloads

Labels:

```
node-type=general
```

Taint:

```
node-type=general:NoSchedule
```

---

### 2. `workloads` (compute)

Purpose: execution layer

Runs:

* Flyte tasks (ELT, training)
* Spark jobs
* Ray workers
* batch jobs

Characteristics:

* autoscaling enabled
* spot allowed
* no long-running services

Labels:

```
node-type=compute
```

Taint:

```
node-type=compute:NoSchedule
```

---

## Scheduling Model (enforced by workloads, not Terraform)

* Services → must target `general`
* Jobs/workers → must target `compute`

Terraform only sets labels/taints.
Kubernetes manifests enforce placement.

---

## Security

The security module creates:

* Node security group
* Required ingress/egress for:

  * control plane ↔ nodes
  * node ↔ node
  * outbound internet

No permissive 0.0.0.0/0 ingress is allowed.

---

## IAM Design

Split into two phases:

---

### `iam_pre_eks`

Created before cluster:

* EKS cluster role
* Nodegroup role
* Cluster Autoscaler policy
* EBS CSI managed policy reference

These are required to create the cluster.

---

### `iam_post_eks`

Created after cluster:

Uses:

* OIDC provider from EKS

Creates:

### IRSA roles for workloads

Each role is mapped to:

* Kubernetes service account
* specific S3 bucket access

Roles include:

* CNPG backup role → `PG_BACKUPS_S3_BUCKET`
* Flyte task role → `S3_BUCKET`
* MLflow role → `MLFLOW_S3_BUCKET`
* Ray inference role → `MLFLOW_S3_BUCKET`
* Iceberg role → `S3_BUCKET`

Each role has:

* least-privilege S3 policy
* OIDC trust bound to namespace + service account

---

### GitHub Actions roles

Also created here:

* `flyte-elt-task`
* `flyte-train-task`
* `tabular-inference-service`

Used via OIDC (no static credentials).

Permissions:

* ECR push/pull (if ECR used)
* optional AWS access if required

---

## S3

The S3 module creates exactly three buckets:

* `S3_BUCKET` → general platform data (Flyte, Iceberg)
* `PG_BACKUPS_S3_BUCKET` → Postgres backups
* `MLFLOW_S3_BUCKET` → MLflow artifacts

Each bucket:

* private
* versioned
* encrypted
* no public access

Names are provided via `.tfvars`.

---

## ECR

The ECR module creates repositories dynamically.

Used only if AWS ECR is required alongside GHCR.

Repositories:

* flyte-elt-task
* flyte-train-task
* tabular-inference-service

Each repo:

* immutable tags (default)
* scan on push
* lifecycle policy (retain N images)

---

# Repository structure

```
src/terraform/aws/
  main.tf
  outputs.tf
  variables.tf
  providers.tf
  versions.tf

  prod.tfvars
  staging.tfvars

  run.sh

  modules/
    vpc/
    security/
    iam_pre_eks/
    eks/
    s3/
    iam_post_eks/
    ecr/
```

---

# Module responsibilities

## `vpc/`

Creates all networking:

* VPC
* subnets
* IGW
* NAT
* route tables

---

## `security/`

Creates:

* node security group
* required rules for EKS communication

---

## `iam_pre_eks/`

Creates:

* cluster role
* node role
* autoscaler policy

---

## `eks/`

Creates:

* EKS cluster
* nodegroups (`system`, `workloads`)
* OIDC provider
* KMS encryption

---

## `s3/`

Creates:

* 3 buckets (tfvars-driven)

Exports:

* name map
* arn map

---

## `iam_post_eks/`

Creates:

* IRSA roles (S3-scoped)
* GitHub OIDC roles

Depends on:

* EKS OIDC
* S3 buckets

---

## `ecr/`

Creates:

* repositories (dynamic)
* lifecycle policies

---

# Outputs

Key outputs exposed:

Networking:

* `vpc_id`
* `private_subnet_ids`
* `public_subnet_ids`
* `availability_zones`

EKS:

* `eks_cluster_name`
* `eks_cluster_endpoint`
* `eks_cluster_ca_data`
* `eks_oidc_provider_arn`

IAM:

* `iam_cluster_role_arn`
* `iam_node_role_arn`
* `irsa_role_arns`

S3:

* `s3_bucket_names`
* `s3_bucket_arns`

ECR:

* `ecr_repository_urls`

---

# Deployment

## 1. Bootstrap state

```
bash src/terraform/aws/run.sh --create --env staging
```

Creates:

* S3 state bucket
* DynamoDB lock table
* runs `tofu init`

---

## 2. Validate

```
tofu validate
```

---

## 3. Plan

```
tofu plan -var-file=src/terraform/aws/staging.tfvars
```

---

## 4. Apply

```
tofu apply -var-file=src/terraform/aws/staging.tfvars
```

---

# Configuration

Environment-specific config:

```
staging.tfvars
prod.tfvars
```

Defines:

* region
* cluster name
* VPC CIDR
* subnet CIDRs
* nodegroup sizes/types
* S3 bucket names
* IRSA role mappings

No secrets are stored here.

---

# State management

Handled by `run.sh`:

* S3 bucket (versioned, encrypted)
* DynamoDB locking

No local state is used.

---

# Invariants

These must not be violated:

* only 2 nodegroups: `system`, `workloads`
* node isolation via labels + taints
* no workloads in public subnets
* no static AWS credentials inside cluster
* all AWS access via IRSA or OIDC
* S3 access is least-privilege per service
* modules communicate only via outputs

---

This document reflects the current infrastructure exactly as defined in the repository.
