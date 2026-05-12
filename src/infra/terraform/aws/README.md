# E2E RAG Platform Infrastructure

This repository defines AWS infrastructure using **OpenTofu (Terraform-compatible)**. It provisions the network, security boundaries, IAM, storage, and a production-ready **EKS cluster** for an end-to-end RAG system.

Ingress is handled externally through the existing cloudflare setup. This layer provisions the underlying AWS platform only.

---

# What this infrastructure provisions

Single AWS account environment with:

- VPC with configurable CIDR
- Multi-AZ private networking
- NAT gateways for outbound access
- EKS cluster with two isolated nodegroups
- IAM roles for cluster bootstrap and post-cluster access
- 2 S3 buckets for platform data and Qdrant backups
- 6 ECR repositories for service images
- Security group for worker nodes
- Remote state via S3 + DynamoDB through `run.sh`

All resources are environment-scoped via `.tfvars`.

---

# Architecture

## Networking (VPC)

The VPC module creates:

- VPC with configurable CIDR
- Public and private subnets across `az_count`
- Internet Gateway
- NAT configuration:
  - one NAT per AZ by default
  - single NAT only as a compatibility escape hatch
- Route tables:
  - public subnets route to the Internet Gateway
  - private subnets route to NAT

Worker nodes run only in private subnets.

VPC endpoints are not used.

---

## EKS Cluster

The EKS module provisions:

- EKS control plane
- OIDC provider for IRSA
- Managed nodegroups
- Cluster security group
- Secrets encryption with KMS

### Nodegroups

The cluster has exactly two managed nodegroups.

---

### 1. `system` nodegroup

**Purpose:** stable, always-on platform services

**Runs:**

- ArgoCD
- Qdrant
- Valkey
- ClickHouse
- Prometheus
- Alertmanager
- Grafana
- CoreDNS
- metrics-server
- other stateful or control-plane-like services

**Characteristics:**

- on-demand only
- stable capacity
- no batch workloads
- no Spot capacity

**Labels:**

```yaml
node-type: general
````

**Taint:**

```yaml
node-type=general:NoSchedule
```

---

### 2. `workloads` nodegroup

**Purpose:** stateless inference and execution layer

**Runs:**

* frontend
* retriever
* dense model service
* sparse model service
* reranker
* indexing jobs and CronJobs
* cloudflared, if retained as a stateless edge component

**Characteristics:**

* autoscaling enabled
* Spot capacity allowed
* no stateful services
* interruptible by design

**Labels:**

```yaml
node-type: compute
```

**Taint:**

```yaml
node-type=compute:NoSchedule
```

---

## Scheduling Model

Terraform defines the scheduling contract through labels and taints. Kubernetes manifests must honor it.

* stateful services → `general`
* stateless services and jobs → `compute`
* Spot capacity is allowed only for stateless workloads

---

## Security

The security module creates:

* one worker-node security group
* ingress for traffic within the VPC CIDR
* egress to the internet through NAT

The security module does not own control-plane security-group rules. Those are managed by the EKS module.

No permissive public ingress is used.

---

## IAM Design

IAM is split into two phases.

---

### `iam_pre_eks`

Created before cluster provisioning:

* EKS cluster role
* EKS node role
* Cluster Autoscaler policy
* EBS CSI managed policy reference

These are required to create the cluster.

---

### `iam_post_eks`

Created after the EKS OIDC provider exists.

This module creates:

#### IRSA roles

These are mapped to Kubernetes service accounts and explicit AWS permissions.

Current IRSA roles:

* `indexer`

  * read/write `DATA_S3_BUCKET`
  * read/write `QDRANT_BACKUPS_BUCKET`

* `frontend`

  * read/write `DATA_S3_BUCKET`

* `retriever`

  * Bedrock invocation permissions only

Dense, sparse, and reranker services do not require AWS access. They load model weights from Hugging Face.

Each IRSA role uses least-privilege policies and exact namespace + service account trust.

---

#### GitHub Actions OIDC roles

These are used by workflows that push images to ECR.

Current roles:

* `gh-actions-frontend`
* `gh-actions-retriever`
* `gh-actions-dense-model`
* `gh-actions-sparse-model`
* `gh-actions-reranker`
* `gh-actions-indexer`

They use OIDC, not static credentials, and are scoped to the repository and branch.

---

## S3

The S3 module creates exactly two buckets:

* `DATA_S3_BUCKET` → platform data and uploads
* `QDRANT_BACKUPS_BUCKET` → Qdrant backups and snapshots

Each bucket is:

* private
* encrypted
* versioned
* protected from public access

Bucket names are provided via `.tfvars`.

---

## ECR

The ECR module creates repositories dynamically from root input.

Current repositories:

* `frontend`
* `retriever`
* `dense-model`
* `sparse-model`
* `reranker`
* `indexer`

Each repository uses:

* immutable tags
* scan-on-push
* AES256 encryption
* lifecycle policy for retention

---

# Repository structure

```text
src/infra/terraform/aws/
  main.tf
  outputs.tf
  variables.tf
  providers.tf
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

Creates:

* VPC
* public and private subnets
* Internet Gateway
* NAT gateways
* route tables

---

## `security/`

Creates:

* worker-node security group
* intra-VPC ingress
* outbound NAT egress

---

## `iam_pre_eks/`

Creates:

* EKS cluster role
* EKS node role
* Cluster Autoscaler policy
* EBS CSI policy reference

---

## `eks/`

Creates:

* EKS cluster
* `system` and `workloads` nodegroups
* OIDC provider
* KMS encryption for secrets
* control-plane security-group rule for nodes

---

## `s3/`

Creates:

* two buckets from tfvars

Exports:

* bucket name map
* bucket ARN map
* bucket ID map

---

## `iam_post_eks/`

Creates:

* IRSA roles for Kubernetes service accounts
* GitHub Actions OIDC roles for ECR access

Depends on:

* EKS OIDC provider
* S3 bucket maps

---

## `ecr/`

Creates:

* repositories from tfvars
* lifecycle policies

---

# Outputs

Key outputs exposed:

## Networking

* `vpc_id`
* `private_subnet_ids`
* `public_subnet_ids`
* `availability_zones`

## EKS

* `eks_cluster_name`
* `eks_cluster_endpoint`
* `eks_cluster_ca_data`
* `eks_oidc_provider_arn`

## IAM

* `iam_cluster_role_arn`
* `iam_node_role_arn`
* `irsa_role_arns`

## S3

* `s3_bucket_names`
* `s3_bucket_arns`

## ECR

* `ecr_repository_urls`

---

# Deployment

## 1. Bootstrap state

```bash
bash src/infra/terraform/aws/run.sh --create --env staging
```

Creates:

* S3 backend bucket
* DynamoDB lock table
* Terraform/OpenTofu backend initialization

---

## 2. Validate

```bash
tofu validate
```

---

## 3. Plan

```bash
tofu plan -var-file=src/infra/terraform/aws/staging.tfvars
```

---

## 4. Apply

```bash
tofu apply -var-file=src/infra/terraform/aws/staging.tfvars
```

---

# Configuration

Environment-specific config is stored in:

* `prod.tfvars`
* `staging.tfvars`

These define:

* region
* cluster name
* VPC CIDR
* subnet CIDRs
* nodegroup sizing
* S3 bucket names
* IRSA role mappings
* GitHub Actions role mappings
* ECR repository mappings

No secrets are stored here.

---

# State management

Handled by `run.sh`:

* S3 backend state bucket
* DynamoDB locking

No local state is used for the platform resources.

---

# Invariants

These must not be violated:

* only 2 nodegroups: `system`, `workloads`
* `system` is stable, on-demand, stateful
* `workloads` is stateless and Spot-eligible
* no workloads in public subnets
* no static AWS credentials inside the cluster
* AWS access is via IRSA or GitHub OIDC only
* S3 access is least-privilege and bucket-scoped
* modules communicate only through outputs
* ECR names match the CI contract exactly

