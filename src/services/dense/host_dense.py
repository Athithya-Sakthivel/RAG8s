import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastembed import TextEmbedding
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel

# Logging
logging.basicConfig(level=os.getenv("DENSE_LOGLEVEL", "INFO"))
log = logging.getLogger("host_dense")

# Configuration from env
DENSE_MODEL_NAME = os.getenv("DENSE_MODEL_NAME", "BAAI/bge-small-en-v1.5")
LOCAL_DENSE_MODEL_PATH = os.getenv("LOCAL_DENSE_MODEL_PATH") or (
    Path("/app/.resolved_model_path").read_text().strip()
    if Path("/app/.resolved_model_path").exists()
    else None
)
DENSE_DIM = int(os.getenv("DENSE_DIM", "384"))
DENSE_BATCH_SIZE = int(os.getenv("DENSE_BATCH_SIZE", "16"))
DENSE_NORMALIZE = os.getenv("DENSE_NORMALIZE", "TRUE").upper() in ("1", "TRUE", "YES")
DENSE_CUDA = os.getenv("DENSE_CUDA", "0").upper() in ("1", "TRUE", "YES")
ENV = os.getenv("ENV", "dev")
PRELOAD_MODEL = os.getenv("PRELOAD_MODEL", "0").upper() in ("1", "TRUE", "YES")

# App
app = FastAPI(title="dense-embedder")

# Prometheus metrics
REQUEST_COUNTER = Counter("dense_requests_total", "Total embed requests", ["status"])
REQUEST_DURATION = Histogram("dense_request_duration_seconds", "Embed request duration seconds")

# Models
class EmbedRequest(BaseModel):
    texts: list[str]

class EmbedResponse(BaseModel):
    vectors: list[list[float]]

# Internal state
_MODEL_LOCK = threading.Lock()
_MODEL: TextEmbedding | None = None
_MODEL_ERROR: str | None = None
_READY_AT: float | None = None

def _l2_normalize(v: list[float]) -> list[float]:
    a = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(a)
    if n > 0:
        a = a / n
    return a.astype(float).tolist()

def _resolve_model_source() -> str:
    # Prefer explicit local path, then treat DENSE_MODEL_NAME as repo id or local path
    if LOCAL_DENSE_MODEL_PATH and Path(LOCAL_DENSE_MODEL_PATH).exists():
        return LOCAL_DENSE_MODEL_PATH
    if Path(DENSE_MODEL_NAME).exists():
        return DENSE_MODEL_NAME
    return DENSE_MODEL_NAME

def _load_model_if_needed():
    global _MODEL, _MODEL_ERROR, _READY_AT
    if _MODEL is not None:
        return
    with _MODEL_LOCK:
        if _MODEL is not None:
            return
        try:
            model_source = _resolve_model_source()
            log.info("Loading model (source=%s) cuda=%s", model_source, DENSE_CUDA)
            if DENSE_CUDA:
                try:
                    _MODEL = TextEmbedding(model_name=model_source, providers=["CUDAExecutionProvider"])
                except TypeError:
                    # older fastembed may not accept providers kwarg
                    _MODEL = TextEmbedding(model_name=model_source)
                    log.warning("providers kwarg not supported; falling back to default provider")
            else:
                _MODEL = TextEmbedding(model_name=model_source)
            # warm up
            _ = list(_MODEL.embed(["_init_"]))
            _READY_AT = time.time()
            log.info("Model loaded successfully at %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_READY_AT)))
            _MODEL_ERROR = None
        except Exception as e:
            _MODEL = None
            _MODEL_ERROR = str(e)
            log.exception("Model load failed: %s", e)

@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    start = time.time()
    if not req.texts or not isinstance(req.texts, list):
        REQUEST_COUNTER.labels(status="bad_request").inc()
        raise HTTPException(status_code=400, detail="texts must be a non-empty list")
    if len(req.texts) > DENSE_BATCH_SIZE:
        REQUEST_COUNTER.labels(status="bad_request").inc()
        raise HTTPException(status_code=400, detail=f"batch too large max={DENSE_BATCH_SIZE}")

    # Ensure model is loaded lazily
    _load_model_if_needed()
    if _MODEL is None:
        REQUEST_COUNTER.labels(status="service_unavailable").inc()
        raise HTTPException(status_code=503, detail=f"model not loaded: {_MODEL_ERROR or 'unknown error'}")

    try:
        with REQUEST_DURATION.time():
            gens = _MODEL.embed(req.texts)
            vecs = []
            for a in gens:
                if hasattr(a, "astype"):
                    v = a.astype(float).tolist()
                else:
                    v = [float(x) for x in a]
                if DENSE_NORMALIZE:
                    v = _l2_normalize(v)
                if len(v) != DENSE_DIM:
                    log.exception("embedding dimension mismatch (expected %d got %d)", DENSE_DIM, len(v))
                    REQUEST_COUNTER.labels(status="error").inc()
                    raise HTTPException(status_code=500, detail="embedding dimension mismatch")
                vecs.append([float(x) for x in v])
        REQUEST_COUNTER.labels(status="ok").inc()
        return {"vectors": vecs}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("embed failed: %s", e)
        REQUEST_COUNTER.labels(status="error").inc()
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        _ = max(time.time() - start, 1e-6)

@app.get("/health")
def health():
    # Lightweight liveness/config endpoint. Do not force model load here.
    return {
        "status": "ok",
        "model": DENSE_MODEL_NAME,
        "local_model_path": LOCAL_DENSE_MODEL_PATH,
        "dim": DENSE_DIM,
        "normalize": DENSE_NORMALIZE,
        "cuda": DENSE_CUDA,
        "model_error": _MODEL_ERROR,
        "env": ENV,
    }

@app.get("/readyz")
def readyz():
    """
    Readiness probe: attempt to ensure the model is loaded.
    Returns 200 when the model is loaded and warmed up.
    Returns 503 when the model is not ready; includes model_error for observability.
    """
    # Try to load model lazily if not already loaded
    if _MODEL is None and _MODEL_ERROR is None:
        # attempt to load; do not block indefinitely
        try:
            _load_model_if_needed()
        except Exception:
            # _load_model_if_needed logs exceptions
            pass

    if _MODEL is not None and _READY_AT is not None:
        return {"status": "ready", "ready_at": _READY_AT, "model": DENSE_MODEL_NAME}
    # Not ready
    raise HTTPException(status_code=503, detail={"status": "not_ready", "model_error": _MODEL_ERROR})

@app.get("/metrics")
def metrics():
    # Expose Prometheus metrics
    data = generate_latest()
    return PlainTextResponse(content=data.decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

# Optionally preload model on startup if PRELOAD_MODEL is set
@app.on_event("startup")
def on_startup():
    if PRELOAD_MODEL:
        log.info("PRELOAD_MODEL enabled; attempting to load model at startup")
        # Load in a background thread to avoid blocking the event loop startup
        def _bg_load():
            try:
                _load_model_if_needed()
            except Exception as e:
                log.exception("Background model preload failed: %s", e)
        t = threading.Thread(target=_bg_load, daemon=True)
        t.start()

# Allow running with `python host_dense.py` for local dev
if __name__ == "__main__":
    import uvicorn
    # Do not pre-load model on startup in __main__ to keep startup fast for local dev.
    uvicorn.run("host_dense:app", host=os.getenv("DENSE_HOST", "0.0.0.0"), port=int(os.getenv("DENSE_PORT", "8200")), log_level="info")
