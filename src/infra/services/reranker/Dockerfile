# Build stage - prewarm model and install heavy deps
FROM docker.io/library/python:3.14.3-slim@sha256:5e59aae31ff0e87511226be8e2b94d78c58f05216efda3b07dbbed938ec8583b AS builder

ARG FASTEMBED_GPU=0
ARG RERANKER_MODEL_NAME

ENV FASTEMBED_CACHE=/models_cache \
    HF_HOME=/models_cache \
    HUGGINGFACE_HUB_CACHE=/models_cache \
    TRANSFORMERS_CACHE=/models_cache \
    RERANKER_MODEL_NAME=${RERANKER_MODEL_NAME}

RUN mkdir -p /models_cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip

# Install fastembed into builder so we can pre-warm (prewarm is strict for reranker)
RUN if [ "$FASTEMBED_GPU" = "1" ]; then \
      pip install --no-cache-dir numpy==2.2.6 fastembed-gpu==0.7.3; \
    else \
      pip install --no-cache-dir numpy==2.2.6 fastembed==0.7.3; \
    fi

WORKDIR /build
COPY host_reranker.py /build/host_reranker.py

# Pre-warm reranker model in builder so model files are cached in the layer.
# Fail build if prewarm fails (fail-fast).
RUN python - <<'PY'
import os, sys
try:
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    m = TextCrossEncoder(model_name=os.environ.get("RERANKER_MODEL_NAME"))
    # Minimal warmup call to ensure model downloads and API works
    _ = m.rerank("warmup", ["a", "b"])
    print("Prewarm OK")
except Exception as e:
    print("ERROR: reranker pre-warm failed:", e, file=sys.stderr)
    sys.exit(1)
PY

# ---------------------------- FINAL RUNTIME IMAGE ----------------------------
FROM docker.io/library/python:3.14.3-slim@sha256:5e59aae31ff0e87511226be8e2b94d78c58f05216efda3b07dbbed938ec8583b

ARG FASTEMBED_GPU=0
ARG RERANKER_MODEL_NAME

ENV FASTEMBED_CACHE=/models_cache \
    HF_HOME=/models_cache \
    HUGGINGFACE_HUB_CACHE=/models_cache \
    TRANSFORMERS_CACHE=/models_cache \
    RERANKER_MODEL_NAME=${RERANKER_MODEL_NAME} \
    RERANKER_MAX_DOCS=50 \
    RERANKER_CUDA=0 \
    RERANKER_HOST=0.0.0.0 \
    RERANKER_PORT=8202 \
    FASTEMBED_CACHE=/models_cache

RUN mkdir -p /models_cache /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /models_cache /models_cache
COPY --from=builder /build/host_reranker.py /app/host_reranker.py

WORKDIR /app

RUN python -m pip install --upgrade pip

# Install runtime packages (conditional for GPU/CPU)
RUN sh -c ' \
    if [ "$FASTEMBED_GPU" = "1" ]; then \
      pip install --no-cache-dir numpy==2.2.6 fastembed-gpu==0.7.3 fastapi==0.136.1 uvicorn==0.46.0 prometheus-client==0.17.0; \
    else \
      pip install --no-cache-dir numpy==2.2.6 fastembed==0.7.3 fastapi==0.136.1 uvicorn==0.46.0 prometheus-client==0.17.0; \
    fi'

# Bake the authoritative model into the image
RUN printf '%s' "${RERANKER_MODEL_NAME}" > /app/.baked_reranker_model && chmod 644 /app/.baked_reranker_model

# Entrypoint enforces that runtime env does not diverge from baked values
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8202

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
