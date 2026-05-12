## Nodegroup Design and AWS Access Contract (RAG Platform)

This document defines **workload placement and AWS access boundaries** for the EKS cluster.

---

# 1) Nodegroup Design

The cluster has **exactly two nodegroups**.

---

## 1.1 System Nodegroup (`general`)

**Purpose:**
Run **stateful, control-plane-like, and critical platform services**.

**Workloads:**

* ArgoCD components
* Qdrant (vector database)
* Valkey (cache / KV store)
* ClickHouse (logging backend)
* Prometheus server
* Alertmanager
* Grafana
* CoreDNS
* metrics-server
* any other stateful or control-plane service

**Instance profile:**

* Moderate, stable capacity (2–4 vCPU, 8–16 GB typical)
* **On-demand only (no Spot)**

**Constraints:**

* Must not run stateless application workloads
* Must not run batch jobs
* Must not be interrupted

---

## 1.2 Workloads Nodegroup (`compute`)

**Purpose:**
Run **stateless services, inference workloads, and batch jobs**.

**Workloads:**

* frontend (SPA service)
* retriever (LLM + orchestration layer)
* dense model service
* sparse model service
* reranker
* indexing jobs and CronJobs
* cloudflared (edge tunnel)
* vector agent (logging DaemonSet)

**Instance profile:**

* Flexible, autoscaling
* **Spot instances allowed (default)** with multi-instance diversification

**Constraints:**

* Must not host stateful services
* Must tolerate interruption
* Must scale horizontally

---

# 2) Scheduling Strategy (Strict)

Terraform defines the contract. Kubernetes manifests must enforce it.

---

## 2.1 Node Labels

* System nodes:

```text
node-type = general
```

* Workload nodes:

```text
node-type = compute
```

---

## 2.2 Mandatory Scheduling Rules

### Rule 1 — System workloads (required)

All stateful and control-plane services must target system nodes:

```yaml
nodeSelector:
  node-type: general

tolerations:
  - key: "node-type"
    operator: "Equal"
    value: "general"
    effect: "NoSchedule"
```

---

### Rule 2 — Stateless workloads (required)

All stateless services and jobs must target compute nodes:

```yaml
nodeSelector:
  node-type: compute
```

No toleration for `general` should be present.

---

## 2.3 Taints (Enforcement Layer)

Taints are mandatory to prevent accidental placement.

### System nodes

```bash
node-type=general:NoSchedule
```

### Workload nodes

```bash
node-type=compute:NoSchedule
```

---

## 2.4 DaemonSet Exception

Cluster-wide agents (e.g., logging) must run on all nodes:

```yaml
tolerations:
  - operator: Exists
```

No nodeSelector required.

---

# 3) Workload Placement Mapping

| Component          | Nodegroup |
| ------------------ | --------- |
| Qdrant             | system    |
| Valkey             | system    |
| ClickHouse         | system    |
| Prometheus         | system    |
| Grafana            | system    |
| ArgoCD             | system    |
| CoreDNS            | system    |
| metrics-server     | system    |
| frontend           | workloads |
| retriever          | workloads |
| dense              | workloads |
| sparse             | workloads |
| reranker           | workloads |
| cloudflared        | workloads |
| indexing jobs      | workloads |
| vector (DaemonSet) | all nodes |

---

# 4) Spot Usage Contract

Spot capacity is **allowed only on the workloads nodegroup**.

### Requirements:

* workloads must be stateless
* workloads must tolerate restarts
* replicas ≥ 2 for critical services (e.g., retriever)
* PodDisruptionBudgets must be defined where needed
* termination must be graceful

### Prohibited:

* stateful workloads on Spot
* single-replica critical services on Spot

---

# 5) AWS Access Model (IRSA)

AWS access is defined strictly via IRSA roles.

---

## 5.1 Access Principles

* no static AWS credentials
* no node-level IAM usage for workloads
* each service account maps to one IAM role
* permissions are minimal and explicit

---

## 5.2 Service → AWS Access Mapping

### Indexer

* S3:

  * read/write `DATA_S3_BUCKET`
  * read/write `QDRANT_BACKUPS_BUCKET`

Purpose:

* ingest documents
* store processed artifacts
* manage Qdrant backups

---

### Frontend

* S3:

  * read/write `DATA_S3_BUCKET`

Purpose:

* generate presigned URLs
* handle direct client uploads/downloads

---

### Retriever

* AWS Bedrock:

  * `bedrock:InvokeModel`
  * `bedrock:InvokeModelWithResponseStream`

Purpose:

* LLM inference

No S3 access.

---

### Dense / Sparse / Reranker

* no AWS access

Purpose:

* load models from Hugging Face
* operate independently of AWS IAM

---

## 5.3 Enforcement

* IRSA roles must match exact:

  * namespace
  * service account name
* no wildcard trust policies
* no shared roles across services

---

# 6) Failure Modes (Must Be Avoided)

* stateful pod scheduled on compute node
* stateless pod scheduled on system node
* missing taints allowing cross-scheduling
* retriever running as a single replica on Spot
* services using node IAM instead of IRSA
* over-permissive S3 or Bedrock policies

---

# 7) Final Model

```text
System nodegroup   → stateful + control plane (on-demand, stable)
Workloads nodegroup → stateless + inference + jobs (spot, scalable)
```

AWS access:

```text
Indexer   → S3 (data + backups)
Frontend  → S3 (presigned access)
Retriever → Bedrock
Others    → no AWS
```

This separation enforces:

* cost efficiency (Spot for stateless)
* reliability (stateful isolated)
* security (least-privilege IRSA)
* predictable scheduling behavior
