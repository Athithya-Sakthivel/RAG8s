FROM python:3.12.3-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    VENV_PATH=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/indexing_pipeline

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    git-lfs \
    unzip \
    jq \
    tini \
    util-linux \
    ffmpeg \
    sox \
    libsndfile1 \
    libmagic1 \
    poppler-utils \
    tesseract-ocr \
    libleptonica-dev \
    libtesseract-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/local/bin/python3 /usr/bin/python || true

WORKDIR /src

COPY requirements.txt /src/requirements.txt
RUN python -m venv /opt/venv \
 && pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /src/requirements.txt

COPY . /indexing_pipeline
WORKDIR /indexing_pipeline

RUN useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /indexing_pipeline /opt/venv

USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/indexing_pipeline/index.py"]
