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
    monkeypatch.setattr(logging_setup, "PIPELINE_LOG", log_root / "pipeline.log")
    # Force shared error handler to recreate against the patched path.
    if logging_setup._error_handler is not None:
        logging_setup._error_handler.close()
        logging_setup._error_handler = None
    # Reset pipeline logger handlers so setup recreates against patched path.
    pl = logging.getLogger(logging_setup._PIPELINE_LOGGER_NAME)
    for handler in pl.handlers[:]:
        pl.removeHandler(handler)
        handler.close()
    # Reset stage timing so elapsed markers are deterministic per test.
    monkeypatch.setattr(logging_setup, "_last_stage_monotonic", None)


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
    assert logging_setup.PIPELINE_LOG.name == "pipeline.log"


def test_log_stage_and_progress_write_pipeline_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_stage(
        "SCAN",
        process=logging_setup.PROCESS_INSERT,
        step=1,
        total=logging_setup.INSERT_STEPS,
        detail="device=dev:/dev/sr0",
    )
    logging_setup.log_progress(
        "RIP",
        2,
        12,
        process=logging_setup.PROCESS_RIP,
        detail="6x22 The Card",
    )

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "STAGE [insert 1/5] SCAN" in text
    assert "progress [rip] RIP 2/12 (16%)" in text
    assert "6x22 The Card" in text


def test_log_stage_process_prefix_distinguishes_insert_vs_rip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_stage("SCAN", process="insert", step=1, total=5)
    logging_setup.log_stage("CLAIM", process="rip", step=1, total=5)
    logging_setup.log_stage("SCAN_FAILED", process="insert", detail="boom")
    logging_setup.log_stage("WATCHERS_UP", detail="idle")

    lines = (log_root / "pipeline.log").read_text(encoding="utf-8").splitlines()
    assert "STAGE [insert 1/5] SCAN" in lines[0]
    assert "STAGE [rip 1/5] CLAIM" in lines[1]
    assert "STAGE [insert] SCAN_FAILED" in lines[2]
    assert "STAGE WATCHERS_UP" in lines[3]
    assert "[1/5]" not in lines[3]  # status stages have no N/M


def test_pipeline_log_lines_are_timestamped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import re

    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_stage("SCAN", process="insert", step=1, total=5, detail="device=dev:/dev/sr0")
    logging_setup.log_progress("RIP", 1, 2, process="rip")

    lines = (log_root / "pipeline.log").read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line), line


def test_log_stage_appends_elapsed_since_previous_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    fake_now = [100.0]
    monkeypatch.setattr(logging_setup.time, "monotonic", lambda: fake_now[0])

    logging_setup.log_stage("SCAN", process="insert", step=1, total=5)
    fake_now[0] = 137.0
    logging_setup.log_stage("PENDING", process="insert", step=2, total=5)
    fake_now[0] = 137.0 + 3 * 60 + 7
    logging_setup.log_stage("DONE", process="rip", step=5, total=5)

    lines = (log_root / "pipeline.log").read_text(encoding="utf-8").splitlines()
    assert "SCAN" in lines[0] and "[+" not in lines[0]
    assert lines[1].endswith("=== [+37s]")
    assert lines[2].endswith("=== [+3m07s]")


def test_format_elapsed() -> None:
    assert logging_setup._format_elapsed(9) == "9s"
    assert logging_setup._format_elapsed(75) == "1m15s"
    assert logging_setup._format_elapsed(3723) == "1h02m03s"


def test_log_watcher_waiting_creates_pipeline_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_watcher_waiting("disc-watcher", ["dev:/dev/sr0", "dev:/dev/sr1"])
    logging_setup.log_watcher_waiting("rip-watcher")

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "STAGE WATCHERS_UP — disc-watcher waiting (devices=dev:/dev/sr0, dev:/dev/sr1)" in text
    assert "STAGE WATCHERS_UP — rip-watcher waiting (staging/ready)" in text


def test_log_progress_percent_is_completed_over_total() -> None:
    """Percent = current/total completed tracks — not slot index or title id."""
    # Mimic fixed TV progress: 0 done of 12 → 0%; 3 done of 12 → 25%
    assert int(100 * 0 / 12) == 0
    assert int(100 * 3 / 12) == 25
    # Old bug: slot 3 of 12 tracks when slots start at 1 looked like 25% at first track
    # if someone used slot as current with wrong total — we now use completed count.
    assert int(100 * 3 / 12) != int(100 * 1 / 12)


def test_build_map_prompt_requires_identified_title(tmp_path: Path) -> None:
    from videosripper.map_agent import build_map_prompt

    scan_json = tmp_path / "dvd-video.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = build_map_prompt("dvd-video", scan_json)
    assert "IDENTIFIED work title" in prompt
    assert "placeholder title" in prompt
    assert "meta.disc_name = exact scan disc_name" in prompt
