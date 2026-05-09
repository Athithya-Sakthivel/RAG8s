# ---------------- BUILDER ----------------
FROM python:3.10-slim AS builder
ENV DEBIAN_FRONTEND=noninteractive
ENV VENV_PATH=/opt/venv
ENV MODEL_HOME=/opt/models
ENV RAPIDOCR_MODEL_DIR=/opt/models/rapidocr
ENV PYTHONUNBUFFERED=1

# build / compile deps needed for python wheels and model utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc g++ clang libclang-dev git curl wget ca-certificates pkg-config \
    libjpeg-dev zlib1g-dev libxml2-dev libxslt-dev libpng-dev libfreetype6-dev \
    libharfbuzz-dev libxcb1-dev libx11-dev libxext-dev libxrender-dev libfontconfig1-dev \
    libopenjp2-7-dev libleptonica-dev libtesseract-dev poppler-utils libmagic1 tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# awscli will be installed here (copied to runtime /usr/local)
RUN pip install --no-cache-dir awscli==1.29.31

WORKDIR /workspace
COPY requirements.txt /workspace/requirements.txt
COPY . /indexing_pipeline

RUN python -m venv ${VENV_PATH}
RUN ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel
RUN mkdir -p /wheels
RUN ${VENV_PATH}/bin/pip wheel -r /workspace/requirements.txt -w /wheels
RUN ${VENV_PATH}/bin/pip install --no-index --find-links=/wheels -r /workspace/requirements.txt

RUN ${VENV_PATH}/bin/pip install --no-cache-dir huggingface_hub

# download faster-whisper artifacts (keeps image reproducible)
RUN /opt/venv/bin/python - <<'PY'
from huggingface_hub import hf_hub_download
from pathlib import Path
import shutil, os, sys

files = ["model.bin","config.json","tokenizer.json","vocabulary.txt","README.md"]
base = Path("/indexing_pipeline/models/faster_whisper/faster-whisper-base")
base.mkdir(parents=True, exist_ok=True)
tmp = Path("/tmp/hf"); tmp.mkdir(parents=True, exist_ok=True)
failed = False
for f in files:
    try:
        src = hf_hub_download("Systran/faster-whisper-base", f, local_dir=str(tmp), local_dir_use_symlinks=False)
        dest = base / f
        if dest.exists():
            try: dest.unlink()
            except Exception: pass
        shutil.move(src, dest)
        try: os.chmod(str(dest), 0o444)
        except Exception: pass
    except Exception as e:
        print("FAILED DOWNLOAD:", f, e, file=sys.stderr)
        failed = True
if failed:
    sys.exit(2)
PY

# download spacy model into workspace models directory
RUN mkdir -p /indexing_pipeline/models/spacy && /opt/venv/bin/python -m spacy download en_core_web_sm --target /indexing_pipeline/models/spacy

# ---------------- RUNTIME ----------------
FROM python:3.10-slim AS runtime
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV VENV_PATH=/opt/venv

# runtime packages required for conversion tools and OCR
# NOTE: libgl1-mesa-glx may be unavailable on some slim images — use libgl1 / libgl1-mesa-dri instead
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 poppler-utils ffmpeg \
    libgl1 libgl1-mesa-dri libglib2.0-0 libsm6 libxrender1 libxext6 \
    tesseract-ocr \
    libreoffice-core libreoffice-writer libreoffice-calc default-jre-headless \
    jq unzip fonts-dejavu-core tini \
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# copy venv, app code and awscli (/usr/local) from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /indexing_pipeline /indexing_pipeline
COPY --from=builder /usr/local /usr/local

# conversion helper
COPY pre_conversions.sh /indexing_pipeline/pre_conversions.sh
RUN chmod +x /indexing_pipeline/pre_conversions.sh

# environment defaults
ENV OVERWRITE_ALL_AUDIO_FILES=${OVERWRITE_ALL_AUDIO_FILES:-false}
ENV OVERWRITE_OTHER_TO_PDF=${OVERWRITE_OTHER_TO_PDF:-true}
ENV OVERWRITE_SPREADSHEETS_WITH_CSV=${OVERWRITE_SPREADSHEETS_WITH_CSV:-false}
ENV MODEL_HOST_PATH=/app/models
ENV PORT=8000
ENV HEALTH_PATH=/health
ENV S3_RAW_PREFIX=data/raw/
ENV S3_CHUNKED_PREFIX=data/chunked/
ENV CHUNK_FORMAT=json
ENV MAX_TOKENS_PER_CHUNK=300
ENV MIN_TOKENS_PER_CHUNK=100
ENV NUMBER_OF_OVERLAPPING_SENTENCES=2
ENV PDF_DISABLE_OCR=false
ENV PDF_OCR_ENGINE=tesseract
ENV PDF_TESSERACT_LANG=eng
ENV PDF_FORCE_OCR=false
ENV PDF_OCR_RENDER_DPI=400
ENV PDF_MIN_IMG_SIZE_BYTES=3072
ENV IMAGE_OCR_ENGINE=tesseract
ENV IMAGE_TESSERACT_LANG=eng
ENV TESSERACT_CMD=/usr/bin/tesseract
ENV TESSERACT_CONFIG="--oem 1 --psm 6"
ENV IMAGE_MIN_IMG_SIZE_BYTES=3072
ENV IMAGE_RENDER_DPI=600
ENV IMAGE_UPSCALE_FACTOR=2.0
ENV IMAGE_ENABLE_WORDSEGMENT=false
ENV TOKEN_ENCODER=cl100k_base
ENV TOKEN_ENCODER_MODEL=cl100k_base
ENV FORCE_PROCESS=false
ENV FORCE_OVERWRITE=false
ENV MODEL_HOME=/opt/models
ENV HF_HOME=/opt/models/hf
ENV HF_HUB_CACHE=/opt/models/hf/hub
ENV HF_ASSETS_CACHE=/opt/models/hf/assets
ENV RAPIDOCR_MODEL_DIR=/opt/models/rapidocr
ENV WORKSPACE_MODELS=/indexing_pipeline/models
ENV FW_MODEL_PATH=/indexing_pipeline/models/faster_whisper/faster-whisper-base
ENV FW_MODEL_BIN=/indexing_pipeline/models/faster_whisper/faster-whisper-base/model.bin

ENV PATH=${VENV_PATH}/bin:$PATH
ENV PYTHONPATH=/indexing_pipeline

WORKDIR /indexing_pipeline
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "indexing_pipeline.py"]
