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


def test_scan_lock_path_matches_makemkv_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(watcher_util, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(watcher_util, "_resolved_lock_paths", {})
    monkeypatch.setattr(watcher_util.os, "access", lambda _path, _mode: False)
    assert watcher_util.scan_lock_path() == watcher_util.makemkv_lock_path()
