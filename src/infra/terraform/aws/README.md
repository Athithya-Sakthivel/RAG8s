# E2E RAG Platform Infrastructure

This repository defines the AWS foundation for the end-to-end RAG platform using **OpenTofu**.

It provisions the cloud infrastructure needed by the platform:
networking, IAM, EKS, S3, ECR, and supporting security controls.

Kubernetes application delivery is handled separately through **ArgoCD / GitOps**.

---

## What this stack provisions

For each environment, this stack provides:

- A multi-AZ VPC
- Public and private subnets
- NAT gateways for outbound access
- Selective VPC endpoints where needed
- An EKS cluster
- IAM roles for cluster bootstrap and post-cluster access
- S3 buckets for platform data and backups
- ECR repositories for service images
- Security groups for worker nodes and optional admin access
- Remote state via S3 + DynamoDB, managed through `run.sh`

Environment-specific values live in `.tfvars` files.

---

## Environment model

The stack supports two deployment modes:

### Staging
- EKS public endpoint enabled
- Public endpoint restricted by CIDR
- No admin EC2 host
- Easier direct cluster access for development

### Production
- EKS private endpoint only
- Optional private admin EC2 host
- More restrictive network posture
- Same AWS/IAM contract as staging

---

## Current architecture

### Networking

The VPC module creates:

- A VPC with a configurable CIDR block
- Public and private subnets across `az_count` Availability Zones
- An Internet Gateway
- NAT gateways
- Route tables for public and private subnets

Worker nodes and cluster-managed compute run in private subnets.

The VPC module also supports the tags required for Karpenter discovery.

---

### EKS cluster

The EKS module provisions:

- The EKS control plane
- An OIDC provider for IRSA
- A system managed nodegroup
- Cluster security-group rules for worker nodes
- KMS-backed secrets encryption
- Access entry support for AWS IAM principals

The cluster is designed around a stable **system** nodegroup for platform components.

Karpenter is used for dynamic compute capacity through GitOps-managed Kubernetes resources.

---

#### 1. `system` nodegroup

Purpose:
- Stable platform services
- Cluster infrastructure
- Always-on components

Typical examples:
- CoreDNS support workloads
- EBS CSI components
- ArgoCD
- Karpenter controller
- Other cluster-critical services

Characteristics:
- On-demand only
- Stable capacity
- Not intended for bursty application workloads

Labels:
```yaml
node-type: general
````

Taints:

* Kept untainted unless explicitly configured otherwise

---

#### 2. Karpenter-managed compute nodes

Purpose:

* Stateless inference
* Batch jobs
* Model-serving workloads
* Indexing jobs and CronJobs

Typical examples:

* frontend
* retriever
* dense model service
* sparse model service
* reranker
* indexing jobs
* other bursty workloads

Characteristics:

* Provisioned dynamically by Karpenter
* Spot or on-demand depending on NodePool policy
* Not intended for cluster-critical services

Labels:

```yaml
node-type: compute
```

Taint:

```yaml
node-type=compute:NoSchedule
```

Workload pods must include matching tolerations and node selectors when they are meant to run on compute nodes.

---

### Scheduling model

The scheduling contract is:

* `system` nodes run platform services
* Karpenter nodes run stateless workloads
* Cluster-critical services should not depend on bursty compute capacity
* Compute nodes are tainted to protect them from accidental scheduling

---

### Karpenter ownership model

Terraform now owns only the AWS-side identity and access needed by Karpenter:

* Karpenter controller IAM role
* Karpenter node IAM role
* EKS access entry for Karpenter nodes

ArgoCD owns the Kubernetes-side Karpenter resources:

* Karpenter Helm release
* `EC2NodeClass`
* `NodePool`

This keeps infrastructure and cluster reconciliation separate.

---

### Security

The security module creates:

* A worker-node security group
* Intra-VPC ingress rules
* Controlled outbound access

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

##### EBS CSI driver role

Provides the EKS add-on with the permissions it needs for persistent volumes.

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

The ECR module creates repositories from root input.

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

```sh
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
    karpenter/
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
* optional Karpenter discovery tagging support

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
* system managed nodegroup
* OIDC provider
* KMS secret encryption
* worker-to-control-plane rule
* Karpenter discovery tagging support, if enabled in the root module

### `modules/s3`

Creates:

* two environment-scoped buckets

Exports:

* bucket name map
* bucket ARN map

### `modules/iam_post_eks`

Creates:

* IRSA roles for Kubernetes service accounts
* GitHub Actions OIDC roles for ECR access
* EBS CSI driver role

### `modules/ecr`

Creates:

* repositories from tfvars
* lifecycle policies

### `modules/karpenter`

Owns Karpenter AWS IAM resources only:

* controller role
* node role
* EKS access entry for Karpenter nodes

Kubernetes manifests for Karpenter are deployed separately through ArgoCD.

---

## Outputs

The root stack exposes outputs for:

### Networking

* `availability_zones`
* `aws_region`

### EKS

* `cluster_name`
* `eks_cluster_endpoint`
* `eks_cluster_ca_data`
* `eks_cluster_security_group_id`
* `eks_oidc_provider_arn`
* `eks_oidc_provider_issuer`

### IAM

* `iam_cluster_role_arn`
* `iam_node_role_arn`
* `irsa_role_arns`
* `github_actions_role_arns`
* `karpenter_controller_role_arn`
* `karpenter_node_role_arn`

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

No local state should be used for platform resources.

---

## Invariants

These must stay true:

* the system nodegroup remains stable and always available
* Karpenter owns bursty/stateless compute capacity
* platform services stay on stable capacity
* no workloads in public subnets
* no static AWS credentials in Kubernetes
* AWS access is through IRSA or GitHub OIDC
* S3 access is least-privilege and bucket-scoped
* modules communicate only through outputs
* ECR names must match the CI contract exactly
* staging should stay low-friction
* production should stay private and controlled
* Karpenter Kubernetes objects are deployed through ArgoCD, not Terraform
