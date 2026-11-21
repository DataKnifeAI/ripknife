"""Watcher stop → resume sidecar + NAS-aware skip/resume tests."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from ripknife import pipeline, runner
from ripknife.models import TvDiscManifest


def _tv_manifest_dict(*, nas_share: str | None = None) -> dict:
    nas: dict = {}
    if nas_share is not None:
        nas = {"share": nas_share}
    return {
        "kind": "tv_disc",
        "meta": {
            "id": "example-cartoon-s3-disc1",
            "show": "Example Cartoon",
            "season": 3,
            "disc_name": "Example Cartoon S3 Disc 1",
            "device": "dev:/dev/sr0",
            "minlength": 300,
        },
        "output": {"season_dir": "Example Cartoon/Season 3"},
        "nas": nas,
        "tracks": [
            {"slot": 1, "makemkv_title": 0, "episode": "01", "title": "First Episode"},
            {"slot": 2, "makemkv_title": 1, "episode": "02", "title": "Second Episode"},
            {"slot": 3, "makemkv_title": 2, "episode": "03", "title": "Third Episode"},
            {"slot": 4, "makemkv_title": 3, "episode": "04", "title": "Fourth Episode"},
            {"slot": 5, "makemkv_title": 4, "episode": "05", "title": "Fifth Episode"},
        ],
        "post": {"eject": True, "copy_to_nas": True},
    }


def test_persist_interrupted_claimed_writes_resume_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready"
    failed = tmp_path / "failed"
    ready.mkdir()
    failed.mkdir()
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "FAILED_DIR", failed)
    monkeypatch.setattr(pipeline, "DONE_DIR", tmp_path / "done")
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)

    from ripknife import paths

    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")

    claimed = ready / "example-cartoon-s3-disc1.yaml.claimed-sr0"
    claimed.write_text(yaml.dump(_tv_manifest_dict()), encoding="utf-8")
    manifest = TvDiscManifest.model_validate(_tv_manifest_dict())
    for track in manifest.tracks[:3]:
        _, _, path = runner.track_outputs(manifest, track)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)

    ids = pipeline.persist_interrupted_claimed_rips(reason="watcher_stop")
    assert ids == ["example-cartoon-s3-disc1"]
    assert not claimed.exists()
    failed_yaml = failed / "example-cartoon-s3-disc1.yaml"
    assert failed_yaml.is_file()
    resume = json.loads((failed / "example-cartoon-s3-disc1.resume.json").read_text(encoding="utf-8"))
    assert resume["resume_from_slot"] == 4
    assert resume["last_reason"] == "watcher_stop"
    assert (failed / "example-cartoon-s3-disc1.reason.txt").read_text(encoding="utf-8").strip() == (
        "watcher_stop"
    )


def test_recover_interrupted_promotes_failed_when_disc_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import nullcontext

    ready = tmp_path / "ready"
    failed = tmp_path / "failed"
    for d in (ready, failed):
        d.mkdir()
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "FAILED_DIR", failed)
    monkeypatch.setattr(pipeline, "DONE_DIR", tmp_path / "done")
    monkeypatch.setattr(pipeline, "STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "setup_pipeline_logger", lambda: None)
    monkeypatch.setattr(pipeline, "flock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(pipeline, "media_present", lambda dev: True)

    from ripknife import paths
    import ripknife.monitor as monitor

    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(monitor, "list_makemkv_processes", lambda device: [])

    # Simulate leftover claimed file from a hard kill (no graceful persist).
    claimed = ready / "example-cartoon-s3-disc1.yaml.claimed-sr0"
    claimed.write_text(yaml.dump(_tv_manifest_dict()), encoding="utf-8")

    monkeypatch.setattr(
        pipeline,
        "scan_device_dict",
        lambda device, ml: {
            "disc_name": "Example Cartoon S3 Disc 1",
            "titles": [{"index": i} for i in range(5)],
        },
    )
    monkeypatch.setattr(
        pipeline,
        "step_verify_disc_match",
        lambda *a, **k: pipeline.StepResult(name="match", ok=True),
    )

    recovered = pipeline.recover_interrupted_rips(
        ["dev:/dev/sr0"], logging.getLogger("test"), hold_locks=False
    )
    assert recovered == [ready / "example-cartoon-s3-disc1.yaml"]
    assert not claimed.exists()
    resume = json.loads((ready / "example-cartoon-s3-disc1.resume.json").read_text(encoding="utf-8"))
    assert resume["resume_from_slot"] == 1
    assert resume["last_reason"] == "orphaned_claim"
    # Watcher interrupt must not burn re-insert budget.
    assert resume["reinsert_count"] == 0


def test_compute_resume_from_slot_uses_nas_when_local_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ripknife import paths

    nas = tmp_path / "nas"
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(paths, "NAS_SHARE", nas)

    manifest = TvDiscManifest.model_validate(_tv_manifest_dict(nas_share=str(nas)))
    for track in manifest.tracks[:3]:
        _, _, local = runner.track_outputs(manifest, track)[0]
        season = 3
        nas_path = runner.tv_episode_nas_path(manifest, local, season)
        nas_path.parent.mkdir(parents=True, exist_ok=True)
        nas_path.write_bytes(b"x" * 200)

    assert pipeline.compute_resume_from_slot(manifest) == 4
    assert pipeline.already_ripped(manifest) is False


def test_episode_output_present_skips_when_only_on_nas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ripknife import paths

    nas = tmp_path / "nas"
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(paths, "NAS_SHARE", nas)

    manifest = TvDiscManifest.model_validate(_tv_manifest_dict(nas_share=str(nas)))
    track = manifest.tracks[0]
    _, _, local = runner.track_outputs(manifest, track)[0]
    assert runner.episode_output_present(local, manifest, 3) is False

    nas_path = runner.tv_episode_nas_path(manifest, local, 3)
    nas_path.parent.mkdir(parents=True, exist_ok=True)
    nas_path.write_bytes(b"x" * 50)
    assert runner.episode_output_present(local, manifest, 3) is True
    assert runner.track_outputs_present(manifest, track) is True

    plan = runner.build_plan(manifest, start_at=1)
    assert plan[0].skip is True
    assert all(not p.skip for p in plan[1:])


def test_already_ripped_tv_when_all_on_nas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ripknife import paths

    nas = tmp_path / "nas"
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(paths, "NAS_SHARE", nas)

    manifest = TvDiscManifest.model_validate(_tv_manifest_dict(nas_share=str(nas)))
    for track in manifest.tracks:
        _, _, local = runner.track_outputs(manifest, track)[0]
        nas_path = runner.tv_episode_nas_path(manifest, local, 3)
        nas_path.parent.mkdir(parents=True, exist_ok=True)
        nas_path.write_bytes(b"y" * 20)

    assert pipeline.already_ripped(manifest) is True
    assert pipeline.tv_disc_outputs_complete(manifest) is True


def test_watcher_stop_on_shutdown_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signal handler on_shutdown runs persist before SystemExit."""
    import logging
    import signal

    from ripknife import logging_setup, paths, watcher_util

    log_root = tmp_path / "logs"
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "PIPELINE_LOG", log_root / "pipeline.log")
    pl = logging.getLogger(logging_setup._PIPELINE_LOGGER_NAME)
    for handler in pl.handlers[:]:
        pl.removeHandler(handler)
        handler.close()
    monkeypatch.setattr(logging_setup, "_last_stage_monotonic", None)
    watcher_util.clear_shutdown()

    called: list[str] = []

    def on_shutdown() -> None:
        called.append("persist")

    logger = logging.getLogger("test-stop-resume")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    try:
        watcher_util.install_watcher_signal_handlers(
            "rip-watcher", logger, on_shutdown=on_shutdown
        )
        with pytest.raises(SystemExit) as ei:
            signal.raise_signal(signal.SIGTERM)
        assert ei.value.code == 0
        assert called == ["persist"]
        assert watcher_util.shutdown_requested()
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        watcher_util.clear_shutdown()
