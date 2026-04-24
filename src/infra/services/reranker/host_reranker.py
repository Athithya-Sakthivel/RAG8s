import asyncio
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("RERANKER_LOGLEVEL", "INFO"))
log = logging.getLogger("host_reranker")

RERANKER_MODEL_NAME = os.getenv("RERANKER_MODEL_NAME", "Xenova/ms-marco-MiniLM-L-6-v2")
RERANKER_HOST = os.getenv("RERANKER_HOST", "0.0.0.0")
RERANKER_PORT = int(os.getenv("RERANKER_PORT", "8202"))
RERANKER_MAX_DOCS = int(os.getenv("RERANKER_MAX_DOCS", "50"))
RERANKER_CUDA = os.getenv("RERANKER_CUDA", "0").upper() in ("1", "TRUE", "YES")
ENV = os.getenv("ENV", "dev")

app = FastAPI(title="reranker")

class RerankRequest(BaseModel):
    query: str
    documents: list[str]

class RerankResponse(BaseModel):
    scores: list[float]

# No-op metrics (preserve calls without prometheus dependency)
class _ValueHolder:
    def __init__(self, inst): self.inst = inst
    def get(self): return getattr(self.inst, "_val", None)
class _MetricInstance:
    def __init__(self): self._val = None; self._value = _ValueHolder(self)
    def inc(self, n=1): return None
    def observe(self, v): return None
    def set(self, v): self._val = v
class NoopMetric:
    def __init__(self): self._store = {}
    def labels(self, **kw):
        key = tuple(sorted(kw.items()))
        inst = self._store.get(key)
        if not inst:
            inst = _MetricInstance(); self._store[key] = inst
        return inst

LABELS = ["service", "env", "endpoint", "status_code"]
REQUEST_COUNT = NoopMetric()
REQUEST_LATENCY = NoopMetric()
RERANK_DOC_COUNT = NoopMetric()
MODEL_READY = NoopMetric()
MODEL_LOAD_TIME = NoopMetric()
MODEL_LOAD_FAILURES = NoopMetric()

SERVICE_NAME = "reranker"
MODEL = None
MODEL_ERROR = None

MODEL_READY.labels(service=SERVICE_NAME, env=ENV).set(0)

async def load_model():
    global MODEL, MODEL_ERROR
    t0 = time.time()
    try:
        log.info("background loading reranker %s cuda=%s", RERANKER_MODEL_NAME, RERANKER_CUDA)
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        if RERANKER_CUDA:
            try:
                MODEL = TextCrossEncoder(model_name=RERANKER_MODEL_NAME, providers=["CUDAExecutionProvider"])
            except TypeError:
                MODEL = TextCrossEncoder(model_name=RERANKER_MODEL_NAME)
        else:
            MODEL = TextCrossEncoder(model_name=RERANKER_MODEL_NAME)
        _ = MODEL.rerank("init", ["a", "b"])
        load_time = time.time() - t0
        MODEL_LOAD_TIME.labels(service=SERVICE_NAME, env=ENV).set(load_time)
        MODEL_READY.labels(service=SERVICE_NAME, env=ENV).set(1)
        log.info("reranker ready (loaded in %.3fs)", load_time)
    except Exception as e:
        MODEL_ERROR = str(e)
        MODEL_LOAD_FAILURES.labels(service=SERVICE_NAME, env=ENV).inc()
        MODEL_READY.labels(service=SERVICE_NAME, env=ENV).set(0)
        log.exception("reranker load failed: %s", e)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(load_model())

def metric_labels(endpoint: str, status_code: int):
    return {"service": SERVICE_NAME, "env": ENV, "endpoint": endpoint, "status_code": str(status_code)}

@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    endpoint = "/rerank"
    start = time.time()
    status_code = 200
    if not req.query:
        status_code = 400
        REQUEST_COUNT.labels(**metric_labels(endpoint, status_code)).inc()
        REQUEST_LATENCY.labels(**metric_labels(endpoint, status_code)).observe(max(time.time() - start, 1e-6))
        raise HTTPException(status_code=400, detail="query must be provided")
    if not req.documents or len(req.documents) == 0:
        status_code = 400
        REQUEST_COUNT.labels(**metric_labels(endpoint, status_code)).inc()
        REQUEST_LATENCY.labels(**metric_labels(endpoint, status_code)).observe(max(time.time() - start, 1e-6))
        raise HTTPException(status_code=400, detail="documents must be a non-empty list")
    if len(req.documents) > RERANKER_MAX_DOCS:
        status_code = 400
        REQUEST_COUNT.labels(**metric_labels(endpoint, status_code)).inc()
        REQUEST_LATENCY.labels(**metric_labels(endpoint, status_code)).observe(max(time.time() - start, 1e-6))
        raise HTTPException(status_code=400, detail=f"too many documents max={RERANKER_MAX_DOCS}")
    if MODEL is None or bool(MODEL_READY.labels(service=SERVICE_NAME, env=ENV)._value.get()) is False:
        status_code = 503
        REQUEST_COUNT.labels(**metric_labels(endpoint, status_code)).inc()
        REQUEST_LATENCY.labels(**metric_labels(endpoint, status_code)).observe(max(time.time() - start, 1e-6))
        raise HTTPException(status_code=503, detail=f"model not ready error={MODEL_ERROR}")
    try:
        RERANK_DOC_COUNT.labels(service=SERVICE_NAME, env=ENV).observe(len(req.documents))
        raw = MODEL.rerank(req.query, req.documents)
        scores = [float(x) for x in raw]
        return {"scores": scores}
    except HTTPException:
        raise
    except Exception as e:
        status_code = 500
        log.exception("rerank failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        elapsed = max(time.time() - start, 1e-6)
        REQUEST_COUNT.labels(**metric_labels(endpoint, status_code)).inc()
        REQUEST_LATENCY.labels(**metric_labels(endpoint, status_code)).observe(elapsed)

@app.get("/health")
def health():
    ready = bool(MODEL_READY.labels(service=SERVICE_NAME, env=ENV)._value.get())
    return {"status": "ok" if ready else "not_ready", "model": RERANKER_MODEL_NAME, "ready": ready, "error": MODEL_ERROR}

@app.get("/metrics")
def metrics():
    return Response("", media_type="text/plain")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=RERANKER_HOST, port=RERANKER_PORT)
