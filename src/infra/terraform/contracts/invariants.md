## 1) Platform identity

The repository is a **private, RAG-first AWS platform bootstrap**.

Hard invariants:

- environment separation is handled by `prod.tfvars` and `staging.tfvars`
- the root module is the only place that consumes tfvars
- child modules never read tfvars directly
- the platform is built for online retrieval, model serving, indexing, storage, and observability

---

## 2) Root-module contract

The root module is the only consumer of environment variables.

Invariant flow:

- `prod.tfvars` / `staging.tfvars`
- `variables.tf`
- `main.tf`
- `modules/*`

Rules:

- child modules never read tfvars directly
- root passes only declared inputs to modules
- outputs must come only from module outputs
- root is the single source of truth for composition

---

## 3) Provider and versioning contract

There is one canonical provider/version source of truth.

Invariants:

- one `terraform` block in `providers.tf`
- no split version policy across multiple files
- AWS provider is pinned to the 6.x line
- TLS provider is present for EKS OIDC thumbprint support
- default tags are platform-neutral and not tied to a legacy MLOps naming scheme

---

## 4) Networking invariants

The VPC design is fixed.

Invariants:

- private EKS cluster only
- no public worker nodes
- no VPC endpoints
- multi-AZ design
- one public subnet per AZ
- one private subnet per AZ
- one NAT gateway per AZ by default
- `single_nat_gateway` exists only as a compatibility escape hatch

Routing invariants:

- public subnets route to the Internet Gateway
- private subnets route to NAT
- worker nodes live only in private subnets

---

## 5) Security invariants

The security module owns only the worker-node security group.

Invariants:

- one node security group only
- ingress allowed within the VPC CIDR
- egress allowed to `0.0.0.0/0` so NAT-based outbound works
- no VPC endpoint security group
- no control-plane security-group rule inside the security module

The EKS module owns the control-plane ↔ node security-group rule.

---

## 6) EKS invariants

The cluster is private-only.

Invariants:

- `endpoint_public_access = false`
- `endpoint_private_access = true`
- EKS control plane is encrypted with a KMS key
- OIDC provider is created for IRSA
- the control-plane security-group rule allowing nodes to reach the API server belongs in the EKS module

Nodegroup invariants:

- exactly two managed nodegroups
- `system` nodegroup = long-running stateful and control-plane-like services
- `workloads` nodegroup = stateless services, jobs, and compute
- no `inference` nodegroup
- labels and taints reflect the nodegroup split

Required labels:

- `node-type = general`
- `node-type = compute`

Required taints:

- `node-type=general:NoSchedule`
- `node-type=compute:NoSchedule`

Scheduling intent:

- `general` = stable, always-on, stateful, control-plane-like
- `compute` = interruptible, horizontally scalable, stateless

---

## 7) Workload placement invariants

Terraform only establishes the platform-side scheduling contract. Kubernetes manifests must honor it.

Invariant mapping:

- `general` nodes host stateful services, control-plane-like services, and platform operators
- `compute` nodes host stateless inference services, batch jobs, and ephemeral workers

Expected placement:

### `system` / `general`
- ArgoCD
- Qdrant
- Valkey
- ClickHouse
- Prometheus server
- Alertmanager
- Grafana
- CoreDNS
- metrics-server
- any other stateful platform service

### `workloads` / `compute`
- frontend
- retriever
- dense model service
- sparse model service
- reranker
- indexing jobs and CronJobs
- cloudflared, if retained as a stateless edge component

Placement rules:

- stateful workloads must not run on Spot
- stateless workloads may run on Spot
- system services must have explicit scheduling constraints
- compute services should not tolerate general-node taints unless required

---

## 8) Storage invariants

S3 is a first-class module.

Invariants:

- exactly two managed buckets
- bucket keys are stable:

  - `DATA_S3_BUCKET`
  - `QDRANT_BACKUPS_BUCKET`

- buckets are private
- buckets are encrypted
- buckets are versioned
- public access is blocked
- bucket ownership is enforced

Role mapping:

- `DATA_S3_BUCKET` = document data, uploads, derived artifacts used by the RAG pipeline
- `QDRANT_BACKUPS_BUCKET` = Qdrant snapshot and backup storage

No additional S3 buckets are allowed unless the contract is explicitly revised.

---

## 9) IAM bootstrap invariants

`iam_pre_eks` is bootstrap-only.

Invariants:

- EKS control-plane role
- EKS node role
- Cluster Autoscaler policy
- EBS CSI managed policy ARN output
- no IRSA roles
- no GitHub OIDC roles
- no workload-specific AWS permissions

---

## 10) IRSA invariants

`iam_post_eks` owns Kubernetes service-account identity.

Invariants:

- IRSA roles are created only after EKS OIDC exists
- each IRSA role is scoped to one namespace/service account
- each IRSA role is scoped to explicit S3 buckets and explicit AWS services
- access mode is restricted to the minimum required for the workload
- trust policy must use:

  - EKS OIDC provider
  - `sts:AssumeRoleWithWebIdentity`
  - `aud = sts.amazonaws.com`
  - exact service-account subject

Required IRSA roles:

- `indexer`:
  - read/write `DATA_S3_BUCKET`
  - read/write `QDRANT_BACKUPS_BUCKET`

- `frontend`:
  - read/write `DATA_S3_BUCKET` for presigned URL flows

- `retriever`:
  - Bedrock invocation permissions only
  - no S3 access required

Dense, sparse, and reranker model services:

- no AWS permissions required
- models load from Hugging Face and do not depend on AWS IAM

---

## 11) GitHub Actions OIDC invariants

GitHub OIDC roles are owned inside `iam_post_eks`, not a separate module.

Invariants:

- one role per repository/workflow identity
- repo-subject must be exact
- branch is `main`
- `token.actions.githubusercontent.com:aud = sts.amazonaws.com`
- roles are repository-scoped, not wildcarded

Required roles:

- `gh-actions-frontend`
- `gh-actions-retriever`
- `gh-actions-dense-model`
- `gh-actions-sparse-model`
- `gh-actions-reranker`
- `gh-actions-indexer`

Important naming invariants:

- repository strings must be lowercase and owner-qualified
- repo names must match the ECR repository names exactly
- the source repository is the E2E RAG system repository, not legacy Flyte repositories

---

## 12) ECR invariants

ECR is required in the final tree.

Invariants:

- `modules/ecr/main.tf` stays present
- repositories are defined from root tfvars
- no hardcoded legacy repository names
- repositories are lowercase and match the CI/image naming contract
- each repository gets exactly one lifecycle policy
- images are immutable by default
- scan-on-push is enabled
- AES256 encryption is enabled

Repository set:

- `frontend`
- `retriever`
- `dense-model`
- `sparse-model`
- `reranker`
- `indexer`

---

## 13) tfvars invariants

`prod.tfvars` and `staging.tfvars` must share the same schema.

Invariant keys:

- `environment`
- `region`
- `cluster_name`
- `vpc_cidr`
- `az_count`
- `private_subnet_cidrs`
- `public_subnet_cidrs`
- `enable_nat_per_az`
- `single_nat_gateway`
- `system_nodegroup`
- `workloads_nodegroup`
- `system_node_taints`
- `workloads_node_taints`
- `system_node_labels`
- `workloads_node_labels`
- `s3_buckets`
- `irsa_roles`
- `github_actions_roles`
- `ecr_repositories`
- `cluster_autoscaler`
- `tags`

Environment invariants:

- staging and prod have the same contract
- only sizing differs between environments
- repo naming does not differ by environment

---

## 14) Root composition invariants

The root module order is fixed conceptually.

Required dependency order:

1. VPC
2. Security
3. ECR
4. IAM pre-EKS
5. EKS
6. S3
7. IAM post-EKS

The root must pass:

- subnet and NAT settings to VPC
- VPC ID and CIDR to security
- cluster and node roles to EKS
- buckets to S3
- OIDC + bucket maps + IRSA roles + GitHub roles to `iam_post_eks`
- ECR repository map to ECR

---

## 15) Documentation invariants

`README.md` must describe the current RAG platform, not the old MLOps platform.

It must reflect:

- E2E RAG platform
- private VPC
- multi-AZ NAT
- two nodegroups: `system` and `workloads`
- two buckets
- three IRSA roles
- six GitHub Actions OIDC roles
- ECR for all service images
- no CloudWatch dependence unless explicitly added
- no VPC endpoints
- no legacy Flyte framing

---

## 16) State/bootstrap invariants

`run.sh` is only for backend bootstrap.

Invariants:

- create the S3 backend bucket
- create the DynamoDB lock table
- run `tofu init`
- no application resources in bootstrap
- neutral naming only

---

## 17) Final correctness invariants

The repo is only considered fully synced when all of these are true:

- no duplicate provider/version policy
- root variables match root module inputs
- root outputs match module outputs
- tfvars values are lowercase and owner-qualified where required
- `main.tf` passes `ecr_repositories`, `irsa_roles`, and `github_actions_roles`
- `iam_post_eks` is the single home for IRSA and GitHub OIDC roles
- ECR repo names match the CI naming contract exactly
- stateful services are pinned to `system`
- stateless services are allowed on `workloads`, including Spot capacity where appropriate

That is the final invariant set for this repository.