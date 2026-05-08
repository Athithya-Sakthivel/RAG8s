## Nodegroup Design

### 1) General Nodegroup (baseline services)

**Purpose:** Run stable, always-on platform components

**Workloads:**

* Flyte control plane (admin, propeller, console)
* CNPG (Postgres)
* MLflow server
* SigNoz (observability stack)
* Cloudflare / ingress
* Operators (KubeRay, Spark)
* Auth service
* Linkerd

**Instance profile:**
* Moderate CPU and memory (2–4 vCPU, 8–16 GB)
* On-demand nodes only

**Constraints:**

* No batch or ephemeral workloads
* No Spark or Ray workers

---

### 2) Compute-heavy Nodegroup (execution layer)

**Purpose:** Run all compute-intensive and batch workloads

**Workloads:**

* Flyte task pods (training, ELT)
* Spark jobs (Iceberg ETL)
* Ray workers (serve + batch inference)
* Drift detection jobs

**Instance profile:**

* CPU/memory optimized (4–16 vCPU, 16–64 GB)
* Autoscaling enabled
* Spot instances allowed

**Constraints:**

* No control-plane or long-running services

---

## Scheduling Strategy (strict)

### Node labels

* General nodes → `node-type=general`
* Compute nodes → `node-type=compute`

---

### Mandatory scheduling rules

**1. Services (default → general)**
All long-running services must explicitly target general nodes:

```yaml
nodeSelector:
  node-type: general
```

---

**2. Compute workloads (default → compute)**
All jobs and workers must explicitly target compute nodes:

```yaml
nodeSelector:
  node-type: compute
```

---

**3. Enforce isolation using taints (recommended)**

Apply taints:

* General nodes:

```bash
node-type=general:NoSchedule
```

* Compute nodes:

```bash
node-type=compute:NoSchedule
```

Then add tolerations accordingly:

* Services tolerate `general`
* Jobs tolerate `compute`

This prevents accidental mis-scheduling.

---

## Component-specific rules

### Ray

* Head pod → general
* Worker pods → compute

---

### Spark

* Driver → general (preferred for stability)
* Executors → compute

---

## Final model

* **General = control plane + stateful services**
* **Compute = execution + autoscaling workloads**

This enforces clear separation, prevents resource contention, and aligns with Flyte + Spark + Ray execution patterns.
