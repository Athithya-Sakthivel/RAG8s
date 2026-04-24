import logging
import os
import sys
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from fastembed import SparseTextEmbedding
from pydantic import BaseModel

logging.basicConfig(level=os.getenv("SPARSE_LOGLEVEL", "INFO"))
log = logging.getLogger("host_sparse")
SPARSE_MODEL_NAME = os.getenv("SPARSE_MODEL_NAME", "Qdrant/minicoil-v1")
SPARSE_HOST = os.getenv("SPARSE_HOST", "0.0.0.0")
SPARSE_PORT = int(os.getenv("SPARSE_PORT", "8201"))
SPARSE_BATCH_SIZE = int(os.getenv("SPARSE_BATCH_SIZE", "8"))
SPARSE_CUDA = os.getenv("SPARSE_CUDA", "0").upper() in ("1", "TRUE", "YES")
ENV = os.getenv("ENV", "dev")
app = FastAPI(title="sparse-embedder")
class SparseOut(BaseModel):
    indices: list[int]
    values: list[float]
class SparseRequest(BaseModel):
    texts: list[str]
class SparseResponse(BaseModel):
    vectors: list[SparseOut]
SERVICE_NAME = "sparse"
MODEL = None
MODEL_ERROR = None
MODEL_READY_BOOL = False
MODEL_LOAD_TIME_VAL = None
MODEL_LOAD_FAILURES_COUNT = 0
def to_sparse(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {"indices": [], "values": []}
    if isinstance(obj, dict):
        inds = list(map(int, obj.get("indices", [])))
        vals = list(map(float, obj.get("values", [])))
        return {"indices": inds, "values": vals}
    if hasattr(obj, "indices") and hasattr(obj, "values"):
        inds = obj.indices.tolist() if hasattr(obj.indices, "tolist") else list(obj.indices)
        vals = obj.values.tolist() if hasattr(obj.values, "tolist") else list(obj.values)
        return {"indices": [int(x) for x in inds], "values": [float(x) for x in vals]}
    if isinstance(obj, (list, tuple)) and len(obj) == 2:
        inds, vals = obj
        return {"indices": [int(x) for x in inds], "values": [float(x) for x in vals]}
    raise RuntimeError("unsupported sparse object")
t0 = time.time()
try:
    log.info("loading sparse model %s cuda=%s", SPARSE_MODEL_NAME, SPARSE_CUDA)
    if SPARSE_CUDA:
        try:
            MODEL = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME, providers=["CUDAExecutionProvider"])
        except TypeError:
            MODEL = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)
            log.warning("provider arg not supported, falling back to default provider")
    else:
        MODEL = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)
    tmp = list(MODEL.embed(["_init_"], batch_size=1))
    if tmp is None or len(tmp) == 0:
        log.error("sparse init produced no output")
        raise RuntimeError("sparse init failed")
    load_time = time.time() - t0
    MODEL_LOAD_TIME_VAL = load_time
    MODEL_READY_BOOL = True
    log.info("sparse model loaded in %.3fs", load_time)
except Exception as e:
    MODEL = None
    MODEL_ERROR = str(e)
    MODEL_LOAD_FAILURES_COUNT += 1
    MODEL_READY_BOOL = False
    log.exception("sparse model init failed: %s", e)
    sys.exit(1)
@app.post("/embed", response_model=SparseResponse)
def embed(req: SparseRequest):
    endpoint = "/embed"
    start = time.time()
    status_code = 200
    batch_len = len(req.texts) if req.texts is not None else 0
    if batch_len <= 0:
        status_code = 400
        raise HTTPException(status_code=400, detail="texts must be a non-empty list")
    if batch_len > SPARSE_BATCH_SIZE:
        status_code = 400
        raise HTTPException(status_code=400, detail=f"batch too large max={SPARSE_BATCH_SIZE}")
    try:
        gen = MODEL.embed(req.texts, batch_size=len(req.texts))
        out = []
        for s in gen:
            out.append(to_sparse(s))
        return {"vectors": out}
    except HTTPException:
        raise
    except Exception as e:
        status_code = 500
        log.exception("sparse embed failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        elapsed = max(time.time()-start,1e-6)
@app.get("/health")
def health():
    ready = bool(MODEL_READY_BOOL)
    return {"status": "ok" if ready else "not_ready", "model": SPARSE_MODEL_NAME, "ready": ready, "error": MODEL_ERROR}
@app.get("/metrics")
def metrics():
    return Response("metrics disabled\n", media_type="text/plain")
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SPARSE_HOST, port=SPARSE_PORT)
