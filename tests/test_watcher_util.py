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
