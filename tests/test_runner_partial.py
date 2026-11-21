"""Tests for TV per-slot continue-on-failure and partial disc completion."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ripknife import paths, pipeline
from ripknife.models import TvDiscManifest
from ripknife.runner import RipSessionStats, SlotFailure, TvRipResult, rip_tv_disc


def _patch_rip_progress_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ripknife.runner.clear_rip_progress", lambda: None)
    monkeypatch.setattr("ripknife.runner.write_rip_progress", lambda *a, **k: None)


def _tv_manifest_dict() -> dict:
    return {
        "kind": "tv_disc",
        "meta": {
            "id": "test-show-s1-disc1",
            "show": "Test Show",
            "season": 1,
            "device": "dev:/dev/sr0",
            "minlength": 300,
        },
        "output": {"season_dir": "Test Show/Season 1"},
        "tracks": [
            {"slot": 1, "makemkv_title": 0, "episode": "01", "title": "Alpha"},
            {"slot": 2, "makemkv_title": 1, "episode": "02", "title": "Beta"},
            {"slot": 3, "makemkv_title": 2, "episode": "03", "title": "Gamma"},
        ],
        "post": {"eject": False, "copy_to_nas": False},
        "retry": {"count": 0},
    }


def test_slot_failure_continues_to_next_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(paths, "STAGING_ROOT", tmp_path / "staging")
    monkeypatch.setattr(
        "ripknife.runner.preflight_disc_open",
        lambda *a, **k: 3,
    )
    monkeypatch.setattr(
        "ripknife.runner.scan",
        lambda *a, **k: MagicMock(titles={0: MagicMock(), 1: MagicMock(), 2: MagicMock()}),
    )
    monkeypatch.setattr("ripknife.runner.raise_on_errors", lambda *a, **k: None)
    monkeypatch.setattr("ripknife.runner.warn_duplicate_makemkv", lambda *a, **k: None)
    monkeypatch.setattr("ripknife.runner.warn_title_count_explosion", lambda *a, **k: None)
    monkeypatch.setattr(
        "ripknife.runner.validate_manifest_titles",
        lambda *a, **k: [],
    )

    rip_calls: list[int] = []

    def fake_rip_title(device, title_index, staging, minlength, **kwargs):
        rip_calls.append(title_index)
        if title_index == 1:
            raise RuntimeError("No MKV produced for title 1")
        out = staging / f"title_{title_index}.mkv"
        out.write_bytes(b"fake mkv")
        return out

    monkeypatch.setattr("ripknife.runner.rip_title", fake_rip_title)
    monkeypatch.setattr(
        "ripknife.runner.ffmpeg_ops.probe_duration",
        lambda path: 100,
    )
    monkeypatch.setattr(
        "ripknife.runner.verify_track_outputs",
        lambda *a, **k: MagicMock(ok=True),
    )
    _patch_rip_progress_io(monkeypatch)

    progress: list[tuple] = []

    def capture_progress(stage, current, total, **kwargs):
        progress.append((stage, current, total, kwargs.get("detail", "")))

    monkeypatch.setattr("ripknife.runner.log_progress", capture_progress)

    manifest = TvDiscManifest.model_validate(_tv_manifest_dict())
    result = rip_tv_disc(manifest)

    assert rip_calls == [0, 1, 2]
    assert result.stats.episodes_written == ["1x01", "1x03"]
    assert len(result.stats.failed_slots) == 1
    assert result.stats.failed_slots[0].slot == 2
    assert result.partial is True
    assert result.plan
    # Failed slot 2 must not increment completed: slot 3 still starts at 1/3.
    ripping = [
        (s, c, t)
        for s, c, t, d in progress
        if s == "RIP" and str(d).startswith("ripping")
    ]
    assert ripping == [("RIP", 0, 3), ("RIP", 1, 3), ("RIP", 1, 3)]
    assert progress[-1][:3] == ("RIP", 2, 3)
    assert "tracks done 2/3" in progress[-1][3]


def test_step_rip_manifest_reports_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = TvDiscManifest.model_validate(_tv_manifest_dict())
    partial = TvRipResult(
        plan=[],
        stats=RipSessionStats(
            episodes_written=["1x01"],
            failed_slots=[
                SlotFailure(
                    slot=2,
                    makemkv_title=1,
                    episode="1x02 Beta",
                    error="stall",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "rip_tv_disc",
        lambda *a, **k: partial,
    )
    monkeypatch.setattr(
        "ripknife.logging_setup.setup_session_logger",
        lambda *a, **k: None,
    )

    result = pipeline.step_rip_manifest(manifest)

    assert result.ok is False
    assert result.data["partial"] is True
    assert "slot 2" in result.message


def test_verify_and_rip_partial_moves_to_failed_with_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    ready = tmp_path / "ready"
    failed = tmp_path / "failed"
    ready.mkdir()
    failed.mkdir()
    monkeypatch.setattr(pipeline, "READY_DIR", ready)
    monkeypatch.setattr(pipeline, "FAILED_DIR", failed)
    monkeypatch.setattr(pipeline, "log_stage", lambda *a, **k: None)
    monkeypatch.setattr(
        "ripknife.logging_setup.setup_session_logger",
        lambda *a, **k: None,
    )

    manifest = TvDiscManifest.model_validate(_tv_manifest_dict())
    yaml_path = ready / "test-show-s1-disc1.yaml"
    yaml_path.write_text("kind: tv_disc\n", encoding="utf-8")

    partial = TvRipResult(
        plan=[],
        stats=RipSessionStats(
            episodes_written=["1x01"],
            failed_slots=[
                SlotFailure(
                    slot=2,
                    makemkv_title=1,
                    episode="1x02 Beta",
                    error="No MKV produced",
                )
            ],
        ),
    )

    monkeypatch.setattr(
        pipeline,
        "step_verify_disc_match",
        lambda *a, **k: pipeline.StepResult(name="disc_match", ok=True),
    )
    monkeypatch.setattr(
        pipeline,
        "step_verify_titles",
        lambda *a, **k: pipeline.StepResult(name="title_verify", ok=True),
    )
    monkeypatch.setattr(
        pipeline,
        "step_rip_manifest",
        lambda *a, **k: pipeline.StepResult(
            name="rip",
            ok=False,
            message="partial rip: failed slot 2 (1x02 Beta)",
            data={"partial": True, "tv_result": partial},
        ),
    )

    result = pipeline._verify_and_rip(
        yaml_path,
        manifest,
        {"disc_name": "Test Show Disc 1", "titles": []},
        logging.getLogger("test"),
    )

    assert result.ok is False
    assert (failed / "test-show-s1-disc1.yaml").is_file()
    partial_path = failed / "test-show-s1-disc1.partial.json"
    assert partial_path.is_file()
    data = json.loads(partial_path.read_text(encoding="utf-8"))
    assert data["episodes_ok"] == ["1x01"]
    assert data["failed_slots"][0]["slot"] == 2
    resume = json.loads((failed / "test-show-s1-disc1.resume.json").read_text(encoding="utf-8"))
    assert resume["resume_from_slot"] == 2


def _playall_manifest_dict() -> dict:
    return {
        "kind": "tv_disc",
        "meta": {
            "id": "test-playall-disc1",
            "show": "Test Show",
            "season": 1,
            "device": "dev:/dev/sr0",
            "minlength": 300,
        },
        "output": {"season_dir": "Test Show/Season 1"},
        "tracks": [
            {
                "slot": 1,
                "makemkv_title": 0,
                "episode": "01",
                "title": "One",
                "extract_chapter": {"chapter_index": 0, "chapter_end": 4},
            },
            {
                "slot": 2,
                "makemkv_title": 0,
                "episode": "02",
                "title": "Two",
                "extract_chapter": {"chapter_index": 5, "chapter_end": 9},
            },
            {
                "slot": 3,
                "makemkv_title": 0,
                "episode": "03",
                "title": "Three",
                "extract_chapter": {"chapter_index": 10, "chapter_end": 12},
            },
        ],
        "post": {"eject": False, "copy_to_nas": False},
        "retry": {"count": 0},
    }


def _stub_tv_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ripknife.runner.preflight_disc_open",
        lambda *a, **k: 3,
    )
    monkeypatch.setattr(
        "ripknife.runner.scan",
        lambda *a, **k: MagicMock(titles={0: MagicMock()}),
    )
    monkeypatch.setattr("ripknife.runner.raise_on_errors", lambda *a, **k: None)
    monkeypatch.setattr("ripknife.runner.warn_duplicate_makemkv", lambda *a, **k: None)
    monkeypatch.setattr("ripknife.runner.warn_title_count_explosion", lambda *a, **k: None)
    monkeypatch.setattr(
        "ripknife.runner.validate_manifest_titles",
        lambda *a, **k: [],
    )


def test_playall_logs_rip_once_then_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(paths, "STAGING_ROOT", tmp_path / "staging")
    _stub_tv_preflight(monkeypatch)
    _patch_rip_progress_io(monkeypatch)

    rip_calls: list[int] = []

    def fake_rip_title(device, title_index, staging, minlength, **kwargs):
        rip_calls.append(title_index)
        out = staging / f"title_{title_index}.mkv"
        out.write_bytes(b"fake mkv")
        return out

    monkeypatch.setattr("ripknife.runner.rip_title", fake_rip_title)
    monkeypatch.setattr(
        "ripknife.runner.ffmpeg_ops.validate_extract_chapter_ranges",
        lambda *a, **k: 13,
    )
    monkeypatch.setattr(
        "ripknife.runner.ffmpeg_ops.chapter_bounds",
        lambda *a, **k: (0.0, 100.0),
    )

    def fake_extract(src, chapter_index, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"cut")

    monkeypatch.setattr("ripknife.runner.ffmpeg_ops.extract_chapter", fake_extract)
    monkeypatch.setattr(
        "ripknife.runner.ffmpeg_ops.probe_duration",
        lambda path: 100,
    )
    monkeypatch.setattr(
        "ripknife.runner.verify_track_outputs",
        lambda *a, **k: MagicMock(ok=True),
    )

    progress: list[tuple] = []

    def capture_progress(stage, current, total, **kwargs):
        progress.append((stage, current, total, kwargs.get("detail", "")))

    monkeypatch.setattr("ripknife.runner.log_progress", capture_progress)

    info_msgs: list[str] = []

    def capture_info(msg, *args):
        info_msgs.append(msg % args if args else str(msg))

    monkeypatch.setattr("ripknife.runner.logger.info", capture_info)

    manifest = TvDiscManifest.model_validate(_playall_manifest_dict())
    result = rip_tv_disc(manifest)

    assert rip_calls == [0]
    assert result.stats.episodes_written == ["1x01", "1x02", "1x03"]
    assert not result.stats.failed_slots
    stages = [p[0] for p in progress]
    assert any(s.startswith("RIP play-all title 0") for s in stages)
    extract_slots = [
        (c, t)
        for s, c, t, d in progress
        if s == "EXTRACT" and t == 3 and "tracks done" not in str(d)
    ]
    assert extract_slots == [(1, 3), (2, 3), (3, 3)]
    extract_details = [d for s, _c, _t, d in progress if s == "EXTRACT"]
    assert any("from cached title 0" in d for d in extract_details)
    assert not any(s == "RIP" and t == 3 for s, _c, t, _d in progress)
    assert any(
        "RIP play-all title 0 (feeds slots 1–3)" in m for m in info_msgs
    )
    assert any("EXTRACT 2/3" in m and "cached title 0" in m for m in info_msgs)
    assert progress[-1][:3] == ("EXTRACT", 3, 3)


def test_rip_tv_disc_clears_rip_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(paths, "STAGING_ROOT", tmp_path / "staging")
    _stub_tv_preflight(monkeypatch)

    clears: list[str] = []
    monkeypatch.setattr(
        "ripknife.runner.clear_rip_progress",
        lambda: clears.append("clear"),
    )
    monkeypatch.setattr("ripknife.runner.write_rip_progress", lambda *a, **k: None)

    def fake_rip_title(device, title_index, staging, minlength, **kwargs):
        out = staging / f"title_{title_index}.mkv"
        out.write_bytes(b"fake mkv")
        return out

    monkeypatch.setattr("ripknife.runner.rip_title", fake_rip_title)
    monkeypatch.setattr(
        "ripknife.runner.ffmpeg_ops.probe_duration",
        lambda path: 100,
    )
    monkeypatch.setattr(
        "ripknife.runner.verify_track_outputs",
        lambda *a, **k: MagicMock(ok=True),
    )

    manifest = TvDiscManifest.model_validate(_tv_manifest_dict())
    rip_tv_disc(manifest)
    assert len(clears) >= 2  # start + end (MakeMKV also clears in real path)
