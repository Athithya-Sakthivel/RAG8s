# E2E RAG Platform Infrastructure

This repository defines the AWS infrastructure for the end-to-end RAG platform using **OpenTofu**. It provisions the network, security boundaries, IAM, storage, container registries, and EKS cluster required to run the platform.

Ingress and application delivery are handled separately from this layer. This repository owns the AWS foundation only.

---

## What this infrastructure provisions

For each environment, this stack provides:

- A multi-AZ VPC
- Public and private subnets
- NAT gateways for outbound internet access
- Selective VPC endpoints for private AWS service access
- An EKS cluster with two managed nodegroups
- IAM roles for pre-cluster bootstrap and post-cluster access
- S3 buckets for platform data and Qdrant backups
- ECR repositories for service images
- Security groups for worker nodes and optional admin EC2 access
- Remote state via S3 + DynamoDB, bootstrapped through `run.sh`

All values are environment-scoped through `.tfvars` files.

---

## Environment model

The stack supports two operating modes:

### Staging
- EKS public endpoint enabled
- Public endpoint restricted by CIDR
- No admin EC2 host
- Easier direct `kubectl` access from your laptop

### Production
- EKS private endpoint only
- Optional admin EC2 host enabled
- Private operational access path
- More restrictive network posture

---

## Architecture

### Networking

The VPC module creates:

- A VPC with a configurable CIDR block
- Public and private subnets across `az_count` Availability Zones
- An Internet Gateway
- NAT gateways
  - one per AZ by default
  - single NAT only as a compatibility escape hatch
- Route tables for public and private subnets

Worker nodes run in private subnets only.

The VPC module also creates the selective endpoints required for a private EKS and SSM-oriented bootstrap path.

---

### EKS cluster

The EKS module provisions:

- The EKS control plane
- An OIDC provider for IRSA
- Two managed nodegroups
- Cluster security-group rules for worker nodes
- Secrets encryption using KMS
- Access-entry support for AWS IAM principals

The cluster is designed around exactly two nodegroups:

---

#### 1. `system` nodegroup

Purpose:
- Stable platform services
- Control-plane-adjacent workloads
- Stateful or always-on services

Typical examples:
- CoreDNS support workloads
- EBS CSI components
- Cluster infrastructure services
- Other always-on platform components

Characteristics:
- On-demand only
- Stable capacity
- Not intended for bursty compute workloads

Labels:
```yaml
node-type: general
````

Taint policy:

* Kept untainted in the current setup unless explicitly configured otherwise

---

#### 2. `workloads` nodegroup

Purpose:

* Stateless inference
* Batch jobs
* Model-serving workloads
* Indexing jobs

Typical examples:

* frontend
* retriever
* dense model service
* sparse model service
* reranker
* indexing jobs and CronJobs
* qdrant, when scheduled to compute-capable nodes

Characteristics:

* Spot-capable
* Suitable for autoscaling
* Not intended for cluster-critical services

Labels:

```yaml
node-type: compute
```

Taint:

```yaml
node-type=compute:NoSchedule
```

Workload pods must include matching tolerations and a node selector when they are meant to run on the compute pool.

---

### Scheduling model

Terraform defines the scheduling contract through labels and taints. Kubernetes manifests must honor that contract.

* system workloads go to `general`
* stateless workloads go to `compute`
* Spot capacity is reserved for stateless workloads
* Stateful platform services should not depend on Spot scheduling

---

### Security

The security module creates:

* A worker-node security group
* Intra-VPC ingress rules
* Egress suitable for private-subnet nodes using NAT or approved endpoints

Control-plane security-group rules are owned by the EKS module.

The stack avoids permissive public ingress on worker nodes.

---

### IAM design

IAM is split into two phases.

#### `iam_pre_eks`

Created before cluster provisioning.

This includes:

* EKS cluster role
* EKS node role
* Other bootstrap IAM wiring needed to create the cluster

#### `iam_post_eks`

Created after the cluster exists and the OIDC provider is available.

This includes:

##### IRSA roles

Mapped to Kubernetes service accounts with least-privilege AWS access.

Current IRSA roles:

* `indexer`

  * read/write `DATA_S3_BUCKET`
  * read/write `QDRANT_BACKUPS_BUCKET`
* `frontend`

  * read/write `DATA_S3_BUCKET`
* `retriever`

  * Bedrock invocation only

Dense, sparse, and reranker services do not require AWS credentials in-cluster.

##### GitHub Actions OIDC roles

Used by CI workflows to push images to ECR without static credentials.

Current roles:

* `gh-actions-frontend`
* `gh-actions-retriever`
* `gh-actions-dense-model`
* `gh-actions-sparse-model`
* `gh-actions-reranker`
* `gh-actions-indexer`

These roles are scoped to:

* the GitHub repository
* the branch condition
* the exact ECR repository they are allowed to push to

---

### S3

The S3 module creates exactly two buckets:

* `DATA_S3_BUCKET` → platform data and uploads
* `QDRANT_BACKUPS_BUCKET` → Qdrant backups and snapshots

Each bucket is:

* private
* encrypted
* versioned
* protected from public access

Bucket names are configured through `.tfvars`.

---

### ECR

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
* lifecycle retention policy

---

## Repository structure

```text
src/infra/terraform/aws/
  main.tf
  outputs.tf
  variables.tf
  providers.tf
  staging.tfvars
  prod.tfvars
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

## Module responsibilities

### `modules/vpc`

Creates:

* VPC
* public and private subnets
* Internet Gateway
* NAT gateways
* route tables
* selective VPC endpoints

### `modules/security`

Creates:

* worker-node security group
* intra-VPC ingress
* controlled outbound access

### `modules/iam_pre_eks`

Creates:

* EKS cluster role
* EKS node role

### `modules/eks`

Creates:

* EKS cluster
* system and workloads nodegroups
* OIDC provider
* KMS secret encryption
* worker-to-control-plane rule

### `modules/s3`

Creates:

* two environment-scoped buckets

Exports:

* bucket name map
* bucket ARN map
* bucket ID map

### `modules/iam_post_eks`

Creates:

* IRSA roles for Kubernetes service accounts
* GitHub Actions OIDC roles for ECR access
* EBS CSI driver role

### `modules/ecr`

Creates:

* repositories from tfvars
* lifecycle policies

---

## Outputs

The root stack exposes outputs for:

### Networking

* `vpc_id`
* `private_subnet_ids`
* `public_subnet_ids`
* `availability_zones`

### EKS

* `eks_cluster_name`
* `eks_cluster_endpoint`
* `eks_cluster_ca_data`
* `eks_oidc_provider_arn`
* `eks_oidc_provider_issuer`

### IAM

* `iam_cluster_role_arn`
* `iam_node_role_arn`
* `irsa_role_arns`
* `github_actions_role_arns`

### S3

* `s3_bucket_name_map`
* `s3_bucket_arn_map`

### ECR

* repository URL, ARN, and name maps

### Admin access

* optional admin EC2 instance ID
* optional admin EC2 private IP
* optional admin security group ID

---
## Configuration

Environment-specific configuration lives in:

* `staging.tfvars`
* `prod.tfvars`

These define:

* AWS region
* cluster name
* VPC CIDR
* subnet CIDRs
* nodegroup sizing
* endpoint exposure mode
* admin EC2 toggle
* S3 bucket names
* IRSA role mappings
* GitHub Actions role mappings
* ECR repository mappings

No secrets are stored in these files.

---

## State management

Remote state is handled through `run.sh` using:

* S3 for state storage
* DynamoDB for state locking

No local state should be used for the platform resources.

---

## Invariants

These must stay true:

* exactly two nodegroups: `system` and `workloads`
* `system` is for platform services
* `workloads` is for stateless compute
* no workloads in public subnets
* no static AWS credentials in Kubernetes
* AWS access is through IRSA or GitHub OIDC
* S3 access is least-privilege and bucket-scoped
* modules communicate only through outputs
* ECR names must match the CI contract exactly
* staging should stay low-friction
* production should stay private and controlled
