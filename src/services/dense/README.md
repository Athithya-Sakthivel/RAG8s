# Dense Embedding Service

A lightweight HTTP service for generating text embeddings using any FastEmbed model. Built with FastAPI and FastEmbed, it runs as a stateless container.

## Supported Models

You can use **any text embedding model** from the official [FastEmbed supported models list](https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-text-embedding-models). The service automatically downloads the model from Hugging Face Hub on first use.

**Popular options (dimension/license):**
- `BAAI/bge-small-en-v1.5` (384-dim, MIT, quantized by default)
- `snowflake/snowflake-arctic-embed-xs` (384-dim, Apache 2.0)
- `BAAI/bge-base-en-v1.5` (768-dim, MIT)
- `nomic-ai/nomic-embed-text-v1.5` (768-dim, Apache 2.0)
- `mixedbread-ai/mxbai-embed-large-v1` (1024-dim, Apache 2.0)

*For multilingual or code-specific needs, see the full list in the FastEmbed docs.*

## Configuration

Configure the service via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DENSE_MODEL_NAME` | `BAAI/bge-small-en-v1.5` | Model ID from Hugging Face Hub |
| `DENSE_DIM` | `384` | Expected embedding dimension (must match model) |
| `DENSE_BATCH_SIZE` | `32` | Max texts per batch |
| `DENSE_NORMALIZE` | `TRUE` | L2-normalize embeddings(Always) |
| `DENSE_CUDA` | `0` | Set `1` to enable GPU (CUDA) |
| `PRELOAD_MODEL` | `0` | Preload model on startup (`1` to enable) |
| `DENSE_HOST` | `0.0.0.0` | HTTP bind address |
| `DENSE_PORT` | `8200` | HTTP port |

## API Endpoints

### `POST /embed` – Generate embeddings
**Request body:**
```json
{
  "texts": ["hello world", "another sentence"]
}
```
**Response:**
```json
{
  "vectors": [[0.12, -0.34, ...], [0.56, -0.78, ...]]
}
```

- Maximum batch size = `DENSE_BATCH_SIZE`
- Returns L2-normalized vectors if `DENSE_NORMALIZE=true`

### `GET /health` – Liveness check
Returns service status, model name, dimension, and configuration.

### `GET /readyz` – Readiness probe
Returns `200 OK` when the model is fully loaded and warmed up.

## Usage Examples

**Generate embeddings (with `curl`):**
```bash
kubectl port-forward -n inference svc/dense-svc 8200:8200
```
```bash
curl -X POST http://localhost:8200/embed \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Hello, world!", "Embed me please"]}'
```

**Check service health:**
```bash
curl http://localhost:8200/health
```

## Notes

- The model is **loaded lazily** on first request (unless `PRELOAD_MODEL=1` is set, which loads it at startup).
- On first use, the model will be **downloaded** from Hugging Face Hub (cache persists in `/models_cache`).
- For **batch processing**, you can send multiple texts in one request up to the configured batch size.
-