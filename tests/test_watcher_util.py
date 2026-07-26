"""Unit tests for videosripper.watcher_util."""

from __future__ import annotations

from pathlib import Path

import pytest

from videosripper import watcher_util


def test_makemkv_lock_path_uses_logs_when_var_lock_not_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(watcher_util, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(watcher_util, "_resolved_lock_paths", {})
    monkeypatch.setattr(watcher_util.os, "access", lambda _path, _mode: False)
    assert watcher_util.makemkv_lock_path() == tmp_path / "logs" / "makemkv.lock"


def test_makemkv_lock_path_uses_var_lock_when_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watcher_util, "_resolved_lock_paths", {})
    monkeypatch.setattr(watcher_util.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        watcher_util.os,
        "open",
        lambda path, flags, mode=0o644: 99,
    )
    monkeypatch.setattr(watcher_util.os, "close", lambda _fd: None)
    assert watcher_util.makemkv_lock_path() == Path(
        "/var/lock/videosripper-makemkv.lock"
    )


def test_makemkv_lock_path_falls_back_when_var_lock_open_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system_lock = Path("/var/lock/videosripper-makemkv.lock")

    def fake_open(path, flags, mode=0o644):
        if str(path) == str(system_lock):
            raise PermissionError(13, "Permission denied", str(system_lock))
        return 99

    monkeypatch.setattr(watcher_util, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(watcher_util, "_resolved_lock_paths", {})
    monkeypatch.setattr(watcher_util.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(watcher_util.os, "open", fake_open)
    monkeypatch.setattr(watcher_util.os, "close", lambda _fd: None)
    assert watcher_util.makemkv_lock_path() == tmp_path / "logs" / "makemkv.lock"


def test_flock_falls_back_when_system_lock_open_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    system_lock = Path("/var/lock/videosripper-makemkv.lock")
    fallback = tmp_path / "logs" / "makemkv.lock"
    opened: list[str] = []

    def fake_open(path, flags, mode=0o644):
        opened.append(str(path))
        if str(path) == str(system_lock):
            raise PermissionError(13, "Permission denied", str(system_lock))
        return 42

    monkeypatch.setattr(watcher_util, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(watcher_util.os, "open", fake_open)
    monkeypatch.setattr(watcher_util.os, "close", lambda _fd: None)
    monkeypatch.setattr(watcher_util.fcntl, "flock", lambda _fd, _flags: None)

    with watcher_util.flock(system_lock):
        pass

    assert opened == [str(system_lock), str(fallback)]


def test_flock_is_reentrant_within_same_thread(tmp_path: Path) -> None:
    """Nested flock on the same path must not deadlock the process against itself.

    Regression: disc-watcher held the scan lock (== makemkv lock) and then
    step_scan_disc re-acquired the same lock file via a new fd, blocking forever.
    """
    import threading

    lock_path = tmp_path / "makemkv-sr0.lock"
    done = threading.Event()

    def nested() -> None:
        with watcher_util.flock(lock_path, blocking=False):
            with watcher_util.flock(lock_path, blocking=True):
                done.set()

    worker = threading.Thread(target=nested, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert done.is_set(), "nested flock deadlocked"

    # Lock must be fully released afterwards: a fresh non-blocking acquire works.
    with watcher_util.flock(lock_path, blocking=False):
        pass


def test_flock_released_after_nested_exit(tmp_path: Path) -> None:
    """Inner reentrant exit must not release the outer hold prematurely."""
    import fcntl
    import os

    lock_path = tmp_path / "makemkv-sr0.lock"
    with watcher_util.flock(lock_path):
        with watcher_util.flock(lock_path):
            pass
        # Outer context still holds the lock: an independent fd cannot acquire it.
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_scan_lock_path_matches_makemkv_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(watcher_util, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(watcher_util, "_resolved_lock_paths", {})
    monkeypatch.setattr(watcher_util.os, "access", lambda _path, _mode: False)
    assert watcher_util.scan_lock_path() == watcher_util.makemkv_lock_path()


def test_makemkv_lock_path_per_device_is_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(watcher_util, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(watcher_util, "_resolved_lock_paths", {})
    monkeypatch.setattr(watcher_util.os, "access", lambda _path, _mode: False)
    global_lock = watcher_util.makemkv_lock_path()
    sr0 = watcher_util.makemkv_lock_path("dev:/dev/sr0")
    sr1 = watcher_util.makemkv_lock_path("dev:/dev/sr1")
    assert global_lock.name == "makemkv.lock"
    assert sr0.name == "makemkv-sr0.lock"
    assert sr1.name == "makemkv-sr1.lock"
    assert sr0 != sr1 != global_lock
    assert watcher_util.scan_lock_path("dev:/dev/sr1") == sr1


def test_scan_device_dict_returns_large_scan_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from videosripper.makemkv import ScanResult, TitleInfo

    titles = {
        i: TitleInfo(
            index=i,
            duration="2:01:00",
            duration_sec=7260,
            size_bytes=30_000_000_000 + i,
            name=f"Title {i}",
        )
        for i in range(330)
    }

    def fake_scan(device: str, minlength: int = 300, *, timeout_sec: int | None = None) -> ScanResult:
        assert device == "dev:/dev/sr0"
        assert minlength == 300
        return ScanResult(raw="", disc_name="John Wick", titles=titles)

    monkeypatch.setattr("videosripper.makemkv.scan", fake_scan)

    data = watcher_util.scan_device_dict("dev:/dev/sr0", 300)
    assert data["disc_name"] == "John Wick"
    assert len(data["titles"]) == 330
    assert data["titles"][0]["index"] == 0
    assert data["titles"][-1]["index"] == 329


def test_touch_heartbeat_writes_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_util.os, "getpid", lambda: 4242)

    watcher_util.touch_heartbeat("disc-watcher", extra={"disc_present": True})

    state = watcher_util.read_state()
    hb = state["heartbeat:disc-watcher"]
    assert hb["status"] == "alive"
    assert hb["pid"] == 4242
    assert hb["disc_present"] is True
    assert "updated_at" in hb


def test_log_watcher_crash_writes_pipeline_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import logging

    from videosripper import logging_setup, paths

    log_root = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "PIPELINE_LOG", log_root / "pipeline.log")
    monkeypatch.setattr(logging_setup, "ERROR_LOG", log_root / "error.log")
    monkeypatch.setattr(watcher_util, "ERROR_LOG", log_root / "error.log")
    if logging_setup._error_handler is not None:
        logging_setup._error_handler.close()
        logging_setup._error_handler = None
    pl = logging.getLogger(logging_setup._PIPELINE_LOGGER_NAME)
    for handler in pl.handlers[:]:
        pl.removeHandler(handler)
        handler.close()
    monkeypatch.setattr(logging_setup, "_last_stage_monotonic", None)

    logger = logging.getLogger("test-watcher-crash")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.ERROR)

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        watcher_util.log_watcher_crash("disc-watcher:sr0", logger, exc)

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "STAGE WATCHER_CRASH" in text
    assert "disc-watcher:sr0" in text
    assert "RuntimeError: boom" in text
    err = (log_root / "error.log").read_text(encoding="utf-8")
    assert "WATCHER_CRASH — disc-watcher:sr0" in err
    assert "RuntimeError: boom" in err


def test_run_resilient_loop_restarts_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    calls = {"n": 0}
    logged: list[str] = []

    def fake_crash(name, logger, exc):
        logged.append(f"{name}:{type(exc).__name__}")

    monkeypatch.setattr(watcher_util, "log_watcher_crash", fake_crash)
    monkeypatch.setattr(watcher_util.time, "sleep", lambda _s: None)
    watcher_util.clear_shutdown()

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    logger = logging.getLogger("test-resilient")
    assert watcher_util.run_resilient_loop("worker", logger, flaky, restart_sec=0) == "ok"
    assert calls["n"] == 3
    assert logged == ["worker:ValueError", "worker:ValueError"]


def test_install_watcher_signal_handlers_logs_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import logging
    import signal

    from videosripper import logging_setup, paths

    log_root = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "PIPELINE_LOG", log_root / "pipeline.log")
    pl = logging.getLogger(logging_setup._PIPELINE_LOGGER_NAME)
    for handler in pl.handlers[:]:
        pl.removeHandler(handler)
        handler.close()
    monkeypatch.setattr(logging_setup, "_last_stage_monotonic", None)
    watcher_util.clear_shutdown()

    logger = logging.getLogger("test-sig")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())

    # Save and restore previous handlers so other tests / pytest aren't affected.
    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    try:
        watcher_util.install_watcher_signal_handlers("disc-watcher", logger)
        with pytest.raises(SystemExit) as ei:
            signal.raise_signal(signal.SIGTERM)
        assert ei.value.code == 0
        assert watcher_util.shutdown_requested()
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        watcher_util.clear_shutdown()

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "STAGE WATCHER_STOPPED" in text
    assert "disc-watcher received SIGTERM" in text


def test_touch_heartbeat_includes_heartbeat_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    watcher_util.touch_heartbeat("rip-watcher")
    hb = watcher_util.read_state()["heartbeat:rip-watcher"]
    assert "heartbeat_at" in hb
    assert "T" in hb["heartbeat_at"]


def test_concurrent_update_state_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two writers must not crash on shared ``.json.tmp`` replace races."""
    import threading

    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        try:
            for i in range(40):
                watcher_util.update_state(name, {"n": i})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("a",)),
        threading.Thread(target=writer, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert errors == []
    state = watcher_util.read_state()
    assert "a" in state and "b" in state
