# Sparse Embedding Service

A lightweight HTTP service for generating **sparse text embeddings** (e.g., SPLADE, BM25, BM42) using FastEmbed. Returns token indices and weights for lexical‑based retrieval (e.g., with Qdrant, Elasticsearch).

## Supported Sparse Models

You can use any sparse embedding model from the [FastEmbed supported models list](https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-sparse-text-embedding-models). The service automatically downloads the model from Hugging Face Hub.

**Available sparse models (size / license):**

| Model | Vocab Size | Description | License |
|-------|------------|-------------|---------|
| `Qdrant/bm25` | – | Classic BM25 as sparse embeddings (requires IDF stats) | Apache‑2.0 |
| `Qdrant/bm42-all-minilm-l6-v2-attentions` | 30,522 | Learned sparse model, better than BM25 | Apache‑2.0 |
| `prithivida/Splade_PP_en_v1` | 30,522 | SPLADE++ for English, high performance | Apache‑2.0 |

*For advanced use, see the full list in the FastEmbed docs.*

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SPARSE_MODEL_NAME` | `prithivida/Splade_PP_en_v1` | Model ID from Hugging Face Hub |
| `SPARSE_BATCH_SIZE` | `8` | Max texts per batch |
| `SPARSE_CUDA` | `0` | Set `1` to enable GPU (CUDA) |
| `PRELOAD_MODEL` | `0` | Preload model on startup (`1` to enable) |
| `SPARSE_HOST` | `0.0.0.0` | HTTP bind address |
| `SPARSE_PORT` | `8201` | HTTP port |

## API Endpoints

### `POST /embed` – Generate sparse embeddings

**Request:**
```json
{
  "texts": ["hello world", "another sentence"]
}
```

**Response:**
```json
{
  "vectors": [
    {"indices": [101, 2012, ...], "values": [0.45, 0.23, ...]},
    {"indices": [1045, 4779, ...], "values": [0.12, 0.95, ...]}
  ]
}
```

- Each vector has `indices` (token IDs) and `values` (weights).
- Output is ready for use with sparse vector indexes (e.g., Qdrant, Vespa).

### `GET /health` – Liveness check
Returns service status, model name, batch size, and CUDA configuration.

### `GET /readyz` – Readiness probe
Returns `200 OK` when the model is fully loaded and warmed up.

## Usage Example

**Generate sparse embeddings (with `curl`):**
```bash
kubectl port-forward -n inference svc/sparse-svc 8201:8201
```
```bash
curl -X POST http://localhost:8201/embed \
  -H "Content-Type: application/json" \
  -d '{"texts": ["What is sparse retrieval?", "BM25 vs SPLADE"]}'
```

**Check health:**
```bash
curl http://localhost:8201/health
```

## Notes

- The model is **loaded lazily** on first request (or at startup if `PRELOAD_MODEL=1`).
- On first use, the model is **downloaded** from Hugging Face Hub (cache in `/models_cache`).
- For **batch processing**, send multiple texts up to `SPARSE_BATCH_SIZE` – larger batches are rejected.
- Sparse vectors are ideal for **keyword‑aware** hybrid search when combined with dense embeddings.

---