#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastembed import SparseTextEmbedding
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().upper() in ("1", "TRUE", "YES", "ON")


def _env_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except Exception:
        return int(default)


LOG_LEVEL = getattr(logging, os.getenv("SPARSE_LOGLEVEL", "WARN").upper(), logging.INFO)
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("host_sparse")

# Configuration
SPARSE_MODEL_NAME = os.getenv("SPARSE_MODEL_NAME", "prithivida/Splade_PP_en_v1")
LOCAL_SPARSE_MODEL_PATH = os.getenv("LOCAL_SPARSE_MODEL_PATH") or (
    Path("/app/.resolved_model_path").read_text().strip()
    if Path("/app/.resolved_model_path").exists()
    else None
)
SPARSE_HOST = os.getenv("SPARSE_HOST", "0.0.0.0")
SPARSE_PORT = _env_int("SPARSE_PORT", "8201")
SPARSE_BATCH_SIZE = max(1, _env_int("SPARSE_BATCH_SIZE", "8"))
SPARSE_CUDA = _env_bool("SPARSE_CUDA", "0")
ENV = os.getenv("ENV", "dev")
PRELOAD_MODEL = _env_bool("PRELOAD_MODEL", "0")

# Thread pool for CPU‑bound embedding tasks
_MAX_WORKERS = max(1, os.cpu_count() or 4)
_EMBED_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

app = FastAPI(title="sparse-embedder")

# ------------------ Prometheus Metrics ------------------
_MODEL_LABELS = {"model": SPARSE_MODEL_NAME, "cuda": str(SPARSE_CUDA).lower()}

REQUESTS = Counter(
    "sparse_requests_total",
    "Total number of sparse embed requests",
    ["model", "cuda", "status"]
)

DURATION = Histogram(
    "sparse_request_duration_seconds",
    "Sparse embed request latency in seconds",
    ["model", "cuda"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)

IN_PROGRESS = Gauge(
    "sparse_requests_in_progress",
    "Number of sparse embed requests currently being processed",
    ["model", "cuda"]
)

ERRORS = Counter(
    "sparse_errors_total",
    "Total number of sparse embed request errors",
    ["model", "cuda", "error_type"]
)


class SparseOut(BaseModel):
    indices: list[int]
    values: list[float]


class SparseRequest(BaseModel):
    texts: list[str]


class SparseResponse(BaseModel):
    vectors: list[SparseOut]


_MODEL_LOCK = threading.Lock()
_MODEL: SparseTextEmbedding | None = None
_MODEL_ERROR: str | None = None
_READY_AT: float | None = None


def _resolve_model_source() -> str:
    if LOCAL_SPARSE_MODEL_PATH and Path(LOCAL_SPARSE_MODEL_PATH).exists():
        return LOCAL_SPARSE_MODEL_PATH
    if Path(SPARSE_MODEL_NAME).exists():
        return SPARSE_MODEL_NAME
    return SPARSE_MODEL_NAME


def _to_int_list(values: Any) -> list[int]:
    if values is None:
        return []
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [int(x) for x in values]


def _to_float_list(values: Any) -> list[float]:
    if values is None:
        return []
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [float(x) for x in values]


def to_sparse(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {"indices": [], "values": []}

    if isinstance(obj, dict):
        return {
            "indices": _to_int_list(obj.get("indices", [])),
            "values": _to_float_list(obj.get("values", [])),
        }

    if hasattr(obj, "indices") and hasattr(obj, "values"):
        return {
            "indices": _to_int_list(obj.indices),
            "values": _to_float_list(obj.values),
        }

    if isinstance(obj, (list, tuple)) and len(obj) == 2:
        inds, vals = obj
        return {
            "indices": _to_int_list(inds),
            "values": _to_float_list(vals),
        }

    raise RuntimeError("unsupported sparse object")


def _warmup(model: SparseTextEmbedding) -> None:
    try:
        out = list(model.embed(["_init_"], batch_size=1))
    except TypeError:
        out = list(model.embed(["_init_"]))
    if not out:
        raise RuntimeError("sparse init produced no output")


def _load_model_if_needed() -> None:
    global _MODEL, _MODEL_ERROR, _READY_AT

    if _MODEL is not None:
        return

    with _MODEL_LOCK:
        if _MODEL is not None:
            return

        try:
            model_source = _resolve_model_source()
            log.info("Loading sparse model (source=%s) cuda=%s", model_source, SPARSE_CUDA)

            if SPARSE_CUDA:
                try:
                    _MODEL = SparseTextEmbedding(model_name=model_source, providers=["CUDAExecutionProvider"])
                except TypeError:
                    _MODEL = SparseTextEmbedding(model_name=model_source)
                    log.warning("providers kwarg not supported; falling back to default provider")
            else:
                _MODEL = SparseTextEmbedding(model_name=model_source)

            _warmup(_MODEL)
            _READY_AT = time.time()
            _MODEL_ERROR = None
            log.info(
                "Sparse model loaded successfully at %s",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_READY_AT)),
            )
        except Exception as e:
            _MODEL = None
            _READY_AT = None
            _MODEL_ERROR = str(e)
            log.exception("Sparse model load failed: %s", e)


def _do_embed(texts: list[str]) -> list[dict[str, Any]]:
    """Synchronous embedding work to be run in thread pool."""
    _load_model_if_needed()
    if _MODEL is None:
        raise RuntimeError(f"model not loaded: {_MODEL_ERROR or 'unknown error'}")

    try:
        gens = _MODEL.embed(texts, batch_size=min(len(texts), SPARSE_BATCH_SIZE))
    except TypeError:
        gens = _MODEL.embed(texts)

    vecs: list[dict[str, Any]] = []
    for s in gens:
        vecs.append(to_sparse(s))

    if len(vecs) != len(texts):
        raise RuntimeError("embedding count mismatch")
    return vecs


@app.post("/embed", response_model=SparseResponse)
async def embed(req: SparseRequest):
    if not req.texts or not isinstance(req.texts, list):
        raise HTTPException(status_code=400, detail="texts must be a non-empty list")

    if len(req.texts) > SPARSE_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"batch too large max={SPARSE_BATCH_SIZE}")

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
        log.exception("sparse embed failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        elapsed = time.perf_counter() - start
        REQUESTS.labels(**{**_MODEL_LABELS, "status": status}).inc()
        DURATION.labels(**_MODEL_LABELS).observe(elapsed)
        IN_PROGRESS.labels(**_MODEL_LABELS).dec()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": SPARSE_MODEL_NAME,
        "local_model_path": LOCAL_SPARSE_MODEL_PATH,
        "batch_size": SPARSE_BATCH_SIZE,
        "cuda": SPARSE_CUDA,
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
        return {"status": "ready", "ready_at": _READY_AT, "model": SPARSE_MODEL_NAME}

    raise HTTPException(status_code=503, detail={"status": "not_ready", "model_error": _MODEL_ERROR})


@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.on_event("startup")
async def on_startup():
    if PRELOAD_MODEL:
        log.info("PRELOAD_MODEL enabled; attempting to load sparse model at startup (background)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_EMBED_EXECUTOR, _load_model_if_needed)


@app.on_event("shutdown")
async def on_shutdown():
    _EMBED_EXECUTOR.shutdown(wait=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "host_sparse:app",
        host=SPARSE_HOST,
        port=SPARSE_PORT,
        log_level="warn",
        loop="uvloop",
    )