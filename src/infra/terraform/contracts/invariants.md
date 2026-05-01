Here are the **system invariants** that emerged from the whole design discussion, stated as the final contract for the Terraform layer.

## 1) Platform identity

The repository is a **neutral MLOps platform bootstrap**, not an AgentOps stack.

Hard invariants:
* environment separation is handled by `prod.tfvars` and `staging.tfvars`
* everything is driven from the root module contract, not from child modules reading tfvars directly

---

## 2) Root-module contract

The root module is the only place that consumes `tfvars`.

Invariant flow:

* `prod.tfvars` / `staging.tfvars`
* `variables.tf`
* `main.tf`
* `modules/*`

Child modules never read tfvars directly.

Root module must pass only declared inputs to modules, and outputs must come only from module outputs.

---

## 3) Versioning and provider contract

There is one canonical provider/version source of truth.

Invariants:

* one `terraform` block in `providers.tf`
* no split version policy across `versions.tf` and `providers.tf`
* AWS provider is pinned to the 6.x line
* TLS provider is present because EKS OIDC thumbprint support depends on it
* default tags are platform-neutral, not AgentOps-specific

---

## 4) Networking invariants

The VPC design is fixed.

Invariants:

* private EKS cluster only
* no public worker nodes
* no VPC endpoints
* multi-AZ design
* one public subnet per AZ
* one private subnet per AZ
* one NAT gateway per AZ by default
* `single_nat_gateway` exists only as a compatibility escape hatch, not the preferred production mode

Routing invariants:

* public subnets route to the Internet Gateway
* private subnets route to NAT
* worker nodes live only in private subnets

---

## 5) Security invariants

The security module owns only the worker-node security group.

Invariants:

* one node security group only
* ingress allowed within the VPC CIDR
* egress allowed to `0.0.0.0/0` so NAT-based outbound works
* no VPC endpoint SG
* no control-plane SG rule inside the security module

The EKS module owns the control-plane ↔ node security-group rule.

---

## 6) EKS invariants

The cluster is private-only.

Invariants:

* `endpoint_public_access = false`
* `endpoint_private_access = true`
* EKS control plane is encrypted with a KMS key
* OIDC provider is created for IRSA
* the control-plane security-group rule allowing nodes to reach the API server belongs in the EKS module

Nodegroup invariants:

* exactly two managed nodegroups
* `system` nodegroup = long-running services
* `workloads` nodegroup = batch / compute / jobs
* no `inference` nodegroup anymore
* labels and taints reflect the nodegroup split

Required labels:

* `node-type = general`
* `node-type = compute`

Required taints:

* `node-type=general:NoSchedule`
* `node-type=compute:NoSchedule`

---

## 7) Workload placement invariants

Terraform only establishes the platform-side scheduling contract. Actual workload manifests must honor it.

Invariant mapping:

* `general` nodes host control-plane-like and stateful services
* `compute` nodes host task pods, workers, executors, and jobs

Expected placement:

* Flyte control plane → `general`
* CNPG → `general`
* MLflow → `general`
* Iceberg REST catalog → `general`
* Spark operators → `general`
* Ray operators / control-plane components → `general`
* Flyte tasks → `compute`
* Spark executors and jobs → `compute`
* Ray workers → `compute`

---

## 8) Storage invariants

S3 is a first-class module.

Invariants:

* exactly three managed buckets
* bucket keys are stable:

  * `S3_BUCKET`
  * `PG_BACKUPS_S3_BUCKET`
  * `MLFLOW_S3_BUCKET`
* buckets are private
* buckets are encrypted
* buckets are versioned
* public access is blocked
* bucket ownership is enforced

Role mapping:

* `S3_BUCKET` = general platform data
* `PG_BACKUPS_S3_BUCKET` = Postgres backups
* `MLFLOW_S3_BUCKET` = MLflow artifacts

---

## 9) IAM bootstrap invariants

`iam_pre_eks` is bootstrap-only.

Invariants:

* EKS control-plane role
* EKS node role
* Cluster Autoscaler policy
* EBS CSI managed policy ARN output
* no IRSA roles
* no GitHub OIDC roles
* no CI-specific permissions here

---

## 10) IRSA invariants

`iam_post_eks` owns Kubernetes service-account identity.

Invariants:

* IRSA roles are created only after EKS OIDC exists
* each IRSA role is scoped to one namespace/service account
* each IRSA role is scoped to one bucket key
* access mode is only `read` or `read_write`
* trust policy must use:

  * EKS OIDC provider
  * `sts:AssumeRoleWithWebIdentity`
  * `aud = sts.amazonaws.com`
  * exact service-account subject

Required IRSA roles:

* CNPG → `PG_BACKUPS_S3_BUCKET`
* Flyte task → `S3_BUCKET`
* Iceberg REST → `S3_BUCKET`
* Ray inference → `MLFLOW_S3_BUCKET` read-only
* MLflow → `MLFLOW_S3_BUCKET` read/write

---

## 11) GitHub Actions OIDC invariants

GitHub OIDC roles are also owned inside `iam_post_eks`, not a separate module.

Invariants:

* one role per repository
* repo-subject must be exact
* branch is `main`
* `token.actions.githubusercontent.com:aud = sts.amazonaws.com`
* roles are repository-scoped, not wildcarded

Required roles:

* `gh-actions-flyte-elt-task`
* `gh-actions-flyte-train-task`
* `gh-actions-tabular-inference-service`

Important naming invariant:

* repository strings must be lowercase and owner-qualified
* correct format is `athithya-sakthivel/<repo-name>`

---

## 12) ECR invariants

ECR is not optional in the final tree.

Invariants:

* `modules/ecr/main.tf` stays present
* repositories are defined from root tfvars
* no hardcoded AgentOps repo names
* repositories are lowercase and match the CI/image naming contract
* each repository gets exactly one lifecycle policy
* images are immutable by default
* scan-on-push is enabled
* AES256 encryption is enabled

Repository set:

* `flyte-elt-task`
* `flyte-train-task`
* `tabular-inference-service`

---

## 13) tfvars invariants

`prod.tfvars` and `staging.tfvars` must share the same schema.

Invariant keys:

* `environment`
* `region`
* `cluster_name`
* `vpc_cidr`
* `az_count`
* `private_subnet_cidrs`
* `public_subnet_cidrs`
* `enable_nat_per_az`
* `single_nat_gateway`
* `system_nodegroup`
* `workloads_nodegroup`
* `system_node_taints`
* `workloads_node_taints`
* `system_node_labels`
* `workloads_node_labels`
* `s3_buckets`
* `irsa_roles`
* `github_actions_roles`
* `ecr_repositories`
* `cluster_autoscaler`
* `tags`

Environment invariants:

* staging and prod have the same contract
* only sizing differs between environments
* repo naming does not differ by environment

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

* subnet and NAT settings to VPC
* VPC ID and CIDR to security
* cluster and node roles to EKS
* buckets to S3
* OIDC + bucket maps + IRSA roles + GitHub roles to `iam_post_eks`
* ECR repository map to ECR

---

## 15) Documentation invariants

`README.md` must describe the current platform, not the old one.

It must reflect:

* MLOps platform
* private VPC
* multi-AZ NAT
* two nodegroups: `system` and `workloads`
* three buckets
* five IRSA roles
* three GitHub Actions OIDC roles
* optional ECR
* no CloudWatch dependence
* no VPC endpoints
* no AgentOps framing

---

## 16) State/bootstrap invariants

`run.sh` is only for backend bootstrap.

Invariants:

* create the S3 backend bucket
* create the DynamoDB lock table
* run `tofu init`
* no application resources in bootstrap
* neutral naming only

---

## 17) Final correctness invariants

The repo is only considered fully synced when all of these are true:

* no duplicate provider/version policy
* root variables match root module inputs
* root outputs match module outputs
* tfvars values are lowercase and owner-qualified where required
* `main.tf` passes `ecr_repositories`, `irsa_roles`, and `github_actions_roles`
* `iam_post_eks` is the single home for IRSA and GitHub OIDC roles
* ECR repo names match the CI naming contract exactly

That is the final invariant set for this repository.
