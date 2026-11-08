import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

# Logging
logging.basicConfig(level=os.getenv("DENSE_LOGLEVEL", "WARN"))
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

# Thread pool for CPU-bound embedding tasks
_MAX_WORKERS = max(1, os.cpu_count() or 4)
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

# ------------------ Prometheus Metrics ------------------
_MODEL_LABELS = {"model": DENSE_MODEL_NAME, "cuda": str(DENSE_CUDA).lower()}

REQUESTS = Counter(
    "dense_requests_total",
    "Total number of embed requests",
    ["model", "cuda", "status"]
)

DURATION = Histogram(
    "dense_request_duration_seconds",
    "Embed request latency in seconds",
    ["model", "cuda"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)

IN_PROGRESS = Gauge(
    "dense_requests_in_progress",
    "Number of embed requests currently being processed",
    ["model", "cuda"]
)

ERRORS = Counter(
    "dense_errors_total",
    "Total number of embed request errors",
    ["model", "cuda", "error_type"]
)

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

def _do_embed(texts: list[str]) -> list[list[float]]:
    """Synchronous embedding work to be run in thread pool."""
    global _MODEL, _MODEL_ERROR, _READY_AT

    _load_model_if_needed()
    if _MODEL is None:
        raise RuntimeError(f"model not loaded: {_MODEL_ERROR or 'unknown error'}")

    gens = _MODEL.embed(texts)
    vecs = []
    for a in gens:
        if hasattr(a, "astype"):
            v = a.astype(float).tolist()
        else:
            v = [float(x) for x in a]
        if DENSE_NORMALIZE:
            v = _l2_normalize(v)
        if len(v) != DENSE_DIM:
            log.error("embedding dimension mismatch (expected %d got %d)", DENSE_DIM, len(v))
            raise RuntimeError("embedding dimension mismatch")
        vecs.append([float(x) for x in v])
    return vecs

@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    """Async endpoint that offloads embedding to a thread pool."""
    if not req.texts or not isinstance(req.texts, list):
        raise HTTPException(status_code=400, detail="texts must be a non-empty list")
    if len(req.texts) > DENSE_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"batch too large max={DENSE_BATCH_SIZE}")

    IN_PROGRESS.labels(**_MODEL_LABELS).inc()
    start = time.perf_counter()
    status = "success"
    try:
        loop = asyncio.get_running_loop()
        vecs = await loop.run_in_executor(_EMBED_EXECUTOR, _do_embed, req.texts)
        return {"vectors": vecs}
    except HTTPException:
        status = "client_error"
        raise
    except RuntimeError as e:
        status = "model_error"
        ERRORS.labels(**{**_MODEL_LABELS, "error_type": "runtime"}).inc()
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        status = "server_error"
        ERRORS.labels(**{**_MODEL_LABELS, "error_type": "exception"}).inc()
        log.exception("embed failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        elapsed = time.perf_counter() - start
        REQUESTS.labels(**{**_MODEL_LABELS, "status": status}).inc()
        DURATION.labels(**_MODEL_LABELS).observe(elapsed)
        IN_PROGRESS.labels(**_MODEL_LABELS).dec()
        log.debug("Embed request completed in %.3fs", elapsed)

@app.get("/health")
def health():
    """Lightweight liveness/config endpoint. Does not force model load."""
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
    Readiness probe: attempts to ensure the model is loaded.
    Returns 200 when the model is loaded and warmed up.
    Returns 503 when the model is not ready.
    """
    if _MODEL is None and _MODEL_ERROR is None:
        try:
            _load_model_if_needed()
        except Exception:
            pass

    if _MODEL is not None and _READY_AT is not None:
        return {"status": "ready", "ready_at": _READY_AT, "model": DENSE_MODEL_NAME}
    raise HTTPException(status_code=503, detail={"status": "not_ready", "model_error": _MODEL_ERROR})

@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.on_event("startup")
async def on_startup():
    """Preload model in background if PRELOAD_MODEL is set."""
    if PRELOAD_MODEL:
        log.info("PRELOAD_MODEL enabled; attempting to load model at startup (background)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_EMBED_EXECUTOR, _load_model_if_needed)

@app.on_event("shutdown")
async def on_shutdown():
    """Clean up thread pool executor."""
    _EMBED_EXECUTOR.shutdown(wait=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "host_dense:app",
        host=os.getenv("DENSE_HOST", "0.0.0.0"),
        port=int(os.getenv("DENSE_PORT", "8200")),
        log_level="warn",
        loop="uvloop",
    )