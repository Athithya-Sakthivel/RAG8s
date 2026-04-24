# syntax=docker/dockerfile:1.7

########################################
# Stage 1 — builder: install build dependencies and build wheels
# This stage contains all compilation tools and native dev libraries.
# Nothing from this stage (compilers/dev headers) will be present in the final image.
########################################
ARG PYTHON_IMAGE=docker.io/library/python@sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63
FROM ${PYTHON_IMAGE} AS builder

# Build-time arguments
ARG DEBIAN_FRONTEND=noninteractive
ARG VENV_PATH=/opt/venv
ARG PIP_EXTRA_INDEX_URL=
ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=${VENV_PATH} \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

# Use bash for safer shell features and parameter expansion
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install packages required to build wheels for packages like lxml, spacy, soundfile, etc.
# These packages are only needed in the builder stage and are not copied to the runtime image.
RUN apt-get update \
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

# Copy only requirements first to maximize Docker layer cache reuse
COPY requirements.txt /src/requirements.txt

# Create a virtualenv, prebuild wheels into /wheels, then install from that wheelhouse.
# If PIP_EXTRA_INDEX_URL is set, pass it to pip as --extra-index-url.
RUN python -m venv "${VENV_PATH}" \
 && "${VENV_PATH}/bin/python" -m pip install --upgrade pip setuptools wheel \
 && mkdir -p /wheels \
 && PIP_CACHE_DIR=/tmp/pip-cache "${VENV_PATH}/bin/python" -m pip wheel --wheel-dir=/wheels -r /src/requirements.txt ${PIP_EXTRA_INDEX_URL:+--extra-index-url ${PIP_EXTRA_INDEX_URL}} \
 && "${VENV_PATH}/bin/python" -m pip install --no-deps --no-index --find-links=/wheels -r /src/requirements.txt \
 && rm -rf /tmp/pip-cache

# Copy application source into builder so we can run any build-time steps if needed.
COPY . /indexing_pipeline
WORKDIR /indexing_pipeline

# Create a non-root user and set ownership of the app and venv directories.
# This ensures the final runtime files are owned by a non-root user.
RUN useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /indexing_pipeline "${VENV_PATH}" /wheels

########################################
# Stage 2 — runtime: minimal final image
# This stage contains only runtime dependencies and the prebuilt virtualenv.
########################################
FROM ${PYTHON_IMAGE} AS runtime

ARG VENV_PATH=/opt/venv
ENV PYTHONUNBUFFERED=1 \
    VENV_PATH=${VENV_PATH} \
    PATH=${VENV_PATH}/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install tini (PID 1) and minimal runtime packages.
# Keep runtime image small: do not install build tools or dev headers here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Create the same non-root user in the runtime image (UID must match builder)
RUN useradd -m -u 10001 appuser || true

# Copy the prebuilt virtualenv from the builder stage into the runtime image.
# Preserve ownership so the non-root user can run the interpreter and installed packages.
COPY --from=builder --chown=appuser:appuser /opt/venv ${VENV_PATH}

# Copy application files from builder to runtime and set ownership.
COPY --from=builder --chown=appuser:appuser /indexing_pipeline /indexing_pipeline
WORKDIR /indexing_pipeline

# Set locale variables explicitly for consistent behavior in runtime.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Run as non-root user for security.
USER appuser

# Use tini as PID 1 to forward signals and reap zombies.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: run the indexing entrypoint script.
CMD ["python", "/indexing_pipeline/index.py"]

# Healthcheck: attempt to connect to localhost:8000.
# Replace host/port with your service's actual health endpoint if different.
# This probe uses Python's socket module and returns non-zero if the connection fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(1); \
try: s.connect(('127.0.0.1',8000)); s.close(); sys.exit(0) \
except Exception: sys.exit(1)"
