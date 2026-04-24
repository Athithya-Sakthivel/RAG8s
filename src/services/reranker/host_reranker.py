#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from fastembed.rerank.cross_encoder import TextCrossEncoder
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("RERANKER_LOGLEVEL", "INFO"))
log = logging.getLogger("host_reranker")

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

app = FastAPI(title="reranker")

REQUEST_COUNT = Counter("reranker_requests_total", "Total rerank requests", ["status"])
REQUEST_LATENCY = Histogram("reranker_request_duration_seconds", "Rerank request duration seconds")
DOC_COUNT = Histogram("reranker_documents_per_request", "Documents per rerank request")
MODEL_READY = Gauge("reranker_model_ready", "Whether reranker model is ready")
MODEL_LOAD_TIME = Gauge("reranker_model_load_seconds", "Reranker model load duration seconds")
MODEL_LOAD_FAILURES = Counter("reranker_model_load_failures_total", "Reranker model load failures total")


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1)


class RerankResponse(BaseModel):
    scores: list[float]


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


def _load_model_if_needed() -> None:
    global _MODEL, _MODEL_ERROR, _READY_AT

    if _MODEL is not None:
        return

    with _MODEL_LOCK:
        if _MODEL is not None:
            return

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
            MODEL_READY.set(1)
            MODEL_LOAD_TIME.set(max(time.time() - _READY_AT, 0.0))
            log.info("Reranker model loaded successfully at %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_READY_AT)))
        except Exception as e:
            _MODEL = None
            _READY_AT = None
            _MODEL_ERROR = str(e)
            MODEL_READY.set(0)
            MODEL_LOAD_FAILURES.inc()
            log.exception("Reranker model load failed: %s", e)


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    start = time.time()

    if not req.query or not req.query.strip():
        REQUEST_COUNT.labels(status="bad_request").inc()
        raise HTTPException(status_code=400, detail="query must be provided")

    if not req.documents or not isinstance(req.documents, list):
        REQUEST_COUNT.labels(status="bad_request").inc()
        raise HTTPException(status_code=400, detail="documents must be a non-empty list")

    if len(req.documents) > RERANKER_MAX_DOCS:
        REQUEST_COUNT.labels(status="bad_request").inc()
        raise HTTPException(status_code=400, detail=f"too many documents max={RERANKER_MAX_DOCS}")

    _load_model_if_needed()
    if _MODEL is None:
        REQUEST_COUNT.labels(status="service_unavailable").inc()
        raise HTTPException(status_code=503, detail=f"model not loaded: {_MODEL_ERROR or 'unknown error'}")

    status = "ok"
    try:
        DOC_COUNT.observe(len(req.documents))
        with REQUEST_LATENCY.time():
            scores = list(_MODEL.rerank(req.query, req.documents))
            if len(scores) != len(req.documents):
                status = "error"
                raise HTTPException(status_code=500, detail="score count mismatch")
            return {"scores": [float(x) for x in scores]}
    except HTTPException:
        raise
    except Exception as e:
        status = "error"
        log.exception("rerank failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        REQUEST_COUNT.labels(status=status).inc()
        _ = max(time.time() - start, 1e-6)


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
        try:
            _load_model_if_needed()
        except Exception:
            pass

    if _MODEL is not None and _READY_AT is not None:
        return {"status": "ready", "ready_at": _READY_AT, "model": RERANKER_MODEL_NAME}

    raise HTTPException(status_code=503, detail={"status": "not_ready", "model_error": _MODEL_ERROR})


@app.get("/metrics")
def metrics():
    data = generate_latest()
    return PlainTextResponse(content=data.decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


@app.on_event("startup")
def on_startup():
    if PRELOAD_MODEL:
        log.info("PRELOAD_MODEL enabled; attempting to load reranker model at startup")

        def _bg_load():
            try:
                _load_model_if_needed()
            except Exception as e:
                log.exception("Background reranker preload failed: %s", e)

        t = threading.Thread(target=_bg_load, daemon=True)
        t.start()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "host_reranker:app",
        host=os.getenv("RERANKER_HOST", "0.0.0.0"),
        port=int(os.getenv("RERANKER_PORT", "8202")),
        log_level="info",
    )
