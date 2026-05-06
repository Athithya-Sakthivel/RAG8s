#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastembed.rerank.cross_encoder import TextCrossEncoder
from pydantic import BaseModel, Field

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
PRELOAD_MODEL = os.getenv("PRELOAD_MODEL", "0").upper() in ("1", "TRUE", "YES")

# Thread pool for CPU‑bound reranking tasks
_MAX_WORKERS = max(1, os.cpu_count() or 4)
_RERANK_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

app = FastAPI(title="reranker")


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1)


class RerankResponse(BaseModel):
    scores: list[float]


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
        log.info(
            "Reranker model loaded successfully at %s",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_READY_AT)),
        )
    except Exception as e:
        _MODEL = None
        _READY_AT = None
        _MODEL_ERROR = str(e)
        log.exception("Reranker model load failed: %s", e)


def _load_model_if_needed() -> None:
    if _MODEL is not None:
        return
    # Use a lock to avoid duplicate loads, but since we always call this from
    # the thread pool executor, a simple global check is sufficient.
    # However, there's a tiny race; we can use a threading.Lock if needed.
    # For simplicity, we accept that two threads might try to load simultaneously
    # but the underlying fastembed handles that gracefully (and load is idempotent).
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

    try:
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            _RERANK_EXECUTOR, _do_rerank, req.query, req.documents
        )
        return {"scores": scores}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.exception("rerank failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
    if _MODEL is None:
        # Try to load in background? No, we want readiness to be synchronous,
        # but we can trigger a load in a thread pool (non‑blocking) and return 503.
        # For simplicity, we do not attempt to load here; load only on first request.
        pass

    if _MODEL is not None and _READY_AT is not None:
        return {"status": "ready", "ready_at": _READY_AT, "model": RERANKER_MODEL_NAME}

    raise HTTPException(status_code=503, detail={"status": "not_ready", "model_error": _MODEL_ERROR})


@app.on_event("startup")
async def on_startup():
    if PRELOAD_MODEL:
        log.info("PRELOAD_MODEL enabled; loading reranker model at startup (background)")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_RERANK_EXECUTOR, _load_model)


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