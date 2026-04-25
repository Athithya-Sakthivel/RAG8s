#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None


DEFAULTS: dict[str, str] = {
    "NAMESPACE": "indexing",
    "CRONJOB_NAME": "indexing-backup-cronjob",
    "CRON_SCHEDULE": "0 */6 * * *",
    "CRONJOB_CONCURRENCY": "Allow",
    "CRONJOB_BACKOFF_LIMIT": "1",
    "CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT": "3",
    "CRONJOB_FAILED_JOBS_HISTORY_LIMIT": "1",
    "CRONJOB_TIMEZONE": "",
    "SERVICE_ACCOUNT_NAME": "indexer-cron-sa",
    "MANIFESTS_DIR": "src/manifests/indexing_cronjob",
    "INDEXING_PIPELINE_CPU_IMAGE_REPO": "ghcr.io/athithya-sakthivel/indexing-pipeline",
    "INDEXING_PIPELINE_CPU_IMAGE_TAG": "2026-04-25-12-23--fd49e79@sha256:4917ba1f0c2e1d9b7fee53719acdbebb1aa86bf32eec75cd01fa57dcd0b86846",
    "INDEXING_BACKUP_CRONJOB_CPU_REQUEST": "2",
    "INDEXING_BACKUP_CRONJOB_CPU_LIMIT": "6",
    "INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST": "1Gi",
    "INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT": "2Gi",
    "LOG_LEVEL": "INFO",
    "HTTP_TIMEOUT": "60",
    "INDEXING_STRICT": "1",
    "RUN_PRE_CONVERSIONS": "0",
    "PYTHONUNBUFFERED": "1",
    "QDRANT_URL": "http://qdrant.qdrant.svc.cluster.local:6333",
    "DENSE_URL": "http://dense-svc.models.svc.cluster.local:8200",
    "SPARSE_URL": "http://sparse-svc.models.svc.cluster.local:8201",
    "DATA_S3_BUCKET": "",
    "DATA_S3_PREFIX": "data/chunked/",
    "AWS_REGION": "",
    "AWS_DEFAULT_REGION": "",
    "STORAGE_RAW_PREFIX": "data/raw/",
    "STORAGE_CHUNKED_PREFIX": "data/chunked/",
    "QDRANT_API_KEY": "",
    "QDRANT_SECRET_NAME": "qdrant-api-key",
    "AWS_CREDENTIALS_SECRET_NAME": "indexer-aws-creds",
    "EXTRA_SECRET_NAME": "indexer-extra-secrets",
    "USE_IRSA": "",
    "IRSA_ROLE_ARN": "",
    "K8S_CLUSTER": "",
    "MAX_TOKENS_PER_CHUNK": "320",
    "MIN_TOKENS_PER_CHUNK": "100",
    "NUMBER_OF_OVERLAPPING_SENTENCES": "2",
    "PDF_DISABLE_OCR": "false",
    "PDF_OCR_ENGINE": "rapidocr",
    "PDF_TESSERACT_LANG": "eng",
    "IMAGE_TESSERACT_LANG": "eng",
    "TESSERACT_CONFIG": "--oem 1 --psm 6",
    "PDF_FORCE_OCR": "false",
    "PDF_OCR_RENDER_DPI": "400",
    "PDF_MIN_IMG_SIZE_BYTES": "3072",
    "IMAGE_OCR_ENGINE": "tesseract",
    "IMAGE_MIN_IMG_SIZE_BYTES": "3072",
    "IMAGE_RENDER_DPI": "400",
    "IMAGE_UPSCALE_FACTOR": "2.0",
    "CSV_TARGET_TOKENS_PER_CHUNK": "400",
    "JSONL_TARGET_TOKENS_PER_CHUNK": "400",
    "PPTX_SLIDES_PER_CHUNK": "4",
    "PPTX_OCR_ENGINE": "rapidocr",
    "COLLECTION_NAME": "default_rag_collection1",
    "DENSE_DIM": "384",
    "BATCH_SIZE": "8",
    "UPSERT_CHUNK": "500",
    "SPARSE_BATCH_FALLBACK": "8",
    "QDRANT_SHARD_NUMBER": "3",
    "QDRANT_REPLICATION_FACTOR": "2",
    "QDRANT_WRITE_CONSISTENCY_FACTOR": "1",
    "QDRANT_HNSW_EF_CONSTRUCT": "128",
    "QDRANT_HNSW_M": "32",
    "QDRANT_HNSW_FULL_SCAN_THRESHOLD": "10000",
    "QDRANT_ONDISK": "false",
    "INDEX_TIMEOUT": "1800",
    "BACKUP_TIMEOUT": "300",
    "ENABLE_QDRANT_BACKUP": "true",
    "MIN_INDEXED_POINTS_FOR_BACKUP": "100",
    "MIN_INDEX_DELTA_RATIO_FOR_BACKUP": "0.0",
    "QDRANT_ENABLE_SCALAR_QUANTIZATION": "true",
    "QDRANT_QUANTIZATION_ALWAYS_RAM": "true",
    "TMPDIR": "/tmp",
}

RUNTIME_KEYS = set(DEFAULTS.keys())


def log(msg: str, /, *args: object) -> None:
    if args:
        msg = msg % args
    print(msg, flush=True)


def fatal(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v is None:
        return default
    v = str(v).strip()
    return v if v else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def pick_env(*names: str, default: str = "") -> str:
    for name in names:
        v = os.environ.get(name)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def run_cmd(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=merged_env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or f"timeout after {timeout}s"


def ensure_kubectl_available() -> None:
    rc, out, err = run_cmd(["kubectl", "version", "--client=true"], timeout=20)
    if rc != 0:
        fatal(f"kubectl not available or not in PATH: {err or out}")


def yaml_dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def namespace_manifest(ns: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}


def serviceaccount_manifest(ns: str, name: str, mode: str, irsa_role_arn: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "namespace": ns}
    if mode != "kind" and irsa_role_arn:
        meta["annotations"] = {"eks.amazonaws.com/role-arn": irsa_role_arn}
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta}


def secret_manifest(ns: str, name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": ns},
        "type": "Opaque",
        "stringData": data,
    }


def build_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for key in sorted(RUNTIME_KEYS):
        cfg[key] = pick_env(key, default=DEFAULTS.get(key, ""))
    cfg["NAMESPACE"] = _env("NAMESPACE", DEFAULTS["NAMESPACE"])
    cfg["CRONJOB_NAME"] = _env("CRONJOB_NAME", DEFAULTS["CRONJOB_NAME"]).lower()
    cfg["CRON_SCHEDULE"] = pick_env("CRON_SCHEDULE", "INDEXING_BACKUP_CRON_EXPRESSION", default=DEFAULTS["CRON_SCHEDULE"])
    cfg["CRONJOB_CONCURRENCY"] = _env("CRONJOB_CONCURRENCY", DEFAULTS["CRONJOB_CONCURRENCY"])
    cfg["CRONJOB_BACKOFF_LIMIT"] = _env("CRONJOB_BACKOFF_LIMIT", DEFAULTS["CRONJOB_BACKOFF_LIMIT"])
    cfg["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"] = _env("CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT", DEFAULTS["CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT"])
    cfg["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"] = _env("CRONJOB_FAILED_JOBS_HISTORY_LIMIT", DEFAULTS["CRONJOB_FAILED_JOBS_HISTORY_LIMIT"])
    cfg["CRONJOB_TIMEZONE"] = _env("CRONJOB_TIMEZONE", DEFAULTS["CRONJOB_TIMEZONE"])
    cfg["SERVICE_ACCOUNT_NAME"] = _env("SERVICE_ACCOUNT_NAME", DEFAULTS["SERVICE_ACCOUNT_NAME"])
    cfg["MANIFESTS_DIR"] = _env("MANIFESTS_DIR", DEFAULTS["MANIFESTS_DIR"])
    cfg["INDEXING_PIPELINE_CPU_IMAGE_REPO"] = _env("INDEXING_PIPELINE_CPU_IMAGE_REPO", DEFAULTS["INDEXING_PIPELINE_CPU_IMAGE_REPO"])
    cfg["INDEXING_PIPELINE_CPU_IMAGE_TAG"] = _env("INDEXING_PIPELINE_CPU_IMAGE_TAG", DEFAULTS["INDEXING_PIPELINE_CPU_IMAGE_TAG"])
    cfg["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"] = _env("INDEXING_BACKUP_CRONJOB_CPU_REQUEST", DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"])
    cfg["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"] = _env("INDEXING_BACKUP_CRONJOB_CPU_LIMIT", DEFAULTS["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"])
    cfg["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"] = _env("INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST", DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"])
    cfg["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"] = _env("INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT", DEFAULTS["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"])
    cfg["LOG_LEVEL"] = _env("LOG_LEVEL", DEFAULTS["LOG_LEVEL"])
    cfg["HTTP_TIMEOUT"] = _env("HTTP_TIMEOUT", DEFAULTS["HTTP_TIMEOUT"])
    cfg["INDEXING_STRICT"] = _env("INDEXING_STRICT", DEFAULTS["INDEXING_STRICT"])
    cfg["RUN_PRE_CONVERSIONS"] = _env("RUN_PRE_CONVERSIONS", DEFAULTS["RUN_PRE_CONVERSIONS"])
    cfg["PYTHONUNBUFFERED"] = _env("PYTHONUNBUFFERED", DEFAULTS["PYTHONUNBUFFERED"])
    cfg["QDRANT_URL"] = _env("QDRANT_URL", DEFAULTS["QDRANT_URL"])
    cfg["DENSE_URL"] = _env("DENSE_URL", DEFAULTS["DENSE_URL"])
    cfg["SPARSE_URL"] = _env("SPARSE_URL", DEFAULTS["SPARSE_URL"])
    cfg["DATA_S3_BUCKET"] = pick_env("DATA_S3_BUCKET", "S3_BUCKET", default=DEFAULTS["DATA_S3_BUCKET"])
    cfg["DATA_S3_PREFIX"] = pick_env("DATA_S3_PREFIX", "BACKUP_PREFIX", default=DEFAULTS["DATA_S3_PREFIX"])
    cfg["AWS_REGION"] = _env("AWS_REGION", DEFAULTS["AWS_REGION"])
    cfg["AWS_DEFAULT_REGION"] = _env("AWS_DEFAULT_REGION", cfg["AWS_REGION"] or DEFAULTS["AWS_DEFAULT_REGION"])
    if not cfg["AWS_REGION"]:
        cfg["AWS_REGION"] = cfg["AWS_DEFAULT_REGION"]
    if not cfg["AWS_DEFAULT_REGION"]:
        cfg["AWS_DEFAULT_REGION"] = cfg["AWS_REGION"]
    cfg["STORAGE_RAW_PREFIX"] = _env("STORAGE_RAW_PREFIX", DEFAULTS["STORAGE_RAW_PREFIX"])
    cfg["STORAGE_CHUNKED_PREFIX"] = _env("STORAGE_CHUNKED_PREFIX", DEFAULTS["STORAGE_CHUNKED_PREFIX"])
    cfg["QDRANT_API_KEY"] = _env("QDRANT_API_KEY", DEFAULTS["QDRANT_API_KEY"])
    cfg["QDRANT_SECRET_NAME"] = _env("QDRANT_SECRET_NAME", DEFAULTS["QDRANT_SECRET_NAME"])
    cfg["AWS_CREDENTIALS_SECRET_NAME"] = _env("AWS_CREDENTIALS_SECRET_NAME", DEFAULTS["AWS_CREDENTIALS_SECRET_NAME"])
    cfg["EXTRA_SECRET_NAME"] = _env("EXTRA_SECRET_NAME", DEFAULTS["EXTRA_SECRET_NAME"])
    cfg["USE_IRSA"] = pick_env("USE_IRSA", "AWS_USE_IRSA", default=DEFAULTS["USE_IRSA"])
    cfg["IRSA_ROLE_ARN"] = _env("IRSA_ROLE_ARN", DEFAULTS["IRSA_ROLE_ARN"])
    cfg["K8S_CLUSTER"] = _env("K8S_CLUSTER", DEFAULTS["K8S_CLUSTER"])
    cfg["MAX_TOKENS_PER_CHUNK"] = _env("MAX_TOKENS_PER_CHUNK", DEFAULTS["MAX_TOKENS_PER_CHUNK"])
    cfg["MIN_TOKENS_PER_CHUNK"] = _env("MIN_TOKENS_PER_CHUNK", DEFAULTS["MIN_TOKENS_PER_CHUNK"])
    cfg["NUMBER_OF_OVERLAPPING_SENTENCES"] = _env("NUMBER_OF_OVERLAPPING_SENTENCES", DEFAULTS["NUMBER_OF_OVERLAPPING_SENTENCES"])
    cfg["PDF_DISABLE_OCR"] = _env("PDF_DISABLE_OCR", DEFAULTS["PDF_DISABLE_OCR"])
    cfg["PDF_OCR_ENGINE"] = _env("PDF_OCR_ENGINE", DEFAULTS["PDF_OCR_ENGINE"])
    cfg["PDF_TESSERACT_LANG"] = _env("PDF_TESSERACT_LANG", DEFAULTS["PDF_TESSERACT_LANG"])
    cfg["IMAGE_TESSERACT_LANG"] = _env("IMAGE_TESSERACT_LANG", DEFAULTS["IMAGE_TESSERACT_LANG"])
    cfg["TESSERACT_CONFIG"] = _env("TESSERACT_CONFIG", DEFAULTS["TESSERACT_CONFIG"])
    cfg["PDF_FORCE_OCR"] = _env("PDF_FORCE_OCR", DEFAULTS["PDF_FORCE_OCR"])
    cfg["PDF_OCR_RENDER_DPI"] = _env("PDF_OCR_RENDER_DPI", DEFAULTS["PDF_OCR_RENDER_DPI"])
    cfg["PDF_MIN_IMG_SIZE_BYTES"] = _env("PDF_MIN_IMG_SIZE_BYTES", DEFAULTS["PDF_MIN_IMG_SIZE_BYTES"])
    cfg["IMAGE_OCR_ENGINE"] = _env("IMAGE_OCR_ENGINE", DEFAULTS["IMAGE_OCR_ENGINE"])
    cfg["IMAGE_MIN_IMG_SIZE_BYTES"] = _env("IMAGE_MIN_IMG_SIZE_BYTES", DEFAULTS["IMAGE_MIN_IMG_SIZE_BYTES"])
    cfg["IMAGE_RENDER_DPI"] = _env("IMAGE_RENDER_DPI", DEFAULTS["IMAGE_RENDER_DPI"])
    cfg["IMAGE_UPSCALE_FACTOR"] = _env("IMAGE_UPSCALE_FACTOR", DEFAULTS["IMAGE_UPSCALE_FACTOR"])
    cfg["CSV_TARGET_TOKENS_PER_CHUNK"] = _env("CSV_TARGET_TOKENS_PER_CHUNK", DEFAULTS["CSV_TARGET_TOKENS_PER_CHUNK"])
    cfg["JSONL_TARGET_TOKENS_PER_CHUNK"] = _env("JSONL_TARGET_TOKENS_PER_CHUNK", DEFAULTS["JSONL_TARGET_TOKENS_PER_CHUNK"])
    cfg["PPTX_SLIDES_PER_CHUNK"] = _env("PPTX_SLIDES_PER_CHUNK", DEFAULTS["PPTX_SLIDES_PER_CHUNK"])
    cfg["PPTX_OCR_ENGINE"] = _env("PPTX_OCR_ENGINE", DEFAULTS["PPTX_OCR_ENGINE"])
    cfg["COLLECTION_NAME"] = _env("COLLECTION_NAME", DEFAULTS["COLLECTION_NAME"])
    cfg["DENSE_DIM"] = _env("DENSE_DIM", DEFAULTS["DENSE_DIM"])
    cfg["BATCH_SIZE"] = _env("BATCH_SIZE", DEFAULTS["BATCH_SIZE"])
    cfg["UPSERT_CHUNK"] = _env("UPSERT_CHUNK", DEFAULTS["UPSERT_CHUNK"])
    cfg["SPARSE_BATCH_FALLBACK"] = _env("SPARSE_BATCH_FALLBACK", DEFAULTS["SPARSE_BATCH_FALLBACK"])
    cfg["QDRANT_SHARD_NUMBER"] = _env("QDRANT_SHARD_NUMBER", DEFAULTS["QDRANT_SHARD_NUMBER"])
    cfg["QDRANT_REPLICATION_FACTOR"] = _env("QDRANT_REPLICATION_FACTOR", DEFAULTS["QDRANT_REPLICATION_FACTOR"])
    cfg["QDRANT_WRITE_CONSISTENCY_FACTOR"] = _env("QDRANT_WRITE_CONSISTENCY_FACTOR", DEFAULTS["QDRANT_WRITE_CONSISTENCY_FACTOR"])
    cfg["QDRANT_HNSW_EF_CONSTRUCT"] = _env("QDRANT_HNSW_EF_CONSTRUCT", DEFAULTS["QDRANT_HNSW_EF_CONSTRUCT"])
    cfg["QDRANT_HNSW_M"] = _env("QDRANT_HNSW_M", DEFAULTS["QDRANT_HNSW_M"])
    cfg["QDRANT_HNSW_FULL_SCAN_THRESHOLD"] = _env("QDRANT_HNSW_FULL_SCAN_THRESHOLD", DEFAULTS["QDRANT_HNSW_FULL_SCAN_THRESHOLD"])
    cfg["QDRANT_ONDISK"] = _env("QDRANT_ONDISK", DEFAULTS["QDRANT_ONDISK"])
    cfg["QDRANT_ENABLE_SCALAR_QUANTIZATION"] = _env("QDRANT_ENABLE_SCALAR_QUANTIZATION", DEFAULTS["QDRANT_ENABLE_SCALAR_QUANTIZATION"])
    cfg["QDRANT_QUANTIZATION_ALWAYS_RAM"] = _env("QDRANT_QUANTIZATION_ALWAYS_RAM", DEFAULTS["QDRANT_QUANTIZATION_ALWAYS_RAM"])
    cfg["INDEX_TIMEOUT"] = _env("INDEX_TIMEOUT", DEFAULTS["INDEX_TIMEOUT"])
    cfg["BACKUP_TIMEOUT"] = _env("BACKUP_TIMEOUT", DEFAULTS["BACKUP_TIMEOUT"])
    cfg["ENABLE_QDRANT_BACKUP"] = _env("ENABLE_QDRANT_BACKUP", DEFAULTS["ENABLE_QDRANT_BACKUP"])
    cfg["MIN_INDEXED_POINTS_FOR_BACKUP"] = _env("MIN_INDEXED_POINTS_FOR_BACKUP", DEFAULTS["MIN_INDEXED_POINTS_FOR_BACKUP"])
    cfg["MIN_INDEX_DELTA_RATIO_FOR_BACKUP"] = _env("MIN_INDEX_DELTA_RATIO_FOR_BACKUP", DEFAULTS["MIN_INDEX_DELTA_RATIO_FOR_BACKUP"])
    cfg["TMPDIR"] = _env("TMPDIR", DEFAULTS["TMPDIR"])
    return cfg


def detect_mode(cfg: dict[str, str]) -> str:
    explicit = cfg.get("K8S_CLUSTER", "").strip().lower()
    if explicit in {"kind", "eks", "eks-auto"}:
        return explicit
    if _env_bool("USE_IRSA", False) or cfg.get("IRSA_ROLE_ARN"):
        return "eks"
    return "kind"


def validate_cfg(cfg: dict[str, str]) -> None:
    missing: list[str] = []
    mode = detect_mode(cfg)

    if not cfg.get("DATA_S3_BUCKET"):
        missing.append("DATA_S3_BUCKET")
    if not cfg.get("QDRANT_URL"):
        missing.append("QDRANT_URL")
    if not cfg.get("DENSE_URL"):
        missing.append("DENSE_URL")
    if not cfg.get("SPARSE_URL"):
        missing.append("SPARSE_URL")
    if not (cfg.get("AWS_REGION") or cfg.get("AWS_DEFAULT_REGION")):
        missing.append("AWS_REGION")

    if missing:
        fatal("missing required env vars: " + ", ".join(missing))

    if mode == "kind":
        if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
            fatal("kind/static mode requires AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    else:
        if not cfg.get("IRSA_ROLE_ARN"):
            fatal("EKS/IRSA mode requires IRSA_ROLE_ARN")


def secret_env_item(name: str, secret_name: str, secret_key: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {
                "name": secret_name,
                "key": secret_key or name,
            }
        },
    }


def plain_env_item(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "value": value}


def build_cronjob_manifest(cfg: dict[str, str], mode: str) -> dict[str, Any]:
    ns = cfg["NAMESPACE"]
    cron_name = cfg["CRONJOB_NAME"]
    image = f"{cfg['INDEXING_PIPELINE_CPU_IMAGE_REPO']}:{cfg['INDEXING_PIPELINE_CPU_IMAGE_TAG']}"
    aws_region = cfg["AWS_REGION"] or cfg["AWS_DEFAULT_REGION"]

    env: list[dict[str, Any]] = [
        plain_env_item("PYTHONUNBUFFERED", cfg["PYTHONUNBUFFERED"]),
        plain_env_item("TMPDIR", cfg["TMPDIR"]),
        plain_env_item("LOG_LEVEL", cfg["LOG_LEVEL"]),
        plain_env_item("HTTP_TIMEOUT", cfg["HTTP_TIMEOUT"]),
        plain_env_item("INDEXING_STRICT", cfg["INDEXING_STRICT"]),
        plain_env_item("RUN_PRE_CONVERSIONS", cfg["RUN_PRE_CONVERSIONS"]),
        plain_env_item("QDRANT_URL", cfg["QDRANT_URL"]),
        plain_env_item("DENSE_URL", cfg["DENSE_URL"]),
        plain_env_item("SPARSE_URL", cfg["SPARSE_URL"]),
        plain_env_item("DATA_S3_BUCKET", cfg["DATA_S3_BUCKET"]),
        plain_env_item("DATA_S3_PREFIX", cfg["DATA_S3_PREFIX"]),
        plain_env_item("STORAGE_RAW_PREFIX", cfg["STORAGE_RAW_PREFIX"]),
        plain_env_item("STORAGE_CHUNKED_PREFIX", cfg["STORAGE_CHUNKED_PREFIX"]),
        plain_env_item("AWS_REGION", aws_region),
        plain_env_item("AWS_DEFAULT_REGION", aws_region),
        plain_env_item("AWS_SDK_LOAD_CONFIG", "1"),
        plain_env_item("AWS_EC2_METADATA_DISABLED", "true"),
        plain_env_item("MAX_TOKENS_PER_CHUNK", cfg["MAX_TOKENS_PER_CHUNK"]),
        plain_env_item("MIN_TOKENS_PER_CHUNK", cfg["MIN_TOKENS_PER_CHUNK"]),
        plain_env_item("NUMBER_OF_OVERLAPPING_SENTENCES", cfg["NUMBER_OF_OVERLAPPING_SENTENCES"]),
        plain_env_item("PDF_DISABLE_OCR", cfg["PDF_DISABLE_OCR"]),
        plain_env_item("PDF_OCR_ENGINE", cfg["PDF_OCR_ENGINE"]),
        plain_env_item("PDF_TESSERACT_LANG", cfg["PDF_TESSERACT_LANG"]),
        plain_env_item("IMAGE_TESSERACT_LANG", cfg["IMAGE_TESSERACT_LANG"]),
        plain_env_item("TESSERACT_CONFIG", cfg["TESSERACT_CONFIG"]),
        plain_env_item("PDF_FORCE_OCR", cfg["PDF_FORCE_OCR"]),
        plain_env_item("PDF_OCR_RENDER_DPI", cfg["PDF_OCR_RENDER_DPI"]),
        plain_env_item("PDF_MIN_IMG_SIZE_BYTES", cfg["PDF_MIN_IMG_SIZE_BYTES"]),
        plain_env_item("IMAGE_OCR_ENGINE", cfg["IMAGE_OCR_ENGINE"]),
        plain_env_item("IMAGE_MIN_IMG_SIZE_BYTES", cfg["IMAGE_MIN_IMG_SIZE_BYTES"]),
        plain_env_item("IMAGE_RENDER_DPI", cfg["IMAGE_RENDER_DPI"]),
        plain_env_item("IMAGE_UPSCALE_FACTOR", cfg["IMAGE_UPSCALE_FACTOR"]),
        plain_env_item("CSV_TARGET_TOKENS_PER_CHUNK", cfg["CSV_TARGET_TOKENS_PER_CHUNK"]),
        plain_env_item("JSONL_TARGET_TOKENS_PER_CHUNK", cfg["JSONL_TARGET_TOKENS_PER_CHUNK"]),
        plain_env_item("PPTX_SLIDES_PER_CHUNK", cfg["PPTX_SLIDES_PER_CHUNK"]),
        plain_env_item("PPTX_OCR_ENGINE", cfg["PPTX_OCR_ENGINE"]),
        plain_env_item("COLLECTION_NAME", cfg["COLLECTION_NAME"]),
        plain_env_item("DENSE_DIM", cfg["DENSE_DIM"]),
        plain_env_item("BATCH_SIZE", cfg["BATCH_SIZE"]),
        plain_env_item("UPSERT_CHUNK", cfg["UPSERT_CHUNK"]),
        plain_env_item("SPARSE_BATCH_FALLBACK", cfg["SPARSE_BATCH_FALLBACK"]),
        plain_env_item("QDRANT_SHARD_NUMBER", cfg["QDRANT_SHARD_NUMBER"]),
        plain_env_item("QDRANT_REPLICATION_FACTOR", cfg["QDRANT_REPLICATION_FACTOR"]),
        plain_env_item("QDRANT_WRITE_CONSISTENCY_FACTOR", cfg["QDRANT_WRITE_CONSISTENCY_FACTOR"]),
        plain_env_item("QDRANT_HNSW_EF_CONSTRUCT", cfg["QDRANT_HNSW_EF_CONSTRUCT"]),
        plain_env_item("QDRANT_HNSW_M", cfg["QDRANT_HNSW_M"]),
        plain_env_item("QDRANT_HNSW_FULL_SCAN_THRESHOLD", cfg["QDRANT_HNSW_FULL_SCAN_THRESHOLD"]),
        plain_env_item("QDRANT_ONDISK", cfg["QDRANT_ONDISK"]),
        plain_env_item("QDRANT_ENABLE_SCALAR_QUANTIZATION", cfg["QDRANT_ENABLE_SCALAR_QUANTIZATION"]),
        plain_env_item("QDRANT_QUANTIZATION_ALWAYS_RAM", cfg["QDRANT_QUANTIZATION_ALWAYS_RAM"]),
        plain_env_item("INDEX_TIMEOUT", cfg["INDEX_TIMEOUT"]),
        plain_env_item("BACKUP_TIMEOUT", cfg["BACKUP_TIMEOUT"]),
        plain_env_item("ENABLE_QDRANT_BACKUP", cfg["ENABLE_QDRANT_BACKUP"]),
        plain_env_item("MIN_INDEXED_POINTS_FOR_BACKUP", cfg["MIN_INDEXED_POINTS_FOR_BACKUP"]),
        plain_env_item("MIN_INDEX_DELTA_RATIO_FOR_BACKUP", cfg["MIN_INDEX_DELTA_RATIO_FOR_BACKUP"]),
    ]

    if cfg.get("QDRANT_API_KEY"):
        env.append(secret_env_item("QDRANT_API_KEY", cfg["QDRANT_SECRET_NAME"], "QDRANT_API_KEY"))

    if mode == "kind":
        env.append(secret_env_item("AWS_ACCESS_KEY_ID", cfg["AWS_CREDENTIALS_SECRET_NAME"], "AWS_ACCESS_KEY_ID"))
        env.append(secret_env_item("AWS_SECRET_ACCESS_KEY", cfg["AWS_CREDENTIALS_SECRET_NAME"], "AWS_SECRET_ACCESS_KEY"))
        if os.environ.get("AWS_SESSION_TOKEN"):
            env.append(secret_env_item("AWS_SESSION_TOKEN", cfg["AWS_CREDENTIALS_SECRET_NAME"], "AWS_SESSION_TOKEN"))

    pod_security_context: dict[str, Any] = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }

    container_security_context: dict[str, Any] = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }

    cronjob: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": cron_name, "namespace": ns},
        "spec": {
            "schedule": cfg["CRON_SCHEDULE"],
            "concurrencyPolicy": cfg["CRONJOB_CONCURRENCY"],
            "successfulJobsHistoryLimit": _env_int("CRONJOB_SUCCESSFUL_JOBS_HISTORY_LIMIT", 3),
            "failedJobsHistoryLimit": _env_int("CRONJOB_FAILED_JOBS_HISTORY_LIMIT", 1),
            "jobTemplate": {
                "spec": {
                    "backoffLimit": _env_int("CRONJOB_BACKOFF_LIMIT", 1),
                    "template": {
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": cron_name,
                                "app.kubernetes.io/component": "indexing",
                            }
                        },
                        "spec": {
                            "serviceAccountName": cfg["SERVICE_ACCOUNT_NAME"],
                            "restartPolicy": "Never",
                            "securityContext": pod_security_context,
                            "volumes": [
                                {"name": "tmp", "emptyDir": {}},
                            ],
                            "containers": [
                                {
                                    "name": "indexer",
                                    "image": image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["/opt/venv/bin/python", "/indexing_pipeline/indexing_pipeline.py"],
                                    "args": ["--workdir", "/indexing_pipeline"],
                                    "env": env,
                                    "securityContext": container_security_context,
                                    "volumeMounts": [
                                        {"name": "tmp", "mountPath": "/tmp"},
                                        {"name": "tmp", "mountPath": "/indexing_pipeline/tmp"},
                                    ],
                                    "resources": {
                                        "requests": {
                                            "cpu": cfg["INDEXING_BACKUP_CRONJOB_CPU_REQUEST"],
                                            "memory": cfg["INDEXING_BACKUP_CRONJOB_MEMORY_REQUEST"],
                                        },
                                        "limits": {
                                            "cpu": cfg["INDEXING_BACKUP_CRONJOB_CPU_LIMIT"],
                                            "memory": cfg["INDEXING_BACKUP_CRONJOB_MEMORY_LIMIT"],
                                        },
                                    },
                                    "workingDir": "/indexing_pipeline",
                                }
                            ],
                        },
                    },
                }
            },
        },
    }

    if cfg["CRONJOB_TIMEZONE"]:
        cronjob["spec"]["timeZone"] = cfg["CRONJOB_TIMEZONE"]

    return cronjob


def write_manifests(manifests_dir: Path, docs: list[tuple[str, dict[str, Any]]]) -> list[Path]:
    manifests_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for filename, doc in docs:
        p = manifests_dir / filename
        write_text(p, yaml_dump(doc))
        out.append(p)
    return out


def apply_yaml(yaml_text: str, *, timeout: int = 60) -> None:
    rc, out, err = run_cmd(["kubectl", "apply", "-f", "-"], input_text=yaml_text, timeout=timeout)
    if rc != 0:
        fatal(err or out or "kubectl apply failed", 4)


def apply_direct_secret(ns: str, name: str, data: dict[str, str], timeout: int = 30) -> None:
    if not data:
        return
    apply_yaml(yaml_dump(secret_manifest(ns, name, data)), timeout=timeout)


def wait_for_namespace(ns: str, timeout: int = 30) -> None:
    start = time.monotonic()
    while True:
        rc, _, _ = run_cmd(["kubectl", "get", "namespace", ns], timeout=10)
        if rc == 0:
            return
        if time.monotonic() - start >= timeout:
            fatal(f"namespace '{ns}' was not observable after creation", 5)
        time.sleep(1)


def render_and_apply(cfg: dict[str, str], dry_run: bool) -> None:
    ensure_kubectl_available()
    mode = detect_mode(cfg)
    manifests_dir = Path(cfg["MANIFESTS_DIR"])

    docs: list[tuple[str, dict[str, Any]]] = [
        ("00-namespace.yaml", namespace_manifest(cfg["NAMESPACE"])),
        ("10-serviceaccount.yaml", serviceaccount_manifest(cfg["NAMESPACE"], cfg["SERVICE_ACCOUNT_NAME"], mode, cfg["IRSA_ROLE_ARN"])),
        ("50-cronjob.yaml", build_cronjob_manifest(cfg, mode)),
    ]

    rendered_files = write_manifests(manifests_dir, docs)
    log(f"Rendered manifests to {manifests_dir}")
    for p in rendered_files:
        log(str(p))

    if dry_run:
        return

    apply_yaml(yaml_dump(namespace_manifest(cfg["NAMESPACE"])), timeout=20)
    wait_for_namespace(cfg["NAMESPACE"], timeout=30)

    if cfg.get("QDRANT_API_KEY"):
        apply_direct_secret(cfg["NAMESPACE"], cfg["QDRANT_SECRET_NAME"], {"QDRANT_API_KEY": cfg["QDRANT_API_KEY"]})

    if mode == "kind":
        aws_data: dict[str, str] = {}
        if os.environ.get("AWS_ACCESS_KEY_ID"):
            aws_data["AWS_ACCESS_KEY_ID"] = os.environ["AWS_ACCESS_KEY_ID"]
        if os.environ.get("AWS_SECRET_ACCESS_KEY"):
            aws_data["AWS_SECRET_ACCESS_KEY"] = os.environ["AWS_SECRET_ACCESS_KEY"]
        if os.environ.get("AWS_SESSION_TOKEN"):
            aws_data["AWS_SESSION_TOKEN"] = os.environ["AWS_SESSION_TOKEN"]
        apply_direct_secret(cfg["NAMESPACE"], cfg["AWS_CREDENTIALS_SECRET_NAME"], aws_data)

    apply_yaml(
        "\n---\n".join(
            yaml_dump(doc)
            for doc in (
                serviceaccount_manifest(cfg["NAMESPACE"], cfg["SERVICE_ACCOUNT_NAME"], mode, cfg["IRSA_ROLE_ARN"]),
                build_cronjob_manifest(cfg, mode),
            )
        ),
        timeout=60,
    )
    log("Applied manifests successfully")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render and optionally apply Kubernetes manifests for the indexing cronjob."
    )
    parser.add_argument("--dry-run", action="store_true", help="Render manifests to disk but do not apply them.")
    parser.add_argument("--manifests-dir", type=str, default="", help="Override output manifests directory.")
    parser.add_argument("--validate-only", action="store_true", help="Validate configuration and exit.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    cfg = build_cfg()
    if args.manifests_dir:
        cfg["MANIFESTS_DIR"] = args.manifests_dir
    validate_cfg(cfg)
    if args.validate_only:
        log("Configuration validated successfully.")
        return
    render_and_apply(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit:
        raise
    except Exception as exc:
        fatal(f"Unhandled error: {exc}", 99)
