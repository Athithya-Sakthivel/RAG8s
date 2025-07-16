


### **Qdrant Payload (Final JSONL Format)**

This is the full, enriched payload per vector chunk to be indexed in Qdrant:

```sh
{
  "id": "chunk_abc123",
  "embedding": [0.123, 0.456, 0.789, 0.321, 0.654],
  "payload": {
    "document_id": "doc_001",
    "chunk_id": "chunk_3",
    "chunk_index": 2,
    "text": "John Doe purchased Product X for $500 on July 1, 2024.",
    "parser": "paddleocr + layoutLM",
    "pipeline_stage": "embedded",
    "source": {
      "path": "s3://bucket/invoice.pdf",
      "hash": "sha256:abc123...",
      "file_type": "pdf",
      "page_number": 4
    },
    "metadata": {
      "language": "en",
      "is_multilingual": false,
      "is_ocr": true,
      "chunk_type": "paragraph",
      "timestamp": "2025-07-01T00:00:00Z",
      "tags": ["purchase", "finance", "invoice"]
    },
    "entities": [
      { "name": "John Doe", "type": "Person" },
      { "name": "Product X", "type": "Product" },
      { "name": "$500", "type": "Price" }
    ],
    "triplets": [
      ["John Doe", "purchased", "Product X"],
      ["Product X", "costs", "$500"],
      ["Purchase", "occurred_on", "2025-07-01"]
    ]
  }
}


```



---

### 🔷 **ArangoDB Schema for Graph RAG**

You’ll model this as a **document collection** (nodes) + **edge collection** (triplets).

#### 1. **Document Collection (e.g., `Chunks`)**

```json
{
  "_key": "chunk_abc123",
  "document_id": "doc_001",
  "chunk_id": "chunk_3",
  "chunk_index": 2,
  "text": "John Doe purchased Product X for $500 on July 1, 2024.",
  "parser": "paddleocr + layoutLM",
  "pipeline_stage": "embedded",
  "source_path": "s3://bucket/invoice.pdf",
  "source_hash": "sha256:abc123...",
  "file_type": "pdf",
  "page_number": 4,
  "language": "en",
  "is_multilingual": false,
  "is_ocr": true,
  "chunk_type": "paragraph",
  "timestamp": "2024-07-01T00:00:00Z",
  "tags": ["purchase", "finance", "invoice"],
  "entities": [
    { "name": "John Doe", "type": "Person" },
    { "name": "Product X", "type": "Product" },
    { "name": "$500", "type": "Price" }
  ],
  "confidence": {
    "embedding": 0.98,
    "ocr": 0.95,
    "parser": 0.93
  }
}
```

#### 2. **Edge Collection (e.g., `KnowledgeTriplets`)**

Each triplet becomes a directed edge between entities.

```json
{
  "_from": "Entities/John_Doe",
  "_to": "Entities/Product_X",
  "predicate": "purchased",
  "source_chunk": "Chunks/chunk_abc123",
  "timestamp": "2024-07-01T00:00:00Z",
  "doc_id": "doc_001"
}
```

Repeat for other triplets like `["Product X", "costs", "$500"]`.

#### 3. **Entity Collection (Optional)**

```json
{
  "_key": "John_Doe",
  "name": "John Doe",
  "type": "Person"
}
```

---

### Summary

| Component           | Purpose           | Description                                         |
| ------------------- | ----------------- | --------------------------------------------------- |
| **Qdrant**          | Vector similarity | Stores embedding + full payload.                    |
| **ArangoDB Chunks** | Document nodes    | Stores parsed chunks as nodes.                      |
| **ArangoDB Edges**  | Knowledge graph   | Triplets as semantic links.                         |
| **Confidence**      | Auditable scoring | Optional but useful for fallback/routing decisions. |






---

| Model             | Params | Context | Avg. nDCG\@10 | Dense+Sparse nDCG\@10 | Inference Speed |
| ----------------- | ------ | ------- | ------------- | --------------------- | --------------- |
| **mGTE‑TRM**      | 306 M  | 8,192   | 58.9          | 71.3                  | \~7× faster     |
| **BGE‑M3 Dense**  | 560 M  | 8,192   | \~64.8        | \~68.9                | Baseline        |
| **BGE‑M3 Sparse** | 560 M  | 8,192   | \~55.1        | —                     | —               |



---

## KubeRay on EKS + Karpenter is the leading production strategy for highly scalable, cost-effective GenAI clusters.
| **Aspect**                           | **KubeRay on EKS + Karpenter**                                        | **Alternatives (summary)**                                                                         |
| ------------------------------------ | --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Node provisioning latency**        | Sub‑minute: just‑in‑time EC2 nodes on unschedulable Ray pods          | 2–5 min boot (Cluster Autoscaler, EC2, Fargate); instant but opaque (serverless)                   |
| **GPU flexibility & fractional use** | Any EC2 GPU (A10G/A100/H100); Ray supports fractional scheduling      | Predefined pools(EKS-Auto), no fractional (EKS CA); limited SKUs (GKE); no GPU (Fargate)                     |
| **Spot + On‑Demand cost mix**        | Optimized Spot usage with fallback; Ray handles preemptions           | Slower rebalancing (CA); extra fees (EKS‑Auto); manual Spot handling (EC2); high cost (serverless) |
| **Observability & debugging**        | Full: Kubernetes events, Ray Dashboard, Prometheus                    | Limited logs (managed Ray services); less verbose (CA); manual scripts (EC2)                       |
| **Operational overhead**             | Minimal: single Karpenter CRD + RayService, fewer IAM policies        | Multiple CRDs/HPA/KEDA/CA rules; custom EC2 scripts; YAML bloat                                    |
| **Self‑healing**                     | Automatic node replacement and Ray task restarts                      | Slower node recovery (CA); manual scripts (EC2); vendor dependent (serverless)                     |
| **Use‑case suitability**             | Top choice: LLM training, fine‑tuning, batch inference, RAG pipelines | Prototype only (serverless); stateless services (ECS); DIY clusters (bare‑metal)                   |
