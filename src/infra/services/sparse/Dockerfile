# Builder stage: install fastembed and pre-warm the sparse model
FROM docker.io/library/python:3.14.3-slim@sha256:5e59aae31ff0e87511226be8e2b94d78c58f05216efda3b07dbbed938ec8583b AS builder

ARG FASTEMBED_GPU=0
ARG SPARSE_MODEL_NAME

# Fail-fast: require build-time model selection
RUN if [ -z "${SPARSE_MODEL_NAME:-}" ]; then echo "SPARSE_MODEL_NAME build-arg is required" >&2 && exit 1; fi

ENV FASTEMBED_CACHE=/models_cache \
    HF_HOME=/models_cache \
    HUGGINGFACE_HUB_CACHE=/models_cache \
    TRANSFORMERS_CACHE=/models_cache \
    SPARSE_MODEL_NAME=${SPARSE_MODEL_NAME}

RUN mkdir -p /models_cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip

# Install fastembed + numpy + huggingface_hub into builder so we can pre-warm
RUN if [ "$FASTEMBED_GPU" = "1" ]; then \
      pip install --no-cache-dir numpy==2.2.6 fastembed-gpu==0.7.3 huggingface_hub; \
    else \
      pip install --no-cache-dir numpy==2.2.6 fastembed==0.7.3 huggingface_hub; \
    fi

WORKDIR /build
COPY host_sparse.py /build/host_sparse.py

# Pre-warm model (best-effort; cached in builder image)
RUN python - <<'PY'
import os, sys
try:
    from fastembed import SparseTextEmbedding
    m = SparseTextEmbedding(model_name=os.environ.get("SPARSE_MODEL_NAME"))
    _ = list(m.embed(["_warmup_"], batch_size=1))
except Exception as e:
    # keep build going; pre-warm is best-effort (remove this catch to make pre-warm strict)
    print("WARNING: sparse model pre-warm failed:", e, file=sys.stderr)
PY

# ---------------------------- FINAL RUNTIME IMAGE ----------------------------
FROM docker.io/library/python:3.14.3-slim@sha256:5e59aae31ff0e87511226be8e2b94d78c58f05216efda3b07dbbed938ec8583b

ARG FASTEMBED_GPU=0
ARG SPARSE_MODEL_NAME

ENV FASTEMBED_CACHE=/models_cache \
    HF_HOME=/models_cache \
    HUGGINGFACE_HUB_CACHE=/models_cache \
    TRANSFORMERS_CACHE=/models_cache \
    SPARSE_MODEL_NAME=${SPARSE_MODEL_NAME} \
    SPARSE_BATCH_SIZE=8 \
    SPARSE_CUDA=0 \
    SPARSE_HOST=0.0.0.0 \
    SPARSE_PORT=8201 \
    FASTEMBED_CACHE=/models_cache

RUN mkdir -p /models_cache /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /models_cache /models_cache
COPY --from=builder /build/host_sparse.py /app/host_sparse.py

WORKDIR /app

RUN python -m pip install --upgrade pip

# Runtime packages
RUN sh -c ' \
    if [ "$FASTEMBED_GPU" = "1" ]; then \
      pip install --no-cache-dir numpy==2.2.6 fastembed-gpu==0.7.3 fastapi==0.136.1 uvicorn==0.46.0 huggingface_hub prometheus-client==0.17.0; \
    else \
      pip install --no-cache-dir numpy==2.2.6 fastembed==0.7.3 fastapi==0.136.1 uvicorn==0.46.0 huggingface_hub prometheus-client==0.17.0; \
    fi'

# Record authoritative baked model into image
RUN printf '%s' "${SPARSE_MODEL_NAME}" > /app/.baked_sparse_model && chmod 644 /app/.baked_sparse_model

# Entrypoint enforces baked model immutability
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8201

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
