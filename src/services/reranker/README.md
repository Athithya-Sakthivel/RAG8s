# Reranker Service

A lightweight HTTP service for **re‑ranking** documents given a query, using FastEmbed cross‑encoders. Returns relevance scores (e.g., 0.8, 0.1) – higher is more relevant.

## Supported Reranker (Cross‑Encoder) Models

You can use any model from the [FastEmbed supported cross‑encoder list](https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-rerank-cross-encoder-models). The service automatically downloads the model from Hugging Face Hub.

**Available models (size / license):**

| Model | Size (GB) | Description | License |
|-------|-----------|-------------|---------|
| `Xenova/ms-marco-MiniLM-L-6-v2` | 0.08 | Lightweight, fast reranker for English | Apache‑2.0 |
| `Xenova/ms-marco-MiniLM-L-12-v2` | 0.12 | Higher quality, still fast | Apache‑2.0 |
| `jinaai/jina-reranker-v1-tiny-en` | 0.13 | Blazing‑fast, 8K context | Apache‑2.0 |
| `jinaai/jina-reranker-v1-turbo-en` | 0.15 | Balanced speed/quality | Apache‑2.0 |
| `BAAI/bge-reranker-base` | 1.04 | Strong English reranker (MIT) | MIT |
| `jinaai/jina-reranker-v2-base-multilingual` | 1.11 | Multilingual (100+ languages) | CC‑BY‑NC‑4.0 |

*For your use case, start with `Xenova/ms-marco-MiniLM-L-6-v2` for low latency, or `BAAI/bge-reranker-base` for highest quality.*

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RERANKER_MODEL_NAME` | `Xenova/ms-marco-MiniLM-L-6-v2` | Model ID from Hugging Face Hub |
| `RERANKER_MAX_DOCS` | `50` | Maximum number of documents per request |
| `RERANKER_CUDA` | `0` | Set `1` to enable GPU (CUDA) |
| `PRELOAD_MODEL` | `0` | Preload model on startup (`1` to enable) |
| `RERANKER_HOST` | `0.0.0.0` | HTTP bind address |
| `RERANKER_PORT` | `8202` | HTTP port |

## API Endpoints

### `POST /rerank` – Re‑rank documents

**Request:**
```json
{
  "query": "best embedding model",
  "documents": [
    "Dense embeddings are fast and popular.",
    "Rerankers improve search quality by re‑ordering results.",
    "Sparse vectors are good for keyword matching."
  ]
}
```

**Response:**
```json
{
  "scores": [0.83, 0.95, 0.12]
}
```
- Scores are in the same order as input `documents`.
- Higher score = more relevant to the query.

### `GET /health` – Liveness check
Returns model name, max documents, CUDA status, and readiness flag.

### `GET /readyz` – Readiness probe
Returns `200 OK` when the model is loaded and ready.

### `GET /metrics` – Prometheus metrics
Exposes the following metrics (sample):
- `reranker_requests_total{model, cuda, status}` — total requests (success/client_error/model_error/server_error)
- `reranker_request_duration_seconds{model, cuda}` — latency histogram
- `reranker_requests_in_progress{model, cuda}` — current in-flight requests
- `reranker_errors_total{model, cuda, error_type}` — error count by type

## Usage Example

**Re‑rank documents (with `curl`):**
```bash
kubectl port-forward -n inference svc/reranker-svc 8202:8202
```
```bash
curl -X POST http://localhost:8202/rerank \
  -H "Content-Type: application/json" \
  -d '{
    "query": "benefits of sparse embeddings",
    "documents": [
      "Sparse embeddings are great for exact term matching.",
      "Dense vectors capture semantic similarity.",
      "Hybrid search combines both."
    ]
  }'
```

**Check health:**
```bash
curl http://localhost:8202/health
```

**Scrape metrics:**
```bash
curl http://localhost:8202/metrics
```

## Notes

- The model is **loaded lazily** on the first request (unless `PRELOAD_MODEL=1` is set).
- On first use, the model is **downloaded** from Hugging Face Hub (cache in `/models_cache`).
- The request fails if `len(documents) > RERANKER_MAX_DOCS`. Adjust the variable for your needs.
- Reranking is **CPU‑bound**; the service uses a thread pool to keep the async event loop responsive. For high throughput, increase `UVICORN_WORKERS` (set in the container) and allocate enough CPU cores.
- Prometheus metrics are exported on port 8202 at `/metrics`. Ensure your Prometheus scrape configuration points to `rag-reranker-model.inference:8202/metrics`
