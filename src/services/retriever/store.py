#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from helpers import build_semantic_cache_payload, is_payload_expired
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.models import PointStruct

logger = logging.getLogger("retrieval.qdrant")


@dataclass(frozen=True)
class QdrantStoreConfig:
    url: str
    api_key: str
    docs_collection: str
    cache_collection: str
    dense_vector_name: str = "dense"
    sparse_vector_name: str = "sparse"
    dense_dim: int = 384

    shard_number: int = 1
    replication_factor: int = 1
    write_consistency_factor: int = 1

    docs_on_disk_payload: bool = True
    cache_on_disk_payload: bool = False

    doc_hnsw_m: int = 32
    doc_ef_construct: int = 128
    cache_hnsw_m: int = 12
    cache_ef_construct: int = 96

    cache_ttl_seconds: int = 86_400
    cache_score_threshold: float = 0.92
    cache_group: str = "semantic_rag_v1"

    enable_scalar_quantization: bool = True
    quantization_always_ram: bool = True
    sparse_on_disk: bool = False

    query_timeout_seconds: int = 20

    @classmethod
    def from_env(cls) -> QdrantStoreConfig:
        docs_collection = os.getenv("COLLECTION_NAME", "default_rag_collection1")
        default_on_disk = os.getenv("QDRANT_ON_DISK_PAYLOAD", "true").lower() == "true"
        return cls(
            url=os.getenv("QDRANT_URL", "http://qdrant.qdrant.svc.cluster.local:6333"),
            api_key=os.getenv("QDRANT_API_KEY", ""),
            docs_collection=docs_collection,
            cache_collection=os.getenv("CACHE_COLLECTION_NAME", f"{docs_collection}__semantic_cache"),
            dense_vector_name=os.getenv("DENSE_VECTOR_NAME", "dense"),
            sparse_vector_name=os.getenv("SPARSE_VECTOR_NAME", "sparse"),
            dense_dim=int(os.getenv("DENSE_DIM", "384")),
            shard_number=int(os.getenv("QDRANT_SHARD_NUMBER", "1")),
            replication_factor=int(os.getenv("QDRANT_REPLICATION_FACTOR", "1")),
            write_consistency_factor=int(os.getenv("QDRANT_WRITE_CONSISTENCY_FACTOR", "1")),
            docs_on_disk_payload=os.getenv("DOCS_ON_DISK_PAYLOAD", str(default_on_disk)).lower() == "true",
            cache_on_disk_payload=os.getenv("CACHE_ON_DISK_PAYLOAD", "false").lower() == "true",
            doc_hnsw_m=int(os.getenv("DOC_HNSW_M", "32")),
            doc_ef_construct=int(os.getenv("DOC_EF_CONSTRUCT", "128")),
            cache_hnsw_m=int(os.getenv("CACHE_HNSW_M", "12")),
            cache_ef_construct=int(os.getenv("CACHE_EF_CONSTRUCT", "96")),
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "86400")),
            cache_score_threshold=float(os.getenv("CACHE_SCORE_THRESHOLD", "0.92")),
            cache_group=os.getenv("CACHE_GROUP", "semantic_rag_v1"),
            enable_scalar_quantization=os.getenv("QDRANT_ENABLE_SCALAR_QUANTIZATION", "true").lower() == "true",
            quantization_always_ram=os.getenv("QDRANT_QUANTIZATION_ALWAYS_RAM", "true").lower() == "true",
            sparse_on_disk=os.getenv("QDRANT_SPARSE_ON_DISK", "false").lower() == "true",
            query_timeout_seconds=int(os.getenv("QDRANT_TIMEOUT_SECONDS", "20")),
        )


def _points_list(resp: Any) -> list[Any]:
    if resp is None:
        return []
    if hasattr(resp, "points"):
        pts = resp.points
        return list(pts) if pts is not None else []
    if hasattr(resp, "result"):
        result = resp.result
        if hasattr(result, "points"):
            pts = result.points
            return list(pts) if pts is not None else []
        if isinstance(result, dict) and isinstance(result.get("points"), list):
            return list(result["points"])
    if isinstance(resp, dict):
        for key in ("points", "result", "items", "hits", "data"):
            val = resp.get(key)
            if isinstance(val, list):
                return list(val)
            if isinstance(val, dict) and isinstance(val.get("points"), list):
                return list(val["points"])
    if isinstance(resp, list):
        return list(resp)
    return []


def _normalize_point(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        pid = item.get("id")
        if pid is None and isinstance(item.get("point"), dict):
            pid = item["point"].get("id")
        payload = item.get("payload")
        if payload is None and isinstance(item.get("point"), dict):
            payload = item["point"].get("payload")
        score = item.get("score", item.get("payload_score", 0.0))
        return {"id": pid, "score": float(score or 0.0), "payload": payload or {}}
    pid = getattr(item, "id", None)
    score = getattr(item, "score", 0.0)
    payload = getattr(item, "payload", None) or {}
    return {"id": pid, "score": float(score or 0.0), "payload": payload}


def _get_nested(obj: Any, *names: str) -> Any:
    cur = obj
    for name in names:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            cur = getattr(cur, name, None)
    return cur


def _point_id_from_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        raw = "empty"
    try:
        return str(uuid.UUID(raw))
    except Exception:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


class QdrantStore:
    def __init__(self, config: QdrantStoreConfig):
        self.config = config
        self.client = AsyncQdrantClient(url=config.url, api_key=config.api_key or None)
        self.docs_ready = False
        self.cache_ready = False
        self.hybrid_capable = False
        self._bootstrap_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> QdrantStore:
        return cls(QdrantStoreConfig.from_env())

    async def close(self) -> None:
        close_fn = getattr(self.client, "close", None)
        if close_fn is None:
            return
        res = close_fn()
        if inspect.isawaitable(res):
            await res

    async def ping(self) -> bool:
        try:
            await self.client.get_collections()
            return True
        except Exception:
            return False

    def _scalar_quantization_config(self) -> Any | None:
        if not self.config.enable_scalar_quantization:
            return None
        return models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(
                type=models.ScalarType.INT8,
                always_ram=self.config.quantization_always_ram,
            )
        )

    async def bootstrap(self) -> tuple[bool, bool]:
        async with self._bootstrap_lock:
            self.docs_ready = await self._ensure_docs_collection()
            self.cache_ready = await self._ensure_cache_collection()
            return self.docs_ready, self.cache_ready

    async def _ensure_collection(
        self,
        *,
        name: str,
        vectors_config: dict[str, models.VectorParams],
        sparse_vectors_config: dict[str, models.SparseVectorParams] | None = None,
        hnsw_m: int,
        hnsw_ef_construct: int,
        on_disk_payload: bool,
        enable_quantization: bool = False,
    ) -> bool:
        try:
            exists = await self.client.collection_exists(name)
        except Exception as e:
            logger.warning("collection_exists failed name=%s error=%s", name, e)
            exists = False

        if not exists:
            create_kwargs: dict[str, Any] = {
                "collection_name": name,
                "vectors_config": vectors_config,
                "sparse_vectors_config": sparse_vectors_config,
                "shard_number": self.config.shard_number,
                "replication_factor": self.config.replication_factor,
                "write_consistency_factor": self.config.write_consistency_factor,
                "on_disk_payload": on_disk_payload,
                "hnsw_config": models.HnswConfigDiff(m=hnsw_m, ef_construct=hnsw_ef_construct),
            }
            if enable_quantization:
                qcfg = self._scalar_quantization_config()
                if qcfg is not None:
                    create_kwargs["quantization_config"] = qcfg

            try:
                await self.client.create_collection(**create_kwargs)
            except Exception as e:
                msg = str(e).lower()
                if "already exists" not in msg and "409" not in msg:
                    logger.warning("collection create failed name=%s error=%s", name, e)
                    return False

        return True

    async def _ensure_payload_index(self, collection_name: str, field_name: str, schema: Any) -> None:
        try:
            await self.client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema,
            )
        except Exception as e:
            msg = str(e).lower()
            if "already exists" in msg or "409" in msg or "duplicate" in msg:
                return
            logger.debug("payload index skipped collection=%s field=%s error=%s", collection_name, field_name, e)

    def _collection_supports_vector_names(self, collection_info: Any, *, dense_name: str, sparse_name: str) -> tuple[bool, bool]:
        params = _get_nested(collection_info, "config", "params")
        vectors = _get_nested(params, "vectors")
        sparse_vectors = _get_nested(params, "sparse_vectors")

        dense_ok = False
        sparse_ok = False

        if isinstance(vectors, dict):
            dense_ok = dense_name in vectors
        else:
            dense_ok = vectors is not None

        if isinstance(sparse_vectors, dict):
            sparse_ok = sparse_name in sparse_vectors
        else:
            sparse_ok = sparse_vectors is not None

        return dense_ok, sparse_ok

    async def _ensure_docs_collection(self) -> bool:
        ok = await self._ensure_collection(
            name=self.config.docs_collection,
            vectors_config={
                self.config.dense_vector_name: models.VectorParams(
                    size=self.config.dense_dim,
                    distance=models.Distance.COSINE,
                    on_disk=self.config.docs_on_disk_payload,
                )
            },
            sparse_vectors_config={
                self.config.sparse_vector_name: models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=self.config.sparse_on_disk)
                )
            },
            hnsw_m=self.config.doc_hnsw_m,
            hnsw_ef_construct=self.config.doc_ef_construct,
            on_disk_payload=self.config.docs_on_disk_payload,
            enable_quantization=True,
        )
        if not ok:
            return False

        for field, schema in (
            ("document_id", models.PayloadSchemaType.KEYWORD),
            ("chunk_id", models.PayloadSchemaType.KEYWORD),
            ("chunk_type", models.PayloadSchemaType.KEYWORD),
            ("source_url", models.PayloadSchemaType.KEYWORD),
            ("file_type", models.PayloadSchemaType.KEYWORD),
            ("parser_version", models.PayloadSchemaType.KEYWORD),
            ("semantic_region", models.PayloadSchemaType.KEYWORD),
            ("page_number", models.PayloadSchemaType.INTEGER),
            ("line_start", models.PayloadSchemaType.INTEGER),
            ("line_end", models.PayloadSchemaType.INTEGER),
            ("tags", models.PayloadSchemaType.KEYWORD),
            ("layout_tags", models.PayloadSchemaType.KEYWORD),
            ("heading_path", models.PayloadSchemaType.KEYWORD),
            ("headings", models.PayloadSchemaType.KEYWORD),
        ):
            await self._ensure_payload_index(self.config.docs_collection, field, schema)

        try:
            info = await self.client.get_collection(self.config.docs_collection)
            dense_ok, sparse_ok = self._collection_supports_vector_names(
                info,
                dense_name=self.config.dense_vector_name,
                sparse_name=self.config.sparse_vector_name,
            )
            self.hybrid_capable = bool(dense_ok and sparse_ok)
            if not self.hybrid_capable:
                logger.warning(
                    "docs collection exists but hybrid vectors are incomplete; dense_ok=%s sparse_ok=%s collection=%s",
                    dense_ok,
                    sparse_ok,
                    self.config.docs_collection,
                )
        except Exception as e:
            logger.warning("could not inspect docs collection capabilities: %s", e)
            self.hybrid_capable = False

        return True

    async def _ensure_cache_collection(self) -> bool:
        ok = await self._ensure_collection(
            name=self.config.cache_collection,
            vectors_config={
                self.config.dense_vector_name: models.VectorParams(
                    size=self.config.dense_dim,
                    distance=models.Distance.COSINE,
                    on_disk=self.config.cache_on_disk_payload,
                )
            },
            sparse_vectors_config=None,
            hnsw_m=self.config.cache_hnsw_m,
            hnsw_ef_construct=self.config.cache_ef_construct,
            on_disk_payload=self.config.cache_on_disk_payload,
            enable_quantization=False,
        )
        if not ok:
            return False

        for field, schema in (
            ("cache_group", models.PayloadSchemaType.KEYWORD),
            ("query_norm_hash", models.PayloadSchemaType.KEYWORD),
            ("corpus_version", models.PayloadSchemaType.KEYWORD),
            ("prompt_version", models.PayloadSchemaType.KEYWORD),
            ("retrieval_version", models.PayloadSchemaType.KEYWORD),
            ("model_name", models.PayloadSchemaType.KEYWORD),
            ("created_at_epoch", models.PayloadSchemaType.INTEGER),
            ("expires_at_epoch", models.PayloadSchemaType.INTEGER),
            ("hit_type", models.PayloadSchemaType.KEYWORD),
        ):
            await self._ensure_payload_index(self.config.cache_collection, field, schema)

        return True

    async def semantic_cache_get_by_id(self, cache_id: str) -> dict[str, Any] | None:
        if not self.cache_ready:
            return None
        pid = _point_id_from_text(cache_id)
        try:
            items = await self.client.retrieve(
                collection_name=self.config.cache_collection,
                ids=[pid],
                with_payload=True,
                with_vectors=False,
            )
            if not items:
                return None
            item = _normalize_point(items[0])
            if is_payload_expired(item["payload"]):
                try:
                    await self.client.delete(
                        collection_name=self.config.cache_collection,
                        points_selector=[pid],
                        wait=False,
                    )
                except Exception:
                    pass
                return None
            return item
        except Exception:
            return None

    async def semantic_cache_lookup(
        self,
        *,
        query_vector: list[float],
        corpus_version: str,
        prompt_version: str,
        retrieval_version: str,
        model_name: str,
        min_score: float,
    ) -> dict[str, Any] | None:
        if not self.cache_ready:
            return None

        now_epoch = int(time.time())
        flt = models.Filter(
            must=[
                models.FieldCondition(key="cache_group", match=models.MatchValue(value=self.config.cache_group)),
                models.FieldCondition(key="corpus_version", match=models.MatchValue(value=corpus_version or "")),
                models.FieldCondition(key="prompt_version", match=models.MatchValue(value=prompt_version or "")),
                models.FieldCondition(key="retrieval_version", match=models.MatchValue(value=retrieval_version or "")),
                models.FieldCondition(key="model_name", match=models.MatchValue(value=model_name or "")),
                models.FieldCondition(key="expires_at_epoch", range=models.Range(gte=now_epoch)),
            ]
        )

        try:
            resp = await self.client.query_points(
                collection_name=self.config.cache_collection,
                query=query_vector,
                using=self.config.dense_vector_name,
                query_filter=flt,
                limit=1,
                score_threshold=min_score,
                with_payload=True,
                with_vectors=False,
            )
            items = _points_list(resp)
            if not items:
                return None
            item = _normalize_point(items[0])
            if is_payload_expired(item["payload"]):
                return None
            return item
        except Exception:
            return None

    async def semantic_cache_upsert(
        self,
        *,
        cache_id: str,
        query_vector: list[float],
        query_text: str,
        query_norm: str,
        corpus_version: str,
        prompt_version: str,
        retrieval_version: str,
        model_name: str,
        answer: str,
        ui_chunks: list[dict[str, Any]],
        ttl_seconds: int | None = None,
        hit_type: str = "llm",
        cache_score: float = 1.0,
    ) -> bool:
        if not self.cache_ready:
            return False

        ttl_seconds = int(ttl_seconds or self.config.cache_ttl_seconds)
        payload = build_semantic_cache_payload(
            cache_id=cache_id,
            query_text=query_text,
            query_norm=query_norm,
            corpus_version=corpus_version,
            prompt_version=prompt_version,
            retrieval_version=retrieval_version,
            model_name=model_name,
            answer=answer,
            ui_chunks=ui_chunks,
            ttl_seconds=ttl_seconds,
            cache_group=self.config.cache_group,
            hit_type=hit_type,
            cache_score=cache_score,
        )

        point = PointStruct(
            id=_point_id_from_text(cache_id),
            vector={self.config.dense_vector_name: query_vector},
            payload=payload,
        )

        try:
            await self.client.upsert(
                collection_name=self.config.cache_collection,
                points=[point],
                wait=True,
            )
            return True
        except Exception as e:
            logger.warning("semantic cache upsert failed id=%s error=%s", cache_id, e)
            return False

    async def cleanup_expired_cache(self) -> int:
        if not self.cache_ready:
            return 0
        now_epoch = int(time.time())
        flt = models.Filter(
            must=[
                models.FieldCondition(key="cache_group", match=models.MatchValue(value=self.config.cache_group)),
                models.FieldCondition(key="expires_at_epoch", range=models.Range(lt=now_epoch)),
            ]
        )
        try:
            await self.client.delete(
                collection_name=self.config.cache_collection,
                points_selector=models.FilterSelector(filter=flt),
                wait=True,
            )
            return 1
        except Exception:
            return 0

    async def _search_common(
        self,
        *,
        collection_name: str,
        query_filter: models.Filter | None,
        query: Any,
        using: str | None = None,
        prefetch: list[models.Prefetch] | None = None,
        limit: int = 10,
        with_payload: bool = True,
    ) -> list[dict[str, Any]]:
        resp = await self.client.query_points(
            collection_name=collection_name,
            query=query,
            prefetch=prefetch,
            query_filter=query_filter,
            limit=limit,
            with_payload=with_payload,
            with_vectors=False,
            using=using,
        )
        items = _points_list(resp)
        out: list[dict[str, Any]] = []
        seen = set()
        for item in items:
            norm = _normalize_point(item)
            payload = norm["payload"] or {}
            key = payload.get("chunk_id") or norm["id"]
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": norm["id"], "score": norm["score"], "payload": payload})
        return out

    async def dense_search(
        self,
        *,
        query_vector: list[float],
        query_filter: models.Filter | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._search_common(
            collection_name=self.config.docs_collection,
            query_filter=query_filter,
            query=query_vector,
            using=self.config.dense_vector_name,
            limit=limit,
        )

    async def sparse_search(
        self,
        *,
        query_vector: models.SparseVector,
        query_filter: models.Filter | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._search_common(
            collection_name=self.config.docs_collection,
            query_filter=query_filter,
            query=query_vector,
            using=self.config.sparse_vector_name,
            limit=limit,
        )

    async def hybrid_search(
        self,
        *,
        dense_vector: list[float] | None,
        sparse_vector: models.SparseVector | None,
        query_filter: models.Filter | None = None,
        top_k: int = 5,
        dense_prefetch: int = 200,
        sparse_prefetch: int = 200,
    ) -> list[dict[str, Any]]:
        if dense_vector is not None and sparse_vector is not None and self.hybrid_capable:
            prefetch = [
                models.Prefetch(query=dense_vector, using=self.config.dense_vector_name, limit=max(dense_prefetch, top_k)),
                models.Prefetch(query=sparse_vector, using=self.config.sparse_vector_name, limit=max(sparse_prefetch, top_k)),
            ]
            return await self._search_common(
                collection_name=self.config.docs_collection,
                query_filter=query_filter,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                prefetch=prefetch,
                limit=max(top_k, 1),
            )

        if dense_vector is not None:
            return await self.dense_search(query_vector=dense_vector, query_filter=query_filter, limit=top_k)

        if sparse_vector is not None:
            return await self.sparse_search(query_vector=sparse_vector, query_filter=query_filter, limit=top_k)

        return []


__all__ = [
    "QdrantStore",
    "QdrantStoreConfig",
]
