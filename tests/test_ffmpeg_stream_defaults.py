"""Tests for ffmpeg stream-defaults remux helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife.ffmpeg_ops import _pick_default_stream, apply_stream_defaults_or_keep
from ripknife.models import MovieManifest
from ripknife.movie import _postprocess_ripped


def test_pick_default_stream_prefers_first_lang_match() -> None:
    streams = [
        {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "fre"}},
        {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
        {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
    ]
    assert _pick_default_stream(streams, "eng") == 1


def test_pick_default_stream_prefers_lossless_when_requested() -> None:
    streams = [
        {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
        {"codec_type": "audio", "codec_name": "truehd", "tags": {"language": "eng"}},
    ]
    assert _pick_default_stream(streams, "eng", prefer_lossless=True) == 1


def test_movie_postprocess_skipped_without_stream_defaults(tmp_path: Path) -> None:
    ripped = tmp_path / "feature.mkv"
    ripped.write_bytes(b"x")
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test", "title": "Test"},
            "output": {"dir": "Movies", "filename": "Test.mkv"},
        }
    )
    assert _postprocess_ripped(ripped, tmp_path, manifest) == ripped


def test_movie_postprocess_calls_ffmpeg_when_audio_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ripped = tmp_path / "feature.mkv"
    ripped.write_bytes(b"x")
    processed = tmp_path / ".defaults_feature.mkv"
    processed.write_bytes(b"y")
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test", "title": "Test"},
            "output": {"dir": "Movies", "filename": "Test.mkv"},
            "stream_defaults": {"keep_all_tracks": True, "audio_default": "eng"},
        }
    )

    def _fake_apply(src: Path, dest: Path, **kwargs: object) -> Path:
        assert src == ripped
        assert kwargs["audio_lang"] == "eng"
        assert kwargs["subtitle_lang"] is None
        return processed

    monkeypatch.setattr(
        "ripknife.movie.ffmpeg_ops.apply_stream_defaults_or_keep",
        _fake_apply,
    )
    assert _postprocess_ripped(ripped, tmp_path, manifest) == processed


def test_apply_stream_defaults_or_keep_moves_when_no_langs(tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"a")
    dest = tmp_path / "b.mkv"
    assert apply_stream_defaults_or_keep(src, dest) == dest
    assert dest.read_bytes() == b"a"
    assert not src.exists()
