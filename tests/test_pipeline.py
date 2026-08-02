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


def _tv_manifest_dict(*, disc_name: str = "House MD: Season 1: Disc 3") -> dict:
    return {
        "kind": "tv_disc",
        "meta": {
            "id": "house-s1-disc3",
            "show": "House MD",
            "season": 1,
            "disc_name": disc_name,
            "device": "dev:/dev/sr0",
            "minlength": 300,
        },
        "output": {"season_dir": "House MD/Season 1"},
        "tracks": [
            {"slot": 1, "makemkv_title": 0, "episode": "11", "title": "Detox"},
            {"slot": 2, "makemkv_title": 1, "episode": "12", "title": "Sports Medicine"},
            {"slot": 3, "makemkv_title": 2, "episode": "13", "title": "Cursed"},
            {"slot": 4, "makemkv_title": 3, "episode": "14", "title": "Control"},
            {"slot": 5, "makemkv_title": 4, "episode": "15", "title": "Mob Rules"},
        ],
        "post": {"eject": True, "copy_to_nas": True},
    }


def test_compute_resume_from_slot_skips_existing_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from videosripper import paths
    from videosripper.models import TvDiscManifest

    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    manifest = TvDiscManifest.model_validate(_tv_manifest_dict())
    for track in manifest.tracks[:4]:
        _, _, path = pipeline.track_outputs(manifest, track)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    assert pipeline.compute_resume_from_slot(manifest) == 5


def test_recover_failed_manifest_promotes_claimed_yaml_to_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    import yaml

    from videosripper import paths
    from videosripper.models import TvDiscManifest

    ready = tmp_path / "ready"
    failed = tmp_path / "failed"
    ready.mkdir()
    failed.mkdir()
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "FAILED_DIR", failed)
    monkeypatch.setattr(pipeline, "REINSERT_MAX", 5)
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")

    manifest_dict = _tv_manifest_dict()
    failed_path = failed / "house-s1-disc3.yaml.claimed-sr0"
    failed_path.write_text(yaml.dump(manifest_dict), encoding="utf-8")
    (failed / "house-s1-disc3.reason.txt").write_text("slot 5 failed\n", encoding="utf-8")

    out_dir = tmp_path / "output" / "House MD" / "Season 1"
    out_dir.mkdir(parents=True)
    manifest = TvDiscManifest.model_validate(manifest_dict)
    for track in manifest.tracks[:4]:
        ep, title, path = pipeline.track_outputs(manifest, track)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    scan_data = {"disc_name": "House MD: Season 1: Disc 3", "titles": [{"index": 0}]}
    recovered = pipeline.recover_failed_manifest_for_scan(
        scan_data, logging.getLogger("test"), disc_id="house-s1-disc3"
    )

    assert recovered == ready / "house-s1-disc3.yaml"
    assert not failed_path.exists()
    resume = json.loads((ready / "house-s1-disc3.resume.json").read_text(encoding="utf-8"))
    assert resume["resume_from_slot"] == 5
    assert resume["reinsert_count"] == 1
    assert "slot 5 failed" in resume["last_reason"]


def test_recover_failed_manifest_respects_reinsert_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    import yaml

    failed = tmp_path / "failed"
    ready = tmp_path / "ready"
    failed.mkdir()
    ready.mkdir()
    monkeypatch.setattr(pipeline, "FAILED_DIR", failed)
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "REINSERT_MAX", 2)
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)

    failed_path = failed / "house-s1-disc3.yaml"
    failed_path.write_text(yaml.dump(_tv_manifest_dict()), encoding="utf-8")
    pipeline._save_resume_info(
        "house-s1-disc3",
        failed,
        pipeline.ResumeInfo(resume_from_slot=5, reinsert_count=2),
    )

    scan_data = {"disc_name": "House MD: Season 1: Disc 3", "titles": [{"index": 0}]}
    assert (
        pipeline.recover_failed_manifest_for_scan(
            scan_data, logging.getLogger("test"), disc_id="house-s1-disc3"
        )
        is None
    )
    assert failed_path.is_file()
    assert not (ready / "house-s1-disc3.yaml").exists()


def test_handle_disc_insert_recovers_failed_before_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging
    import yaml

    from videosripper import paths

    pending = tmp_path / "pending"
    ready = tmp_path / "ready"
    failed = tmp_path / "failed"
    for d in (pending, ready, failed):
        d.mkdir()
    monkeypatch.setattr(pipeline, "PENDING_DIR", pending)
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "FAILED_DIR", failed)
    monkeypatch.setattr(pipeline, "setup_pipeline_logger", lambda: None)
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "log_progress", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_log_identify", lambda p: None)
    monkeypatch.setattr(pipeline, "disc_media_generation", lambda dev: None)
    monkeypatch.setattr(pipeline, "REINSERT_MAX", 5)
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")

    scan_data = {"disc_name": "House MD: Season 1: Disc 3", "titles": [{"index": i} for i in range(5)]}
    monkeypatch.setattr(
        pipeline,
        "step_scan_disc",
        lambda dev, ml, logger: pipeline.StepResult(
            name="scan", ok=True, message="ok", data={"scan_data": scan_data}
        ),
    )
    map_called = {"called": False}

    def fail_map(*args, **kwargs):
        map_called["called"] = True
        raise AssertionError("map should not run when failed job recovered")

    monkeypatch.setattr(pipeline, "step_trigger_map_async", fail_map)
    monkeypatch.setattr(pipeline, "step_registry_lookup", lambda name: None)

    (failed / "house-s1-disc3.yaml.claimed-sr0").write_text(
        yaml.dump(_tv_manifest_dict()), encoding="utf-8"
    )

    result = pipeline.handle_disc_insert("dev:/dev/sr0", 300, logging.getLogger("test"))
    assert result.ok
    assert result.message == "failed_recovered"
    assert map_called["called"] is False
    assert (ready / "house-s1-disc3.yaml").is_file()
