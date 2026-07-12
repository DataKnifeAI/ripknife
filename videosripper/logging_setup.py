"""Logging configuration for rip, debug, scan, and error sessions."""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

from videosripper.paths import LOG_ROOT

RIP_LOG = LOG_ROOT / "rip.log"
DEBUG_LOG = LOG_ROOT / "debug.log"
SCAN_LOG = LOG_ROOT / "scan.log"
ERROR_LOG = LOG_ROOT / "error.log"

MAX_LOG_LINES = 50_000
BACKUP_COUNT = 2

_LOGGER_NAME = "videosripper"
_DEBUG_LOGGER_NAME = "videosripper.debug"
_SCAN_LOGGER_NAME = "videosripper.scan"

_SHARED_ERROR_ATTR = "_videosripper_shared_error"
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
