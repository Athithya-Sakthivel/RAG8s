# Pin Python 3.13 slim by digest for reproducibility
FROM python:3.13.12-slim@sha256:f1927c75e81efd1e091dbd64b6c0ecaa5630b38635a3d1c04034ac636e1f94c8

# Build-time toggles for GPU and sparse model config
ARG FASTEMBED_GPU=0
ARG SPARSE_MODEL_NAME=prithivida/Splade_PP_en_v1
ARG SPARSE_BATCH_SIZE=8

# Centralize cache paths and runtime env
ENV FASTEMBED_CACHE=/models_cache \
    HF_HOME=/models_cache \
    HUGGINGFACE_HUB_CACHE=/models_cache \
    TRANSFORMERS_CACHE=/models_cache \
    PYTHONUNBUFFERED=1 \
    SPARSE_MODEL_NAME=${SPARSE_MODEL_NAME} \
    SPARSE_BATCH_SIZE=${SPARSE_BATCH_SIZE} \
    SPARSE_CUDA=0 \
    SPARSE_HOST=0.0.0.0 \
    SPARSE_PORT=8201 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    ORT_NUM_THREADS=1

# Create dirs and non-root user with fixed uid/gid
RUN mkdir -p /models_cache /app /var/log/sparse \
 && groupadd -g 1000 appgroup \
 && useradd -m -u 1000 -g appgroup -s /bin/sh appuser \
 && chown -R appuser:appgroup /models_cache /app /var/log/sparse

# Install minimal runtime deps and patch openssl
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates openssl libssl3 \
 && rm -rf /var/lib/apt/lists/*

# Upgrade pip tooling
RUN python -m pip install --upgrade pip wheel

# Install fastembed stack (cpu/gpu selected at build)
RUN if [ "$FASTEMBED_GPU" = "1" ]; then \
      python -m pip install --no-cache-dir fastembed-gpu==0.8.0 numpy==2.4.4 onnxruntime-gpu==1.25.0 huggingface-hub==1.11.0; \
    else \
      python -m pip install --no-cache-dir fastembed==0.8.0 numpy==2.4.4 onnxruntime==1.25.0 huggingface-hub==1.11.0; \
    fi

# Install API deps, uvloop, and prometheus-client for metrics
RUN python -m pip install --no-cache-dir fastapi==0.136.1 "uvicorn[standard]==0.46.0" uvloop==0.21.0 prometheus-client==0.21.0

# Copy app and entrypoint
WORKDIR /app
COPY host_sparse.py /app/host_sparse.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
 && chown -R appuser:appgroup /app

# Drop to non-root for runtime
USER appuser
ENV HOME=/home/appuser

# Expose HTTP port
EXPOSE 8201

# Probe readiness endpoint for container health
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8201/readyz || exit 1

# Start service via entrypoint
ENTRYPOINT ["sh", "/app/entrypoint.sh"]