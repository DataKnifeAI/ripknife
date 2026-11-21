"""Offline map plus the short OpenRouter decisions. No network and no rip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripknife import llm, map_agent, paths
from ripknife.models import MovieManifest, load_manifest
from ripknife.pipeline import step_verify_disc_match, step_verify_titles

FIXTURE = Path(__file__).parent / "fixtures" / "harbor-lantern.scan.json"
DISC_ID = "harbor-lantern"
DISC_NAME = "HARBOR_LANTERN"
FEATURE_INDEX = 1


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _chat(content: str) -> bytes:
    payload = {"choices": [{"message": {"content": content}}]}
    return json.dumps(payload).encode("utf-8")


def _movie_manifest() -> dict[str, object]:
    return {
        "kind": "movie",
        "meta": {
            "id": "harbor-lantern-1984",
            "title": "Harbor Lantern (1984)",
            "disc_name": DISC_NAME,
            "device": "dev:/dev/sr0",
            "minlength": 300,
        },
        "output": {
            "dir": "Movies",
            "filename": "Harbor Lantern (1984).mkv",
        },
        "nas": {
            "movies": True,
            "share": paths.PLACEHOLDER_MOVIES,
        },
        "stream_defaults": {
            "keep_all_tracks": True,
            "audio_default": "eng",
        },
        "title_selection": {
            "mode": "fixed",
            "title": FEATURE_INDEX,
            "min_duration_sec": 4800,
        },
        "validation": {
            "min_duration_sec": 4800,
            "min_size_bytes": 1024 * 1024 * 1024,
        },
        "skips": [{"makemkv_title": 0, "reason": "trailer"}],
    }


def _models() -> dict[str, str]:
    return {
        llm.ENV_API_KEY: "test-openrouter-key",
        llm.ENV_MAP_MODEL: "test/map-model",
        llm.ENV_FAILURE_MODEL: "test/failure-model",
        llm.ENV_GAP_MODEL: "test/gap-model",
        llm.ENV_VERIFY_MODEL: "test/verify-model",
        llm.ENV_VISION_MODEL: "test/vision-model",
    }


def _patch_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log_root = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(map_agent, "LOG_ROOT", log_root)
    monkeypatch.setattr(map_agent, "MAP_LOG", log_root / "map.log")
    monkeypatch.setattr(map_agent, "ROOT", tmp_path)
    monkeypatch.setattr(map_agent, "READY_DIR", tmp_path / "staging" / "ready")
    return log_root


def _ready_yamls(tmp_path: Path) -> list[Path]:
    ready = tmp_path / "staging" / "ready"
    if not ready.is_dir():
        return []
    return list(ready.glob("*.yaml"))


def test_map_and_decisions_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_paths(monkeypatch, tmp_path)
    scan_json = tmp_path / "staging" / "pending" / f"{DISC_ID}.scan.json"
    scan_json.parent.mkdir(parents=True)
    scan = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scan_json.write_text(json.dumps(scan), encoding="utf-8")
    assert scan["disc_name"] == DISC_NAME
    assert llm.label_needs_jacket(DISC_NAME) is False

    spawned: list[object] = []

    def _spawn(*args: object, **kwargs: object) -> None:
        spawned.append(args[0] if args else kwargs)
        raise AssertionError("model subprocess must not be launched")

    monkeypatch.setattr(map_agent.subprocess, "Popen", _spawn)
    monkeypatch.setattr(map_agent.subprocess, "run", _spawn)

    captured: list[object] = []
    pending: list[bytes] = []

    def urlopen(req: object, timeout: float | None = None) -> _Resp:
        captured.append(req)
        assert timeout == llm.HTTP_TIMEOUT_SEC
        assert pending, "OpenRouter called with no scripted response"
        return _Resp(pending.pop(0))

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    env = _models()

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("missing key must not call OpenRouter")

    rc = map_agent.run_map_agent(
        DISC_ID, scan_json, background=True, env={}, opener=boom
    )
    assert rc == map_agent.MAP_EXIT_AUTH
    assert _ready_yamls(tmp_path) == []

    pending.append(_chat("this is not json"))
    rc = map_agent.run_map_agent(DISC_ID, scan_json, background=True, env=env)
    assert rc == 1
    assert _ready_yamls(tmp_path) == []

    pending.append(
        _chat(json.dumps({"action": "copy_to_nas", "manifest": _movie_manifest()}))
    )
    rc = map_agent.run_map_agent(DISC_ID, scan_json, background=True, env=env)
    assert rc == 1
    assert _ready_yamls(tmp_path) == []

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    pending.append(
        _chat(json.dumps({"action": "write", "manifest": _movie_manifest()}))
    )
    ok, message, kind = map_agent.supervise_background_map(DISC_ID, scan_json)
    assert ok is True
    assert kind is None
    assert message == "manifest written"
    written = _ready_yamls(tmp_path)
    assert len(written) == 1
    manifest = load_manifest(written[0])
    assert isinstance(manifest, MovieManifest)
    assert manifest.nas.movies is True
    assert manifest.nas.share == paths.PLACEHOLDER_MOVIES
    assert manifest.title_selection.mode == "fixed"
    assert manifest.title_selection.title == FEATURE_INDEX
    assert [skip.makemkv_title for skip in manifest.skips] == [0]
    assert step_verify_disc_match(written[0], manifest, scan).ok
    verified = step_verify_titles(manifest, scan)
    assert verified.ok, verified.message

    map_req = captured[2]
    map_body = json.loads(map_req.data.decode("utf-8"))
    assert map_req.get_full_url() == llm.OPENROUTER_CHAT_URL
    assert map_body["model"] == "test/map-model"
    assert map_body["model"] == env[llm.ENV_MAP_MODEL]
    assert map_body["temperature"] == 0
    assert map_body["reasoning"] == {"effort": "none"}
    map_content = map_body["messages"][0]["content"]
    assert isinstance(map_content, str)
    assert "image_url" not in map_content
    assert "--force" not in map_content
    assert DISC_NAME in map_content

    situation = "Harbor Lantern (1984) feature title 1; trailer skipped"
    pending.append(_chat(json.dumps({"action": "bad_sectors"})))
    assert (
        llm.classify_disc_failure(situation, volume_label=DISC_NAME, env=env)
        == "bad_sectors"
    )
    pending.append(_chat(json.dumps({"action": "another_drive"})))
    assert llm.choose_gap_action(situation, env=env) == "another_drive"
    pending.append(_chat(json.dumps({"action": "keep"})))
    assert llm.choose_verify_action(situation, env=env) == "keep"
    pending.append(_chat(json.dumps({"action": "wrong_title"})))
    jpeg = b"\xff\xd8\xff\xd9"
    assert (
        llm.classify_disc_failure(
            "placeholder label",
            volume_label="UNKNOWN",
            jacket=jpeg,
            env=env,
        )
        == "wrong_title"
    )

    decision_bodies = [
        json.loads(req.data.decode("utf-8")) for req in captured[3:]
    ]
    assert [body["model"] for body in decision_bodies] == [
        "test/failure-model",
        "test/gap-model",
        "test/verify-model",
        "test/vision-model",
    ]
    failure_content = decision_bodies[0]["messages"][0]["content"]
    assert isinstance(failure_content, str)
    vision_content = decision_bodies[3]["messages"][0]["content"]
    assert isinstance(vision_content, list)
    assert vision_content[1]["type"] == "image_url"
    assert vision_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert spawned == []
