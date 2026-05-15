#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

DEFAULT_WORKDIR = "/indexing_pipeline"
ROUTER = "parse_chunk/router.py"
INDEX = "index.py"
PRE_CONVERSIONS = "pre_conversions.sh"
RUN_PRE_CONVERSIONS_DEFAULT = True

STRICT_MODE = os.getenv("INDEXING_STRICT", "").strip().lower() in ("1", "true", "yes", "y", "on")

logger = logging.getLogger("indexing_pipeline")
logger.handlers.clear()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.DEBUG if os.getenv("LOG_LEVEL", "INFO").strip().upper() == "DEBUG" else logging.INFO)
logger.propagate = False

for name in ("botocore", "boto3", "urllib3", "httpx", "qdrant_client", "asyncio"):
    lg = logging.getLogger(name)
    lg.handlers.clear()
    lg.setLevel(logging.CRITICAL)
    lg.propagate = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


def _flush_log_handlers() -> None:
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass


def _format_extra(extra: dict[str, Any]) -> str:
    if not extra:
        return ""
    parts = []
    for k, v in extra.items():
        if v is None:
            continue
        if isinstance(v, (dict, list, tuple)):
            try:
                v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                v = str(v)
        parts.append(f"{k}={v}")
    return (" " + " ".join(parts)) if parts else ""


def log_info(msg: str, **extra: Any) -> None:
    logger.info("%s%s", msg, _format_extra(extra))


def log_warn(msg: str, **extra: Any) -> None:
    logger.warning("%s%s", msg, _format_extra(extra))


def log_error(msg: str, **extra: Any) -> None:
    logger.error("%s%s", msg, _format_extra(extra))


def _pretty_child_json(line: str, script_name: str, stream_name: str) -> str:
    raw = line.strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw

    if not isinstance(obj, dict):
        return raw

    level = str(obj.get("level") or obj.get("lvl") or "").lower()
    event = str(obj.get("event") or obj.get("evt") or "").strip()
    msg = str(obj.get("msg") or obj.get("message") or "").strip()

    base_fields = []
    for key in ("bucket", "region", "run_id", "path", "key", "count", "saved_chunks", "total_input_chunks", "indexed_points", "skipped_existing", "batch", "hybrid", "collection", "reason", "status", "attempt", "max_retries", "page_number", "chunks", "processed"):
        if key in obj:
            val = obj.get(key)
            if val is not None:
                base_fields.append(f"{key}={val}")

    extra_fields = []
    for k, v in obj.items():
        if k in {"ts", "level", "lvl", "event", "evt", "msg", "message"}:
            continue
        if k in {"bucket", "region", "run_id", "path", "key", "count", "saved_chunks", "total_input_chunks", "indexed_points", "skipped_existing", "batch", "hybrid", "collection", "reason", "status", "attempt", "max_retries", "page_number", "chunks", "processed"}:
            continue
        if v is None:
            continue
        if isinstance(v, (dict, list, tuple)):
            try:
                v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                v = str(v)
        extra_fields.append(f"{k}={v}")

    fields = base_fields + extra_fields
    suffix = f" {' '.join(fields)}" if fields else ""
    if event and msg:
        return f"{script_name} | {level or stream_name} | {event} | {msg}{suffix}"
    if event:
        return f"{script_name} | {level or stream_name} | {event}{suffix}"
    if msg:
        return f"{script_name} | {level or stream_name} | {msg}{suffix}"
    if fields:
        return f"{script_name} | {level or stream_name} | {raw}{suffix}"
    return f"{script_name} | {level or stream_name} | {raw}"


def _render_child_line(line: str, script_name: str, stream_name: str) -> tuple[str, str]:
    rendered = _pretty_child_json(line, script_name, stream_name)
    if rendered == line.strip():
        return rendered, "plain"
    return rendered, "json"


def run_cmd(
    cmd: list[str],
    cwd: str = ".",
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    env_used = os.environ.copy()
    if env:
        env_used.update(env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env_used,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, (getattr(e, "stdout", "") or ""), (getattr(e, "stderr", "") or f"timeout after {timeout}s")
    except Exception as e:
        return 1, "", f"Exception while running {cmd}: {e}"


def run_local_and_stream(
    script_path: Path,
    workdir: str,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> int:
    cmd = [sys.executable, str(script_path)]
    log_info("subprocess.start", cmd=" ".join(cmd), cwd=workdir)
    env_used = os.environ.copy()
    if extra_env:
        env_used.update(extra_env)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env_used,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
    except Exception as e:
        log_error("subprocess.start_failed", script=str(script_path), error=str(e))
        return 1

    def reader(stream, is_err: bool, prefix: str):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n")
                rendered, mode = _render_child_line(text, prefix, "stderr" if is_err else "stdout")
                if is_err:
                    log_warn(rendered, script=prefix, stream="stderr", mode=mode, line=text)
                else:
                    log_info(rendered, script=prefix, stream="stdout", mode=mode, line=text)
        except Exception:
            log_error("subprocess.reader_failed", script=prefix, traceback=traceback.format_exc())

    t_out = threading.Thread(target=reader, args=(proc.stdout, False, script_path.name), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, True, f"{script_path.name}:err"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log_error("subprocess.timeout", script=str(script_path), timeout=timeout)
        try:
            proc.kill()
        except Exception:
            log_error("subprocess.kill_failed", script=str(script_path), traceback=traceback.format_exc())
        return 124

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    return proc.returncode


def run_local_and_capture(
    script_path: Path,
    workdir: str,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
    max_lines: int = 2000,
) -> tuple[int, list[str], list[str]]:
    cmd = [sys.executable, str(script_path)]
    log_info("subprocess.start_capture", cmd=" ".join(cmd), cwd=workdir)
    env_used = os.environ.copy()
    if extra_env:
        env_used.update(extra_env)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env_used,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
    except Exception as e:
        log_error("subprocess.start_failed", script=str(script_path), error=str(e))
        return 1, [], [str(e)]

    out_deque: deque[str] = deque(maxlen=max_lines)
    err_deque: deque[str] = deque(maxlen=max_lines)

    def reader(stream, collect_deque: deque[str], is_err: bool, prefix: str):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n")
                collect_deque.append(text)
                rendered, mode = _render_child_line(text, prefix, "stderr" if is_err else "stdout")
                if is_err:
                    log_warn(rendered, script=prefix, stream="stderr", mode=mode, line=text)
                else:
                    log_info(rendered, script=prefix, stream="stdout", mode=mode, line=text)
        except Exception:
            log_error("subprocess.reader_failed", script=prefix, traceback=traceback.format_exc())

    t_out = threading.Thread(target=reader, args=(proc.stdout, out_deque, False, script_path.name), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_deque, True, f"{script_path.name}:err"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log_error("subprocess.timeout", script=str(script_path), timeout=timeout)
        try:
            proc.kill()
        except Exception:
            log_error("subprocess.kill_failed", script=str(script_path), traceback=traceback.format_exc())
        return 124, list(out_deque), list(err_deque)

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    return proc.returncode, list(out_deque), list(err_deque)


def run_pre_conversions(workdir: str) -> bool:
    enabled = _env_bool("RUN_PRE_CONVERSIONS", RUN_PRE_CONVERSIONS_DEFAULT)
    if not enabled:
        log_info("preconversions.skipped", enabled=False)
        return True

    script = Path(workdir).resolve() / PRE_CONVERSIONS
    if not script.exists():
        log_info("preconversions.skipped", reason="script_missing", path=str(script))
        return True

    timeout_env = os.getenv("PRE_CONVERSIONS_TIMEOUT", "").strip()
    try:
        timeout = int(timeout_env) if timeout_env else None
    except Exception:
        timeout = None

    log_info("preconversions.start", path=str(script), timeout=timeout)

    # If the pre-conversion script is a shell script, run it with bash.
    if script.suffix == ".sh":
        cmd = ["bash", str(script)]
        rc, out, err = run_cmd(cmd, cwd=workdir, timeout=timeout)
        if rc != 0:
            log_warn("preconversions.failed", rc=rc, stdout=(out[-300:] if out else ""), stderr=(err[-300:] if err else ""))
            return not STRICT_MODE
        log_info("preconversions.ok", path=str(script))
        return True

    # Fallback: if it's not a shell script, attempt to run it as a Python script (original behavior).
    rc = run_local_and_stream(script, workdir, timeout=timeout)
    if rc != 0:
        log_warn("preconversions.failed", rc=rc)
        return not STRICT_MODE
    log_info("preconversions.ok", path=str(script))
    return True


def parse_index_summary(stdout_lines: list[str]) -> dict[str, Any] | None:
    if not stdout_lines:
        return None

    for line in reversed(stdout_lines):
        s = line.strip()
        if not s:
            continue
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            start = s.find("{")
            end = s.rfind("}")
            if 0 <= start < end:
                try:
                    parsed = json.loads(s[start : end + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    continue
    return None


def should_run_backup_from_summary(summary: dict[str, Any]) -> tuple[bool, str]:
    enable = _env_bool("ENABLE_QDRANT_BACKUP", True)
    if not enable:
        return False, "ENABLE_QDRANT_BACKUP=false"

    force = _env_bool("FORCE_QDRANT_BACKUP", False)
    avoid_empty = _env_bool("AVOID_BACKUP_AFTER_EMPTY_INDEXING", True)
    min_points = _env_int("MIN_INDEXED_POINTS_FOR_BACKUP", 100)
    min_delta_ratio = _env_float("MIN_INDEX_DELTA_RATIO_FOR_BACKUP", 0.0)

    indexed = int(summary.get("indexed_points", 0) or 0)
    skipped_existing = int(summary.get("skipped_existing", 0) or 0)

    existing_points = None
    if "existing_points" in summary:
        try:
            existing_points = int(summary.get("existing_points", 0) or 0)
        except Exception:
            existing_points = None
    else:
        existing_points = skipped_existing if skipped_existing > 0 else None

    if force:
        return True, "FORCE_QDRANT_BACKUP=true"
    if avoid_empty and indexed == 0:
        return False, "indexed_points=0"
    if indexed < min_points:
        return False, f"indexed_points {indexed} < MIN_INDEXED_POINTS_FOR_BACKUP {min_points}"
    if min_delta_ratio and min_delta_ratio > 0.0:
        if existing_points is None or existing_points <= 0:
            return True, "existing_points_unknown"
        ratio = indexed / float(existing_points)
        if ratio < min_delta_ratio:
            return False, f"indexed/existing ratio {ratio:.6f} < MIN_INDEX_DELTA_RATIO_FOR_BACKUP {min_delta_ratio}"
    return True, "passes_guards"


def _sleep_with_backoff(base: float, attempt: int, cap: float = 60.0):
    backoff = min(cap, base * (2 ** max(0, attempt - 1)))
    time.sleep(backoff * (0.5 + random.random() * 0.5))


def _find_backup_script(workdir: str) -> str | None:
    candidates: list[str] = []
    env_path = os.getenv("RUN_QDRANT_BACKUP_PATH")
    if env_path:
        candidates.append(env_path)

    candidates.extend(
        [
            os.path.join(workdir, "run_qdrant_backup.py"),
            os.path.join(workdir, "run_qdrant_backup_service.py"),
            os.path.join(workdir, "infra", "runners", "run_qdrant_backup_service.py"),
            os.path.join(workdir, "infra", "runners", "run_qdrant_backup.py"),
        ]
    )

    here = Path(__file__).resolve().parent
    candidates.extend(
        [
            str(here / "run_qdrant_backup.py"),
            str(here / "infra" / "runners" / "run_qdrant_backup_service.py"),
            str(here / "infra" / "runners" / "run_qdrant_backup.py"),
        ]
    )

    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        p = Path(c)
        if not p.is_absolute():
            p = (Path(workdir) / c).resolve()
        if p.exists() and p.is_file():
            return str(p)
    return None


def _resolve_backup_destination() -> tuple[str | None, str | None]:
    bucket = (
        os.getenv("DATA_S3_BUCKET")
        or os.getenv("BACKUP_S3_BUCKET")
        or os.getenv("BACKUP_BUCKET")
        or os.getenv("BACKUP_AWS_BUCKET")
    )
    prefix = (
        os.getenv("DATA_S3_PREFIX")
        or os.getenv("BACKUP_S3_PREFIX")
        or os.getenv("BACKUP_PREFIX")
        or os.getenv("BACKUP_AWS_PREFIX")
    )
    return bucket, prefix


def invoke_backup(workdir: str) -> bool:
    backup_script = _find_backup_script(workdir)
    if not backup_script:
        log_error("backup.script_missing")
        return False

    s3_bucket, s3_prefix = _resolve_backup_destination()
    if not s3_bucket or not s3_prefix:
        log_error("backup.destination_missing", bucket=s3_bucket, prefix=s3_prefix)
        return False

    retries = _env_int("BACKUP_INVOKE_RETRIES", 3)
    base = _env_float("BACKUP_INVOKE_RETRY_BASE", 2.0)
    timeout = _env_int("BACKUP_TIMEOUT", 300)

    env = os.environ.copy()
    env["DATA_S3_BUCKET"] = s3_bucket
    env["DATA_S3_PREFIX"] = s3_prefix
    env.setdefault("BACKUP_S3_BUCKET", s3_bucket)
    env.setdefault("BACKUP_BUCKET", s3_bucket)
    env.setdefault("BACKUP_PREFIX", s3_prefix)

    cmd = [sys.executable, backup_script]
    last = None

    for attempt in range(1, retries + 1):
        log_info("backup.start", attempt=attempt, retries=retries, script=backup_script, prefix=s3_prefix)
        rc, out, err = run_cmd(cmd, cwd=workdir, env=env, timeout=timeout + 30)
        if rc == 0:
            log_info("backup.ok", script=backup_script)
            return True

        last = (rc, out, err)
        log_warn("backup.failed", attempt=attempt, rc=rc, stdout_tail=(out[-300:] if out else ""), stderr_tail=(err[-300:] if err else ""))
        if attempt < retries:
            _sleep_with_backoff(base, attempt)

    rc, out, err = last if last else (3, "", "unknown error")
    log_error("backup.failed_final", rc=rc, stdout=(out[:2000] if out else ""), stderr=(err[:2000] if err else ""))
    return False


def run_pipeline(workdir: str) -> int:
    workdir = str(Path(workdir).resolve())
    if not Path(workdir).exists():
        log_error("workdir_missing", workdir=workdir)
        return 2

    log_info("pipeline.start", workdir=workdir, strict=STRICT_MODE)

    if not run_pre_conversions(workdir) and STRICT_MODE:
        return 1

    router_path = Path(workdir) / ROUTER
    if not router_path.exists():
        log_error("router_missing", path=str(router_path))
        return 1

    log_info("pipeline.mode", mode="local")
    rc = run_local_and_stream(router_path, workdir)
    if rc != 0:
        log_error("router.failed", rc=rc)
        if STRICT_MODE:
            return rc or 1
        return rc or 1

    log_info("router.ok", path=str(router_path))

    index_path = Path(workdir) / INDEX
    if not index_path.exists():
        log_error("index_missing", path=str(index_path))
        return 1

    index_timeout = _env_int("INDEX_TIMEOUT", 1800)
    stdout_lines_max = _env_int("INDEX_STDOUT_TAIL_LINES", 2000)
    rc, stdout_lines, stderr_lines = run_local_and_capture(
        index_path,
        workdir,
        timeout=index_timeout,
        max_lines=stdout_lines_max,
    )

    if rc != 0:
        log_error(
            "index.failed",
            rc=rc,
            stdout=(stdout_lines[-1] if stdout_lines else ""),
            stderr=(stderr_lines[-1] if stderr_lines else ""),
        )
        return rc or 1

    log_info("index.ok", path=str(index_path))

    summary = parse_index_summary(stdout_lines)
    if summary is None:
        log_warn("backup.skipped", reason="index_summary_missing")
        log_info("pipeline.done")
        return 0

    should_backup, reason = should_run_backup_from_summary(summary)
    log_info("backup.decision", should_backup=should_backup, reason=reason, summary=summary)

    if should_backup:
        ok = invoke_backup(workdir)
        if not ok and STRICT_MODE:
            return 3
    else:
        log_info("backup.skipped", reason=reason)

    log_info("pipeline.done", message="Pipeline completed successfully")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.getenv("WORKDIR", DEFAULT_WORKDIR))
    args = parser.parse_args()

    def _handler(sig, frame):
        log_warn("signal.received", signal=sig)
        raise SystemExit(1)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    try:
        code = run_pipeline(args.workdir)
    except SystemExit as e:
        code = int(getattr(e, "code", 1) or 1)
        if code:
            log_error("exit.system", exit_code=code)
        else:
            log_info("exit.system", exit_code=0)
    except Exception:
        code = 2
        log_error("unhandled_exception", traceback=traceback.format_exc())
    finally:
        _flush_log_handlers()

    raise SystemExit(code)


if __name__ == "__main__":
    main()
