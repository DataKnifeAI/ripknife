"""Tests for make watch-status helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from videosripper import watcher_status, watcher_util


def test_collect_watch_status_reports_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "_pgrep_watcher", lambda _script: [])
    monkeypatch.setattr(watcher_status, "media_present", lambda _device="dev:/dev/sr0": False)

    watcher_util.touch_heartbeat("disc-watcher", extra={"status": "alive"})
    info = watcher_status.collect_watch_status()
    assert info["disc_present"] is False
    assert info["watchers"]["disc-watcher"]["running"] is False
    assert info["watchers"]["rip-watcher"]["running"] is False
    assert info["watchers"]["disc-watcher"]["heartbeat_at"]


def test_collect_watch_status_reports_pids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "STATE_PATH", state_path)

    def fake_pgrep(script: str) -> list[int]:
        if "disc-watcher" in script:
            return [111]
        if "rip-watcher" in script:
            return [222]
        return []

    monkeypatch.setattr(watcher_status, "_pgrep_watcher", fake_pgrep)
    monkeypatch.setattr(watcher_status, "media_present", lambda _device="dev:/dev/sr0": True)

    info = watcher_status.collect_watch_status()
    assert info["disc_present"] is True
    assert info["watchers"]["disc-watcher"]["pids"] == [111]
    assert info["watchers"]["rip-watcher"]["pids"] == [222]


def test_collect_watch_status_surfaces_rip_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "_pgrep_watcher", lambda _script: [])
    monkeypatch.setattr(watcher_status, "media_present", lambda _device="dev:/dev/sr0": True)

    watcher_util.update_state("rip_progress", {"title": 0, "pct": 40, "eta_sec": 125})
    info = watcher_status.collect_watch_status()
    assert info["rip_progress"]["pct"] == 40
    assert info["rip_progress"]["title"] == 0
    assert info["rip_progress"]["eta_sec"] == 125
    assert info["rip_progress"]["age_sec"] is not None


def test_collect_watch_status_no_rip_progress_when_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "_pgrep_watcher", lambda _script: [])
    monkeypatch.setattr(watcher_status, "media_present", lambda _device="dev:/dev/sr0": False)

    info = watcher_status.collect_watch_status()
    assert info["rip_progress"] is None


def test_print_watch_status_includes_formatted_eta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "_pgrep_watcher", lambda _script: [])
    monkeypatch.setattr(watcher_status, "media_present", lambda _device="dev:/dev/sr0": True)

    watcher_util.update_state("rip_progress", {"title": 0, "pct": 40, "eta_sec": 840})
    watcher_status.print_watch_status()
    out = capsys.readouterr().out
    assert "rip:    title 0 — 40% (MakeMKV, ~14m left)" in out


def test_print_watch_status_omits_eta_when_unstable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "STATE_PATH", state_path)
    monkeypatch.setattr(watcher_status, "_pgrep_watcher", lambda _script: [])
    monkeypatch.setattr(watcher_status, "media_present", lambda _device="dev:/dev/sr0": True)

    watcher_util.update_state("rip_progress", {"title": 0, "pct": 5, "eta_sec": None})
    watcher_status.print_watch_status()
    out = capsys.readouterr().out
    assert "rip:    title 0 — 5% (MakeMKV)" in out
    assert "left" not in out
