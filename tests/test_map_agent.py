"""Tests for ripknife.map_agent."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ripknife import map_agent, paths


def _patch_map_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    log_root = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(map_agent, "LOG_ROOT", log_root)
    monkeypatch.setattr(map_agent, "MAP_LOG", log_root / "map.log")
    monkeypatch.setattr(map_agent, "ROOT", tmp_path)
    monkeypatch.setattr(map_agent, "READY_DIR", tmp_path / "staging" / "ready")
    return log_root


def test_agent_output_log_path_uses_disc_id() -> None:
    assert map_agent.agent_output_log_path("example-show-s2p1") == (
        map_agent.LOG_ROOT / "map-example-show-s2p1.log"
    )


def test_background_run_calls_openrouter_not_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ripknife import llm

    log_root = _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "TEST", "titles": []}', encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError(f"subprocess started: {args!r} {kwargs!r}")

    monkeypatch.setattr(map_agent.subprocess, "Popen", boom)
    monkeypatch.setattr(map_agent.subprocess, "run", boom)
    captured: list[object] = []

    def opener(req, timeout=None):
        captured.append(req)
        body = json.dumps(
            {"choices": [{"message": {"content": '{"action":"stop","manifest":null}'}}]}
        ).encode()

        class _Resp:
            def read(self) -> bytes:
                return body

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        assert timeout == llm.HTTP_TIMEOUT_SEC
        return _Resp()

    rc = map_agent.run_map_agent(
        "test-disc",
        scan_json,
        background=True,
        env={llm.ENV_API_KEY: "test-openrouter-key"},
        opener=opener,
    )
    assert rc == 1
    assert len(captured) == 1
    assert captured[0].get_full_url() == llm.OPENROUTER_CHAT_URL
    sent = json.loads(captured[0].data.decode("utf-8"))
    assert sent["model"] == llm.DEFAULT_MODEL
    assert "--force" not in sent["messages"][0]["content"]
    assert not (tmp_path / "staging" / "ready" / "test-disc.yaml").exists()
    agent_log = log_root / "map-test-disc.log"
    assert "action=stop" in agent_log.read_text(encoding="utf-8")


def test_find_ready_yaml_accepts_identified_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    scan_json = tmp_path / "staging" / "pending" / "example-force-ww.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text(
        '{"disc_name": "EXAMPLE_FORCE_WW", "titles": []}', encoding="utf-8"
    )
    identified = ready / "example-force-1973.yaml"
    identified.write_text(
        "kind: movie\nmeta:\n  id: example-force-1973\n  disc_name: EXAMPLE_FORCE_WW\n",
        encoding="utf-8",
    )
    (ready / "unrelated-1971.yaml").write_text(
        "kind: movie\nmeta:\n  id: unrelated-1971\n  disc_name: OTHER_DISC\n",
        encoding="utf-8",
    )
    found = map_agent.find_ready_yaml("example-force-ww", scan_json=scan_json)
    assert found == identified


def test_find_ready_yaml_prefers_exact_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    exact = ready / "example-force-ww.yaml"
    exact.write_text("kind: movie\nmeta:\n  disc_name: EXAMPLE_FORCE_WW\n", encoding="utf-8")
    (ready / "example-force-1973.yaml").write_text(
        "kind: movie\nmeta:\n  disc_name: EXAMPLE_FORCE_WW\n", encoding="utf-8"
    )
    assert map_agent.find_ready_yaml("example-force-ww") == exact


def test_find_ready_yaml_accepts_claimed_identified_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rip-watcher may claim the identified-slug YAML before map monitor exits."""
    _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    scan_json = tmp_path / "staging" / "pending" / "logical-volume-id.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text(
        '{"disc_name": "LOGICAL_VOLUME_ID", "titles": []}', encoding="utf-8"
    )
    claimed = ready / "north-film-1987.yaml.claimed-sr0"
    claimed.write_text(
        "kind: movie\nmeta:\n  id: north-film-1987\n"
        "  disc_name: LOGICAL_VOLUME_ID\n",
        encoding="utf-8",
    )
    found = map_agent.find_ready_yaml("logical-volume-id", scan_json=scan_json)
    assert found == claimed


def test_find_ready_yaml_uses_agent_log_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    log_root.mkdir(parents=True, exist_ok=True)
    identified = ready / "example-film-1971.yaml"
    identified.write_text("kind: movie\nmeta:\n  id: example-film-1971\n", encoding="utf-8")
    (log_root / "map-example-film-ww.log").write_text(
        "AGENT_OUTPUT_BEGIN DISC_ID=example-film-ww (background)\n"
        "I wrote the manifest to `staging/ready/example-film-1971.yaml`\n",
        encoding="utf-8",
    )
    assert map_agent.find_ready_yaml("example-film-ww") == identified


def test_monitor_background_map_success_on_identified_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    scan_json = tmp_path / "staging" / "pending" / "example-force-ww.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "EXAMPLE_FORCE_WW"}', encoding="utf-8")
    (ready / "example-force-1973.yaml").write_text(
        "kind: movie\nmeta:\n  disc_name: EXAMPLE_FORCE_WW\n", encoding="utf-8"
    )
    mock = MagicMock()
    mock.returncode = 0
    monkeypatch.setattr(
        map_agent,
        "emit_map_alerts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no alert")),
    )
    map_agent._monitor_background_map(
        mock, "example-force-ww", logger=None, device=None, scan_json=scan_json
    )
    text = (log_root / "map.log").read_text(encoding="utf-8")
    assert "SUCCESS: background map wrote ready YAML for example-force-ww" in text
    assert "example-force-1973.yaml" in text


def test_monitor_background_map_success_on_claimed_identified_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    scan_json = tmp_path / "staging" / "pending" / "logical-volume-id.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "LOGICAL_VOLUME_ID"}', encoding="utf-8")
    (ready / "north-film-1987.yaml.claimed-sr0").write_text(
        "kind: movie\nmeta:\n  disc_name: LOGICAL_VOLUME_ID\n", encoding="utf-8"
    )
    mock = MagicMock()
    mock.returncode = 0
    monkeypatch.setattr(
        map_agent,
        "emit_map_alerts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no alert")),
    )
    map_agent._monitor_background_map(
        mock, "logical-volume-id", logger=None, device=None, scan_json=scan_json
    )
    text = (log_root / "map.log").read_text(encoding="utf-8")
    assert "SUCCESS: background map wrote ready YAML for logical-volume-id" in text
    assert "north-film-1987.yaml.claimed-sr0" in text


def test_run_map_agent_refuses_ambiguous_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "bdrom.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text(
        json.dumps(
            {
                "disc_name": "BDROM",
                "identification": {
                    "ambiguous": True,
                    "ambiguous_reason": (
                        "generic volume label; 2 films share ~6572s "
                        "(North Film (1987), South Film (2006))"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    alerts: list[tuple[str, str]] = []

    def _alert(disc_id: str, kind: str, **kwargs: object) -> list[str]:
        alerts.append((disc_id, kind))
        assert kwargs.get("device") == "dev:/dev/sr0"
        assert "South Film" in str(kwargs.get("detail"))
        return ["alert"]

    monkeypatch.setattr(map_agent, "emit_map_alerts", _alert)
    rc = map_agent.run_map_agent(
        "bdrom", scan_json, background=True, device="dev:/dev/sr0"
    )
    assert rc == map_agent.MAP_EXIT_AMBIGUOUS
    assert alerts == [("bdrom", map_agent.MAP_FAIL_AMBIGUOUS)]
    text = (log_root / "map.log").read_text(encoding="utf-8")
    assert "MAP_AMBIGUOUS DISC_ID=bdrom" in text
    assert not (tmp_path / "staging" / "ready").exists() or not list(
        (tmp_path / "staging" / "ready").glob("*.yaml")
    )


def test_user_yaml_still_wins_when_scan_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    (ready / "north-film-1987.yaml").write_text(
        "kind: movie\nmeta:\n  disc_name: BDROM\n", encoding="utf-8"
    )
    scan_json = tmp_path / "staging" / "pending" / "bdrom.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text(
        '{"disc_name": "BDROM", "identification": {"ambiguous": true}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        map_agent,
        "emit_map_alerts",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no alert")),
    )
    rc = map_agent.run_map_agent("bdrom", scan_json, background=False)
    assert rc == 0
    assert (ready / "north-film-1987.yaml").is_file()


def test_monitor_quarantines_guessed_yaml_on_map_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    log_root.mkdir(parents=True)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    scan_json = tmp_path / "staging" / "pending" / "bdrom.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "BDROM"}', encoding="utf-8")
    guessed = ready / "example-film-2006.yaml"
    guessed.write_text(
        "kind: movie\nmeta:\n  disc_name: BDROM\n", encoding="utf-8"
    )
    (log_root / "map-bdrom.log").write_text(
        "AGENT_OUTPUT_BEGIN DISC_ID=bdrom (background)\n"
        "MAP_AMBIGUOUS: North Film (1987), South Film (2006) at 6572s\n"
        "staging/ready/example-film-2006.yaml\n",
        encoding="utf-8",
    )
    alerts: list[str] = []
    monkeypatch.setattr(
        map_agent,
        "emit_map_alerts",
        lambda _disc, kind, **_k: alerts.append(kind) or ["alert"],
    )
    mock = MagicMock()
    mock.returncode = 0
    map_agent._monitor_background_map(
        mock, "bdrom", logger=None, device=None, scan_json=scan_json
    )
    assert not guessed.exists()
    quarantined = tmp_path / "staging" / "ambiguous" / "example-film-2006.yaml"
    assert quarantined.is_file()
    assert alerts == [map_agent.MAP_FAIL_AMBIGUOUS]
    text = (log_root / "map.log").read_text(encoding="utf-8")
    assert "SUCCESS" not in text
    assert "quarantined=" in text


def test_monitor_leaves_claimed_yaml_when_map_ambiguous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    log_root.mkdir(parents=True)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    scan_json = tmp_path / "staging" / "pending" / "logical-volume-id.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "LOGICAL_VOLUME_ID"}', encoding="utf-8")
    claimed = ready / "north-film-1987.yaml.claimed-sr0"
    claimed.write_text(
        "kind: movie\nmeta:\n  disc_name: LOGICAL_VOLUME_ID\n", encoding="utf-8"
    )
    (log_root / "map-logical-volume-id.log").write_text(
        "AGENT_OUTPUT_BEGIN\nMAP_AMBIGUOUS: runtime collision\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        map_agent, "emit_map_alerts", lambda *_a, **_k: ["alert"]
    )
    mock = MagicMock()
    mock.returncode = 0
    map_agent._monitor_background_map(
        mock, "logical-volume-id", logger=None, device=None, scan_json=scan_json
    )
    assert claimed.is_file()
    assert "SUCCESS" not in (log_root / "map.log").read_text(encoding="utf-8")
    assert "leave claimed manifest" in (log_root / "map.log").read_text(encoding="utf-8")


def test_run_map_agent_skips_when_identified_slug_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)
    (ready / "example-force-1973.yaml").write_text(
        "kind: movie\nmeta:\n  disc_name: EXAMPLE_FORCE_WW\n", encoding="utf-8"
    )
    scan_json = tmp_path / "staging" / "pending" / "example-force-ww.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "EXAMPLE_FORCE_WW", "titles": []}', encoding="utf-8")
    rc = map_agent.run_map_agent("example-force-ww", scan_json, background=False)
    assert rc == 0
    assert "SKIP: ready manifest already exists" in (log_root / "map.log").read_text(
        encoding="utf-8"
    )
    assert "example-force-1973.yaml" in (log_root / "map.log").read_text(encoding="utf-8")


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

    def boom(*args, **kwargs):
        launched["n"] += 1
        raise AssertionError("subprocess")

    monkeypatch.setattr(map_agent.subprocess, "run", boom)
    monkeypatch.setattr(map_agent.subprocess, "Popen", boom)

    rc = map_agent.run_map_agent("test-disc", scan_json, background=False)
    assert rc == 0
    assert launched["n"] == 0
    assert "SKIP: ready manifest already exists" in (log_root / "map.log").read_text(
        encoding="utf-8"
    )


def test_merge_repo_env_fills_missing_openrouter_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "# comment\nexport OPENROUTER_API_KEY='test-openrouter-key'\nALREADY=from-file\n",
        encoding="utf-8",
    )
    merged = map_agent.merge_repo_env({"ALREADY": "from-parent", "HOME": "/home/user"})
    assert merged["OPENROUTER_API_KEY"] == "test-openrouter-key"
    assert merged["ALREADY"] == "from-parent"
    assert merged["HOME"] == "/home/user"


def test_map_reads_openrouter_key_from_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ripknife import llm

    _patch_map_paths(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key\n", encoding="utf-8")
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "TEST", "titles": []}', encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    captured: list[object] = []

    def opener(req, timeout=None):
        captured.append(req)
        body = json.dumps(
            {"choices": [{"message": {"content": '{"action":"stop","manifest":null}'}}]}
        ).encode()

        class _Resp:
            def read(self) -> bytes:
                return body

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        return _Resp()

    map_agent.run_map_agent("test-disc", scan_json, background=True, env={}, opener=opener)
    assert captured[0].get_header("Authorization") == "Bearer test-openrouter-key"
    assert llm.ENV_API_KEY not in captured[0].data.decode("utf-8")


def test_build_map_prompt_routes_cartoons_to_anime(tmp_path: Path) -> None:
    scan_json = tmp_path / "foo.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = map_agent.build_map_prompt("foo", scan_json)
    assert "cartoon OR animation" in prompt
    assert "nas.share: /path/to/library/Anime/<Show>" in prompt
    assert "Example Cartoon → /path/to/library/Anime/Example Cartoon  (NOT Shows)" in prompt
    assert "Animated movies / cartoon features → Anime/Movies" in prompt
    assert "nas.share: /path/to/library/Anime/Movies" in prompt
    assert "Live-action movies → /path/to/library/Movies" in prompt
    assert "Japanese anime: keep_all_tracks true, audio_default jpn" in prompt


def test_build_map_prompt_shorts_compilations_use_volume_seasons(
    tmp_path: Path,
) -> None:
    scan_json = tmp_path / "foo.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = map_agent.build_map_prompt("foo", scan_json)
    assert "Theatrical shorts compilations" in prompt
    assert "unique show name" in prompt
    assert "plex.local: true" in prompt
    assert "Season N = collection volume N" in prompt
    assert "Vol 1 D1 = 01–25, D2 = 26–50" in prompt
    assert "meta.min_episode_sec: 360" in prompt
    assert 'title: "Zip, Zap"' in prompt


def test_build_map_prompt_warns_title_index_not_episode_order(tmp_path: Path) -> None:
    scan_json = tmp_path / "foo.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = map_agent.build_map_prompt("foo", scan_json)
    assert "title index is NOT episode order" in prompt
    assert "Do not assign episode 09 because the title is t9" in prompt
    assert "playlist/title name, duration, chapter cards" in prompt
    assert "TheTVDB DVD order" in prompt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Authentication required. Please set a key",
            map_agent.MAP_FAIL_AUTH,
        ),
        (
            "Error: Authentication required.",
            map_agent.MAP_FAIL_AUTH,
        ),
        ("401 Unauthorized", map_agent.MAP_FAIL_AUTH),
        ("unauthenticated: invalid api key", map_agent.MAP_FAIL_AUTH),
        ("Error: resource_exhausted", map_agent.MAP_FAIL_RESOURCE),
        ("RESOURCE_EXHAUSTED: quota exceeded", map_agent.MAP_FAIL_RESOURCE),
        ("rate limited by provider", map_agent.MAP_FAIL_RESOURCE),
        ("helper not found", None),
        ("bash: tool: command not found", None),
        ("Wrote staging/ready/foo.yaml", None),
        ("", None),
        ("   \n", None),
        (
            "MAP_AMBIGUOUS: North Film (1987), South Film (2006) at 6572s",
            map_agent.MAP_FAIL_AMBIGUOUS,
        ),
        ("The reply must be MAP_AMBIGUOUS: when titles collide", None),
    ],
)
def test_classify_map_failure_detects_agent_errors(text: str, expected: str | None) -> None:
    assert map_agent.classify_map_failure(text) == expected


def test_map_alert_code_auth_vs_failed() -> None:
    assert map_agent.map_alert_code(map_agent.MAP_FAIL_AUTH) == "MAP_AGENT_AUTH"
    assert map_agent.map_alert_code(map_agent.MAP_FAIL_RESOURCE) == "MAP_FAILED"
    assert map_agent.map_alert_code(map_agent.MAP_FAIL_NOT_FOUND) == "MAP_FAILED"
    assert map_agent.map_alert_code(map_agent.MAP_FAIL_OTHER) == "MAP_FAILED"
    assert map_agent.map_alert_code(map_agent.MAP_FAIL_AMBIGUOUS) == "MAP_AMBIGUOUS"


def test_series_prefix_strips_season_disc() -> None:
    assert map_agent.series_prefix("example-show-s4-d1") == "example-show"
    assert map_agent.series_prefix("example-show-s3-d1") == "example-show"
    assert map_agent.series_prefix("example-movie-1983") == "example-movie-1983"


def test_find_similar_series_manifests_does_not_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    done = tmp_path / "staging" / "done"
    done.mkdir(parents=True)
    prior = done / "example-show-s3-d1.yaml"
    prior.write_text("kind: tv_disc\n", encoding="utf-8")
    ready = tmp_path / "staging" / "ready"
    ready.mkdir(parents=True)

    hits = map_agent.find_similar_series_manifests(
        "example-show-s4-d1",
        search_roots=[tmp_path / "configs", ready, done],
    )
    assert prior in hits
    assert not (ready / "example-show-s4-d1.yaml").exists()
    assert list(ready.glob("*.yaml")) == []


def test_format_map_alert_mentions_recovery_and_similar() -> None:
    lines = map_agent.format_map_alert_lines(
        "example-show-s4-d1",
        map_agent.MAP_FAIL_AUTH,
        similar=[Path("staging/done/example-show-s3-d1.yaml")],
    )
    assert lines[0].startswith("MAP_AGENT_AUTH —")
    assert "OPENROUTER_API_KEY" in lines[1]
    assert "hand-write" in lines[1]
    assert "not copied" in lines[2]
    assert "example-show-s3-d1" in lines[2]


def test_supervise_auth_fail_retries_once_then_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text('{"disc_name": "TEST", "titles": []}', encoding="utf-8")
    launches = {"n": 0}

    def dead_proc() -> MagicMock:
        mock = MagicMock()
        mock.poll.return_value = 1
        mock.returncode = 1
        return mock

    def fake_run(*_a, **_k):
        launches["n"] += 1
        path = log_root / "map-test-disc.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "AGENT_OUTPUT_BEGIN DISC_ID=test-disc (background)\n"
            "Authentication required\n",
            encoding="utf-8",
        )
        return dead_proc()

    monkeypatch.setattr(map_agent, "run_map_agent", fake_run)
    alerts: list[str] = []
    monkeypatch.setattr(
        map_agent,
        "emit_map_alerts",
        lambda disc_id, kind, **_k: alerts.append(f"{disc_id}:{kind}") or ["alert"],
    )

    ok, message, kind = map_agent.supervise_background_map(
        "test-disc", scan_json, quick_fail_sec=0.05
    )
    assert ok is False
    assert kind == map_agent.MAP_FAIL_AUTH
    assert launches["n"] == 2
    assert alerts == ["test-disc:auth"]
    assert "MAP_AGENT_AUTH" in message or "auth" in message.lower()
    assert not (tmp_path / "staging" / "ready" / "test-disc.yaml").exists()


def test_supervise_still_running_does_not_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text("{}", encoding="utf-8")

    mock = MagicMock()
    mock.poll.return_value = None
    monkeypatch.setattr(map_agent, "run_map_agent", lambda *_a, **_k: mock)
    monkeypatch.setattr(map_agent, "emit_map_alerts", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no alert")))
    started: list[str] = []
    monkeypatch.setattr(
        map_agent.threading,
        "Thread",
        lambda **kw: type("T", (), {"start": lambda self: started.append("ok")})(),
    )

    ok, message, kind = map_agent.supervise_background_map(
        "test-disc", scan_json, quick_fail_sec=0.01
    )
    assert ok is True
    assert kind is None
    assert message == "background map still running"
    assert started == ["ok"]


def test_supervise_resource_exhausted_alerts_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text("{}", encoding="utf-8")
    launches = {"n": 0}

    def fake_run(*_a, **_k):
        launches["n"] += 1
        path = log_root / "map-test-disc.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "AGENT_OUTPUT_BEGIN DISC_ID=test-disc (background)\nresource_exhausted\n",
            encoding="utf-8",
        )
        mock = MagicMock()
        mock.poll.return_value = 1
        mock.returncode = 1
        return mock

    monkeypatch.setattr(map_agent, "run_map_agent", fake_run)
    kinds: list[str] = []
    monkeypatch.setattr(
        map_agent, "emit_map_alerts", lambda _d, kind, **_k: kinds.append(kind) or ["x"]
    )

    ok, _message, kind = map_agent.supervise_background_map(
        "test-disc", scan_json, quick_fail_sec=0.01
    )
    assert ok is False
    assert kind == map_agent.MAP_FAIL_RESOURCE
    assert launches["n"] == 1
    assert kinds == [map_agent.MAP_FAIL_RESOURCE]


def test_supervise_missing_scan_alerts_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_map_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / "test-disc.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan_json.write_text("{}", encoding="utf-8")
    launches = {"n": 0}

    def boom(*_a, **_k):
        launches["n"] += 1
        raise FileNotFoundError("scan JSON not found")

    monkeypatch.setattr(map_agent, "run_map_agent", boom)
    kinds: list[str] = []
    monkeypatch.setattr(
        map_agent, "emit_map_alerts", lambda _d, kind, **_k: kinds.append(kind) or ["x"]
    )

    ok, _message, kind = map_agent.supervise_background_map(
        "test-disc", scan_json, quick_fail_sec=0.01
    )
    assert ok is False
    assert kind == map_agent.MAP_FAIL_OTHER
    assert launches["n"] == 1
    assert kinds == [map_agent.MAP_FAIL_OTHER]


def test_emit_map_alerts_sets_watcher_last_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ripknife import watcher_util

    _patch_map_paths(monkeypatch, tmp_path)
    state_path = tmp_path / "watcher-state.json"
    monkeypatch.setattr(watcher_util, "STATE_PATH", state_path)
    monkeypatch.setattr(map_agent, "find_similar_series_manifests", lambda *_a, **_k: [])

    from ripknife import logging_setup

    monkeypatch.setattr(logging_setup, "setup_pipeline_logger", lambda: logging.getLogger("pipe"))

    lines = map_agent.emit_map_alerts(
        "foo-s1-d1",
        map_agent.MAP_FAIL_AUTH,
        device="dev:/dev/sr0",
    )
    assert any("=== ALERT === MAP_AGENT_AUTH" in line for line in lines)
    section = watcher_util.read_state()["disc_watcher:sr0"]
    assert section["status"] == "map_agent_auth"
    assert section["last_error"].startswith("MAP_AGENT_AUTH")
    assert section["map_failure"] == "auth"
