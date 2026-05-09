#!/usr/bin/env python3
# indexing_cronjob.py  —  robust manifest manager for the Indexing CronJob
# Usage: python3 indexing_cronjob.py rollout
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required.", file=sys.stderr)
    raise SystemExit(2) from None

# ---------------------------------------------------------------------------
# Logging (identical to generator)
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("INDEXING_CRON_LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("indexing_cron")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MANIFESTS_DIR = Path("src/manifests/indexing-cronjob")
STATE_DIRNAME = ".state"

DEFAULTS: dict[str, Any] = {
    "NAMESPACE": "indexing",
    "CRONJOB_NAME": "indexing-backup-cronjob",
    "CRON_SCHEDULE": "0 */6 * * *",
    "CRONJOB_CONCURRENCY": "Allow",
    "CRONJOB_BACKOFF_LIMIT": 1,
    "CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT": 3,
    "CRONJOB_FAILED_JOBS_HISTORY_LIMIT": 1,
    "CRONJOB_TIMEZONE": "",
    "SERVICE_ACCOUNT_NAME": "indexer-cron-sa",
    "IMAGE": "ghcr.io/athithya-sakthivel/indexing-pipeline:2026-05-09-09-38--403ed30@sha256:e3fb21311e763228c9fdae6cbb3491d274016159a747c5cdf04215d67e2a1462",
    "CPU_REQUEST": "2",
    "CPU_LIMIT": "6",
    "MEMORY_REQUEST": "1Gi",
    "MEMORY_LIMIT": "2Gi",
    "AWS_CREDENTIALS_SECRET_NAME": "indexer-aws-creds",
    "QDRANT_SECRET_NAME": "qdrant-api-key",
    "IRSA_ROLE_ARN": "",
    "USE_IRSA": False,
    "QDRANT_URL": "http://qdrant.qdrant.svc.cluster.local:6333",
    "DENSE_URL": "http://dense-svc.models.svc.cluster.local:8200",
    "SPARSE_URL": "http://sparse-svc.models.svc.cluster.local:8201",
    "DATA_S3_BUCKET": "",
    "S3_BUCKET": "",
    "DATA_S3_PREFIX": "data/chunked/",
    "AWS_REGION": "",
    "AWS_DEFAULT_REGION": "",
    "STORAGE_RAW_PREFIX": "data/raw/",
    "STORAGE_CHUNKED_PREFIX": "data/chunked/",
    "QDRANT_API_KEY": "",
    "LOG_LEVEL": "INFO",
    "HTTP_TIMEOUT": 60,
    "INDEXING_STRICT": True,
    "RUN_PRE_CONVERSIONS": False,
    "PYTHONUNBUFFERED": "1",
    "MAX_TOKENS_PER_CHUNK": 320,
    "MIN_TOKENS_PER_CHUNK": 100,
    "NUMBER_OF_OVERLAPPING_SENTENCES": 2,
    "PDF_DISABLE_OCR": False,
    "PDF_OCR_ENGINE": "rapidocr",
    "PDF_TESSERACT_LANG": "eng",
    "IMAGE_TESSERACT_LANG": "eng",
    "TESSERACT_CONFIG": "--oem 1 --psm 6",
    "PDF_FORCE_OCR": False,
    "PDF_OCR_RENDER_DPI": 400,
    "PDF_MIN_IMG_SIZE_BYTES": 3072,
    "IMAGE_OCR_ENGINE": "tesseract",
    "IMAGE_MIN_IMG_SIZE_BYTES": 3072,
    "IMAGE_RENDER_DPI": 400,
    "IMAGE_UPSCALE_FACTOR": 2.0,
    "CSV_TARGET_TOKENS_PER_CHUNK": 400,
    "JSONL_TARGET_TOKENS_PER_CHUNK": 400,
    "PPTX_SLIDES_PER_CHUNK": 4,
    "PPTX_OCR_ENGINE": "rapidocr",
    "COLLECTION_NAME": "default_rag_collection1",
    "DENSE_DIM": 384,
    "BATCH_SIZE": 8,
    "UPSERT_CHUNK": 500,
    "SPARSE_BATCH_FALLBACK": 8,
    "QDRANT_SHARD_NUMBER": 3,
    "QDRANT_REPLICATION_FACTOR": 2,
    "QDRANT_WRITE_CONSISTENCY_FACTOR": 1,
    "QDRANT_HNSW_EF_CONSTRUCT": 128,
    "QDRANT_HNSW_M": 32,
    "QDRANT_HNSW_FULL_SCAN_THRESHOLD": 10000,
    "QDRANT_ONDISK": False,
    "QDRANT_ENABLE_SCALAR_QUANTIZATION": True,
    "QDRANT_QUANTIZATION_ALWAYS_RAM": True,
    "INDEX_TIMEOUT": 1800,
    "BACKUP_TIMEOUT": 300,
    "ENABLE_QDRANT_BACKUP": True,
    "MIN_INDEXED_POINTS_FOR_BACKUP": 100,
    "MIN_INDEX_DELTA_RATIO_FOR_BACKUP": 0.0,
    "TMPDIR": "/tmp",
}

# Sensitive keys – will only be applied directly to the cluster, never stored in YAML
SECRET_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "QDRANT_API_KEY")

# ---------------------------------------------------------------------------
# Typed environment helpers
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip()
    return text if text else default

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Atomic file write
# ---------------------------------------------------------------------------
def atomic_write(path: Path, content: str) -> None:
    """Write content atomically using a temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

# ---------------------------------------------------------------------------
# Hashing (for change detection, identical to generator)
# ---------------------------------------------------------------------------
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def canonical_inputs_hash(payload: dict[str, Any]) -> str:
    """Hash of all config that affects the manifests, excluding state fields."""
    serial: dict[str, Any] = {}
    for k in sorted(payload.keys()):
        if k in ("INPUTS_HASH_PATH", "MANIFESTS_DIR", "STATE_DIRNAME", "FILES", "UUID_SHORT"):
            continue
        v = payload.get(k)
        try:
            json.dumps(v)
            serial[k] = v
        except Exception:
            serial[k] = str(v)
    j = json.dumps(serial, sort_keys=True, separators=(",", ":"))
    return _sha256(j)

def secret_keys_hash(secret_env: dict[str, str]) -> str:
    """Hash only the names of enabled secret keys (not their values)."""
    payload = json.dumps(sorted(secret_env.keys()), separators=(",", ":"), ensure_ascii=False)
    return _sha256(payload)

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------
def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}

    # paths
    cfg["MANIFESTS_DIR"] = Path(os.getenv("MANIFESTS_DIR", str(MANIFESTS_DIR)))
    cfg["STATE_DIRNAME"] = os.getenv("STATE_DIRNAME", STATE_DIRNAME)
    cfg["INPUTS_HASH_PATH"] = cfg["MANIFESTS_DIR"] / cfg["STATE_DIRNAME"] / "inputs.sha256"

    # core settings
    cfg["NAMESPACE"] = _env_str("NAMESPACE", DEFAULTS["NAMESPACE"])
    cfg["CRONJOB_NAME"] = _env_str("CRONJOB_NAME", DEFAULTS["CRONJOB_NAME"])
    cfg["CRON_SCHEDULE"] = _env_str("CRON_SCHEDULE", DEFAULTS["CRON_SCHEDULE"])
    cfg["CRONJOB_CONCURRENCY"] = _env_str("CRONJOB_CONCURRENCY", DEFAULTS["CRONJOB_CONCURRENCY"])
    cfg["CRONJOB_BACKOFF_LIMIT"] = _env_int("CRONJOB_BACKOFF_LIMIT", DEFAULTS["CRONJOB_BACKOFF_LIMIT"])
    cfg["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"] = _env_int("CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT", DEFAULTS["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"])
    cfg["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"] = _env_int("CRONJOB_FAILED_JOBS_HISTORY_LIMIT", DEFAULTS["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"])
    cfg["CRONJOB_TIMEZONE"] = _env_str("CRONJOB_TIMEZONE", DEFAULTS["CRONJOB_TIMEZONE"])
    cfg["SERVICE_ACCOUNT_NAME"] = _env_str("SERVICE_ACCOUNT_NAME", DEFAULTS["SERVICE_ACCOUNT_NAME"])
    cfg["IMAGE"] = _env_str("IMAGE", DEFAULTS["IMAGE"])
    cfg["CPU_REQUEST"] = _env_str("CPU_REQUEST", DEFAULTS["CPU_REQUEST"])
    cfg["CPU_LIMIT"] = _env_str("CPU_LIMIT", DEFAULTS["CPU_LIMIT"])
    cfg["MEMORY_REQUEST"] = _env_str("MEMORY_REQUEST", DEFAULTS["MEMORY_REQUEST"])
    cfg["MEMORY_LIMIT"] = _env_str("MEMORY_LIMIT", DEFAULTS["MEMORY_LIMIT"])
    cfg["AWS_CREDENTIALS_SECRET_NAME"] = _env_str("AWS_CREDENTIALS_SECRET_NAME", DEFAULTS["AWS_CREDENTIALS_SECRET_NAME"])
    cfg["QDRANT_SECRET_NAME"] = _env_str("QDRANT_SECRET_NAME", DEFAULTS["QDRANT_SECRET_NAME"])
    cfg["USE_IRSA"] = _env_bool("USE_IRSA", DEFAULTS["USE_IRSA"])
    cfg["IRSA_ROLE_ARN"] = _env_str("IRSA_ROLE_ARN", DEFAULTS["IRSA_ROLE_ARN"])

    # external services & S3
    cfg["QDRANT_URL"] = _env_str("QDRANT_URL", DEFAULTS["QDRANT_URL"])
    cfg["DENSE_URL"] = _env_str("DENSE_URL", DEFAULTS["DENSE_URL"])
    cfg["SPARSE_URL"] = _env_str("SPARSE_URL", DEFAULTS["SPARSE_URL"])
    s3 = _env_str("DATA_S3_BUCKET", "") or _env_str("S3_BUCKET", DEFAULTS["DATA_S3_BUCKET"])
    cfg["DATA_S3_BUCKET"] = s3
    cfg["S3_BUCKET"] = s3
    cfg["DATA_S3_PREFIX"] = _env_str("DATA_S3_PREFIX", DEFAULTS["DATA_S3_PREFIX"])
    region = _env_str("AWS_REGION", "") or _env_str("AWS_DEFAULT_REGION", DEFAULTS["AWS_REGION"])
    cfg["AWS_REGION"] = region
    cfg["AWS_DEFAULT_REGION"] = region
    cfg["STORAGE_RAW_PREFIX"] = _env_str("STORAGE_RAW_PREFIX", DEFAULTS["STORAGE_RAW_PREFIX"])
    cfg["STORAGE_CHUNKED_PREFIX"] = _env_str("STORAGE_CHUNKED_PREFIX", DEFAULTS["STORAGE_CHUNKED_PREFIX"])
    cfg["QDRANT_API_KEY"] = _env_str("QDRANT_API_KEY", DEFAULTS["QDRANT_API_KEY"])

    # pipeline tuning
    cfg["LOG_LEVEL"] = _env_str("LOG_LEVEL", DEFAULTS["LOG_LEVEL"])
    cfg["HTTP_TIMEOUT"] = _env_int("HTTP_TIMEOUT", DEFAULTS["HTTP_TIMEOUT"])
    cfg["INDEXING_STRICT"] = _env_bool("INDEXING_STRICT", DEFAULTS["INDEXING_STRICT"])
    cfg["RUN_PRE_CONVERSIONS"] = _env_bool("RUN_PRE_CONVERSIONS", DEFAULTS["RUN_PRE_CONVERSIONS"])
    cfg["PYTHONUNBUFFERED"] = _env_str("PYTHONUNBUFFERED", DEFAULTS["PYTHONUNBUFFERED"])
    cfg["MAX_TOKENS_PER_CHUNK"] = _env_int("MAX_TOKENS_PER_CHUNK", DEFAULTS["MAX_TOKENS_PER_CHUNK"])
    cfg["MIN_TOKENS_PER_CHUNK"] = _env_int("MIN_TOKENS_PER_CHUNK", DEFAULTS["MIN_TOKENS_PER_CHUNK"])
    cfg["NUMBER_OF_OVERLAPPING_SENTENCES"] = _env_int("NUMBER_OF_OVERLAPPING_SENTENCES", DEFAULTS["NUMBER_OF_OVERLAPPING_SENTENCES"])
    cfg["PDF_DISABLE_OCR"] = _env_bool("PDF_DISABLE_OCR", DEFAULTS["PDF_DISABLE_OCR"])
    cfg["PDF_OCR_ENGINE"] = _env_str("PDF_OCR_ENGINE", DEFAULTS["PDF_OCR_ENGINE"])
    cfg["PDF_TESSERACT_LANG"] = _env_str("PDF_TESSERACT_LANG", DEFAULTS["PDF_TESSERACT_LANG"])
    cfg["IMAGE_TESSERACT_LANG"] = _env_str("IMAGE_TESSERACT_LANG", DEFAULTS["IMAGE_TESSERACT_LANG"])
    cfg["TESSERACT_CONFIG"] = _env_str("TESSERACT_CONFIG", DEFAULTS["TESSERACT_CONFIG"])
    cfg["PDF_FORCE_OCR"] = _env_bool("PDF_FORCE_OCR", DEFAULTS["PDF_FORCE_OCR"])
    cfg["PDF_OCR_RENDER_DPI"] = _env_int("PDF_OCR_RENDER_DPI", DEFAULTS["PDF_OCR_RENDER_DPI"])
    cfg["PDF_MIN_IMG_SIZE_BYTES"] = _env_int("PDF_MIN_IMG_SIZE_BYTES", DEFAULTS["PDF_MIN_IMG_SIZE_BYTES"])
    cfg["IMAGE_OCR_ENGINE"] = _env_str("IMAGE_OCR_ENGINE", DEFAULTS["IMAGE_OCR_ENGINE"])
    cfg["IMAGE_MIN_IMG_SIZE_BYTES"] = _env_int("IMAGE_MIN_IMG_SIZE_BYTES", DEFAULTS["IMAGE_MIN_IMG_SIZE_BYTES"])
    cfg["IMAGE_RENDER_DPI"] = _env_int("IMAGE_RENDER_DPI", DEFAULTS["IMAGE_RENDER_DPI"])
    cfg["IMAGE_UPSCALE_FACTOR"] = _env_float("IMAGE_UPSCALE_FACTOR", DEFAULTS["IMAGE_UPSCALE_FACTOR"])
    cfg["CSV_TARGET_TOKENS_PER_CHUNK"] = _env_int("CSV_TARGET_TOKENS_PER_CHUNK", DEFAULTS["CSV_TARGET_TOKENS_PER_CHUNK"])
    cfg["JSONL_TARGET_TOKENS_PER_CHUNK"] = _env_int("JSONL_TARGET_TOKENS_PER_CHUNK", DEFAULTS["JSONL_TARGET_TOKENS_PER_CHUNK"])
    cfg["PPTX_SLIDES_PER_CHUNK"] = _env_int("PPTX_SLIDES_PER_CHUNK", DEFAULTS["PPTX_SLIDES_PER_CHUNK"])
    cfg["PPTX_OCR_ENGINE"] = _env_str("PPTX_OCR_ENGINE", DEFAULTS["PPTX_OCR_ENGINE"])
    cfg["COLLECTION_NAME"] = _env_str("COLLECTION_NAME", DEFAULTS["COLLECTION_NAME"])
    cfg["DENSE_DIM"] = _env_int("DENSE_DIM", DEFAULTS["DENSE_DIM"])
    cfg["BATCH_SIZE"] = _env_int("BATCH_SIZE", DEFAULTS["BATCH_SIZE"])
    cfg["UPSERT_CHUNK"] = _env_int("UPSERT_CHUNK", DEFAULTS["UPSERT_CHUNK"])
    cfg["SPARSE_BATCH_FALLBACK"] = _env_int("SPARSE_BATCH_FALLBACK", DEFAULTS["SPARSE_BATCH_FALLBACK"])
    cfg["QDRANT_SHARD_NUMBER"] = _env_int("QDRANT_SHARD_NUMBER", DEFAULTS["QDRANT_SHARD_NUMBER"])
    cfg["QDRANT_REPLICATION_FACTOR"] = _env_int("QDRANT_REPLICATION_FACTOR", DEFAULTS["QDRANT_REPLICATION_FACTOR"])
    cfg["QDRANT_WRITE_CONSISTENCY_FACTOR"] = _env_int("QDRANT_WRITE_CONSISTENCY_FACTOR", DEFAULTS["QDRANT_WRITE_CONSISTENCY_FACTOR"])
    cfg["QDRANT_HNSW_EF_CONSTRUCT"] = _env_int("QDRANT_HNSW_EF_CONSTRUCT", DEFAULTS["QDRANT_HNSW_EF_CONSTRUCT"])
    cfg["QDRANT_HNSW_M"] = _env_int("QDRANT_HNSW_M", DEFAULTS["QDRANT_HNSW_M"])
    cfg["QDRANT_HNSW_FULL_SCAN_THRESHOLD"] = _env_int("QDRANT_HNSW_FULL_SCAN_THRESHOLD", DEFAULTS["QDRANT_HNSW_FULL_SCAN_THRESHOLD"])
    cfg["QDRANT_ONDISK"] = _env_bool("QDRANT_ONDISK", DEFAULTS["QDRANT_ONDISK"])
    cfg["QDRANT_ENABLE_SCALAR_QUANTIZATION"] = _env_bool("QDRANT_ENABLE_SCALAR_QUANTIZATION", DEFAULTS["QDRANT_ENABLE_SCALAR_QUANTIZATION"])
    cfg["QDRANT_QUANTIZATION_ALWAYS_RAM"] = _env_bool("QDRANT_QUANTIZATION_ALWAYS_RAM", DEFAULTS["QDRANT_QUANTIZATION_ALWAYS_RAM"])
    cfg["INDEX_TIMEOUT"] = _env_int("INDEX_TIMEOUT", DEFAULTS["INDEX_TIMEOUT"])
    cfg["BACKUP_TIMEOUT"] = _env_int("BACKUP_TIMEOUT", DEFAULTS["BACKUP_TIMEOUT"])
    cfg["ENABLE_QDRANT_BACKUP"] = _env_bool("ENABLE_QDRANT_BACKUP", DEFAULTS["ENABLE_QDRANT_BACKUP"])
    cfg["MIN_INDEXED_POINTS_FOR_BACKUP"] = _env_int("MIN_INDEXED_POINTS_FOR_BACKUP", DEFAULTS["MIN_INDEXED_POINTS_FOR_BACKUP"])
    cfg["MIN_INDEX_DELTA_RATIO_FOR_BACKUP"] = _env_float("MIN_INDEX_DELTA_RATIO_FOR_BACKUP", DEFAULTS["MIN_INDEX_DELTA_RATIO_FOR_BACKUP"])
    cfg["TMPDIR"] = _env_str("TMPDIR", DEFAULTS["TMPDIR"])

    cfg["FILES"] = {
        "namespace": cfg["MANIFESTS_DIR"] / "00-namespace.yaml",
        "serviceaccount": cfg["MANIFESTS_DIR"] / "10-serviceaccount.yaml",
        "cronjob": cfg["MANIFESTS_DIR"] / "50-cronjob.yaml",
    }
    cfg["UUID_SHORT"] = str(uuid.uuid4())[:8]
    log.info("Loaded config: namespace=%s cronjob=%s schedule=%s",
             cfg["NAMESPACE"], cfg["CRONJOB_NAME"], cfg["CRON_SCHEDULE"])
    return cfg

def collect_secret_env() -> dict[str, str]:
    """Return a dict of non‑empty secret values found in the environment."""
    return {k: v.strip() for k in SECRET_KEYS if (v := os.getenv(k, "").strip())}

# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------
def build_namespace_manifest(ns: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}

def build_serviceaccount_manifest(cfg: dict[str, Any], mode: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": cfg["SERVICE_ACCOUNT_NAME"], "namespace": cfg["NAMESPACE"]}
    if mode in ("eks", "eks-auto") and cfg.get("IRSA_ROLE_ARN"):
        meta["annotations"] = {"eks.amazonaws.com/role-arn": cfg["IRSA_ROLE_ARN"]}
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta}

def build_cronjob_manifest(cfg: dict[str, Any], mode: str) -> dict[str, Any]:
    ns = cfg["NAMESPACE"]
    name = cfg["CRONJOB_NAME"]
    image = cfg["IMAGE"]

    # all plain environment variables
    env = [{"name": k, "value": str(v)} for k, v in {
        "PYTHONUNBUFFERED": cfg["PYTHONUNBUFFERED"],
        "TMPDIR": cfg["TMPDIR"],
        "LOG_LEVEL": cfg["LOG_LEVEL"],
        "HTTP_TIMEOUT": str(cfg["HTTP_TIMEOUT"]),
        "INDEXING_STRICT": str(cfg["INDEXING_STRICT"]).lower(),
        "RUN_PRE_CONVERSIONS": str(cfg["RUN_PRE_CONVERSIONS"]).lower(),
        "QDRANT_URL": cfg["QDRANT_URL"],
        "DENSE_URL": cfg["DENSE_URL"],
        "SPARSE_URL": cfg["SPARSE_URL"],
        "S3_BUCKET": cfg["S3_BUCKET"],
        "DATA_S3_BUCKET": cfg["DATA_S3_BUCKET"],
        "DATA_S3_PREFIX": cfg["DATA_S3_PREFIX"],
        "STORAGE_RAW_PREFIX": cfg["STORAGE_RAW_PREFIX"],
        "STORAGE_CHUNKED_PREFIX": cfg["STORAGE_CHUNKED_PREFIX"],
        "AWS_REGION": cfg["AWS_REGION"] or cfg["AWS_DEFAULT_REGION"],
        "AWS_DEFAULT_REGION": cfg["AWS_DEFAULT_REGION"],
        "AWS_SDK_LOAD_CONFIG": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "MAX_TOKENS_PER_CHUNK": str(cfg["MAX_TOKENS_PER_CHUNK"]),
        "MIN_TOKENS_PER_CHUNK": str(cfg["MIN_TOKENS_PER_CHUNK"]),
        "NUMBER_OF_OVERLAPPING_SENTENCES": str(cfg["NUMBER_OF_OVERLAPPING_SENTENCES"]),
        "PDF_DISABLE_OCR": str(cfg["PDF_DISABLE_OCR"]).lower(),
        "PDF_OCR_ENGINE": cfg["PDF_OCR_ENGINE"],
        "PDF_TESSERACT_LANG": cfg["PDF_TESSERACT_LANG"],
        "IMAGE_TESSERACT_LANG": cfg["IMAGE_TESSERACT_LANG"],
        "TESSERACT_CONFIG": cfg["TESSERACT_CONFIG"],
        "PDF_FORCE_OCR": str(cfg["PDF_FORCE_OCR"]).lower(),
        "PDF_OCR_RENDER_DPI": str(cfg["PDF_OCR_RENDER_DPI"]),
        "PDF_MIN_IMG_SIZE_BYTES": str(cfg["PDF_MIN_IMG_SIZE_BYTES"]),
        "IMAGE_OCR_ENGINE": cfg["IMAGE_OCR_ENGINE"],
        "IMAGE_MIN_IMG_SIZE_BYTES": str(cfg["IMAGE_MIN_IMG_SIZE_BYTES"]),
        "IMAGE_RENDER_DPI": str(cfg["IMAGE_RENDER_DPI"]),
        "IMAGE_UPSCALE_FACTOR": str(cfg["IMAGE_UPSCALE_FACTOR"]),
        "CSV_TARGET_TOKENS_PER_CHUNK": str(cfg["CSV_TARGET_TOKENS_PER_CHUNK"]),
        "JSONL_TARGET_TOKENS_PER_CHUNK": str(cfg["JSONL_TARGET_TOKENS_PER_CHUNK"]),
        "PPTX_SLIDES_PER_CHUNK": str(cfg["PPTX_SLIDES_PER_CHUNK"]),
        "PPTX_OCR_ENGINE": cfg["PPTX_OCR_ENGINE"],
        "COLLECTION_NAME": cfg["COLLECTION_NAME"],
        "DENSE_DIM": str(cfg["DENSE_DIM"]),
        "BATCH_SIZE": str(cfg["BATCH_SIZE"]),
        "UPSERT_CHUNK": str(cfg["UPSERT_CHUNK"]),
        "SPARSE_BATCH_FALLBACK": str(cfg["SPARSE_BATCH_FALLBACK"]),
        "QDRANT_SHARD_NUMBER": str(cfg["QDRANT_SHARD_NUMBER"]),
        "QDRANT_REPLICATION_FACTOR": str(cfg["QDRANT_REPLICATION_FACTOR"]),
        "QDRANT_WRITE_CONSISTENCY_FACTOR": str(cfg["QDRANT_WRITE_CONSISTENCY_FACTOR"]),
        "QDRANT_HNSW_EF_CONSTRUCT": str(cfg["QDRANT_HNSW_EF_CONSTRUCT"]),
        "QDRANT_HNSW_M": str(cfg["QDRANT_HNSW_M"]),
        "QDRANT_HNSW_FULL_SCAN_THRESHOLD": str(cfg["QDRANT_HNSW_FULL_SCAN_THRESHOLD"]),
        "QDRANT_ONDISK": str(cfg["QDRANT_ONDISK"]).lower(),
        "QDRANT_ENABLE_SCALAR_QUANTIZATION": str(cfg["QDRANT_ENABLE_SCALAR_QUANTIZATION"]).lower(),
        "QDRANT_QUANTIZATION_ALWAYS_RAM": str(cfg["QDRANT_QUANTIZATION_ALWAYS_RAM"]).lower(),
        "INDEX_TIMEOUT": str(cfg["INDEX_TIMEOUT"]),
        "BACKUP_TIMEOUT": str(cfg["BACKUP_TIMEOUT"]),
        "ENABLE_QDRANT_BACKUP": str(cfg["ENABLE_QDRANT_BACKUP"]).lower(),
        "MIN_INDEXED_POINTS_FOR_BACKUP": str(cfg["MIN_INDEXED_POINTS_FOR_BACKUP"]),
        "MIN_INDEX_DELTA_RATIO_FOR_BACKUP": str(cfg["MIN_INDEX_DELTA_RATIO_FOR_BACKUP"]),
    }.items()]

    # Qdrant API key (if provided) comes from its own secret
    if cfg.get("QDRANT_API_KEY"):
        env.append({
            "name": "QDRANT_API_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": cfg["QDRANT_SECRET_NAME"], "key": "QDRANT_API_KEY"}
            }
        })

    # In non‑IRSA (kind) mode, add AWS credential secret references
    if mode == "kind":
        env.append({
            "name": "AWS_ACCESS_KEY_ID",
            "valueFrom": {"secretKeyRef": {"name": cfg["AWS_CREDENTIALS_SECRET_NAME"], "key": "AWS_ACCESS_KEY_ID"}}
        })
        env.append({
            "name": "AWS_SECRET_ACCESS_KEY",
            "valueFrom": {"secretKeyRef": {"name": cfg["AWS_CREDENTIALS_SECRET_NAME"], "key": "AWS_SECRET_ACCESS_KEY"}}
        })

    container = {
        "name": "indexer",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/opt/venv/bin/python", "/indexing_pipeline/indexing_pipeline.py"],
        "args": ["--workdir", "/indexing_pipeline"],
        "env": env,
        "workingDir": "/indexing_pipeline",
        "volumeMounts": [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "tmp", "mountPath": "/indexing_pipeline/tmp"},
        ],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {
            "requests": {"cpu": cfg["CPU_REQUEST"], "memory": cfg["MEMORY_REQUEST"]},
            "limits": {"cpu": cfg["CPU_LIMIT"], "memory": cfg["MEMORY_LIMIT"]},
        },
    }

    pod_spec = {
        "serviceAccountName": cfg["SERVICE_ACCOUNT_NAME"],
        "restartPolicy": "Never",
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "fsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumes": [{"name": "tmp", "emptyDir": {}}],
        "containers": [container],
    }

    cronjob_spec = {
        "schedule": cfg["CRON_SCHEDULE"],
        "concurrencyPolicy": cfg["CRONJOB_CONCURRENCY"],
        "successfulJobsHistoryLimit": cfg["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"],
        "failedJobsHistoryLimit": cfg["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"],
        "jobTemplate": {
            "spec": {
                "backoffLimit": cfg["CRONJOB_BACKOFF_LIMIT"],
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/name": name,
                            "app.kubernetes.io/component": "indexing",
                        }
                    },
                    "spec": pod_spec,
                },
            }
        },
    }
    if cfg["CRONJOB_TIMEZONE"]:
        cronjob_spec["timeZone"] = cfg["CRONJOB_TIMEZONE"]

    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": name, "namespace": ns},
        "spec": cronjob_spec,
    }

# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------
def _namespace_phase(ns: str) -> str | None:
    rc, out, _ = subprocess.run(
        ["kubectl", "get", "namespace", ns, "-o", "json"],
        check=False, capture_output=True, text=True,
    )
    if rc != 0:
        return None
    try:
        return json.loads(out).get("status", {}).get("phase", "")
    except Exception:
        return None

def _force_finalize_namespace(ns: str) -> None:
    rc, out, _ = subprocess.run(
        ["kubectl", "get", "namespace", ns, "-o", "json"],
        check=False, capture_output=True, text=True,
    )
    if rc != 0:
        return
    try:
        data = json.loads(out)
    except Exception:
        return
    data.setdefault("spec", {})["finalizers"] = []
    subprocess.run(
        ["kubectl", "replace", "--raw", f"/api/v1/namespaces/{ns}/finalize", "-f", "-"],
        input=json.dumps(data), text=True, check=False, capture_output=True,
    )

def ensure_namespace(cfg: dict[str, Any]) -> None:
    """Make sure the namespace exists and is Active."""
    ns = cfg["NAMESPACE"]
    phase = _namespace_phase(ns)

    if phase == "Terminating":
        log.warning("Namespace %s is terminating – force finalizing", ns)
        _force_finalize_namespace(ns)
        import time
        for _ in range(15):
            if _namespace_phase(ns) is None:
                break
            time.sleep(2)
        phase = None

    if phase is None:
        log.info("Creating namespace %s", ns)
        subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=yaml.safe_dump(build_namespace_manifest(ns)),
            text=True, check=True, capture_output=True,
        )
        import time
        for _ in range(30):
            if _namespace_phase(ns) == "Active":
                break
            time.sleep(2)

    if _namespace_phase(ns) != "Active":
        raise RuntimeError(f"Namespace {ns} could not be brought to Active")

# ---------------------------------------------------------------------------
# Secret application (direct, no YAML written)
# ---------------------------------------------------------------------------
def apply_secret_direct(name: str, ns: str, data: dict[str, str]) -> None:
    """Create/update a Kubernetes secret directly from key‑value pairs."""
    if not data:
        return
    cmd = ["kubectl", "create", "secret", "generic", name, "-n", ns]
    for k, v in sorted(data.items()):
        cmd.extend(["--from-literal", f"{k}={v}"])
    cmd.extend(["--dry-run=client", "-o", "yaml"])
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    subprocess.run(["kubectl", "apply", "-f", "-"], input=proc.stdout, text=True, check=True, capture_output=True)
    log.info("Applied secret '%s' in namespace '%s'", name, ns)

# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------
def generate_manifests(cfg: dict[str, Any], secret_env: dict[str, str], force: bool = False) -> None:
    """
    Write YAML manifests to disk, using atomic writes.
    If any manifest file is missing, regeneration is forced.
    """
    manifests_dir = cfg["MANIFESTS_DIR"]
    manifests_dir.mkdir(parents=True, exist_ok=True)
    files = cfg["FILES"]

    # Force regeneration if any file is absent
    if not force:
        for p in files.values():
            if not p.exists():
                log.info("Missing %s, forcing regeneration", p.name)
                force = True
                break

    mode = "eks" if cfg["USE_IRSA"] else "kind"
    keyhash = secret_keys_hash(secret_env)
    payload = {
        "ns": cfg["NAMESPACE"],
        "cron": cfg["CRONJOB_NAME"],
        "schedule": cfg["CRON_SCHEDULE"],
        "image": cfg["IMAGE"],
        "mode": mode,
        "keyhash": keyhash,
        "irsa": cfg["IRSA_ROLE_ARN"],
    }
    inputs_hash = canonical_inputs_hash(payload)

    state_dir = manifests_dir / cfg["STATE_DIRNAME"]
    state_dir.mkdir(parents=True, exist_ok=True)
    hash_file = state_dir / "inputs.sha256"
    old_hash = hash_file.read_text().strip() if hash_file.exists() else ""

    if not force and old_hash == inputs_hash:
        log.info("No changes detected; using existing manifests.")
        return

    # Build documents
    ns_doc = build_namespace_manifest(cfg["NAMESPACE"])
    sa_doc = build_serviceaccount_manifest(cfg, mode)
    cj_doc = build_cronjob_manifest(cfg, mode)

    # Write atomically
    atomic_write(files["namespace"], yaml.safe_dump(ns_doc, sort_keys=False))
    atomic_write(files["serviceaccount"], yaml.safe_dump(sa_doc, sort_keys=False))
    atomic_write(files["cronjob"], yaml.safe_dump(cj_doc, sort_keys=False))

    hash_file.write_text(inputs_hash + "\n")
    log.info("Manifests written to %s (hash=%s)", manifests_dir, inputs_hash)

def rollout(cfg: dict[str, Any], secret_env: dict[str, str],
            dry_run: bool = False, force: bool = False) -> None:
    """
    Full rollout:
      - ensure manifests are on disk
      - create namespace (if needed)
      - apply secrets directly
      - apply the CronJob and ServiceAccount
    """
    generate_manifests(cfg, secret_env, force=force)

    if dry_run:
        log.info("[DRY RUN] Would apply namespace, secrets, and manifests.")
        return

    # 1. Namespace must be present before secrets
    ensure_namespace(cfg)

    # 2. Apply secrets (they must exist before the pod references them)
    if secret_env.get("QDRANT_API_KEY"):
        apply_secret_direct(cfg["QDRANT_SECRET_NAME"], cfg["NAMESPACE"],
                            {"QDRANT_API_KEY": secret_env["QDRANT_API_KEY"]})

    if not cfg["USE_IRSA"]:
        aws_data = {k: secret_env[k] for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
                    if k in secret_env}
        if aws_data:
            apply_secret_direct(cfg["AWS_CREDENTIALS_SECRET_NAME"], cfg["NAMESPACE"], aws_data)

    # 3. Apply the ServiceAccount and CronJob (these are idempotent)
    for file_key in ("serviceaccount", "cronjob"):
        path = cfg["FILES"][file_key]
        if not path.exists():
            log.warning("Manifest %s not found – skipping", path)
            continue
        log.info("Applying %s", path)
        try:
            subprocess.run(["kubectl", "replace", "--force", "-f", str(path)],
                           check=True, capture_output=True)
        except subprocess.CalledProcessError:
            subprocess.run(["kubectl", "apply", "-f", str(path)],
                           check=True, capture_output=True)

    log.info("Rollout completed successfully.")

def delete(cfg: dict[str, Any], delete_secrets: bool = False) -> None:
    """
    Delete the Kubernetes namespace (which cascades all resources).
    Local manifest files are **never** removed – they remain for future apply.
    """
    ns = cfg["NAMESPACE"]
    log.info("Deleting resources in namespace %s", ns)

    # Delete namespace (cascades all objects)
    subprocess.run(["kubectl", "delete", "namespace", ns, "--ignore-not-found"],
                   check=False, capture_output=True)

    # Force finalize if stuck
    if _namespace_phase(ns) == "Terminating":
        _force_finalize_namespace(ns)

    # Optionally delete secrets (they are not inside the namespace)
    if delete_secrets:
        for secret in (cfg["QDRANT_SECRET_NAME"], cfg["AWS_CREDENTIALS_SECRET_NAME"]):
            subprocess.run(["kubectl", "delete", "secret", secret, "-n", ns,
                            "--ignore-not-found"], check=False, capture_output=True)

    log.info("Deletion completed. Local manifests remain intact.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Robust Indexing CronJob manager.")
    sub = p.add_subparsers(dest="action", required=True)

    roll = sub.add_parser("rollout", help="Write manifests and deploy everything")
    roll.add_argument("--dry-run", action="store_true", help="Only show what would be done")
    roll.add_argument("--force", action="store_true", help="Force regeneration of manifests")
    roll.add_argument("--verbose", action="store_true", help="Enable debug logging")

    write = sub.add_parser("write", help="Only write manifests to disk (no apply)")
    write.add_argument("--force", action="store_true", help="Force regeneration")
    write.add_argument("--verbose", action="store_true")

    del_cmd = sub.add_parser("delete", help="Delete Kubernetes resources (local files stay)")
    del_cmd.add_argument("--delete-secret", action="store_true", help="Also delete in-cluster secrets")
    del_cmd.add_argument("--dry-run", action="store_true")
    del_cmd.add_argument("--verbose", action="store_true")

    return p.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not shutil.which("kubectl"):
        log.error("kubectl not found in PATH. Cannot continue.")
        return 2

    cfg = load_config()
    secret_env = collect_secret_env()

    try:
        if args.action == "rollout":
            rollout(cfg, secret_env,
                    dry_run=getattr(args, "dry_run", False),
                    force=getattr(args, "force", False))
        elif args.action == "write":
            generate_manifests(cfg, secret_env, force=getattr(args, "force", False))
        elif args.action == "delete":
            if getattr(args, "dry_run", False):
                log.info("[DRY RUN] Would delete namespace (and optionally secrets).")
            else:
                delete(cfg, delete_secrets=getattr(args, "delete_secret", False))
        return 0
    except subprocess.CalledProcessError as e:
        log.error("kubectl error: %s", e)
        return e.returncode or 1
    except Exception:
        log.exception("Fatal error")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())