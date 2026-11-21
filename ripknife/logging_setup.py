"""Logging configuration for rip, debug, scan, and error sessions.

File logs live under ``paths.LOG_ROOT`` (``RIPKNIFE_LOGS`` or ``$ROOT/logs``).
systemd user units set ``RIPKNIFE_LOGS=/var/log/ripknife``; ``make watch-all``
keeps the repo ``logs/`` default. Rotation is by line count (``MAX_LOG_LINES`` /
``BACKUP_COUNT``) in both cases.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from pathlib import Path

from ripknife.paths import LOG_ROOT

RIP_LOG = LOG_ROOT / "rip.log"
DEBUG_LOG = LOG_ROOT / "debug.log"
SCAN_LOG = LOG_ROOT / "scan.log"
ERROR_LOG = LOG_ROOT / "error.log"
PIPELINE_LOG = LOG_ROOT / "pipeline.log"

MAX_LOG_LINES = 50_000
BACKUP_COUNT = 2

_LOGGER_NAME = "ripknife"
_DEBUG_LOGGER_NAME = "ripknife.debug"
_SCAN_LOGGER_NAME = "ripknife.scan"
_PIPELINE_LOGGER_NAME = "ripknife.pipeline"

_SHARED_ERROR_ATTR = "_ripknife_shared_error"
_error_handler = None  # set to _LineRotatingFileHandler after first use


def _count_file_lines(path: Path) -> int:
    """Count newline-terminated lines in an existing file (0 if missing)."""
    if not path.is_file():
        return 0
    count = 0
    with path.open("rb") as fh:
        for _ in fh:
            count += 1
    return count


class _LineRotatingFileHandler(logging.Handler):
    """Append log records; rotate when line count exceeds ``max_lines``.

    Backup naming matches :class:`logging.handlers.RotatingFileHandler`:
    ``log.log`` → ``log.log.1`` (newest) … ``log.log.N`` (oldest).
    """

    def __init__(
        self,
        log_path: Path,
        *,
        max_lines: int = MAX_LOG_LINES,
        backup_count: int = BACKUP_COUNT,
        write_header: str | None = None,
    ) -> None:
        super().__init__()
        if max_lines < 1:
            raise ValueError("max_lines must be >= 1")
        if backup_count < 0:
            raise ValueError("backup_count must be >= 0")
        self.log_path = Path(log_path)
        self.max_lines = max_lines
        self.backup_count = backup_count
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._line_count = _count_file_lines(self.log_path)
        self._file = self.log_path.open("a", encoding="utf-8")
        if write_header:
            self._write_line(write_header)

    def _write_line(self, text: str) -> None:
        if self._line_count >= self.max_lines:
            self._do_rollover()
        self._file.write(text + "\n")
        self._file.flush()
        self._line_count += 1

    def _do_rollover(self) -> None:
        self._file.close()
        if self.backup_count > 0:
            for idx in range(self.backup_count - 1, 0, -1):
                src = Path(f"{self.log_path}.{idx}")
                dst = Path(f"{self.log_path}.{idx + 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
            backup1 = Path(f"{self.log_path}.1")
            if backup1.exists():
                backup1.unlink()
            if self.log_path.exists():
                self.log_path.rename(backup1)
        elif self.log_path.exists():
            self.log_path.unlink()
        self._file = self.log_path.open("a", encoding="utf-8")
        self._line_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._write_line(self.format(record))
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            super().close()


class _ConsoleHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()


def _session_header(manifest: Path | str | None) -> str:
    label = str(manifest) if manifest else "unknown"
    return f"=== Session started manifest={label} ==="


def _is_shared_error_handler(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _SHARED_ERROR_ATTR, False))


def _get_error_handler() -> _LineRotatingFileHandler:
    """Return the shared ERROR-level handler for ``error.log`` (recreate if path changed)."""
    global _error_handler
    existing = _error_handler
    if existing is not None and existing.log_path != Path(ERROR_LOG):
        existing.close()
        _error_handler = None
        existing = None
    if existing is None:
        handler = _LineRotatingFileHandler(ERROR_LOG)
        handler.setLevel(logging.ERROR)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _SHARED_ERROR_ATTR, True)
        _error_handler = handler
        return handler
    return existing


def _attach_error_handler(logger: logging.Logger) -> None:
    """Attach the shared error.log handler if not already present."""
    handler = _get_error_handler()
    if handler not in logger.handlers:
        logger.addHandler(handler)


def _clear_logger(name: str) -> None:
    logger = logging.getLogger(name)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        if _is_shared_error_handler(handler):
            continue
        handler.close()


def _configure_file_logger(
    name: str,
    log_path: Path,
    *,
    level: int,
    header: str | None = None,
    console: bool = False,
) -> logging.Logger:
    _clear_logger(name)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("%(message)s")
    file_handler = _LineRotatingFileHandler(log_path, write_header=header)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    if console:
        console_handler = _ConsoleHandler()
        console_handler.setFormatter(fmt)
        logger.addHandler(console_handler)
    _attach_error_handler(logger)
    return logger


def setup_session_logger(manifest: Path | str | None = None) -> logging.Logger:
    """Configure append-mode rip.log + debug.log (+ error.log) for a rip session."""
    header = _session_header(manifest)
    rip_logger = _configure_file_logger(
        _LOGGER_NAME,
        RIP_LOG,
        level=logging.INFO,
        header=header,
        console=True,
    )
    _configure_file_logger(
        _DEBUG_LOGGER_NAME,
        DEBUG_LOG,
        level=logging.DEBUG,
        header=header,
    )
    return rip_logger


def setup_scan_logger(manifest: Path | str | None = None, *, console: bool = True) -> logging.Logger:
    """Configure append-mode scan.log + debug.log (+ error.log) for a scan session."""
    label = str(manifest) if manifest else "unknown"
    header = f"=== Scan started manifest={label} ==="
    scan_logger = _configure_file_logger(
        _SCAN_LOGGER_NAME,
        SCAN_LOG,
        level=logging.INFO,
        header=header,
        console=console,
    )
    _configure_file_logger(
        _DEBUG_LOGGER_NAME,
        DEBUG_LOG,
        level=logging.DEBUG,
        header=header,
    )
    return scan_logger


def log_user(msg: str, *args) -> None:
    """High-level user-facing message → rip.log + console."""
    logging.getLogger(_LOGGER_NAME).info(msg, *args)


def log_debug(msg: str, *args) -> None:
    """Verbose/debug message → debug.log only."""
    logging.getLogger(_DEBUG_LOGGER_NAME).info(msg, *args)


def log_scan(msg: str, *args) -> None:
    """Scan summary message → scan.log + console."""
    logging.getLogger(_SCAN_LOGGER_NAME).info(msg, *args)


def log_error(msg: str, *args) -> None:
    """ERROR-level message → rip.log + console + error.log."""
    logging.getLogger(_LOGGER_NAME).error(msg, *args)


def log_exception(exc: BaseException, *, user_message: str | None = None) -> None:
    """User-facing ALERT in rip.log (+ error.log); full traceback in debug.log + error.log."""
    rip = logging.getLogger(_LOGGER_NAME)
    msg = user_message or f"{type(exc).__name__}: {exc}"
    rip.error("=== ALERT === %s", msg)
    debug = logging.getLogger(_DEBUG_LOGGER_NAME)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    debug.error("Traceback for: %s", msg)
    debug.error("%s", tb)


def setup_pipeline_logger() -> logging.Logger:
    """Configure append-mode pipeline.log for high-level stage progress."""
    logger = logging.getLogger(_PIPELINE_LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = _LineRotatingFileHandler(PIPELINE_LOG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    console_handler = _ConsoleHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)
    _attach_error_handler(logger)
    return logger


# Monotonic time of the previous log_stage call in this process (None = no stage yet).
_last_stage_monotonic: float | None = None

# Monotonic start of each named pipeline (set when its step 1 stage is logged) so
# DONE/FAILED lines can report total wall time, not just time since the last stage.
_process_started: dict[str, float] = {}


def _format_elapsed(seconds: float) -> str:
    """Render an elapsed duration compactly, e.g. ``42s``, ``3m07s``, ``1h02m09s``."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


# Named pipelines so ``[1/5]`` is never ambiguous across Process 1 vs Process 2.
PROCESS_INSERT = "insert"  # disc-watcher: scan → pending → registry/map
PROCESS_RIP = "rip"  # rip-watcher: validate/claim → rescan → verify → rip → copy → eject → done
INSERT_STEPS = 5
RIP_STEP_VALIDATE = 1
RIP_STEP_RESCAN = 2
RIP_STEP_VERIFY = 3
RIP_STEP_RIP = 4
RIP_STEP_COPY = 5
RIP_STEP_EJECT = 6
RIP_STEP_DONE = 7
RIP_STEPS = 7


def log_watcher_waiting(watcher: str, devices: list[str] | None = None) -> None:
    """Startup marker → pipeline.log so ``tail -F`` works before the first disc.

    Example: ``log_watcher_waiting("disc-watcher", ["dev:/dev/sr0"])``
    → ``=== STAGE WATCHERS_UP — disc-watcher waiting (devices=dev:/dev/sr0) ===``

    Idle/status stages omit ``[process N/M]`` — they are outside the numbered pipelines.
    """
    scope = f"devices={', '.join(devices)}" if devices else "staging/ready"
    log_stage("WATCHERS_UP", detail=f"{watcher} waiting ({scope})")


def log_stage(
    stage: str,
    *,
    process: str | None = None,
    step: int | None = None,
    total: int | None = None,
    detail: str = "",
) -> None:
    """High-level pipeline stage marker → pipeline.log (+ console).

    Numbered stages must pass ``process`` so insert vs rip are unambiguous:

    ``log_stage("SCAN", process="insert", step=1, total=5, detail="device=…")``
    → ``=== STAGE [insert 1/5] SCAN — device=… === [+37s]``

    ``log_stage("RIP", process="rip", step=4, total=7, detail="…")``
    → ``=== STAGE [rip 4/7] RIP — … ===``

    Status-only stages (WATCHERS_UP, IDLE, WAITING_MANIFEST, DISC_DETECTED) omit
    ``process`` / step — they are outside the numbered insert/rip pipelines.

    Failures inside a pipeline pass ``process`` without step:
    → ``=== STAGE [insert] SCAN_FAILED — … ===``

    File lines are timestamped by the pipeline formatter; the trailing
    ``[+…]`` is the elapsed time since the previous stage in this process.
    """
    global _last_stage_monotonic
    setup_pipeline_logger()
    now = time.monotonic()
    elapsed = "" if _last_stage_monotonic is None else f" [+{_format_elapsed(now - _last_stage_monotonic)}]"
    _last_stage_monotonic = now
    if process and step == 1:
        _process_started[process] = now
    if step is not None and total is not None:
        bracket = f"{process} {step}/{total}" if process else f"{step}/{total}"
        prefix = f"=== STAGE [{bracket}] {stage}"
    elif process:
        prefix = f"=== STAGE [{process}] {stage}"
    else:
        prefix = f"=== STAGE {stage}"
    msg = f"{prefix} — {detail} ==={elapsed}" if detail else f"{prefix} ==={elapsed}"
    logging.getLogger(_PIPELINE_LOGGER_NAME).info("%s", msg)
    # Mirror highlights into rip.log when a rip session is active
    rip = logging.getLogger(_LOGGER_NAME)
    if rip.handlers:
        rip.info("%s", msg)


def process_elapsed(process: str) -> str | None:
    """Formatted wall time since step 1 of ``process`` (None if that step never ran).

    Used by terminal stages (``DONE`` / ``RIP_FAILED``) so one line carries the whole
    duration of the rip — the trailing ``[+…]`` only covers the previous stage.
    """
    started = _process_started.get(process)
    if started is None:
        return None
    return _format_elapsed(time.monotonic() - started)


def log_rip_stage(stage: str, step: int, detail: str = "") -> None:
    """Numbered rip-pipeline stage → pipeline.log."""
    log_stage(stage, process=PROCESS_RIP, step=step, total=RIP_STEPS, detail=detail)


def log_copy_start(src_name: str, dest: Path | str, *, numbered: bool = True) -> None:
    """NAS copy start. ``numbered=True`` for the sole movie COPY step; TV uses per-file unnumbered."""
    detail = f"start {src_name} → {dest}"
    if numbered:
        log_rip_stage("COPY", RIP_STEP_COPY, detail)
    else:
        log_stage("COPY", process=PROCESS_RIP, detail=detail)


def log_copy_result(ok: bool, src_name: str, dest: Path | str) -> None:
    """NAS copy success/fail (unnumbered; follows a COPY start)."""
    name = "COPY_OK" if ok else "COPY_FAILED"
    log_stage(name, process=PROCESS_RIP, detail=f"{src_name} → {dest}")


def log_local_cleanup(src_name: str, dest: Path | str, *, deleted: bool) -> None:
    """Local file removal after verified NAS copy."""
    if deleted:
        detail = f"deleted {src_name} (NAS: {dest})"
    else:
        detail = f"skipped {src_name} (NAS verify failed or delete error)"
    log_stage("LOCAL_CLEANUP", process=PROCESS_RIP, detail=detail)


def log_copy_skipped(reason: str) -> None:
    """Numbered COPY stage when NAS copy is disabled or has no destination."""
    log_rip_stage("COPY", RIP_STEP_COPY, f"skipped ({reason})")


def log_copy_summary(ok: int, failed: int, dest_hint: str = "") -> None:
    """Numbered COPY stage summarizing TV multi-file NAS copies."""
    detail = f"{ok} ok, {failed} failed"
    if dest_hint:
        detail = f"{detail} → {dest_hint}"
    log_rip_stage("COPY", RIP_STEP_COPY, detail)


def log_eject_stage(device: str, *, skipped: str | None = None, ok: bool | None = None) -> None:
    """Numbered EJECT stage (skipped / start+result via ``ok``)."""
    if skipped is not None:
        log_rip_stage("EJECT", RIP_STEP_EJECT, f"skipped ({skipped})")
        return
    if ok is True:
        log_rip_stage("EJECT", RIP_STEP_EJECT, f"ok {device}")
    elif ok is False:
        log_rip_stage("EJECT", RIP_STEP_EJECT, f"FAILED {device} — eject manually")
    else:
        log_rip_stage("EJECT", RIP_STEP_EJECT, f"start {device}")


def log_remux_stage(detail: str) -> None:
    """stream_defaults ffmpeg remux (unnumbered; runs inside RIP per title)."""
    log_stage("REMUX", process=PROCESS_RIP, detail=detail)


def log_stitch_stage(stage: str, detail: str) -> None:
    """Multi-disc stitch stages (post-rip, outside makemkv lock)."""
    log_stage(stage, process=PROCESS_RIP, detail=detail)


def log_progress(
    stage: str,
    current: int,
    total: int,
    *,
    process: str | None = None,
    detail: str = "",
    include_percent: bool = True,
) -> None:
    """In-stage progress → pipeline.log (+ console).

    Example: ``log_progress("RIP", 2, 12, process="rip", detail="6x22 The Card")``
    → ``  progress [rip] RIP 2/12 (17%) — 6x22 The Card``

    ``total <= 0`` omits N/M (play-all MakeMKV headline). ``include_percent=False``
    keeps N/M without implying that current/total is elapsed work (EXTRACT 2/5).
    """
    setup_pipeline_logger()
    suffix = f" — {detail}" if detail else ""
    tag = f"[{process}] " if process else ""
    if total <= 0:
        msg = f"  progress {tag}{stage}{suffix}"
    else:
        pct_s = f" ({int(100 * current / total)}%)" if include_percent else ""
        msg = f"  progress {tag}{stage} {current}/{total}{pct_s}{suffix}"
    logging.getLogger(_PIPELINE_LOGGER_NAME).info("%s", msg)
    rip = logging.getLogger(_LOGGER_NAME)
    if rip.handlers:
        rip.info("%s", msg)


def log_rip_percent(
    pct: int,
    *,
    title: int | str | None = None,
    source: str = "MakeMKV",
    eta_sec: int | None = None,
    stage: str = "RIP",
    label: str | None = None,
) -> None:
    """Live within-title percent → pipeline.log (+ console).

    Example: ``log_rip_percent(40, title=0, eta_sec=840)``
    → ``  progress [rip] RIP title 0 — 40% (MakeMKV, ~14m left)``

    ``stage="EXTRACT"`` is ffmpeg chapter cut, not MakeMKV. Emitted at ~10% steps
    (see :class:`ripknife.monitor.RipDecileTracker`) so ``tail -F logs/pipeline.log``
    shows progress without spamming the overview.
    ETA is omitted when ``eta_sec`` is ``None`` (unstable early estimate).
    """
    from ripknife.monitor import format_eta_remaining

    setup_pipeline_logger()
    if label is None:
        label = f"title {title}" if title is not None else "current title"
    eta_s = format_eta_remaining(eta_sec)
    paren = f"{source}, {eta_s} left" if eta_s else source
    msg = f"  progress [{PROCESS_RIP}] {stage} {label} — {pct}% ({paren})"
    logging.getLogger(_PIPELINE_LOGGER_NAME).info("%s", msg)
    rip = logging.getLogger(_LOGGER_NAME)
    if rip.handlers:
        rip.info("%s", msg)
