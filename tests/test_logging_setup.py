"""Unit tests for videosripper.logging_setup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from videosripper import logging_setup, paths


def _patch_log_paths(monkeypatch: pytest.MonkeyPatch, log_root: Path) -> None:
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "RIP_LOG", log_root / "rip.log")
    monkeypatch.setattr(logging_setup, "DEBUG_LOG", log_root / "debug.log")
    monkeypatch.setattr(logging_setup, "SCAN_LOG", log_root / "scan.log")
    monkeypatch.setattr(logging_setup, "ERROR_LOG", log_root / "error.log")
    # Force shared error handler to recreate against the patched path.
    if logging_setup._error_handler is not None:
        logging_setup._error_handler.close()
        logging_setup._error_handler = None


def test_session_header_includes_manifest() -> None:
    assert "configs/test.yaml" in logging_setup._session_header("configs/test.yaml")


def test_session_header_unknown_when_missing() -> None:
    assert "unknown" in logging_setup._session_header(None)


def test_setup_session_logger_writes_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logger = logging_setup.setup_session_logger("configs/example.yaml")
    logger.info("rip message")
    logging_setup.log_debug("debug message")

    rip_text = (log_root / "rip.log").read_text(encoding="utf-8")
    debug_text = (log_root / "debug.log").read_text(encoding="utf-8")

    assert "Session started manifest=configs/example.yaml" in rip_text
    assert "rip message" in rip_text
    assert "Session started manifest=configs/example.yaml" in debug_text
    assert "debug message" in debug_text


def test_error_log_captures_error_not_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logger = logging_setup.setup_session_logger("configs/example.yaml")
    logger.info("info only")
    logger.error("=== ALERT === boom")

    error_path = log_root / "error.log"
    assert error_path.is_file()
    error_text = error_path.read_text(encoding="utf-8")
    rip_text = (log_root / "rip.log").read_text(encoding="utf-8")

    assert "=== ALERT === boom" in error_text
    assert "info only" not in error_text
    assert "=== ALERT === boom" in rip_text
    assert "info only" in rip_text


def test_log_exception_writes_traceback_to_error_and_debug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.setup_session_logger("configs/example.yaml")
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as exc:
        logging_setup.log_exception(exc, user_message="kaboom")

    error_text = (log_root / "error.log").read_text(encoding="utf-8")
    debug_text = (log_root / "debug.log").read_text(encoding="utf-8")
    rip_text = (log_root / "rip.log").read_text(encoding="utf-8")

    assert "=== ALERT === kaboom" in rip_text
    assert "=== ALERT === kaboom" in error_text
    assert "Traceback for: kaboom" in error_text
    assert "RuntimeError: kaboom" in error_text
    assert "Traceback for: kaboom" in debug_text
    assert "RuntimeError: kaboom" in debug_text


def test_scan_error_goes_to_error_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    scan_logger = logging_setup.setup_scan_logger("configs/example.yaml")
    scan_logger.info("scan info")
    scan_logger.error("=== ALERT === scan fail")

    error_text = (log_root / "error.log").read_text(encoding="utf-8")
    scan_text = (log_root / "scan.log").read_text(encoding="utf-8")

    assert "=== ALERT === scan fail" in error_text
    assert "scan info" not in error_text
    assert "=== ALERT === scan fail" in scan_text


def test_line_rotating_handler_rotates_and_keeps_backups(tmp_path: Path) -> None:
    log_path = tmp_path / "rip.log"
    handler = logging_setup._LineRotatingFileHandler(
        log_path,
        max_lines=5,
        backup_count=3,
    )
    try:
        handler.setFormatter(logging.Formatter("%(message)s"))
        for i in range(12):
            record = logging.LogRecord(
                name="test",
                level=20,
                pathname=__file__,
                lineno=1,
                msg=f"line-{i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)
    finally:
        handler.close()

    assert log_path.is_file()
    current = log_path.read_text(encoding="utf-8").splitlines()
    assert len(current) == 2
    assert current == ["line-10", "line-11"]

    backup1 = Path(f"{log_path}.1").read_text(encoding="utf-8").splitlines()
    backup2 = Path(f"{log_path}.2").read_text(encoding="utf-8").splitlines()
    assert backup1 == ["line-5", "line-6", "line-7", "line-8", "line-9"]
    assert backup2 == ["line-0", "line-1", "line-2", "line-3", "line-4"]
    assert not Path(f"{log_path}.3").exists()


def test_line_rotating_handler_caps_backup_count(tmp_path: Path) -> None:
    log_path = tmp_path / "debug.log"
    handler = logging_setup._LineRotatingFileHandler(
        log_path,
        max_lines=2,
        backup_count=3,
    )
    try:
        handler.setFormatter(logging.Formatter("%(message)s"))
        for i in range(10):
            record = logging.LogRecord(
                name="test",
                level=20,
                pathname=__file__,
                lineno=1,
                msg=f"n{i}",
                args=(),
                exc_info=None,
            )
            handler.emit(record)
    finally:
        handler.close()

    assert Path(f"{log_path}.1").is_file()
    assert Path(f"{log_path}.2").is_file()
    assert Path(f"{log_path}.3").is_file()
    assert not Path(f"{log_path}.4").exists()
    assert log_path.read_text(encoding="utf-8").splitlines() == ["n8", "n9"]
    assert Path(f"{log_path}.1").read_text(encoding="utf-8").splitlines() == ["n6", "n7"]
    assert Path(f"{log_path}.2").read_text(encoding="utf-8").splitlines() == ["n4", "n5"]
    assert Path(f"{log_path}.3").read_text(encoding="utf-8").splitlines() == ["n2", "n3"]


def test_line_rotating_handler_counts_existing_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "scan.log"
    log_path.write_text("a\nb\nc\n", encoding="utf-8")
    handler = logging_setup._LineRotatingFileHandler(
        log_path,
        max_lines=4,
        backup_count=3,
    )
    try:
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test",
            level=20,
            pathname=__file__,
            lineno=1,
            msg="d",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        # Next write should rotate (4 lines already at max)
        record2 = logging.LogRecord(
            name="test",
            level=20,
            pathname=__file__,
            lineno=1,
            msg="e",
            args=(),
            exc_info=None,
        )
        handler.emit(record2)
    finally:
        handler.close()

    assert log_path.read_text(encoding="utf-8").splitlines() == ["e"]
    assert Path(f"{log_path}.1").read_text(encoding="utf-8").splitlines() == ["a", "b", "c", "d"]


def test_constants() -> None:
    assert logging_setup.MAX_LOG_LINES == 50_000
    assert logging_setup.BACKUP_COUNT == 2
    assert logging_setup.ERROR_LOG.name == "error.log"
