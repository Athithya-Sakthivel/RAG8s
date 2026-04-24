from __future__ import annotations

import base64
import hashlib
import json
import os
import resource
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def TS() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(level: str, event: str, msg: str, **k: Any) -> None:
    payload = {"ts": TS(), "level": level, "event": event, "msg": msg}
    if k:
        payload.update(k)
    print(json.dumps(payload, default=str), flush=True)


def is_truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "")).strip() or None
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET", os.getenv("S3_BUCKET", "")).strip()
S3_RAW_PREFIX = (
    os.getenv("S3_RAW_PREFIX", os.getenv("STORAGE_RAW_PREFIX", "data/raw/")).lstrip("/").rstrip("/") + "/"
)

STRICT_VALIDATE = is_truthy(os.getenv("AWS_STRICT_VALIDATE", "true"))

TMP_ROOT = os.getenv("TMP_ROOT", os.getenv("TMP_DIR", "/tmp/preconv"))
Path(TMP_ROOT).mkdir(parents=True, exist_ok=True)

OVERWRITE_ALL_AUDIO_FILES = is_truthy(os.getenv("OVERWRITE_ALL_AUDIO_FILES", "true"))
OVERWRITE_OTHER_TO_PDF = is_truthy(os.getenv("OVERWRITE_OTHER_TO_PDF", "true"))
OVERWRITE_SPREADSHEETS_WITH_CSV = is_truthy(os.getenv("OVERWRITE_SPREADSHEETS_WITH_CSV", "true"))

DESIRED_NOFILE = int(os.getenv("DESIRED_NOFILE", "262144"))
FDS_PER_CONVERT = int(os.getenv("FDS_PER_CONVERT", "60"))
SYSTEM_RESERVED_FDS = int(os.getenv("SYSTEM_RESERVED_FDS", "200"))
ENV_MAX_PARALLEL = int(os.getenv("MAX_PARALLEL_CONVERTS", os.getenv("MAX_PARALLEL_CONVERTS_DEFAULT", "2")))

SOURCE_AUDIO_EXTS = ("mp3", "m4a", "aac", "flac", "ogg", "opus", "webm", "amr", "wma", "aiff", "aif")
SHEET_EXTS = ("xls", "xlsx", "ods", "xlsm", "xlsb")
DOC_EXTS = ("doc", "docx")

if not AWS_S3_BUCKET:
    log("ERROR", "missing_env", "AWS_S3_BUCKET (or S3_BUCKET) must be set")
    raise SystemExit(2)

S3_CLIENT = boto3.client(
    "s3",
    region_name=AWS_REGION,
    config=Config(
        retries={"max_attempts": 10, "mode": "standard"},
        max_pool_connections=max(10, ENV_MAX_PARALLEL * 8),
    ),
)


def ensure_nofile_limit(desired: int) -> int:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        new_soft = min(desired, hard) if hard != resource.RLIM_INFINITY else desired
        if new_soft > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            log("INFO", "nofile.raised", "Raised RLIMIT_NOFILE", old_soft=soft, new_soft=new_soft, hard=hard)
            return new_soft
        log("DEBUG", "nofile.unchanged", "RLIMIT_NOFILE unchanged", soft=soft, hard=hard)
        return soft
    except Exception as e:
        try:
            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        except Exception:
            soft = 1024
        log("WARN", "nofile_raise_failed", "Failed to raise RLIMIT_NOFILE", error=str(e))
        return soft


def compute_max_workers(soft_limit: int, fds_per_worker: int, reserve: int, env_max: int) -> int:
    usable = max(0, soft_limit - reserve)
    calc = max(1, usable // max(1, fds_per_worker))
    return max(1, min(calc, env_max))


def b64(v: str) -> str:
    return base64.b64encode(v.encode("utf-8")).decode("ascii")


def compute_hashes(path: str, chunk_size: int = 8 * 1024 * 1024) -> tuple[str, str]:
    # nosemgrep: python.lang.security.insecure-hash-algorithms-md5
    # MD5 used for deterministic hashing (non-cryptographic).
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def prepare_metadata(d: dict[str, str | None]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (d or {}).items():
        kk = str(k).replace(" ", "_").lower()
        out[kk] = b64(str(v)) if v is not None else ""
    return out


def object_exists(key: str) -> bool:
    try:
        S3_CLIENT.head_object(Bucket=AWS_S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        code = (e.response.get("Error") or {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound", "403"):
            return False
        raise


def get_object_props(key: str) -> dict[str, Any]:
    try:
        p = S3_CLIENT.head_object(Bucket=AWS_S3_BUCKET, Key=key)
        return {
            "exists": True,
            "etag": str(p.get("ETag", "")).strip('"'),
            "size": int(p.get("ContentLength", 0) or 0),
            "metadata": p.get("Metadata", {}) or {},
        }
    except ClientError as e:
        code = (e.response.get("Error") or {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound", "403"):
            return {"exists": False}
        raise


def download_object_to_file(key: str, dst_path: str) -> bool:
    try:
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dst_path, "wb") as fh:
            S3_CLIENT.download_fileobj(AWS_S3_BUCKET, key, fh)
        return True
    except Exception as e:
        log("WARN", "download_failed", "download failed", blob=key, error=str(e))
        return False


def upload_file_to_s3(
    key: str,
    src_path: str,
    metadata: dict[str, str],
    content_type: str = "application/octet-stream",
    overwrite: bool = True,
) -> dict[str, Any]:
    try:
        extra_args = {"ContentType": content_type, "Metadata": metadata}
        with open(src_path, "rb") as fh:
            S3_CLIENT.upload_fileobj(fh, AWS_S3_BUCKET, key, ExtraArgs=extra_args)
        props = S3_CLIENT.head_object(Bucket=AWS_S3_BUCKET, Key=key)
        remote_meta = props.get("Metadata", {}) or {}
        verified = all((k in remote_meta and remote_meta[k] == metadata[k]) for k in metadata)
        return {"action": "uploaded", "verified": bool(verified), "etag": str(props.get("ETag", "")).strip('"')}
    except Exception as e:
        log("ERROR", "s3_upload_failed", "upload failed", target=key, error=str(e))
        return {"action": "failed", "error": str(e)}


def _with_suffix_in_key(key: str, suffix_text: str) -> str:
    p = PurePosixPath(key)
    stem = p.stem
    suffix = p.suffix
    parent = str(p.parent)
    if parent == ".":
        parent = ""
    candidate_name = f"{stem}{suffix_text}{suffix}"
    return f"{parent}/{candidate_name}" if parent else candidate_name


def make_unique_target(base_target: str) -> str:
    if not object_exists(base_target):
        return base_target

    i = 1
    while True:
        candidate = _with_suffix_in_key(base_target, f"-{i}")
        if not object_exists(candidate):
            return candidate
        i += 1


def safe_move_object(src: str, dst: str) -> tuple[bool, str]:
    src_props = get_object_props(src)
    dst_props = get_object_props(dst)

    if dst_props.get("exists"):
        try:
            src_etag = src_props.get("etag")
            dst_etag = dst_props.get("etag")
            src_size = src_props.get("size", 0)
            dst_size = dst_props.get("size", 0)
            if src_etag and dst_etag and src_etag == dst_etag and src_size == dst_size:
                S3_CLIENT.delete_object(Bucket=AWS_S3_BUCKET, Key=src)
                log("INFO", "group_dedup", "deleted source as identical target exists", src=src, dst=dst)
                return True, dst
            dst = make_unique_target(dst)
        except Exception:
            dst = make_unique_target(dst)

    tmp = str(Path(TMP_ROOT) / "move" / PurePosixPath(src).name)
    try:
        if not download_object_to_file(src, tmp):
            return False, dst
        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5
        # MD5 used for deterministic hashing (non-cryptographic).
        _md5, sha = compute_hashes(tmp)
        meta = prepare_metadata({"sha256": sha, "original_name": PurePosixPath(src).name})
        up = upload_file_to_s3(dst, tmp, meta, content_type="application/octet-stream", overwrite=True)
        if up.get("action") == "uploaded":
            try:
                S3_CLIENT.delete_object(Bucket=AWS_S3_BUCKET, Key=src)
            except Exception:
                pass
            return True, dst
        return False, dst
    except Exception as e:
        log("WARN", "group_move_failed", "exception during move", src=src, dst=dst, error=str(e))
        return False, dst
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def ext_to_subdir(name: str) -> str:
    ext = PurePosixPath(name).suffix.lstrip(".").lower()
    mapping = {e: "audio" for e in (*SOURCE_AUDIO_EXTS, "wav")}
    mapping.update({e: "images" for e in ("jpg", "jpeg", "png", "webp", "tif", "tiff", "bmp", "gif")})
    mapping.update({e: "docs" for e in DOC_EXTS})
    mapping.update({e: "ppts" for e in ("ppt", "pptx")})
    mapping.update({e: "sheets" for e in SHEET_EXTS})
    mapping.update({"pdf": "pdfs", "csv": "csvs", "md": "mds", "txt": "txts", "html": "htmls", "htm": "htmls"})
    return mapping.get(ext, "others")


def list_object_keys(prefix: str) -> Iterable[str]:
    paginator = S3_CLIENT.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=AWS_S3_BUCKET, Prefix=prefix):
        for item in page.get("Contents", []) or []:
            key = item.get("Key")
            if key:
                yield key


def run_soffice_convert(src: str, outdir: str, convert_to: str) -> tuple[bool, str]:
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = env.get("SAL_USE_VCLPLUGIN", "gen")
    env["HOME"] = env.get("HOME", "/tmp")
    cmd = [
        "soffice",
        "--headless",
        "--invisible",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        "--nolockcheck",
        "--convert-to",
        convert_to,
        "--outdir",
        outdir,
        src,
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            timeout=120,
            close_fds=True,
            cwd=outdir,
        )
        stderr = (res.stderr or b"").decode("utf-8", errors="ignore").strip()
        if res.returncode == 0:
            return True, ""
        return False, ("\n".join(stderr.splitlines()[:6]) if stderr else f"soffice rc={res.returncode}")
    except Exception as e:
        return False, str(e)


def run_ffmpeg_convert(src: str, dst: str) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        src,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-sample_fmt",
        "s16",
        dst,
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
            close_fds=True,
            cwd=str(Path(dst).parent),
        )
        if res.returncode == 0:
            return True, ""
        out = (res.stderr or b"").decode("utf-8", errors="ignore").splitlines()[:6]
        return False, ("\n".join(out) if out else "ffmpeg error")
    except Exception as e:
        return False, str(e)


def process_doc(key: str) -> None:
    name = PurePosixPath(key).name
    ext = PurePosixPath(name).suffix.lstrip(".").lower()
    if ext not in DOC_EXTS:
        return

    with tempfile.TemporaryDirectory(prefix="preconv-", dir=TMP_ROOT) as tmpdir:
        src_local = str(Path(tmpdir) / "src" / name)
        outdir = str(Path(tmpdir) / "out")
        Path(src_local).parent.mkdir(parents=True, exist_ok=True)
        Path(outdir).mkdir(parents=True, exist_ok=True)

        if not download_object_to_file(key, src_local):
            log("WARN", "doc_download_failed", "download failed", blob=key)
            return

        ok, err = run_soffice_convert(src_local, outdir, "pdf:writer_pdf_Export")
        if not ok:
            qname = f"{S3_RAW_PREFIX}quarantine/{name}.corrupt"
            metadata = prepare_metadata({"quarantined_from": key, "error": err, "original_name": name})
            upload_file_to_s3(qname, src_local, metadata, content_type="application/octet-stream", overwrite=True)
            log("WARN", "doc_convert_failed", "soffice failed", blob=key, error=err)
            return

        pdf_path = None
        for p in Path(outdir).glob(f"{Path(name).stem}*.pdf"):
            pdf_path = str(p)
            break

        if not pdf_path:
            log("WARN", "doc_no_pdf", "conversion produced no pdf", blob=key)
            return

        target = f"{S3_RAW_PREFIX}pdfs/{Path(name).stem}.pdf"
        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5

        # MD5 used for deterministic hashing (non-cryptographic).
        _md5, sha = compute_hashes(src_local)
        src_props = get_object_props(key)
        src_etag = src_props.get("etag", "")
        meta = prepare_metadata(
            {
                "sha256": sha,
                "original_name": name,
                "original_ext": ext,
                "converted_from": key,
                "converted_etag": src_etag,
            }
        )

        up = upload_file_to_s3(target, pdf_path, meta, content_type="application/pdf", overwrite=True)
        if up.get("action") == "uploaded":
            log("INFO", "doc_uploaded", "Uploaded pdf", target=target, result=up)
            if OVERWRITE_OTHER_TO_PDF:
                try:
                    S3_CLIENT.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
                    log("INFO", "doc_deleted_old", "Deleted original doc", name=key)
                except Exception as e:
                    log("WARN", "delete_old_failed", "failed delete original doc", name=key, error=str(e))
        else:
            log("ERROR", "doc_upload_failed", "upload failed", target=target, result=up)


def process_sheet(key: str) -> None:
    name = PurePosixPath(key).name
    ext = PurePosixPath(name).suffix.lstrip(".").lower()
    if ext not in SHEET_EXTS:
        return

    with tempfile.TemporaryDirectory(prefix="preconv-", dir=TMP_ROOT) as tmpdir:
        src_local = str(Path(tmpdir) / "src" / name)
        outdir = str(Path(tmpdir) / "out")
        Path(src_local).parent.mkdir(parents=True, exist_ok=True)
        Path(outdir).mkdir(parents=True, exist_ok=True)

        if not download_object_to_file(key, src_local):
            log("WARN", "sheet_download_failed", "download failed", blob=key)
            return

        ok, err = run_soffice_convert(src_local, outdir, "csv")
        if not ok:
            log("WARN", "sheet_convert_failed", "soffice csv conversion failed", blob=key, error=err)
            return

        created = list(Path(outdir).glob("*.csv"))
        if not created:
            log("WARN", "sheet_no_csv", "no csvs produced", blob=key)
            return

        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5

        # MD5 used for deterministic hashing (non-cryptographic).
        _md5, sha = compute_hashes(src_local)
        src_props = get_object_props(key)
        src_etag = src_props.get("etag", "")
        meta = prepare_metadata(
            {
                "sha256": sha,
                "original_name": name,
                "original_ext": ext,
                "converted_from": key,
                "converted_etag": src_etag,
            }
        )

        for f in created:
            dest = f"{S3_RAW_PREFIX}csvs/{Path(name).stem}/{f.name}"
            up = upload_file_to_s3(dest, str(f), meta, content_type="text/csv", overwrite=True)
            if up.get("action") == "uploaded":
                log("INFO", "sheet_uploaded", "Uploaded csv", target=dest, result=up)

        if OVERWRITE_SPREADSHEETS_WITH_CSV:
            try:
                S3_CLIENT.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
                log("INFO", "sheet_deleted_old", "Deleted original sheet", name=key)
            except Exception as e:
                log("WARN", "delete_old_failed", "failed deleting original sheet", name=key, error=str(e))


def process_audio(key: str) -> None:
    name = PurePosixPath(key).name
    ext = PurePosixPath(name).suffix.lstrip(".").lower()
    if ext not in SOURCE_AUDIO_EXTS:
        return

    with tempfile.TemporaryDirectory(prefix="preconv-", dir=TMP_ROOT) as tmpdir:
        src_local = str(Path(tmpdir) / "src" / name)
        out_local = str(Path(tmpdir) / "out" / f"{Path(name).stem}.wav")
        Path(src_local).parent.mkdir(parents=True, exist_ok=True)
        Path(out_local).parent.mkdir(parents=True, exist_ok=True)

        if not download_object_to_file(key, src_local):
            log("WARN", "audio_download_failed", "download failed", blob=key)
            return

        ok, err = run_ffmpeg_convert(src_local, out_local)
        if not ok:
            log("ERROR", "audio_convert_failed", "ffmpeg failed", blob=key, error=err)
            return

        target = f"{S3_RAW_PREFIX}audio/{Path(name).stem}.wav"
        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5

        # MD5 used for deterministic hashing (non-cryptographic).
        _md5, sha = compute_hashes(src_local)
        src_props = get_object_props(key)
        src_etag = src_props.get("etag", "")
        meta = prepare_metadata(
            {
                "sha256": sha,
                "original_name": name,
                "original_ext": ext,
                "converted_from": key,
                "converted_etag": src_etag,
            }
        )

        up = upload_file_to_s3(target, out_local, meta, content_type="audio/wav", overwrite=True)
        if up.get("action") == "uploaded":
            log("INFO", "audio_uploaded", "Uploaded audio", target=target, result=up)
            if OVERWRITE_ALL_AUDIO_FILES and target != key:
                try:
                    S3_CLIENT.delete_object(Bucket=AWS_S3_BUCKET, Key=key)
                    log("INFO", "audio_deleted_old", "Deleted original audio", name=key)
                except Exception as e:
                    log("WARN", "delete_old_failed", "failed deleting original audio", name=key, error=str(e))
        else:
            log("ERROR", "audio_upload_failed", "upload failed", target=target, result=up)


def group_all(prefix: str) -> None:
    log("INFO", "group_start", "Grouping start", prefix=prefix)
    keys = list(list_object_keys(prefix))
    for key in keys:
        try:
            if key.endswith("/") or key.endswith(".manifest.json"):
                continue
            if not key.startswith(prefix):
                continue

            rel = key[len(prefix) :]
            if not rel.strip():
                continue

            basename = PurePosixPath(rel).name
            correct_subdir = ext_to_subdir(basename)

            first = rel.split("/", 1)[0]

            if first in ("audio", "images", "pdfs", "docs", "ppts", "sheets", "csvs", "mds", "txts", "htmls", "chunked", "quarantine", "others"):
                if first == correct_subdir:
                    continue
                target = f"{prefix}{correct_subdir}/{basename}"
                ok, final = safe_move_object(key, target)
                if ok:
                    log("INFO", "group_moved", "moved to correct dir", src=key, dst=final)
                else:
                    log("WARN", "group_move_failed", "failed to move to correct dir", src=key, dst=target)
                continue

            target = f"{prefix}{correct_subdir}/{basename}"
            ok, final = safe_move_object(key, target)
            if ok:
                log("INFO", "group_moved", "grouped file", src=key, dst=final)
            else:
                log("WARN", "group_move_failed", "failed to group file", src=key, dst=target)
        except Exception as e:
            log("ERROR", "group_iteration_failed", "error grouping object", blob=key, error=str(e))
    log("INFO", "group_done", "Grouping completed", prefix=prefix)


def worker_wrapper(key: str) -> None:
    try:
        if key.endswith("/") or key.endswith(".manifest.json"):
            return

        rel = key[len(S3_RAW_PREFIX) :] if key.startswith(S3_RAW_PREFIX) else key
        first = rel.split("/", 1)[0] if "/" in rel else rel

        if first in ("csvs", "pdfs", "chunked", "quarantine"):
            return

        ext = PurePosixPath(key).suffix.lstrip(".").lower()
        if ext in DOC_EXTS:
            process_doc(key)
        elif ext in SHEET_EXTS:
            process_sheet(key)
        elif ext in SOURCE_AUDIO_EXTS:
            process_audio(key)
    except Exception as e:
        log("ERROR", "processing_failed", f"processing {key} failed", error=str(e))


def main() -> None:
    soft = ensure_nofile_limit(DESIRED_NOFILE)
    try:
        hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    except Exception:
        hard = -1
    log("INFO", "nofile.limits", "Current RLIMIT_NOFILE", soft=soft, hard=hard)

    try:
        if STRICT_VALIDATE:
            S3_CLIENT.head_bucket(Bucket=AWS_S3_BUCKET)
            log("INFO", "bucket_validation", "bucket accessible", bucket=AWS_S3_BUCKET)
    except Exception as e:
        log("ERROR", "bucket_validation_failed", "Failed to validate bucket", error=str(e))
        raise SystemExit(2) from e

    try:
        group_all(S3_RAW_PREFIX)
    except Exception as e:
        log("ERROR", "grouping_failed", "grouping phase failed", error=str(e))
        raise SystemExit(2) from e

    try:
        keys = list(list_object_keys(S3_RAW_PREFIX))
    except Exception as e:
        log("ERROR", "list_after_group_failed", "failed listing objects after grouping", error=str(e))
        raise SystemExit(2) from e

    workers = compute_max_workers(soft, FDS_PER_CONVERT, SYSTEM_RESERVED_FDS, ENV_MAX_PARALLEL)
    log(
        "INFO",
        "concurrency",
        "Computed worker concurrency",
        workers=workers,
        fds_per_worker=FDS_PER_CONVERT,
        reserved=SYSTEM_RESERVED_FDS,
        env_max=ENV_MAX_PARALLEL,
    )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    processed = 0
    skipped = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker_wrapper, key): key for key in keys}
        for fut in as_completed(futures):
            key = futures.get(fut)
            try:
                fut.result()
                processed += 1
            except Exception as e:
                log("WARN", "fut.failed", "future failed for object", blob=key, error=str(e))
                skipped += 1

    log("INFO", "finished", "pre_conversions completed", processed=processed, skipped=skipped)


if __name__ == "__main__":
    main()
