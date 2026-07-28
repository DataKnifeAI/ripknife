"""extract_chapter range bounds and model validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from videosripper.ffmpeg_ops import chapter_bounds
from videosripper.models import ExtractChapterConfig, TvTrack


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
    with patch("videosripper.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        start, end = chapter_bounds(fake, 1, chapter_end=2)
    assert start == 10.0
    assert end == 35.0


def test_chapter_bounds_single(tmp_path: Path) -> None:
    fake = tmp_path / "playall.mkv"
    fake.write_bytes(b"")
    lines = ["0.0,10.0", "10.0,25.0"]
    with patch("videosripper.ffmpeg_ops._probe_chapter_lines", return_value=lines):
        start, end = chapter_bounds(fake, 1)
    assert start == 10.0
    assert end == 25.0
