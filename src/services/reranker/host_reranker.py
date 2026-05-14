#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastembed.rerank.cross_encoder import TextCrossEncoder
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

logging.basicConfig(level=os.getenv("RERANKER_LOGLEVEL", "WARN"))
log = logging.getLogger("host_reranker")

# Configuration
RERANKER_MODEL_NAME = os.getenv("RERANKER_MODEL_NAME", "Xenova/ms-marco-MiniLM-L-6-v2")
LOCAL_RERANKER_MODEL_PATH = os.getenv("LOCAL_RERANKER_MODEL_PATH") or (
    Path("/app/.resolved_model_path").read_text().strip()
    if Path("/app/.resolved_model_path").exists()
    else None
)
RERANKER_HOST = os.getenv("RERANKER_HOST", "0.0.0.0")
RERANKER_PORT = int(os.getenv("RERANKER_PORT", "8202"))
RERANKER_MAX_DOCS = int(os.getenv("RERANKER_MAX_DOCS", "50"))
RERANKER_CUDA = os.getenv("RERANKER_CUDA", "0").upper() in ("1", "TRUE", "YES")
ENV = os.getenv("ENV", "dev")
PRELOAD_MODEL = os.getenv("PRELOAD_MODEL", "1").upper() in ("1", "TRUE", "YES")

# Thread pool for CPU‑bound reranking tasks
_MAX_WORKERS = max(1, os.cpu_count() or 4)
_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

app = FastAPI(title="reranker")

# ------------------ Prometheus Metrics ------------------
_MODEL_LABELS = {"model": RERANKER_MODEL_NAME, "cuda": str(RERANKER_CUDA).lower()}

REQUESTS = Counter(
    "reranker_requests_total",
    "Total number of rerank requests",
    ["model", "cuda", "status"]
)

DURATION = Histogram(
    "reranker_request_duration_seconds",
    "Rerank request latency in seconds",
    ["model", "cuda"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)

IN_PROGRESS = Gauge(
    "reranker_requests_in_progress",
    "Number of rerank requests currently being processed",
    ["model", "cuda"]
)

ERRORS = Counter(
    "reranker_errors_total",
    "Total number of rerank request errors",
    ["model", "cuda", "error_type"]
)

class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1)

class RerankResponse(BaseModel):
    scores: list[float]

# Internal state with thread lock
_MODEL_LOCK = threading.Lock()
_MODEL: TextCrossEncoder | None = None
_MODEL_ERROR: str | None = None
_READY_AT: float | None = None

def _resolve_model_source() -> str:
    if LOCAL_RERANKER_MODEL_PATH and Path(LOCAL_RERANKER_MODEL_PATH).exists():
        return LOCAL_RERANKER_MODEL_PATH
    if Path(RERANKER_MODEL_NAME).exists():
        return RERANKER_MODEL_NAME
    return RERANKER_MODEL_NAME

def _warmup(model: TextCrossEncoder) -> None:
    try:
        _ = list(model.rerank("_init_", ["a", "b"]))
    except TypeError:
        _ = list(model.rerank("_init_", ["a", "b"]))
    except Exception as e:
        raise RuntimeError(f"reranker warmup failed: {e}") from e

def _load_model() -> None:
    """Load the model (must be called under _MODEL_LOCK)."""
    global _MODEL, _MODEL_ERROR, _READY_AT
    try:
        model_source = _resolve_model_source()
        log.info("Loading reranker model (source=%s) cuda=%s", model_source, RERANKER_CUDA)
        if RERANKER_CUDA:
            try:
                _MODEL = TextCrossEncoder(model_name=model_source, providers=["CUDAExecutionProvider"])
            except TypeError:
                _MODEL = TextCrossEncoder(model_name=model_source)
                log.warning("providers kwarg not supported; falling back to default provider")
        else:
            _MODEL = TextCrossEncoder(model_name=model_source)
        _warmup(_MODEL)
        _READY_AT = time.time()
        _MODEL_ERROR = None
        log.info("Reranker model loaded successfully at %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_READY_AT)))
    except Exception as e:
        _MODEL = None
        _READY_AT = None
        _MODEL_ERROR = str(e)
        log.exception("Reranker model load failed: %s", e)

def _load_model_if_needed() -> None:
    """Thread-safe model loading."""
    if _MODEL is not None:
        return
    with _MODEL_LOCK:
        if _MODEL is not None:
            return
        _load_model()

def _do_rerank(query: str, documents: list[str]) -> list[float]:
    _load_model_if_needed()
    if _MODEL is None:
        raise RuntimeError(f"model not loaded: {_MODEL_ERROR or 'unknown error'}")
    scores = list(_MODEL.rerank(query, documents))
    if len(scores) != len(documents):
        raise RuntimeError("score count mismatch")
    return [float(x) for x in scores]

@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="query must be provided")
    if not req.documents or not isinstance(req.documents, list):
        raise HTTPException(status_code=400, detail="documents must be a non-empty list")
    if len(req.documents) > RERANKER_MAX_DOCS:
        raise HTTPException(status_code=400, detail=f"too many documents max={RERANKER_MAX_DOCS}")

    IN_PROGRESS.labels(**_MODEL_LABELS).inc()
    start = time.perf_counter()
    status = "success"
    try:
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(_RERANK_EXECUTOR, _do_rerank, req.query, req.documents)
        return {"scores": scores}
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
        log.exception("rerank failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        elapsed = time.perf_counter() - start
        REQUESTS.labels(**{**_MODEL_LABELS, "status": status}).inc()
        DURATION.labels(**_MODEL_LABELS).observe(elapsed)
        IN_PROGRESS.labels(**_MODEL_LABELS).dec()

@app.get("/health")
def health():
    return {
        "status": "ok" if _MODEL is not None else "not_ready",
        "model": RERANKER_MODEL_NAME,
        "local_model_path": LOCAL_RERANKER_MODEL_PATH,
        "max_docs": RERANKER_MAX_DOCS,
        "cuda": RERANKER_CUDA,
        "ready": _MODEL is not None,
        "ready_at": _READY_AT,
        "model_error": _MODEL_ERROR,
        "env": ENV,
    }

@app.get("/readyz")
def readyz():
    if _MODEL is None and _MODEL_ERROR is None:
        try:
            _load_model_if_needed()
        except Exception:
            pass
    if _MODEL is not None and _READY_AT is not None:
        return {"status": "ready", "ready_at": _READY_AT, "model": RERANKER_MODEL_NAME}
    raise HTTPException(status_code=503, detail={"status": "not_ready", "model_error": _MODEL_ERROR})

@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.on_event("startup")
async def on_startup():
    if PRELOAD_MODEL:
        log.info("PRELOAD_MODEL enabled; loading reranker model at startup (background)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_RERANK_EXECUTOR, _load_model_if_needed)

@app.on_event("shutdown")
async def on_shutdown():
    _RERANK_EXECUTOR.shutdown(wait=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "host_reranker:app",
        host=RERANKER_HOST,
        port=RERANKER_PORT,
        log_level="warn",
        loop="uvloop",
    )