# syntax=docker/dockerfile:1.7
########################################
# Stage 1 — builder: build wheels and artifacts
########################################
ARG PYTHON_IMAGE=docker.io/library/python:3.12.3-slim@sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63
FROM ${PYTHON_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive
ARG VENV_PATH=/opt/venv
ARG PIP_EXTRA_INDEX_URL=
ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=${VENV_PATH} \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install build-time dependencies only (no runtime -dev packages will be present in final image)
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends \
      build-essential \
      gcc \
      libffi-dev \
      libssl-dev \
      libxml2-dev \
      libxslt-dev \
      zlib1g-dev \
      libsndfile1-dev \
      curl \
      ca-certificates \
      git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Copy requirements first to maximize cache reuse
COPY src/indexing_pipeline/requirements.txt /src/requirements.txt

# Create virtualenv, build wheels into /wheels, then install from that wheelhouse.
RUN python -m venv "${VENV_PATH}" \
 && "${VENV_PATH}/bin/python" -m pip install --upgrade pip setuptools wheel \
 && mkdir -p /wheels \
 && PIP_CACHE_DIR=/tmp/pip-cache "${VENV_PATH}/bin/python" -m pip wheel --wheel-dir=/wheels -r /src/requirements.txt ${PIP_EXTRA_INDEX_URL:+--extra-index-url ${PIP_EXTRA_INDEX_URL}} \
 && "${VENV_PATH}/bin/python" -m pip install --no-deps --no-index --find-links=/wheels -r /src/requirements.txt \
 && rm -rf /tmp/pip-cache

# Copy application source into builder (for build-time steps if needed)
COPY . /indexing_pipeline
WORKDIR /indexing_pipeline

# Create non-root user and set ownership of app and venv directories
RUN useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /indexing_pipeline "${VENV_PATH}" /wheels

########################################
# Stage 2 — runtime: minimal final image with security upgrades
########################################
FROM ${PYTHON_IMAGE} AS runtime

ARG VENV_PATH=/opt/venv
ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=${VENV_PATH} \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Ensure Debian security repository is available and attempt to upgrade vulnerable system libs.
# This will install patched libsqlite3-0 if a security update exists for the base image.
# We avoid installing any -dev packages in runtime.
RUN set -eux; \
    # ensure security repo is present (Bookworm / Debian 12)
    echo "deb http://deb.debian.org/debian bookworm main contrib non-free" > /etc/apt/sources.list.d/bookworm.list; \
    echo "deb http://security.debian.org/debian-security bookworm-security main contrib non-free" > /etc/apt/sources.list.d/bookworm-security.list; \
    apt-get update; \
    # attempt to only-upgrade the system libraries of concern; if no candidate exists this is a no-op
    apt-get install -y --only-upgrade --no-install-recommends libsqlite3-0 zlib1g || true; \
    # install tini and minimal runtime deps
    apt-get install -y --no-install-recommends tini ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Create the same non-root user (UID must match builder)
RUN useradd -m -u 10001 appuser || true

# Copy the prebuilt virtualenv and application files from builder
COPY --from=builder --chown=appuser:appuser ${VENV_PATH} ${VENV_PATH}
COPY --from=builder --chown=appuser:appuser /indexing_pipeline /indexing_pipeline
WORKDIR /indexing_pipeline

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/indexing_pipeline/index.py"]

# Healthcheck: attempt to connect to localhost:8000 (adjust host/port to your service).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python","-c","import socket,sys\ns=socket.socket();s.settimeout(1)\ntry:\n s.connect(('127.0.0.1',8000));s.close();sys.exit(0)\nexcept Exception:\n sys.exit(1)"]
