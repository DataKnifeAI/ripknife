"""Unit tests for scripts/disc-watcher state updates."""

from __future__ import annotations

import importlib.util
import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from ripknife import watcher_util

ROOT = Path(__file__).resolve().parents[1]


def _load_disc_watcher():
    path = ROOT / "scripts" / "disc-watcher.py"
    spec = importlib.util.spec_from_file_location("disc_watcher", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["disc_watcher"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_handle_disc_drops_stale_ready_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    watcher_util.update_state(
        "disc_watcher:sr0",
        {
            "status": "ready_exists",
            "ready_yaml": "/old/previous.yaml",
            "disc_id": "old-disc",
        },
    )
    mod = _load_disc_watcher()

    class Result:
        ok = True
        message = "mapping_triggered"
        data = {"disc_id": "new-disc", "disc_name": "NEW_DISC"}

    monkeypatch.setattr(mod, "handle_disc_insert", lambda *a, **k: Result())
    monkeypatch.setattr(mod, "flock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(mod, "scan_lock_path", lambda _device: tmp_path / "scan.lock")

    mod.handle_disc("dev:/dev/sr0", SimpleNamespace(minlength=300), logging.getLogger("test"))
    section = watcher_util.read_state()["disc_watcher:sr0"]
    assert section["disc_id"] == "new-disc"
    assert "ready_yaml" not in section
    assert section["status"] == "mapping_triggered"


def test_handle_disc_records_map_auth_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    mod = _load_disc_watcher()

    class Result:
        ok = False
        message = "map_agent_auth"
        data = {
            "disc_id": "example-show-s4-d1",
            "disc_name": "EXAMPLE_SHOW_S4_D1",
            "last_error": "MAP_AGENT_AUTH — OpenRouter key missing or rejected for disc example-show-s4-d1",
            "map_failure": "auth",
        }

    monkeypatch.setattr(mod, "handle_disc_insert", lambda *a, **k: Result())
    monkeypatch.setattr(mod, "flock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(mod, "scan_lock_path", lambda _device: tmp_path / "scan.lock")

    mod.handle_disc("dev:/dev/sr0", SimpleNamespace(minlength=300), logging.getLogger("test"))
    section = watcher_util.read_state()["disc_watcher:sr0"]
    assert section["status"] == "map_agent_auth"
    assert section["last_error"].startswith("MAP_AGENT_AUTH")
    assert section["map_failure"] == "auth"
    assert section["disc_id"] == "example-show-s4-d1"
