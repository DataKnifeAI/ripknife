"""Tests for videosripper.pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videosripper import pipeline
from videosripper.map_agent import build_map_prompt


def test_run_serial_stops_on_failure() -> None:
    calls: list[str] = []

    def ok() -> pipeline.StepResult:
        calls.append("ok")
        return pipeline.StepResult(name="ok", ok=True)

    def fail() -> pipeline.StepResult:
        calls.append("fail")
        return pipeline.StepResult(name="fail", ok=False)

    def never() -> pipeline.StepResult:
        calls.append("never")
        return pipeline.StepResult(name="never", ok=True)

    results = pipeline.run_serial([("a", ok), ("b", fail), ("c", never)])
    assert [r.name for r in results] == ["a", "b"]
    assert calls == ["ok", "fail"]


def test_run_parallel_runs_all_local_steps() -> None:
    seen: set[str] = set()

    def make(name: str):
        def _step() -> pipeline.StepResult:
            seen.add(name)
            return pipeline.StepResult(name=name, ok=True)

        return _step

    results = pipeline.run_parallel(
        [("x", make("x")), ("y", make("y")), ("z", make("z"))]
    )
    assert len(results) == 3
    assert seen == {"x", "y", "z"}


def test_step_write_pending_creates_json_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "PENDING_DIR", tmp_path / "pending")
    scan_data = {
        "disc_name": "Test Show Disc 1",
        "titles": [{"index": 0, "duration_sec": 700}],
    }
    pending = pipeline.step_write_pending(
        scan_data,
        device="dev:/dev/sr0",
        minlength=300,
    )
    assert pending.disc_id == "test-show-disc-1"
    assert pending.scan_json.is_file()
    assert pending.meta_json.is_file()
    meta = json.loads(pending.meta_json.read_text(encoding="utf-8"))
    assert meta["title_count"] == 1


def test_log_identify_reports_hints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Generic-label discs used to jump SCAN → PENDING with no identify trace."""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pipeline, "log_stage", lambda stage, **kw: calls.append((stage, kw))
    )

    pending = pipeline.PendingDisc(
        disc_id="dvd-video",
        disc_name="DVD_VIDEO",
        scan_json=tmp_path / "dvd-video.scan.json",
        meta_json=tmp_path / "dvd-video.meta.json",
        scan_data={
            "identification": {
                "is_generic_label": True,
                "bluray_title": None,
                "feature_duration_sec": 6123,
                "candidates": [{"title": "A"}, {"title": "B"}],
            }
        },
    )
    pipeline._log_identify(pending)
    stage, kwargs = calls[0]
    assert stage == "IDENTIFY"
    assert kwargs["process"] == pipeline.PROCESS_INSERT
    assert "generic=True" in kwargs["detail"]
    assert "candidates=2" in kwargs["detail"]
    assert "feature=6123s" in kwargs["detail"]
    assert "bdmt=-" in kwargs["detail"]

    # No identification block (e.g. older scan JSON) → no line at all
    calls.clear()
    pipeline._log_identify(
        pipeline.PendingDisc(
            disc_id="x",
            disc_name="X",
            scan_json=tmp_path / "x.scan.json",
            meta_json=tmp_path / "x.meta.json",
            scan_data={},
        )
    )
    assert calls == []


def test_run_device_worker_once_binds_manifest_to_worker_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    from contextlib import nullcontext

    ready = tmp_path / "ready"
    ready.mkdir()
    yaml_path = ready / "john-wick.yaml"
    yaml_path.write_text("kind: movie\n", encoding="utf-8")

    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "setup_pipeline_logger", lambda: None)
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "flock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(
        pipeline,
        "scan_device_dict",
        lambda dev, ml: {"disc_name": "John Wick", "titles": [{"index": 1}]},
    )

    class _Meta:
        device = "dev:/dev/sr0"  # manifest's original drive
        id = "john-wick"
        title = "John Wick"

    class _Manifest:
        meta = _Meta()

    manifest = _Manifest()
    monkeypatch.setattr(
        pipeline,
        "_match_ready_manifest_for_scan",
        lambda scan, log: (yaml_path, manifest),
    )
    monkeypatch.setattr(pipeline, "already_ripped", lambda m: False)

    captured: dict[str, object] = {}

    def fake_verify(yp, m, scan, logger):
        captured["yaml"] = yp
        captured["device"] = m.meta.device
        return pipeline.StepResult(name="process_ready", ok=True, message="ripped")

    monkeypatch.setattr(pipeline, "_verify_and_rip", fake_verify)

    result = pipeline.run_device_worker_once("dev:/dev/sr1", logging.getLogger("test"))

    assert result.ok and result.message == "ripped"
    assert captured["device"] == "dev:/dev/sr1"  # rebound to the worker's drive
    assert not yaml_path.exists()  # claimed via atomic rename
    assert any(p.name.endswith(".claimed-sr1") for p in ready.iterdir())


def test_build_map_prompt_includes_disc_id_and_paths(tmp_path: Path) -> None:
    scan_json = tmp_path / "foo.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = build_map_prompt("foo", scan_json)
    assert "identified slug" in prompt.lower() or "IDENTIFIED" in prompt
    assert str(scan_json) in prompt
    assert "staging/ready/" in prompt
    assert "meta.disc_name = exact scan disc_name" in prompt


def test_find_cached_pending_scan_reuses_fresh_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    monkeypatch.setattr(pipeline, "PENDING_DIR", pending)
    monkeypatch.setattr(pipeline, "SCAN_CACHE_MAX_AGE_SEC", 86400)

    scan_data = {"disc_name": "Test Disc", "titles": [{"index": 0}]}
    (pending / "test-disc.scan.json").write_text(
        __import__("json").dumps(scan_data), encoding="utf-8"
    )
    meta = {
        "id": "test-disc",
        "disc_name": "Test Disc",
        "device": "dev:/dev/sr0",
        "media_generation": "media_change=3",
        "scanned_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    }
    (pending / "test-disc.meta.json").write_text(
        __import__("json").dumps(meta), encoding="utf-8"
    )

    cached = pipeline.find_cached_pending_scan("dev:/dev/sr0", "media_change=3")
    assert cached is not None
    assert cached.disc_id == "test-disc"
    assert cached.scan_data["disc_name"] == "Test Disc"


def test_find_cached_pending_scan_misses_stale_media_gen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    monkeypatch.setattr(pipeline, "PENDING_DIR", pending)

    meta = {
        "id": "test-disc",
        "device": "dev:/dev/sr0",
        "media_generation": "media_change=2",
        "scanned_at": "2026-01-01T00:00:00Z",
    }
    (pending / "test-disc.meta.json").write_text(
        __import__("json").dumps(meta), encoding="utf-8"
    )
    (pending / "test-disc.scan.json").write_text("{}", encoding="utf-8")

    assert pipeline.find_cached_pending_scan("dev:/dev/sr0", "media_change=3") is None


def test_handle_disc_insert_uses_scan_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    pending = tmp_path / "pending"
    ready = tmp_path / "ready"
    pending.mkdir()
    ready.mkdir()
    monkeypatch.setattr(pipeline, "PENDING_DIR", pending)
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "setup_pipeline_logger", lambda: None)
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "log_progress", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_log_identify", lambda p: None)
    monkeypatch.setattr(pipeline, "disc_media_generation", lambda dev: "media_change=1")

    scan_data = {"disc_name": "Cached Disc", "titles": [{"index": 0}]}
    (pending / "cached-disc.scan.json").write_text(
        __import__("json").dumps(scan_data), encoding="utf-8"
    )
    meta = {
        "id": "cached-disc",
        "disc_name": "Cached Disc",
        "device": "dev:/dev/sr0",
        "media_generation": "media_change=1",
        "scanned_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    }
    (pending / "cached-disc.meta.json").write_text(
        __import__("json").dumps(meta), encoding="utf-8"
    )

    scanned = {"called": False}

    def fail_scan(*args, **kwargs):
        scanned["called"] = True
        raise AssertionError("scan should be skipped")

    monkeypatch.setattr(pipeline, "step_scan_disc", fail_scan)
    monkeypatch.setattr(pipeline, "step_registry_lookup", lambda name: None)
    monkeypatch.setattr(
        pipeline,
        "step_trigger_map_async",
        lambda pending, logger: pipeline.StepResult(name="map", ok=True),
    )

    result = pipeline.handle_disc_insert("dev:/dev/sr0", 300, logging.getLogger("test"))
    assert result.ok
    assert scanned["called"] is False
