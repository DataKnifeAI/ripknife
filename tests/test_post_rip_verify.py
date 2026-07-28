"""Tests for post-rip ffprobe validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from videosripper.makemkv import ScanResult, TitleInfo
from videosripper.models import MIN_SIZE_FLOOR_BYTES, SplitConfig, SplitOutput, TvTrack
from videosripper.post_rip_verify import (
    PostRipVerifyError,
    duration_tolerance_sec,
    effective_min_size_bytes,
    size_tolerance_bytes,
    verify_output,
    verify_track_outputs,
    warn_title_count_explosion,
)


def _title(**kwargs) -> TitleInfo:
    defaults = dict(
        index=1,
        name="Episode",
        duration="0:11:30",
        duration_sec=690,
        size_bytes=700_000_000,
    )
    defaults.update(kwargs)
    return TitleInfo(**defaults)


def test_duration_tolerance_uses_larger_of_pct_or_30s() -> None:
    assert duration_tolerance_sec(600) == 30
    assert duration_tolerance_sec(1000) == 50


def test_size_tolerance_is_10_percent() -> None:
    assert size_tolerance_bytes(1_000_000_000) == 100_000_000


def test_effective_min_size_caps_to_scan_fraction() -> None:
    scan_size = 5_484_017_664  # Me, Myself & Irene feature
    assert effective_min_size_bytes(5_368_709_120, scan_size) == int(scan_size * 0.90)
    assert effective_min_size_bytes(MIN_SIZE_FLOOR_BYTES, scan_size) == MIN_SIZE_FLOOR_BYTES


def test_effective_min_size_without_scan() -> None:
    assert effective_min_size_bytes(5_368_709_120, None) == 5_368_709_120
    assert effective_min_size_bytes(5_368_709_120, 0) == 5_368_709_120


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=6961)
def test_verify_output_irene_sized_rip_passes_with_scan_cap(mock_probe, tmp_path: Path) -> None:
    """Rip slightly under 5 GiB but within scan tolerance should pass when floor is capped."""
    scan_size = 5_484_017_664
    rip_size = 5_367_352_991  # actual Irene failure size from logs
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * rip_size)
    eff_min = effective_min_size_bytes(5_368_709_120, scan_size)
    result = verify_output(
        _title(duration_sec=6961, size_bytes=scan_size),
        path,
        min_duration_sec=4800,
        min_size_bytes=eff_min,
    )
    assert rip_size >= eff_min
    assert result.ok


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=690)
def test_verify_output_passes_within_duration_tolerance(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    path.write_bytes(b"x" * 700_000_000)
    result = verify_output(_title(), path)
    assert result.ok
    mock_probe.assert_called_once_with(path)


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=600)
def test_verify_output_fails_duration_mismatch(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    path.write_bytes(b"x" * 700_000_000)
    result = verify_output(_title(duration_sec=690), path)
    assert not result.ok
    assert any("likely wrong title" in err for err in result.errors)


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=655)
def test_verify_output_split_first_segment(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "part_a.mkv"
    path.write_bytes(b"x" * 700_000_000)
    split = SplitConfig(
        at_seconds=660.0,
        outputs=[
            SplitOutput(episode="01", title="Part A"),
            SplitOutput(episode="02", title="Part B"),
        ],
    )
    result = verify_output(
        _title(duration_sec=1320, size_bytes=1_400_000_000),
        path,
        split_config=split,
        split_episode="01",
    )
    assert result.ok
    assert result.duration_expected == 660


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=620)
def test_verify_output_split_second_segment_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "part_b.mkv"
    path.write_bytes(b"x" * 700_000_000)
    split = SplitConfig(
        at_seconds=660.0,
        outputs=[
            SplitOutput(episode="01", title="Part A"),
            SplitOutput(episode="02", title="Part B"),
        ],
    )
    result = verify_output(
        _title(duration_sec=1320, size_bytes=1_400_000_000),
        path,
        split_config=split,
        split_episode="02",
    )
    assert not result.ok
    assert result.duration_expected == 660


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=690)
def test_verify_output_size_mismatch(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    path.write_bytes(b"x" * 500_000_000)
    result = verify_output(_title(), path)
    assert not result.ok
    assert any("size" in err for err in result.errors)


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=690)
def test_verify_output_skips_size_when_scan_missing(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    path.write_bytes(b"x" * 1000)
    result = verify_output(_title(size_bytes=0), path)
    assert result.ok


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=5400)
def test_verify_output_movie_minimum_floor(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * 6_000_000_000)
    result = verify_output(
        _title(duration_sec=5400, size_bytes=6_000_000_000),
        path,
        min_duration_sec=4800,
        min_size_bytes=5_000_000_000,
    )
    assert result.ok


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration", return_value=3000)
def test_verify_output_movie_minimum_duration_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"x" * 6_000_000_000)
    result = verify_output(
        _title(duration_sec=5400, size_bytes=6_000_000_000),
        path,
        min_duration_sec=4800,
        min_size_bytes=5_000_000_000,
    )
    assert not result.ok
    assert any("minimum" in err for err in result.errors)


@patch("videosripper.post_rip_verify.ffmpeg_ops.probe_duration")
def test_verify_track_outputs_deletes_all_on_failure(mock_probe, tmp_path: Path) -> None:
    mock_probe.side_effect = [330, 620]
    out1 = tmp_path / "a.mkv"
    out2 = tmp_path / "b.mkv"
    out1.write_bytes(b"a" * 100)
    out2.write_bytes(b"b" * 100)
    track = TvTrack.model_validate(
        {
            "slot": 1,
            "makemkv_title": 1,
            "split": {
                "at_seconds": 660.0,
                "outputs": [
                    {"episode": "01", "title": "A"},
                    {"episode": "02", "title": "B"},
                ],
            },
        }
    )
    scan = ScanResult(
        raw="",
        titles={1: _title(duration_sec=1320, size_bytes=1_400_000_000)},
    )
    outputs = [("01", "A", out1), ("02", "B", out2)]
    result = verify_track_outputs(scan, track, outputs)
    assert not result.ok
    assert not out1.exists()
    assert not out2.exists()


def test_post_rip_verify_error_is_runtime_error() -> None:
    assert issubclass(PostRipVerifyError, RuntimeError)


def test_warn_title_count_explosion(caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    warn_title_count_explosion(51)
    assert any(">50" in rec.message for rec in caplog.records)
    caplog.clear()
    warn_title_count_explosion(50)
    assert not caplog.records
