"""Tests for post-rip ffprobe validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ripknife.makemkv import ScanResult, TitleInfo
from ripknife.models import MIN_SIZE_FLOOR_BYTES, SplitConfig, SplitOutput, TvTrack
from ripknife.post_rip_verify import (
    PostRipVerifyError,
    duration_tolerance_sec,
    effective_min_size_bytes,
    proportional_size_floor_bytes,
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


def _sized_file(path: Path, size: int) -> None:
    """Create a sparse file of the given byte size (avoids multi-GB write_bytes)."""
    with path.open("wb") as f:
        f.truncate(size)


def test_duration_tolerance_uses_larger_of_pct_or_30s() -> None:
    assert duration_tolerance_sec(600) == 30
    assert duration_tolerance_sec(1000) == 50


def test_size_tolerance_is_10_percent() -> None:
    assert size_tolerance_bytes(1_000_000_000) == 100_000_000


def test_proportional_size_floor_is_50_percent() -> None:
    assert proportional_size_floor_bytes(7_328_693_503) == 3_664_346_751


def test_effective_min_size_caps_to_scan_fraction() -> None:
    scan_size = 5_484_017_664  # feature scan size
    assert effective_min_size_bytes(5_368_709_120, scan_size) == int(scan_size * 0.90)
    assert effective_min_size_bytes(MIN_SIZE_FLOOR_BYTES, scan_size) == MIN_SIZE_FLOOR_BYTES


def test_effective_min_size_without_scan() -> None:
    assert effective_min_size_bytes(5_368_709_120, None) == 5_368_709_120
    assert effective_min_size_bytes(5_368_709_120, 0) == 5_368_709_120


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=6961)
def test_verify_output_irene_sized_rip_passes_with_scan_cap(mock_probe, tmp_path: Path) -> None:
    """Rip slightly under 5 GiB but within scan tolerance should pass when floor is capped."""
    scan_size = 5_484_017_664
    rip_size = 5_367_352_991  # rip slightly under the 5 GiB floor
    path = tmp_path / "movie.mkv"
    _sized_file(path, rip_size)
    eff_min = effective_min_size_bytes(5_368_709_120, scan_size)
    result = verify_output(
        _title(duration_sec=6961, size_bytes=scan_size),
        path,
        min_duration_sec=4800,
        min_size_bytes=eff_min,
    )
    assert rip_size >= eff_min
    assert result.ok


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=690)
def test_verify_output_passes_within_duration_tolerance(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    _sized_file(path, 700_000_000)
    result = verify_output(_title(), path)
    assert result.ok
    mock_probe.assert_called_once_with(path)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=600)
def test_verify_output_fails_duration_mismatch(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    _sized_file(path, 700_000_000)
    result = verify_output(_title(duration_sec=690), path)
    assert not result.ok
    assert any("likely wrong title" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=655)
def test_verify_output_split_first_segment(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "part_a.mkv"
    _sized_file(path, 700_000_000)
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


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=620)
def test_verify_output_split_second_segment_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "part_b.mkv"
    _sized_file(path, 700_000_000)
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


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=690)
def test_verify_output_size_mismatch(mock_probe, tmp_path: Path) -> None:
    """Duration-matched full title: fail only if under 50% of scan size."""
    path = tmp_path / "ep.mkv"
    _sized_file(path, 140_000_000)  # 20% of 700MB
    result = verify_output(_title(), path)
    assert not result.ok
    assert any("truncated" in err for err in result.errors)


# Seamless-branch feature: TINFO sums every referenced m2ts
# (26.3 GB) but the muxed file is 19.9 GB / 5398s exact (29.6% short, ±10% failed).
_BRANCH_SEC = 5398
_BRANCH_SCAN = 28_281_956_352
_BRANCH_RIP = 19_905_131_930  # 70.4% of scan; above 50% floor
_BRANCH_TINY = 5_656_391_270  # 20% of scan


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_BRANCH_SEC)
def test_verify_output_branch_feature_playlist_size_passes(mock_probe, tmp_path: Path) -> None:
    """Seamless-branch playlist size shortfall must pass when duration matches."""
    path = tmp_path / "branch_feature.mkv"
    _sized_file(path, _BRANCH_RIP)
    result = verify_output(
        _title(index=2, duration_sec=_BRANCH_SEC, size_bytes=_BRANCH_SCAN),
        path,
        min_duration_sec=5300,
        min_size_bytes=effective_min_size_bytes(MIN_SIZE_FLOOR_BYTES, _BRANCH_SCAN),
    )
    assert result.ok
    assert result.duration_expected == _BRANCH_SEC
    assert result.size_expected == _BRANCH_SCAN
    mock_probe.assert_called_once_with(path)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_BRANCH_SEC)
def test_verify_output_branch_feature_truncated_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "truncated.mkv"
    _sized_file(path, _BRANCH_TINY)
    result = verify_output(
        _title(index=2, duration_sec=_BRANCH_SEC, size_bytes=_BRANCH_SCAN),
        path,
        min_duration_sec=5300,
        min_size_bytes=effective_min_size_bytes(MIN_SIZE_FLOOR_BYTES, _BRANCH_SCAN),
    )
    assert not result.ok
    assert any("truncated" in err for err in result.errors)


# Duration-exact feature that finished under the 90% scan cap.
# 6756s, mux 19,541,894,857 bytes. TINFO 21,785,567,232 × 0.90 = 19,607,010,508
# (89.70%). The 90% cap deleted a complete rip; 50% still catches truncation.
_MUX_SEC = 6756
_MUX_SCAN = 21_785_567_232
_MUX_RIP = 19_541_894_857  # 89.70% of scan; above 50% floor
_MUX_MIN = 19_607_010_508  # int(scan × 0.90)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_MUX_SEC)
def test_verify_output_duration_exact_under_scan_cap_passes(mock_probe, tmp_path: Path) -> None:
    """Duration-matched mux just under the 90% TINFO cap must pass."""
    path = tmp_path / "duration_exact.mkv"
    _sized_file(path, _MUX_RIP)
    result = verify_output(
        _title(index=0, duration_sec=_MUX_SEC, size_bytes=_MUX_SCAN),
        path,
        min_duration_sec=6000,
        min_size_bytes=effective_min_size_bytes(_MUX_MIN, _MUX_SCAN),
    )
    assert _MUX_RIP < _MUX_MIN
    assert _MUX_RIP >= int(_MUX_SCAN * 0.50)
    assert result.ok
    assert result.errors == ()
    mock_probe.assert_called_once_with(path)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_MUX_SEC)
def test_verify_output_duration_exact_truncated_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "truncated.mkv"
    short = int(_MUX_SCAN * 0.20)
    _sized_file(path, short)
    result = verify_output(
        _title(index=0, duration_sec=_MUX_SEC, size_bytes=_MUX_SCAN),
        path,
        min_duration_sec=6000,
        min_size_bytes=effective_min_size_bytes(_MUX_MIN, _MUX_SCAN),
    )
    assert not result.ok
    assert any("minimum" in err or "truncated" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=3000)
def test_verify_output_duration_exact_wrong_duration_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "wrong_cut.mkv"
    _sized_file(path, _MUX_RIP)
    result = verify_output(
        _title(index=0, duration_sec=_MUX_SEC, size_bytes=_MUX_SCAN),
        path,
        min_duration_sec=6000,
        min_size_bytes=effective_min_size_bytes(_MUX_MIN, _MUX_SCAN),
    )
    assert not result.ok
    assert any("likely wrong title" in err for err in result.errors)
    assert any("bytes < minimum" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=5400)
def test_verify_output_explicit_min_below_scan_cap_still_fails(mock_probe, tmp_path: Path) -> None:
    """A manifest floor under the 90% cap is still a hard minimum."""
    scan = 20_000_000_000
    explicit_min = 12_000_000_000  # below 90% (18 GB), above 50% (10 GB)
    path = tmp_path / "short_of_manifest_min.mkv"
    _sized_file(path, 11_000_000_000)  # above 50%, under the explicit floor
    result = verify_output(
        _title(duration_sec=5400, size_bytes=scan),
        path,
        min_duration_sec=4800,
        min_size_bytes=explicit_min,
    )
    assert not result.ok
    assert any("minimum" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=3000)
def test_verify_output_branch_feature_wrong_duration_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "wrong_cut.mkv"
    _sized_file(path, _BRANCH_RIP)
    result = verify_output(
        _title(index=2, duration_sec=_BRANCH_SEC, size_bytes=_BRANCH_SCAN),
        path,
        min_duration_sec=5300,
        min_size_bytes=effective_min_size_bytes(MIN_SIZE_FLOOR_BYTES, _BRANCH_SCAN),
    )
    assert not result.ok
    assert any("likely wrong title" in err for err in result.errors)


# Play-all title 0: 7354s / 36688367616 bytes.
# CBR chapter estimate for 1469s = 7328693503. One chapter file was ~17% short.
_PLAYALL = dict(index=0, duration_sec=7354, size_bytes=36_688_367_616)
_CHAPTER_SEC = 1469
_CBR_SIZE = 7_328_693_503
_VBR_SMALL = 6_055_604_396  # chapter file 17.4% under CBR, duration OK


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_CHAPTER_SEC)
def test_verify_output_extract_chapter_vbr_smaller_passes(mock_probe, tmp_path: Path) -> None:
    """VBR chapter 17% under CBR estimate must pass when duration is correct."""
    path = tmp_path / "1x04.mkv"
    _sized_file(path, _VBR_SMALL)
    result = verify_output(
        _title(**_PLAYALL),
        path,
        expected_duration_sec=_CHAPTER_SEC,
    )
    assert result.ok
    assert result.duration_expected == _CHAPTER_SEC
    assert result.size_expected == _CBR_SIZE
    mock_probe.assert_called_once_with(path)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_CHAPTER_SEC)
def test_verify_output_extract_chapter_truncated_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "truncated.mkv"
    _sized_file(path, 2_000_000_000)  # ~27% of CBR estimate
    result = verify_output(
        _title(**_PLAYALL),
        path,
        expected_duration_sec=_CHAPTER_SEC,
    )
    assert not result.ok
    assert any("truncated" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=1200)
def test_verify_output_extract_chapter_duration_mismatch_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "wrong_cut.mkv"
    _sized_file(path, _VBR_SMALL)
    result = verify_output(
        _title(**_PLAYALL),
        path,
        expected_duration_sec=_CHAPTER_SEC,
    )
    assert not result.ok
    assert any("likely wrong title" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=_CHAPTER_SEC)
def test_verify_track_outputs_extract_chapter_vbr_keeps_file(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "1x03.mkv"
    _sized_file(path, 6_469_441_212)
    track = TvTrack.model_validate(
        {
            "slot": 3,
            "makemkv_title": 0,
            "episode": "03",
            "title": "Third Chapter",
            "extract_chapter": {"chapter_index": 12, "chapter_end": 17},
        }
    )
    scan = ScanResult(
        raw="",
        titles={0: _title(**_PLAYALL)},
    )
    result = verify_track_outputs(
        scan,
        track,
        [("03", "Third Chapter", path)],
        chapter_duration_sec=_CHAPTER_SEC,
    )
    assert result.ok
    assert path.exists()


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=660)
def test_verify_output_split_vbr_smaller_passes(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "part_a.mkv"
    _sized_file(path, 580_000_000)  # ~17% under 700MB CBR half
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


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=690)
def test_verify_output_skips_size_when_scan_missing(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "ep.mkv"
    _sized_file(path, 1000)
    result = verify_output(_title(size_bytes=0), path)
    assert result.ok


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=5400)
def test_verify_output_movie_minimum_floor(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    _sized_file(path, 6_000_000_000)
    result = verify_output(
        _title(duration_sec=5400, size_bytes=6_000_000_000),
        path,
        min_duration_sec=4800,
        min_size_bytes=5_000_000_000,
    )
    assert result.ok


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration", return_value=3000)
def test_verify_output_movie_minimum_duration_fails(mock_probe, tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    _sized_file(path, 6_000_000_000)
    result = verify_output(
        _title(duration_sec=5400, size_bytes=6_000_000_000),
        path,
        min_duration_sec=4800,
        min_size_bytes=5_000_000_000,
    )
    assert not result.ok
    assert any("minimum" in err for err in result.errors)


@patch("ripknife.post_rip_verify.ffmpeg_ops.probe_duration")
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
