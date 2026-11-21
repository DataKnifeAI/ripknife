"""extract_chapter range bounds and model validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ripknife.ffmpeg_ops import (
    chapter_bounds,
    ffmpeg_time_pct,
    parse_ffmpeg_out_time_us,
    validate_extract_chapter_ranges,
)
from ripknife.models import ExtractChapterConfig, TvDiscManifest, TvTrack


def test_extract_chapter_config_allows_range() -> None:
    cfg = ExtractChapterConfig(chapter_index=0, chapter_end=4)
    assert cfg.chapter_end == 4


def test_extract_chapter_config_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="chapter_end"):
        ExtractChapterConfig(chapter_index=5, chapter_end=4)


def test_tv_track_extract_chapter_range() -> None:
    track = TvTrack(
        slot=1,
        makemkv_title=0,
        episode="01",
        title="Hidden Inventory",
        extract_chapter=ExtractChapterConfig(chapter_index=0, chapter_end=4),
    )
    assert track.extract_chapter is not None
    assert track.extract_chapter.chapter_end == 4


def test_chapter_bounds_range(tmp_path: Path) -> None:
    fake = tmp_path / "playall.mkv"
    fake.write_bytes(b"")
    lines = [
        "0.0,10.0",
        "10.0,20.0",
        "20.0,35.0",
        "35.0,50.0",
    ]
    with patch("ripknife.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        start, end = chapter_bounds(fake, 1, chapter_end=2)
    assert start == 10.0
    assert end == 35.0


def test_chapter_bounds_single(tmp_path: Path) -> None:
    fake = tmp_path / "playall.mkv"
    fake.write_bytes(b"")
    lines = ["0.0,10.0", "10.0,25.0"]
    with patch("ripknife.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        start, end = chapter_bounds(fake, 1)
    assert start == 10.0
    assert end == 25.0


def test_chapter_bounds_off_by_one_hint(tmp_path: Path) -> None:
    fake = tmp_path / "title_4.mkv"
    fake.write_bytes(b"")
    lines = [f"{i}.0,{i + 1}.0" for i in range(13)]
    with patch("ripknife.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        with pytest.raises(RuntimeError, match="use 12 for the last chapter"):
            chapter_bounds(fake, 9, chapter_end=13)


def test_validate_extract_chapter_ranges_ok(tmp_path: Path) -> None:
    fake = tmp_path / "playall.mkv"
    fake.write_bytes(b"")
    lines = [f"{i}.0,{i + 1}.0" for i in range(13)]
    with patch("ripknife.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        count = validate_extract_chapter_ranges(
            fake,
            [(2, 0, 2), (3, 3, 5), (4, 6, 8), (5, 9, 12)],
        )
    assert count == 13


def test_validate_extract_chapter_ranges_fail_fast(tmp_path: Path) -> None:
    fake = tmp_path / "title_4.mkv"
    fake.write_bytes(b"")
    lines = [f"{i}.0,{i + 1}.0" for i in range(13)]
    with patch("ripknife.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        with pytest.raises(RuntimeError, match="slot 5 .*use 12 for the last chapter"):
            validate_extract_chapter_ranges(
                fake,
                [(2, 0, 2), (3, 3, 5), (4, 6, 8), (5, 9, 13)],
            )


def test_tv_manifest_rejects_overlapping_extract_chapter() -> None:
    data = {
        "kind": "tv_disc",
        "meta": {"id": "overlap-disc", "show": "Test", "season": 1},
        "output": {"season_dir": "Season 1"},
        "tracks": [
            {
                "slot": 1,
                "makemkv_title": 4,
                "episode": "01",
                "title": "A",
                "extract_chapter": {"chapter_index": 0, "chapter_end": 3},
            },
            {
                "slot": 2,
                "makemkv_title": 4,
                "episode": "02",
                "title": "B",
                "extract_chapter": {"chapter_index": 3, "chapter_end": 5},
            },
        ],
    }
    with pytest.raises(ValidationError, match="extract_chapter ranges overlap"):
        TvDiscManifest.model_validate(data)


def test_parse_ffmpeg_out_time_us() -> None:
    assert parse_ffmpeg_out_time_us("out_time_us=5000000\n") == 5_000_000
    assert parse_ffmpeg_out_time_us("frame=12") is None
    assert parse_ffmpeg_out_time_us("out_time_us=N/A") is None


def test_ffmpeg_time_pct_clamps() -> None:
    assert ffmpeg_time_pct(0, 10_000_000) == 0
    assert ffmpeg_time_pct(5_000_000, 10_000_000) == 50
    assert ffmpeg_time_pct(20_000_000, 10_000_000) == 100
    assert ffmpeg_time_pct(1, 0) == 0
