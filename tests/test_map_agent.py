"""Tests for videosripper.map_agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from videosripper import map_agent, paths


def _patch_map_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    log_root = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(map_agent, "LOG_ROOT", log_root)
    monkeypatch.setattr(map_agent, "MAP_LOG", log_root / "map.log")
    monkeypatch.setattr(map_agent, "ROOT", tmp_path)
    monkeypatch.setattr(map_agent, "WORKFLOW", tmp_path / "LLM-RIP-WORKFLOW.md")
    monkeypatch.setattr(map_agent, "READY_DIR", tmp_path / "staging" / "ready")
    (tmp_path / "LLM-RIP-WORKFLOW.md").write_text("# workflow\n", encoding="utf-8")
    return log_root


def test_agent_output_log_path_uses_disc_id() -> None:
    assert map_agent.agent_output_log_path("jujutsukaisen-s2p1") == (
        map_agent.LOG_ROOT / "map-jujutsukaisen-s2p1.log"
    )


def test_background_run_streams_to_per_disc_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "TEST", "titles": []}', encoding="utf-8")

    fake_agent = tmp_path / "agent"
    fake_agent.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_agent.chmod(0o755)
    monkeypatch.setattr(map_agent, "resolve_agent", lambda: fake_agent)

    popen_calls: list[dict] = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(kwargs)
        handle = kwargs["stdout"]
        handle.write("agent stdout line\n")
        handle.flush()
        mock = MagicMock()
        mock.pid = 4242
        return mock

    monkeypatch.setattr(map_agent.subprocess, "Popen", fake_popen)

    proc = map_agent.run_map_agent("test-disc", scan_json, background=True)
    assert proc.pid == 4242
    assert len(popen_calls) == 1
    assert popen_calls[0]["stderr"] is map_agent.subprocess.STDOUT
    assert popen_calls[0]["start_new_session"] is True

    agent_log = log_root / "map-test-disc.log"
    assert agent_log.is_file()
    assert "AGENT_OUTPUT_BEGIN DISC_ID=test-disc (background)" in agent_log.read_text(
        encoding="utf-8"
    )
    assert "agent stdout line" in agent_log.read_text(encoding="utf-8")


def test_run_map_agent_skips_when_ready_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    (ready / "test-disc.yaml").write_text("kind: movie\n", encoding="utf-8")

    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "TEST", "titles": []}', encoding="utf-8")

    launched = {"n": 0}
    monkeypatch.setattr(map_agent, "resolve_agent", lambda: (_ for _ in ()).throw(AssertionError("no agent")))

    def fake_run(*args, **kwargs):
        launched["n"] += 1
        return 0

    monkeypatch.setattr(map_agent.subprocess, "run", fake_run)
    monkeypatch.setattr(map_agent.subprocess, "Popen", lambda *a, **k: MagicMock())

    rc = map_agent.run_map_agent("test-disc", scan_json, background=False)
    assert rc == 0
    assert launched["n"] == 0
    assert "SKIP: ready manifest already exists" in (log_root / "map.log").read_text(encoding="utf-8")
