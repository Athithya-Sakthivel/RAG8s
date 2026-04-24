# Use a stable Python 3.13 base
FROM python:3.13.12-slim

ARG FASTEMBED_GPU=0
ARG DENSE_MODEL_NAME
ARG DENSE_DIM

ENV FASTEMBED_CACHE=/models_cache \
    HF_HOME=/models_cache \
    HUGGINGFACE_HUB_CACHE=/models_cache \
    TRANSFORMERS_CACHE=/models_cache \
    PYTHONUNBUFFERED=1 \
    DENSE_MODEL_NAME=${DENSE_MODEL_NAME} \
    DENSE_DIM=${DENSE_DIM} \
    DENSE_BATCH_SIZE=16 \
    DENSE_NORMALIZE=TRUE \
    DENSE_CUDA=0 \
    DENSE_HOST=0.0.0.0 \
    DENSE_PORT=8200 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    ORT_NUM_THREADS=1

# Create app directories and a non-root user early so ownership can be set
RUN mkdir -p /models_cache /app /var/log/dense \
 && groupadd -g 1000 appgroup \
 && useradd -m -u 1000 -g appgroup -s /bin/sh appuser \
 && chown -R appuser:appgroup /models_cache /app /var/log/dense

# Install minimal system deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates jq curl git \
 && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install build-time helpers as root
RUN python -m pip install --upgrade pip wheel

# Install fastembed + runtime deps. Keep versions compatible.
RUN if [ "$FASTEMBED_GPU" = "1" ]; then \
      python -m pip install --no-cache-dir fastembed-gpu==0.8.0 numpy==2.4.4 onnxruntime-gpu==1.25.0 huggingface-hub==1.11.0; \
    else \
      python -m pip install --no-cache-dir fastembed==0.8.0 numpy==2.4.4 onnxruntime==1.25.0 huggingface-hub==1.11.0; \
    fi

RUN python -m pip install --no-cache-dir fastapi==0.136.1 "uvicorn[standard]==0.46.0" orjson==3.11.8 prometheus-client==0.25.0

# Copy application code and entrypoint
WORKDIR /app
COPY host_dense.py /app/host_dense.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
 && chown -R appuser:appgroup /app

# Switch to non-root user for runtime
USER appuser
ENV HOME=/home/appuser

EXPOSE 8200

# Healthcheck: probe the readiness endpoint. Use a short interval and a reasonable timeout.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8200/readyz || exit 1

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
