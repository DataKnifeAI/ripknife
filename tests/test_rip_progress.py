"""rip_progress watcher-state helpers and play-all overall percent."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife import makemkv, watcher_util
from ripknife.makemkv import playall_overall_pct


def test_playall_overall_pct_weights_makemkv() -> None:
    assert playall_overall_pct(makemkv_pct=0, extracts_total=5) == 0
    assert playall_overall_pct(makemkv_pct=50, extracts_total=5) == 45
    assert playall_overall_pct(makemkv_pct=100, extracts_done=0, extracts_total=5) == 90
    assert playall_overall_pct(
        makemkv_pct=100, extracts_done=5, extracts_total=5, extract_pct=0
    ) == 100
    assert playall_overall_pct(
        makemkv_pct=100, extracts_done=0, extracts_total=5, extract_pct=50
    ) == 91


def test_clear_rip_progress_removes_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    watcher_util.update_state("rip_progress", {"title": 0, "pct": 100, "eta_sec": 0})
    assert "rip_progress" in watcher_util.read_state()
    makemkv.clear_rip_progress()
    assert "rip_progress" not in watcher_util.read_state()


def test_write_rip_progress_then_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    makemkv.write_rip_progress(0, 40, start=0.0)
    rp = watcher_util.read_state()["rip_progress"]
    assert rp["pct"] == 40
    assert rp["title"] == 0
    makemkv.clear_rip_progress()
    assert watcher_util.read_state().get("rip_progress") is None
