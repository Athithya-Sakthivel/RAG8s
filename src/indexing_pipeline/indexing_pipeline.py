#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_WORKDIR = "/indexing_pipeline"
ROUTER = "parse_chunk/router.py"
INDEX = "index.py"
PRE_CONVERSIONS = "pre_conversions.py"

RUN_PRE_CONVERSIONS_DEFAULT = True
STRICT_MODE = os.getenv("INDEXING_STRICT", "").strip().lower() in ("1", "true", "yes", "y", "on")
REQUESTED_EXIT_CODE = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def slog(level: str, event: str, msg: str = "", **extra: Any) -> None:
    payload: dict[str, Any] = {
        "ts": _now(),
        "level": level,
        "event": event,
    }
    if msg:
        payload["msg"] = msg
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def record_exit(code: int) -> None:
    global REQUESTED_EXIT_CODE
    try:
        ival = int(code)
    except Exception:
        ival = 1
    if ival > REQUESTED_EXIT_CODE:
        REQUESTED_EXIT_CODE = ival


def note_issue(level: str, event: str, msg: str, code: int = 1, **extra: Any) -> None:
    slog(level, event, msg, **extra)
    record_exit(code)


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
        return 124, getattr(e, "stdout", "") or "", getattr(e, "stderr", "") or f"timeout after {timeout}s"
    except Exception as e:
        return 1, "", f"Exception while running {cmd}: {e}"


def connect_or_start_local() -> None:
    slog("info", "pipeline.mode", "Running pipeline in local mode", mode="local")


def run_local_and_stream(
    script_path: Path,
    workdir: str,
    timeout: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> int:
    cmd = [sys.executable, str(script_path)]
    slog("info", "subprocess.start", "Starting local script", cmd=" ".join(cmd), cwd=workdir)
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
        note_issue("error", "subprocess.spawn_failed", f"Failed to start {script_path}", code=1, error=str(e))
        return 1

    def reader(stream, is_err: bool, prefix: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n")
                slog(
                    "warning" if is_err else "info",
                    "subprocess.line",
                    text,
                    script=prefix,
                    stream="stderr" if is_err else "stdout",
                    line=text,
                )
        except Exception as e:
            note_issue("error", "subprocess.reader_failed", f"Reader thread failed for {prefix}", code=1, script=prefix, error=str(e))

    prefix_out = script_path.name
    prefix_err = f"{script_path.name}:err"
    t_out = threading.Thread(target=reader, args=(proc.stdout, False, prefix_out), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, True, prefix_err), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        note_issue("error", "subprocess.timeout", f"Script timed out after {timeout} seconds", code=124, script=str(script_path), timeout=timeout)
        try:
            proc.kill()
        except Exception as e:
            slog("error", "subprocess.kill_failed", "Failed to kill timed-out process", script=str(script_path), error=str(e))
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
    slog("info", "subprocess.start", "Starting local script with capture", cmd=" ".join(cmd), cwd=workdir)
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
        note_issue("error", "subprocess.spawn_failed", f"Failed to start {script_path}", code=1, error=str(e))
        return 1, [], [str(e)]

    out_deque: deque[str] = deque(maxlen=max_lines)
    err_deque: deque[str] = deque(maxlen=max_lines)

    def reader(stream, collect_deque: deque[str], is_err: bool, prefix: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                text = line.rstrip("\n")
                collect_deque.append(text)
                slog(
                    "warning" if is_err else "info",
                    "subprocess.line",
                    text,
                    script=prefix,
                    stream="stderr" if is_err else "stdout",
                    line=text,
                )
        except Exception as e:
            note_issue("error", "subprocess.reader_failed", f"Reader thread failed for {prefix}", code=1, script=prefix, error=str(e))

    prefix_out = script_path.name
    prefix_err = f"{script_path.name}:err"
    t_out = threading.Thread(target=reader, args=(proc.stdout, out_deque, False, prefix_out), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_deque, True, prefix_err), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        note_issue("error", "subprocess.timeout", f"Script timed out after {timeout} seconds", code=124, script=str(script_path), timeout=timeout)
        try:
            proc.kill()
        except Exception as e:
            slog("error", "subprocess.kill_failed", "Failed to kill timed-out process", script=str(script_path), error=str(e))
        return 124, list(out_deque), list(err_deque)

    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)
    return proc.returncode, list(out_deque), list(err_deque)


def _env_bool_opt(name: str, default: bool) -> bool:
    return _env_bool(name, default)


def _env_int_opt(name: str, default: int) -> int:
    return _env_int(name, default)


def _env_float_opt(name: str, default: float) -> float:
    return _env_float(name, default)


def run_pre_conversions(workdir: str) -> bool:
    enabled = _env_bool_opt("RUN_PRE_CONVERSIONS", RUN_PRE_CONVERSIONS_DEFAULT)
    if not enabled:
        slog("info", "preconversions.skipped", "Skipping pre_conversions", enabled=False)
        return True

    workdir_path = Path(workdir).resolve()
    script = workdir_path / PRE_CONVERSIONS

    if not script.exists():
        slog("info", "preconversions.skipped", "pre_conversions not found", path=str(script))
        return True

    timeout_env = os.getenv("PRE_CONVERSIONS_TIMEOUT", "")
    try:
        timeout = int(timeout_env) if timeout_env else None
    except Exception:
        timeout = None

    slog("info", "preconversions.start", "Running pre_conversions", path=str(script), timeout=timeout)

    if not os.access(str(script), os.R_OK):
        try:
            script.chmod(script.stat().st_mode | 0o444)
        except Exception as e:
            slog("warning", "preconversions.chmod_failed", "Unable to adjust pre_conversions permissions", path=str(script), error=str(e))

    rc = run_local_and_stream(script, str(workdir_path), timeout=timeout)
    if rc != 0:
        note_issue("error", "preconversions.failed", "pre_conversions failed", code=rc, rc=rc, path=str(script))
        return True

    slog("info", "preconversions.ok", "pre_conversions completed", path=str(script))
    return True


def parse_index_summary(stdout_lines: list[str]) -> dict | None:
    if not stdout_lines:
        slog("warning", "index.summary_missing", "Index stdout empty")
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
                    pass

    slog("warning", "index.summary_unparsed", "Failed to locate JSON summary in index stdout")
    return None


def should_run_backup_from_summary(summary: dict) -> tuple[bool, str]:
    enable = _env_bool_opt("ENABLE_QDRANT_BACKUP", True)
    if not enable:
        return False, "ENABLE_QDRANT_BACKUP=false"

    force = _env_bool_opt("FORCE_QDRANT_BACKUP", False)
    avoid_empty = _env_bool_opt("AVOID_BACKUP_AFTER_EMPTY_INDEXING", True)
    min_points = _env_int_opt("MIN_INDEXED_POINTS_FOR_BACKUP", 100)
    min_delta_ratio = _env_float_opt("MIN_INDEX_DELTA_RATIO_FOR_BACKUP", 0.0)

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
        return False, "no points indexed (empty) and AVOID_BACKUP_AFTER_EMPTY_INDEXING=true"

    if indexed < min_points:
        return False, f"indexed_points {indexed} < MIN_INDEXED_POINTS_FOR_BACKUP {min_points}"

    if min_delta_ratio and min_delta_ratio > 0.0:
        if existing_points is None or existing_points <= 0:
            slog("warning", "backup.ratio_skipped", "MIN_INDEX_DELTA_RATIO_FOR_BACKUP set but existing_points unknown")
        else:
            ratio = indexed / float(existing_points)
            if ratio < min_delta_ratio:
                return False, f"indexed/existing ratio {ratio:.6f} < MIN_INDEX_DELTA_RATIO_FOR_BACKUP {min_delta_ratio}"

    return True, "passes all guards"


def _sleep_with_backoff(base: float, attempt: int, cap: float = 60.0) -> None:
    backoff = min(cap, base * (2 ** max(0, attempt - 1)))
    jittered = backoff * (0.5 + random.random() * 0.5)
    time.sleep(jittered)


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
            slog("info", "backup.script_found", "Found backup script", path=str(p))
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


def invoke_backup(workdir: str) -> None:
    backup_script = _find_backup_script(workdir)
    if not backup_script:
        note_issue("error", "backup.script_missing", "Backup script not found", code=3)
        return

    s3_bucket, s3_prefix = _resolve_backup_destination()
    if not s3_bucket or not s3_prefix:
        note_issue("error", "backup.env_missing", "Backup destination envs missing", code=3, bucket=s3_bucket, prefix=s3_prefix)
        return

    retries = _env_int_opt("BACKUP_INVOKE_RETRIES", 3)
    base = _env_float_opt("BACKUP_INVOKE_RETRY_BASE", 2.0)
    timeout = _env_int_opt("BACKUP_TIMEOUT", 300)

    env = os.environ.copy()
    env["DATA_S3_BUCKET"] = s3_bucket
    env["DATA_S3_PREFIX"] = s3_prefix
    env.setdefault("BACKUP_S3_BUCKET", s3_bucket)
    env.setdefault("BACKUP_BUCKET", s3_bucket)
    env.setdefault("BACKUP_PREFIX", s3_prefix)

    cmd = [sys.executable, backup_script]
    last_err: tuple[int, str, str] | None = None

    for attempt in range(1, retries + 1):
        slog(
            "info",
            "backup.invoke",
            "Invoking backup script",
            attempt=attempt,
            retries=retries,
            script=backup_script,
            bucket=s3_bucket,
            prefix=s3_prefix,
        )
        rc, out, err = run_cmd(cmd, cwd=workdir, env=env, timeout=timeout + 30)
        if rc == 0:
            slog("info", "backup.ok", "Backup script completed successfully", stdout=(out[:2000] if out else ""))
            return

        last_err = (rc, out, err)
        slog(
            "warning",
            "backup.failed_attempt",
            "Backup attempt failed",
            attempt=attempt,
            rc=rc,
            stdout=(out[-200:] if out else ""),
            stderr=(err[-200:] if err else ""),
        )
        if attempt < retries:
            _sleep_with_backoff(base, attempt)

    rc, out, err = last_err if last_err else (3, "", "unknown error")
    note_issue(
        "error",
        "backup.failed",
        "Backup failed after retries",
        code=rc or 3,
        rc=rc,
        stdout=(out[:2000] if out else ""),
        stderr=(err[:2000] if err else ""),
    )


def run_pipeline(workdir: str) -> None:
    workdir = str(Path(workdir).resolve())
    if not Path(workdir).exists():
        note_issue("error", "workdir.missing", "Workdir not found", code=2, workdir=workdir)
        return

    slog("info", "pipeline.start", "Pipeline start order", workdir=workdir, strict=STRICT_MODE)
    run_pre_conversions(workdir)
    connect_or_start_local()

    router_path = Path(workdir) / ROUTER
    if not router_path.exists():
        note_issue("error", "router.missing", "Router missing", code=1, path=str(router_path))
        return

    rc = run_local_and_stream(router_path, workdir)
    if rc != 0:
        note_issue("error", "router.failed", "Router failed", code=rc, rc=rc, path=str(router_path))
        slog("warning", "pipeline.continue", "Continuing after router failure is disabled", next_step="index")
        return

    slog("info", "router.ok", "Router completed successfully", path=str(router_path))

    index_path = Path(workdir) / INDEX
    if not index_path.exists():
        note_issue("error", "index.missing", "Index missing", code=1, path=str(index_path))
        return

    index_timeout = _env_int_opt("INDEX_TIMEOUT", 1800)
    index_tail = _env_int_opt("INDEX_STDOUT_TAIL_LINES", 2000)
    rc, stdout_lines, stderr_lines = run_local_and_capture(
        index_path,
        workdir,
        timeout=index_timeout,
        max_lines=index_tail,
    )

    if rc != 0:
        note_issue(
            "error",
            "index.failed",
            "Index failed",
            code=rc,
            rc=rc,
            stdout=(stdout_lines[-1] if stdout_lines else ""),
            stderr=(stderr_lines[-1] if stderr_lines else ""),
        )
        return

    slog("info", "index.ok", "Index completed successfully", path=str(index_path))
    summary = parse_index_summary(stdout_lines)
    if summary is None:
        slog("warning", "backup.skipped", "Index summary missing or unparsable; backup will be skipped")
        return

    should_backup, reason = should_run_backup_from_summary(summary)
    slog("info", "backup.decision", "Backup decision computed", should_backup=bool(should_backup), reason=reason, summary=summary)

    if should_backup:
        invoke_backup(workdir)
    else:
        slog("info", "backup.skipped", "Skipping backup", reason=reason)

    slog("info", "pipeline.done", "Pipeline completed successfully")


def _finalize_and_exit() -> None:
    if REQUESTED_EXIT_CODE != 0:
        if STRICT_MODE:
            slog("error", "exit.strict", "Exiting with recorded error code", exit_code=REQUESTED_EXIT_CODE)
            sys.exit(REQUESTED_EXIT_CODE)
        slog("warning", "exit.non_strict", "Non-fatal errors recorded; exiting 0 because INDEXING_STRICT is disabled", recorded_exit_code=REQUESTED_EXIT_CODE)
    sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=os.getenv("WORKDIR", DEFAULT_WORKDIR))
    args = parser.parse_args()

    def _handler(sig, frame):
        slog("warning", "signal.received", "Signal received", signal=sig)
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    try:
        run_pipeline(args.workdir)
    except SystemExit:
        raise
    except Exception as e:
        note_issue("error", "main.unhandled", "Unhandled exception in main", code=2, error=str(e))
    _finalize_and_exit()


if __name__ == "__main__":
    main()
