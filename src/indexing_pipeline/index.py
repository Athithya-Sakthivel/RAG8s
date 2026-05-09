# Load chunk files from S3, normalize their schema, and prepare them for Qdrant.
# Build dense and sparse embedding clients with retries and health checks.
# Create the Qdrant collection once, with optional scalar quantization for vectors.
# Create payload indexes only for filter-heavy metadata fields.
# Read parquet or JSON chunk rows from S3 and convert them into Qdrant points.
# Embed text in batches, fall back on retries, and split batches when needed.
# Upsert points to Qdrant, then print a final JSON summary for the pipeline.

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import signal
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import boto3
import httpx
import numpy as np
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, ReadTimeoutError
from qdrant_client import QdrantClient, models
from qdrant_client.models import PointStruct, SparseVector

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception as e:
    print(
        json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": "error",
                "event": "startup",
                "msg": "pyarrow missing",
                "error": str(e),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    raise SystemExit(2) from e

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip() or None
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "default_rag_collection1").strip()

AWS_REGION = os.getenv("AWS_REGION", "").strip()
DATA_S3_BUCKET = os.getenv("DATA_S3_BUCKET", "").strip()
DATA_S3_PREFIX = os.getenv("DATA_S3_PREFIX", "data/chunked/").strip().lstrip("/")
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "").strip() or None

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333").strip()
DENSE_URL = os.getenv("DENSE_URL", "http://dense-svc.models.svc.cluster.local:8200").strip()
SPARSE_URL = os.getenv("SPARSE_URL", "http://sparse-svc.models.svc.cluster.local:8201").strip()

DENSE_DIM = max(1, int(os.getenv("DENSE_DIM", "384") or 384))
BATCH_SIZE = max(1, int(os.getenv("BATCH_SIZE", "16") or 16))
UPSERT_CHUNK = max(1, int(os.getenv("UPSERT_CHUNK", "500") or 500))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10.0") or 10.0)
DENSE_EMBED_TIMEOUT = float(os.getenv("DENSE_EMBED_TIMEOUT", str(max(HTTP_TIMEOUT, 30.0))) or max(HTTP_TIMEOUT, 30.0))
SPARSE_EMBED_TIMEOUT = float(os.getenv("SPARSE_EMBED_TIMEOUT", str(max(HTTP_TIMEOUT, 30.0))) or max(HTTP_TIMEOUT, 30.0))
EMBED_RETRIES = max(0, int(os.getenv("EMBED_RETRIES", "3") or 3))
EMBED_BACKOFF_BASE = float(os.getenv("EMBED_BACKOFF_BASE", "1.0") or 1.0)
NETWORK_RETRY_COUNT = max(1, int(os.getenv("NETWORK_RETRY_COUNT", "5") or 5))
NETWORK_RETRY_BACKOFF_BASE = float(os.getenv("NETWORK_RETRY_BACKOFF_BASE", "1.0") or 1.0)
NETWORK_RETRY_BACKOFF_MAX = float(os.getenv("NETWORK_RETRY_BACKOFF_MAX", "30.0") or 30.0)
SPARSE_BATCH_FALLBACK = max(1, int(os.getenv("SPARSE_BATCH_FALLBACK", "8") or 8))

QDRANT_HNSW_EF_CONSTRUCT = int(os.getenv("QDRANT_HNSW_EF_CONSTRUCT", "128") or 128)
QDRANT_HNSW_M = int(os.getenv("QDRANT_HNSW_M", "32") or 32)
QDRANT_HNSW_FULL_SCAN_THRESHOLD = int(os.getenv("QDRANT_HNSW_FULL_SCAN_THRESHOLD", "10000") or 10000)
QDRANT_ONDISK = os.getenv("QDRANT_ONDISK", "false").strip().lower() in ("1", "true", "yes", "y", "on")
QDRANT_ENABLE_SCALAR_QUANTIZATION = os.getenv("QDRANT_ENABLE_SCALAR_QUANTIZATION", "true").strip().lower() in ("1", "true", "yes", "y", "on")
QDRANT_QUANTIZATION_ALWAYS_RAM = os.getenv("QDRANT_QUANTIZATION_ALWAYS_RAM", "true").strip().lower() in ("1", "true", "yes", "y", "on")

NORMALIZE_DENSE = True
SHUTDOWN = False

INFO_EVENTS = {
    "startup",
    "load.chunks",
    "clients.created",
    "collection.created",
    "collection.exists",
    "payload.index.created",
    "index.start",
    "batch.embedded",
    "index.prepared",
    "index.completed",
    "pipeline.done",
}

for proxy_name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(proxy_name, None)

_root = logging.getLogger()
_root.handlers.clear()
_root.setLevel(logging.CRITICAL)
for name in ("httpx", "httpcore", "urllib3", "boto3", "botocore", "qdrant_client", "asyncio"):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.setLevel(logging.CRITICAL)
    lg.propagate = False

logger = logging.getLogger("index")
logger.handlers.clear()
logger.setLevel(logging.DEBUG if LOG_LEVEL == "DEBUG" else logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def slog(level: str, event: str, msg: str = "", **extra: Any) -> None:
    entry: dict[str, Any] = {"ts": now_ts(), "level": level, "event": event, "msg": msg}
    if extra:
        entry.update(extra)
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    if level == "error":
        logger.error(line)
    elif level == "warning":
        logger.warning(line)
    elif level == "debug":
        logger.debug(line)
    else:
        logger.info(line)


def _escape_stack(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(s)
    except Exception:
        try:
            return int(float(s))
        except Exception:
            return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, float):
        return value
    s = str(value).strip()
    if not s:
        return default
    try:
        return float(s)
    except Exception:
        return default


def _parse_list_like(x: Any) -> list[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    s = str(x).strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
            return [parsed]
        except Exception:
            inner = s[1:-1].strip()
            if not inner:
                return []
            out: list[Any] = []
            for part in inner.split(","):
                p = part.strip().strip('"').strip("'")
                if not p:
                    continue
                try:
                    out.append(int(p))
                    continue
                except Exception:
                    pass
                out.append(p)
            return out
    if "-" in s and all(piece.strip().isdigit() for piece in s.split("-", 1)):
        a, b = s.split("-", 1)
        return [_safe_int(a, 0), _safe_int(b, 0)]
    return [s]


def _to_str_list(x: Any) -> list[str]:
    return [str(v) for v in _parse_list_like(x) if v is not None and str(v).strip() != ""]


def _to_int_list(x: Any) -> list[int] | None:
    values: list[int] = []
    for item in _parse_list_like(x):
        try:
            values.append(int(item))
        except Exception:
            continue
    return values or None


def _to_bool_or_none(x: Any) -> bool | None:
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return None


def _sleep_with_jitter(base: float, cap: float, attempt: int) -> None:
    backoff = min(cap, base * (2 ** max(0, attempt - 1)))
    time.sleep(backoff * (0.5 + random.random() * 0.5))


def retry_call(
    func: Callable[[], Any],
    *,
    retries: int = NETWORK_RETRY_COUNT,
    backoff_base: float = NETWORK_RETRY_BACKOFF_BASE,
    backoff_cap: float = NETWORK_RETRY_BACKOFF_MAX,
    retriable: Callable[[BaseException], bool] | None = None,
) -> Any:
    last_exc: BaseException | None = None
    for attempt in range(1, retries + 1):
        if SHUTDOWN:
            raise RuntimeError("shutdown requested")
        try:
            return func()
        except BaseException as exc:
            last_exc = exc
            should_retry = True
            if retriable is not None:
                try:
                    should_retry = bool(retriable(exc))
                except Exception:
                    should_retry = False
            if attempt >= retries or not should_retry:
                raise
            slog("warning", "retry", "Transient error, retrying", attempt=attempt, max_retries=retries, error=str(exc))
            _sleep_with_jitter(backoff_base, backoff_cap, attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_call failed unexpectedly")


def sha256_hex_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_hex_str(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def l2_normalize(v: list[float]) -> list[float]:
    arr = np.asarray(v, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr.astype(float).tolist()


class TransientHTTPError(Exception):
    pass


class S3Client:
    def __init__(self) -> None:
        self.session = boto3.session.Session(region_name=AWS_REGION or None)
        config = Config(
            region_name=AWS_REGION or None,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        kwargs: dict[str, Any] = {"config": config}
        if AWS_ENDPOINT_URL:
            kwargs["endpoint_url"] = AWS_ENDPOINT_URL
        self.client = self.session.client("s3", **kwargs)

    def head_bucket(self, bucket: str) -> None:
        self.client.head_bucket(Bucket=bucket)

    def get_paginator(self, name: str):
        return self.client.get_paginator(name)

    def head_object(self, bucket: str, key: str) -> dict[str, Any]:
        return self.client.head_object(Bucket=bucket, Key=key)

    def get_object_bytes(self, bucket: str, key: str) -> bytes:
        resp = self.client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"].read()
        if isinstance(body, bytes):
            return body
        if isinstance(body, bytearray):
            return bytes(body)
        return str(body).encode("utf-8")

    def put_object(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)

    def upload_file(self, local_path: str, bucket: str, key: str, content_type: str) -> None:
        self.client.upload_file(local_path, bucket, key, ExtraArgs={"ContentType": content_type})


class DenseClient:
    def __init__(self, url: str, timeout: float = HTTP_TIMEOUT, embed_timeout: float = DENSE_EMBED_TIMEOUT):
        self.url = url.rstrip("/")
        self.embed_timeout = embed_timeout
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _request(self, method: str, path: str, *, timeout: float | None = None, json_body: Any = None) -> httpx.Response:
        url = f"{self.url}{path}"

        def call() -> httpx.Response:
            resp = self.client.request(method, url, timeout=timeout or self.embed_timeout, json=json_body)
            if 500 <= resp.status_code < 600:
                raise TransientHTTPError(f"{method} {url} -> {resp.status_code}")
            return resp

        return retry_call(call, retriable=lambda exc: isinstance(exc, (httpx.HTTPError, TransientHTTPError)))

    def health(self) -> bool:
        try:
            resp = self._request("GET", "/health", timeout=HTTP_TIMEOUT)
            return resp.status_code == 200
        except Exception as exc:
            slog("warning", "dense.health.error", "Dense service unhealthy", error=str(exc), url=self.url)
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._request("POST", "/embed", json_body={"texts": texts})
        if resp.status_code != 200:
            raise RuntimeError(f"dense embed failed status={resp.status_code} body={resp.text}")
        data = resp.json()
        vectors = data.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError("dense embed invalid response")
        out: list[list[float]] = []
        for vec in vectors:
            if not isinstance(vec, list):
                raise RuntimeError("dense embed vector invalid")
            v = [float(x) for x in vec]
            if NORMALIZE_DENSE:
                v = l2_normalize(v)
            if len(v) != DENSE_DIM:
                raise RuntimeError(f"dense embed dim mismatch expected={DENSE_DIM} got={len(v)}")
            out.append(v)
        return out


class SparseClient:
    def __init__(self, url: str, timeout: float = HTTP_TIMEOUT, embed_timeout: float = SPARSE_EMBED_TIMEOUT):
        self.url = url.rstrip("/")
        self.embed_timeout = embed_timeout
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _request(self, method: str, path: str, *, timeout: float | None = None, json_body: Any = None) -> httpx.Response:
        url = f"{self.url}{path}"

        def call() -> httpx.Response:
            resp = self.client.request(method, url, timeout=timeout or self.embed_timeout, json=json_body)
            if 500 <= resp.status_code < 600:
                raise TransientHTTPError(f"{method} {url} -> {resp.status_code}")
            return resp

        return retry_call(call, retriable=lambda exc: isinstance(exc, (httpx.HTTPError, TransientHTTPError)))

    def health(self) -> bool:
        try:
            resp = self._request("GET", "/health", timeout=HTTP_TIMEOUT)
            return resp.status_code == 200
        except Exception as exc:
            slog("warning", "sparse.health.error", "Sparse service unhealthy", error=str(exc), url=self.url)
            return False

    def embed_chunked(self, texts: list[str]) -> list[dict[str, Any]]:
        if not texts:
            return []
        resp = self._request("POST", "/embed", json_body={"texts": texts})
        if resp.status_code == 200:
            data = resp.json()
            vectors = data.get("vectors")
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise RuntimeError("sparse embed invalid response")
            out: list[dict[str, Any]] = []
            for item in vectors:
                if not isinstance(item, dict) or "indices" not in item or "values" not in item:
                    raise RuntimeError("sparse embed element invalid")
                out.append(
                    {
                        "indices": [int(x) for x in item.get("indices", [])],
                        "values": [float(x) for x in item.get("values", [])],
                    }
                )
            return out

        if resp.status_code in (400, 422):
            try:
                detail = ""
                try:
                    detail = str(resp.json().get("detail", ""))
                except Exception:
                    detail = resp.text or ""
                m = re.search(r"max=(\d+)", detail)
                if m:
                    max_batch = max(1, int(m.group(1)))
                    out: list[dict[str, Any]] = []
                    for i in range(0, len(texts), max_batch):
                        out.extend(self.embed_chunked(texts[i : i + max_batch]))
                    return out
            except Exception as exc:
                slog("warning", "sparse.batch.split.failed", "Sparse batch split failed", error=str(exc), url=self.url)
                if resp.status_code == 422:
                    out: list[dict[str, Any]] = []
                    for i in range(0, len(texts), SPARSE_BATCH_FALLBACK):
                        out.extend(self.embed_chunked(texts[i : i + SPARSE_BATCH_FALLBACK]))
                    return out

        raise RuntimeError(f"sparse embed failed status={resp.status_code} body={resp.text}")


def validate_envs() -> None:
    missing = []
    if not AWS_REGION:
        missing.append("AWS_REGION")
    if not DATA_S3_BUCKET:
        missing.append("DATA_S3_BUCKET")
    if not QDRANT_URL:
        missing.append("QDRANT_URL")
    if missing:
        slog("error", "env.missing", "Missing required environment variables", missing=missing)
        raise SystemExit(2)


def full_path_from_key(key: str) -> str:
    return f"s3://{DATA_S3_BUCKET.rstrip('/')}/{key.lstrip('/')}"


def strip_root_from_path(full: str) -> str:
    root = f"s3://{DATA_S3_BUCKET.rstrip('/')}/"
    if full.startswith(root):
        return full[len(root) :]
    if full.startswith("s3://"):
        rest = full[len("s3://") :]
        if rest.startswith(DATA_S3_BUCKET.rstrip("/") + "/"):
            return rest[len(DATA_S3_BUCKET.rstrip("/")) + 1 :]
        if rest == DATA_S3_BUCKET.rstrip("/"):
            return ""
    if full.startswith(DATA_S3_BUCKET.rstrip("/") + "/"):
        return full[len(DATA_S3_BUCKET.rstrip("/")) + 1 :]
    return full


def storage_object_exists(s3: S3Client, key: str) -> bool:
    try:
        s3.head_object(DATA_S3_BUCKET, key)
        return True
    except ClientError as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound", "NotFoundException"):
            return False
        return False
    except Exception:
        return False


def normalize_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    c = dict(chunk or {})
    c["document_id"] = str(c.get("document_id") or "")
    c["file_name"] = str(c.get("file_name") or "")
    c["chunk_id"] = str(c.get("chunk_id") or "")
    c["chunk_type"] = str(c.get("chunk_type") or "")
    c["text"] = str(c.get("text") or "")
    c["token_count"] = _safe_int(c.get("token_count"), 0)
    c["source_url"] = str(c.get("source_url") or "")
    c["timestamp"] = str(c.get("timestamp") or "")
    c["parser_version"] = str(c.get("parser_version") or "")
    c["page_number"] = _safe_int(c.get("page_number"), None) if c.get("page_number") is not None else None
    c["row_range"] = _to_int_list(c.get("row_range"))
    c["line_range"] = _to_int_list(c.get("line_range"))
    c["token_range"] = _to_int_list(c.get("token_range"))
    c["semantic_region"] = str(c.get("semantic_region") or "")
    c["audio_range"] = _parse_list_like(c.get("audio_range")) or None
    c["slide_range"] = _parse_list_like(c.get("slide_range")) or None
    c["headings"] = _to_str_list(c.get("headings"))
    c["heading_path"] = _to_str_list(c.get("heading_path"))
    c["tags"] = _to_str_list(c.get("tags"))
    c["layout_tags"] = _to_str_list(c.get("layout_tags"))
    c["figures"] = _parse_list_like(c.get("figures"))
    c["file_type"] = str(c.get("file_type") or "")
    used_ocr = _to_bool_or_none(c.get("used_ocr"))
    c["used_ocr"] = bool(used_ocr) if used_ocr is not None else False
    if "layout" in c and c.get("layout") is not None:
        c["layout"] = c.get("layout")
    return c


def _safe_json_load(raw: Any) -> Any:
    if raw is None:
        return []
    if isinstance(raw, (list, dict)):
        return raw
    s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    s = s.strip()
    if not s:
        return []
    try:
        return json.loads(s)
    except Exception:
        return [s]


def load_chunks_from_s3(bucket: str, prefix: str) -> list[dict[str, Any]]:
    s3 = S3Client()
    paginator = s3.get_paginator("list_objects_v2")
    pages = retry_call(
        lambda: list(paginator.paginate(Bucket=bucket, Prefix=prefix)),
        retriable=lambda exc: isinstance(exc, (BotoCoreError, ClientError, EndpointConnectionError, ReadTimeoutError, httpx.HTTPError, TransientHTTPError, Exception)),
    )

    keys: list[str] = []
    for page in pages:
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key")
            if not key or key.endswith("/"):
                continue
            keys.append(key)

    parquet_keys = sorted([k for k in keys if k.lower().endswith(".parquet")])
    json_keys = sorted([k for k in keys if k.lower().endswith(".json")])
    selected_keys = parquet_keys if parquet_keys else json_keys

    if not selected_keys:
        slog("error", "no_s3_chunks", "No chunk files found", bucket=bucket, prefix=prefix)
        raise SystemExit(2)

    chunks: list[dict[str, Any]] = []
    for key in selected_keys:
        body = retry_call(
            lambda key=key: s3.get_object_bytes(bucket, key),
            retriable=lambda exc: isinstance(exc, (BotoCoreError, ClientError, EndpointConnectionError, ReadTimeoutError, Exception)),
        )
        if key.lower().endswith(".parquet"):
            table = pq.read_table(pa.BufferReader(body))
            data = table.to_pydict()
            if not data:
                continue
            row_count = len(next(iter(data.values())))
            for idx in range(row_count):
                row = {col: (data[col][idx] if idx < len(data[col]) else None) for col in data.keys()}
                chunk = {
                    "document_id": row.get("document_id") or "",
                    "file_name": row.get("file_name") or "",
                    "chunk_id": row.get("chunk_id") or "",
                    "chunk_type": row.get("chunk_type") or "",
                    "text": row.get("text") or "",
                    "token_count": _safe_int(row.get("token_count"), 0),
                    "figures": _safe_json_load(row.get("figures")),
                    "tags": _safe_json_load(row.get("tags")),
                    "layout_tags": _safe_json_load(row.get("layout_tags")),
                    "heading_path": _safe_json_load(row.get("heading_path")),
                    "headings": _safe_json_load(row.get("headings")),
                    "file_type": row.get("file_type") or "",
                    "source_url": row.get("source_url") or "",
                    "audio_range": _safe_json_load(row.get("audio_range")) if row.get("audio_range") is not None else None,
                    "timestamp": row.get("timestamp") or None,
                    "parser_version": row.get("parser_version") or None,
                    "used_ocr": _safe_bool(row.get("used_ocr")),
                    "line_range": [_safe_int(row.get("line_start"), 1), _safe_int(row.get("line_end"), 1)],
                    "page_number": _safe_int(row.get("page_number"), None) if row.get("page_number") is not None else None,
                    "row_range": _safe_json_load(row.get("row_range")) or None,
                    "token_range": _safe_json_load(row.get("token_range")) or None,
                    "slide_range": _safe_json_load(row.get("slide_range")) or None,
                    "semantic_region": row.get("semantic_region") or "",
                    "layout": row.get("layout"),
                }
                chunks.append(normalize_chunk(chunk))
        else:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, list):
                for raw in parsed:
                    if isinstance(raw, dict):
                        chunks.append(normalize_chunk(raw))
            elif isinstance(parsed, dict):
                chunks.append(normalize_chunk(parsed))
            else:
                raise SystemExit(f"Unexpected JSON format in s3://{bucket}/{key}")

    slog("info", "load.chunks", "Loaded chunks", original_chunks=len(chunks), bucket=bucket, prefix=prefix)
    return chunks


def create_payload_indexes(client: QdrantClient, collection_name: str) -> None:
    index_specs: list[tuple[str, Any]] = [
        ("document_id", models.PayloadSchemaType.KEYWORD),
        ("file_name", models.PayloadSchemaType.KEYWORD),
        ("chunk_id", models.PayloadSchemaType.KEYWORD),
        ("chunk_type", models.PayloadSchemaType.KEYWORD),
        ("file_type", models.PayloadSchemaType.KEYWORD),
        ("parser_version", models.PayloadSchemaType.KEYWORD),
        ("semantic_region", models.PayloadSchemaType.KEYWORD),
        ("page_number", models.PayloadSchemaType.INTEGER),
        ("used_ocr", models.PayloadSchemaType.BOOL),
        ("tags", models.PayloadSchemaType.KEYWORD),
        ("layout_tags", models.PayloadSchemaType.KEYWORD),
        ("heading_path", models.PayloadSchemaType.KEYWORD),
    ]
    for field_name, field_schema in index_specs:
        try:
            retry_call(
                lambda field_name=field_name, field_schema=field_schema: client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                ),
                retries=3,
                retriable=lambda exc: True,
            )
            slog("info", "payload.index.created", "Created payload index", collection=collection_name, field=field_name, schema=str(field_schema))
        except Exception as exc:
            slog("warning", "payload.index.failed", "Payload index creation failed", collection=collection_name, field=field_name, error=str(exc))


def build_quantization_config() -> Any | None:
    if not QDRANT_ENABLE_SCALAR_QUANTIZATION:
        return None
    return models.ScalarQuantization(
        scalar=models.ScalarQuantizationConfig(
            type=models.ScalarType.INT8,
            always_ram=QDRANT_QUANTIZATION_ALWAYS_RAM,
        )
    )


def create_collection(client: QdrantClient, collection_name: str, dense_dim: int, dense_enabled: bool, sparse_enabled: bool) -> None:
    exists = False
    try:
        exists = client.collection_exists(collection_name)
    except Exception as exc:
        slog("warning", "collection.exists.check.failed", "Collection existence check failed", collection=collection_name, error=str(exc))
    if exists:
        slog("info", "collection.exists", "Collection already exists", collection=collection_name)
        return

    if not dense_enabled and not sparse_enabled:
        raise RuntimeError("at least one vector mode must be enabled")

    quantization_config = build_quantization_config()
    vectors_config: Any = None
    sparse_config: Any = None

    if dense_enabled:
        vectors_config = {
            "dense": models.VectorParams(
                size=dense_dim,
                distance=models.Distance.COSINE,
                on_disk=QDRANT_ONDISK,
            )
        }
    if sparse_enabled:
        sparse_config = {"sparse": models.SparseVectorParams()}

    retry_call(
        lambda: client.create_collection(
            collection_name=collection_name,
            vectors_config=vectors_config,
            sparse_vectors_config=sparse_config,
            quantization_config=quantization_config,
        ),
        retries=3,
        retriable=lambda exc: True,
    )
    slog(
        "info",
        "collection.created",
        "Created collection",
        name=collection_name,
        dense_dim=dense_dim,
        dense_enabled=dense_enabled,
        sparse_enabled=sparse_enabled,
        quantization=bool(quantization_config),
    )


def existing_point_ids(client: QdrantClient, collection_name: str, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    try:
        records = retry_call(
            lambda: client.retrieve(collection_name=collection_name, ids=ids, with_payload=False, with_vectors=False),
            retries=NETWORK_RETRY_COUNT,
            retriable=lambda exc: True,
        )
    except Exception as exc:
        slog("warning", "retrieve.failed", "Failed to retrieve existing ids", collection=collection_name, error=str(exc))
        return set()
    out: set[int] = set()
    if isinstance(records, list):
        for rec in records:
            try:
                rid = getattr(rec, "id", None)
                if rid is None and isinstance(rec, dict):
                    rid = rec.get("id")
                if rid is not None:
                    out.add(int(rid))
            except Exception:
                continue
    return out


def sparse_to_qdrant_sparsevector(sparse_obj: Any) -> SparseVector:
    if sparse_obj is None:
        return SparseVector(indices=[], values=[])
    if isinstance(sparse_obj, dict):
        return SparseVector(
            indices=[int(x) for x in sparse_obj.get("indices", [])],
            values=[float(x) for x in sparse_obj.get("values", [])],
        )
    raise RuntimeError("unsupported sparse object")


def id_from_string(s: str) -> int:
    digest = hashlib.md5(s.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


FULL_PAYLOAD_KEYS = [
    "document_id",
    "file_name",
    "chunk_id",
    "chunk_type",
    "text",
    "token_count",
    "source_url",
    "timestamp",
    "parser_version",
    "page_number",
    "row_range",
    "line_range",
    "token_range",
    "semantic_region",
    "audio_range",
    "slide_range",
    "headings",
    "heading_path",
    "tags",
    "layout_tags",
    "figures",
    "file_type",
    "used_ocr",
    "layout",
]


def chunk_to_point(chunk: dict[str, Any], dense_vec: list[float] | None, sparse_obj: dict[str, Any] | None) -> PointStruct | None:
    cid = chunk.get("chunk_id") or f"{chunk.get('document_id')}_0"
    pid = id_from_string(str(cid))
    vectors: dict[str, Any] = {}
    if dense_vec is not None:
        vectors["dense"] = dense_vec
    if sparse_obj is not None:
        sparse_vector = sparse_to_qdrant_sparsevector(sparse_obj)
        if sparse_vector.indices:
            vectors["sparse"] = sparse_vector
    if not vectors:
        return None
    payload = {k: chunk.get(k) for k in FULL_PAYLOAD_KEYS if k in chunk}
    return PointStruct(id=pid, vector=vectors, payload=payload)


def embed_with_retry_and_split_dense(client: DenseClient, texts: list[str]) -> list[list[float] | None]:
    attempts = 0
    while attempts <= EMBED_RETRIES:
        try:
            return client.embed(texts)
        except Exception as exc:
            attempts += 1
            slog("warning", "dense.embed.retry", "Dense embedding retry", attempt=attempts, error=str(exc), count=len(texts))
            if attempts > EMBED_RETRIES:
                break
            time.sleep(EMBED_BACKOFF_BASE * (2 ** (attempts - 1)))
    if len(texts) <= 1:
        raise RuntimeError("dense embed failed after retries")
    mid = max(1, len(texts) // 2)
    return embed_with_retry_and_split_dense(client, texts[:mid]) + embed_with_retry_and_split_dense(client, texts[mid:])


def embed_with_retry_and_split_sparse(client: SparseClient, texts: list[str]) -> list[dict[str, Any] | None]:
    attempts = 0
    while attempts <= EMBED_RETRIES:
        try:
            return client.embed_chunked(texts)
        except Exception as exc:
            attempts += 1
            slog("warning", "sparse.embed.retry", "Sparse embedding retry", attempt=attempts, error=str(exc), count=len(texts))
            if attempts > EMBED_RETRIES:
                break
            time.sleep(EMBED_BACKOFF_BASE * (2 ** (attempts - 1)))
    if len(texts) <= 1:
        raise RuntimeError("sparse embed failed after retries")
    mid = max(1, len(texts) // 2)
    return embed_with_retry_and_split_sparse(client, texts[:mid]) + embed_with_retry_and_split_sparse(client, texts[mid:])


def safe_embed_and_points(
    chunks: list[dict[str, Any]],
    sparse_client: SparseClient | None,
    dense_client: DenseClient | None,
    hybrid: bool,
) -> list[PointStruct]:
    texts = [str(c.get("text", "") or "") for c in chunks]
    dense_vecs: list[list[float] | None] = [None] * len(texts)
    sparse_objs: list[dict[str, Any] | None] = [None] * len(texts)

    if dense_client is not None:
        try:
            dense_vecs = embed_with_retry_and_split_dense(dense_client, texts)
            slog("info", "dense.embedded", "Dense vectors embedded", count=len(dense_vecs))
        except Exception as exc:
            slog("warning", "dense.embed.failed", "Dense embedding failed; continuing without dense vectors", error=str(exc))
            dense_vecs = [None] * len(texts)

    if sparse_client is not None:
        try:
            sparse_objs = embed_with_retry_and_split_sparse(sparse_client, texts)
            slog("info", "sparse.embedded", "Sparse vectors embedded", count=len(sparse_objs))
        except Exception as exc:
            slog("warning", "sparse.embed.failed", "Sparse embedding failed; continuing without sparse vectors", error=str(exc))
            sparse_objs = [None] * len(texts)

    points: list[PointStruct] = []
    for idx, chunk in enumerate(chunks):
        point = chunk_to_point(chunk, dense_vecs[idx] if idx < len(dense_vecs) else None, sparse_objs[idx] if idx < len(sparse_objs) else None)
        if point is not None:
            points.append(point)
    return points


def validate_and_build_clients() -> tuple[DenseClient | None, SparseClient | None]:
    dense_client = DenseClient(DENSE_URL, timeout=HTTP_TIMEOUT, embed_timeout=DENSE_EMBED_TIMEOUT)
    sparse_client = SparseClient(SPARSE_URL, timeout=HTTP_TIMEOUT, embed_timeout=SPARSE_EMBED_TIMEOUT)
    slog("info", "clients.created", "Created embedding clients", dense_url=dense_client.url, sparse_url=sparse_client.url, qdrant_url=QDRANT_URL)

    dense = dense_client if dense_client.health() else None
    sparse = sparse_client if sparse_client.health() else None

    if dense is None and sparse is None:
        slog("error", "no_embed_services", "Neither dense nor sparse embedding service is healthy")
        raise SystemExit(2)
    return dense, sparse


def embed_and_upsert(
    client: QdrantClient,
    collection_name: str,
    chunks: list[dict[str, Any]],
    sparse_client: SparseClient | None,
    dense_client: DenseClient | None,
    hybrid: bool,
    metrics: dict[str, int],
) -> None:
    total = len(chunks)
    slog("info", "index.start", "Indexing started", total_input_chunks=total, batch=BATCH_SIZE, hybrid=hybrid)

    processed = 0
    total_upserted = 0

    for offset in range(0, total, BATCH_SIZE):
        if SHUTDOWN:
            slog("warning", "shutdown.during_index", "Shutdown requested during indexing", offset=offset)
            break

        batch = chunks[offset : offset + BATCH_SIZE]
        ids = [id_from_string(str(c.get("chunk_id") or f"{c.get('document_id')}_0")) for c in batch]
        if len(batch) != len(ids):
            raise RuntimeError("batch and ids length mismatch")

        start = time.time()
        existing = existing_point_ids(client, collection_name, ids)
        elapsed = round(time.time() - start, 3)

        to_process = [c for c, pid in zip(batch, ids, strict=True) if pid not in existing]
        skipped = len(batch) - len(to_process)

        slog(
            "debug",
            "batch.check",
            "Checked batch against existing points",
            batch_range=f"{offset}..{offset + len(batch) - 1}",
            skipped=skipped,
            retrieve_time=elapsed,
        )

        if not to_process:
            continue

        try:
            points = safe_embed_and_points(to_process, sparse_client, dense_client, hybrid)
        except Exception as exc:
            slog("error", "batch.embed.failed", "Failed to embed batch", error=str(exc), offset=offset)
            continue

        if not points:
            continue

        processed += len(points)
        slog("info", "batch.embedded", "Batch embedded", embedded=len(points), processed=processed)

        for j in range(0, len(points), UPSERT_CHUNK):
            if SHUTDOWN:
                slog("warning", "shutdown.before_upsert", "Shutdown requested before upsert", offset=offset, chunk_start=j)
                break

            slice_pts = points[j : j + UPSERT_CHUNK]
            try:
                retry_call(
                    lambda pts=slice_pts: client.upsert(
                        collection_name=collection_name,
                        points=pts,
                    ),
                    retries=NETWORK_RETRY_COUNT,
                    retriable=lambda exc: True,
                )
            except Exception as exc:
                slog(
                    "error",
                    "upsert.failed",
                    "Failed to upsert chunk",
                    error=str(exc),
                    offset=offset,
                    chunk_start=j,
                    count=len(slice_pts),
                )
                continue

            total_upserted += len(slice_pts)
            metrics["indexed_points"] = int(metrics.get("indexed_points", 0)) + len(slice_pts)
            slog("debug", "upsert.chunk", "Upserted chunk", offset=offset, chunk_start=j, count=len(slice_pts))

    slog("info", "index.completed", "Indexing completed", total_processed=processed, total_upserted=total_upserted)


def create_qdrant_client() -> QdrantClient:
    kwargs: dict[str, Any] = {"url": QDRANT_URL}
    if QDRANT_API_KEY:
        kwargs["api_key"] = QDRANT_API_KEY
    return QdrantClient(**kwargs)


def retrieve_and_index(metrics: dict[str, int]) -> None:
    chunks = load_chunks_from_s3(DATA_S3_BUCKET, DATA_S3_PREFIX)
    metrics["total_input_chunks"] = len(chunks)

    dense_client, sparse_client = validate_and_build_clients()
    hybrid_mode = dense_client is not None and sparse_client is not None

    try:
        qdrant = create_qdrant_client()
    except Exception as exc:
        slog("error", "qdrant.client.init.failed", "Unable to initialize Qdrant client", error=str(exc))
        raise SystemExit(2) from exc

    try:
        retry_call(lambda: qdrant.get_collections(), retries=NETWORK_RETRY_COUNT, retriable=lambda exc: True)
    except Exception as exc:
        slog("error", "qdrant.unreachable", "Qdrant is unreachable", error=str(exc))
        raise SystemExit(2) from exc

    create_collection(
        qdrant,
        COLLECTION_NAME,
        DENSE_DIM,
        dense_enabled=(dense_client is not None),
        sparse_enabled=(sparse_client is not None),
    )
    create_payload_indexes(qdrant, COLLECTION_NAME)
    embed_and_upsert(qdrant, COLLECTION_NAME, chunks, sparse_client, dense_client, hybrid_mode, metrics)


def _safe_summary(metrics: dict[str, int], exit_code: int) -> dict[str, Any]:
    total_input_chunks = int(metrics.get("total_input_chunks", 0) or 0)
    indexed_points = int(metrics.get("indexed_points", 0) or 0)
    skipped = max(0, total_input_chunks - indexed_points)
    return {
        "collection": COLLECTION_NAME,
        "indexed_points": indexed_points,
        "total_input_chunks": total_input_chunks,
        "skipped_existing": skipped,
        "exit_code": exit_code,
    }


def handle_sigterm(signum, frame):
    global SHUTDOWN
    SHUTDOWN = True
    slog("warning", "shutdown.requested", "Shutdown signal received", signal=signum)


signal.signal(signal.SIGINT, handle_sigterm)
signal.signal(signal.SIGTERM, handle_sigterm)


def main() -> None:
    validate_envs()
    slog("info", "startup", "Index pipeline starting", aws_region=AWS_REGION, bucket=DATA_S3_BUCKET, prefix=DATA_S3_PREFIX, collection=COLLECTION_NAME)

    metrics: dict[str, int] = {"total_input_chunks": 0, "indexed_points": 0}
    exit_code = 0

    try:
        retrieve_and_index(metrics)
    except SystemExit as exc:
        exit_code = _safe_int(getattr(exc, "code", 1), 1) or 1
    except Exception as exc:
        exit_code = 1
        slog("error", "index.unhandled_exception", "Unhandled exception in index pipeline", error=str(exc), stack=_escape_stack(exc))

    summary = _safe_summary(metrics, exit_code)
    print(json.dumps(summary, separators=(",", ":"), ensure_ascii=False), flush=True)

    if exit_code != 0:
        raise SystemExit(exit_code)
    slog("info", "pipeline.done", "Pipeline completed successfully", **summary)


if __name__ == "__main__":
    main()