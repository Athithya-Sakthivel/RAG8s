# syntax=docker/dockerfile:1.7
########################################
# Stage 1 — builder: install build deps and build wheels
########################################
ARG PYTHON_IMAGE=python:3.14-slim@sha256:3989a23fd2c28a34c7be819e488b958a10601d421ac25bea1e7a5d757365e2d5
FROM ${PYTHON_IMAGE} AS builder

# Build-time args for reproducibility
ARG DEBIAN_FRONTEND=noninteractive
ARG VENV_PATH=/opt/venv
ARG PIP_EXTRA_INDEX_URL=
ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=${VENV_PATH} \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

# Install system build deps only in builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      gcc \
      libffi-dev \
      libssl-dev \
      libxml2-dev \
      libxslt-dev \
      zlib1g-dev \
      curl \
      ca-certificates \
      git \
 && rm -rf /var/lib/apt/lists/*


WORKDIR /src

# Copy only requirements first to leverage Docker cache
COPY requirements.txt /src/requirements.txt

# Create virtualenv and pre-build wheels into a local wheelhouse
RUN python -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
 && mkdir -p /wheels \
 && PIP_CACHE_DIR=/tmp/pip-cache ${VENV_PATH}/bin/pip wheel --wheel-dir=/wheels -r /src/requirements.txt ${PIP_EXTRA_INDEX_URL} \
 && ${VENV_PATH}/bin/pip install --no-deps --no-index --find-links=/wheels -r /src/requirements.txt \
 && rm -rf /tmp/pip-cache

# Copy application source for any build-time steps (if needed)
COPY . /indexing_pipeline
WORKDIR /indexing_pipeline

# Fix permissions for later copy
RUN useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /indexing_pipeline ${VENV_PATH} /wheels

########################################
# Stage 2 — runtime: minimal final image
########################################
FROM ${PYTHON_IMAGE} AS runtime

ARG VENV_PATH=/opt/venv
ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=${VENV_PATH} \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

# Install tini and minimal runtime deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Create non-root user early
RUN useradd -m -u 10001 appuser || true

# Copy virtualenv from builder (preserves compiled wheels and installed packages)
COPY --from=builder --chown=appuser:appuser /opt/venv ${VENV_PATH}

# Copy only the runtime application files
COPY --from=builder --chown=appuser:appuser /indexing_pipeline /indexing_pipeline
WORKDIR /indexing_pipeline

# Set a small, explicit umask and locale if needed
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Security: drop capabilities and set a non-root user
USER appuser

# Entrypoint and default command
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/indexing_pipeline/index.py"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys, socket; sys.exit(0)" || exit 1
