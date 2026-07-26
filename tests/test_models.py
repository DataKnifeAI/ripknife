"""Unit tests for videosripper.models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from videosripper import paths
from videosripper.models import (
    MovieManifest,
    RetryConfig,
    SplitConfig,
    StreamDefaultsConfig,
    TvDiscManifest,
    TvTrack,
    load_manifest,
    movie_output_path,
    tv_nas_season_dir,
    tv_output_dir,
)

ROOT = Path(__file__).resolve().parents[1]


def _minimal_tv_manifest(**overrides: object) -> dict:
    data = {
        "kind": "tv_disc",
        "meta": {
            "id": "test-disc",
            "show": "Test Show",
            "season": 1,
        },
        "output": {"season_dir": "Season 1"},
        "tracks": [
            {"slot": 1, "makemkv_title": 1, "episode": "01", "title": "Pilot"},
        ],
    }
    data.update(overrides)
    return data


def test_tv_disc_manifest_valid() -> None:
    manifest = TvDiscManifest.model_validate(_minimal_tv_manifest())
    assert manifest.meta.show == "Test Show"
    assert len(manifest.tracks) == 1
    assert manifest.retry.count == 3


def test_retry_config_default_is_three_retries() -> None:
    assert RetryConfig().count == 3
    assert RetryConfig().delay_sec == 30.0
    assert RetryConfig().stall_retry is True


def test_tv_disc_requires_season_dir() -> None:
    data = _minimal_tv_manifest(output={})
    with pytest.raises(ValidationError, match="season_dir"):
        TvDiscManifest.model_validate(data)


def test_tv_track_requires_episode_and_title() -> None:
    with pytest.raises(ValidationError, match="episode and title"):
        TvTrack.model_validate({"slot": 1, "makemkv_title": 1})


def test_split_outputs_must_be_two() -> None:
    with pytest.raises(ValidationError, match="exactly 2 episodes"):
        SplitConfig.model_validate(
            {
                "at_seconds": 600.0,
                "outputs": [{"episode": "01", "title": "Part A"}],
            }
        )


def test_split_track_rejects_parent_episode_title() -> None:
    with pytest.raises(ValidationError, match="must not set episode/title"):
        TvTrack.model_validate(
            {
                "slot": 1,
                "makemkv_title": 1,
                "episode": "01",
                "title": "Combined",
                "split": {
                    "at_seconds": 600.0,
                    "outputs": [
                        {"episode": "01", "title": "Part A"},
                        {"episode": "02", "title": "Part B"},
                    ],
                },
            }
        )


def test_track_slots_must_be_unique() -> None:
    data = _minimal_tv_manifest(
        tracks=[
            {"slot": 1, "makemkv_title": 1, "episode": "01", "title": "A"},
            {"slot": 1, "makemkv_title": 2, "episode": "02", "title": "B"},
        ]
    )
    with pytest.raises(ValidationError, match="unique"):
        TvDiscManifest.model_validate(data)


def test_load_manifest_tv_disc() -> None:
    manifest = load_manifest(ROOT / "configs/spongebob/s8-disc1.yaml")
    assert isinstance(manifest, TvDiscManifest)
    assert manifest.meta.season == 8
    assert len(manifest.tracks) == 11


def test_load_manifest_movie() -> None:
    manifest = load_manifest(ROOT / "configs/movies/ghost-rider-spirit-of-vengeance.yaml")
    assert isinstance(manifest, MovieManifest)
    assert manifest.meta.title.startswith("Ghost Rider")
    assert manifest.stream_defaults is not None
    assert manifest.stream_defaults.audio_default == "eng"
    assert manifest.stream_defaults.subtitle_default is None
    assert manifest.stream_defaults.keep_all_tracks is True


def test_load_hudson_hawk_movie_stream_defaults() -> None:
    manifest = load_manifest(ROOT / "configs/movies/hudson-hawk-1991.yaml")
    assert isinstance(manifest, MovieManifest)
    assert manifest.stream_defaults is not None
    assert manifest.stream_defaults.audio_default == "eng"
    assert manifest.skips
    assert manifest.skips[0].makemkv_title == 1


def test_movie_fixed_title_requires_title() -> None:
    with pytest.raises(ValidationError, match="title_selection.title"):
        MovieManifest.model_validate(
            {
                "kind": "movie",
                "meta": {"id": "test-movie", "title": "Test Movie"},
                "output": {"dir": "Movies", "filename": "Test.mkv"},
                "title_selection": {"mode": "fixed"},
            }
        )


def test_tv_output_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    manifest = TvDiscManifest.model_validate(_minimal_tv_manifest())
    assert tv_output_dir(manifest) == tmp_path / "output" / "Season 1"


def test_movie_output_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test-movie", "title": "Test Movie"},
            "output": {"dir": "Movies", "filename": "Test.mkv"},
        }
    )
    assert movie_output_path(manifest) == tmp_path / "output" / "Movies" / "Test.mkv"


def test_stream_defaults_requires_option() -> None:
    with pytest.raises(ValidationError, match="stream_defaults requires"):
        StreamDefaultsConfig.model_validate({})


def test_stream_defaults_audio_only_omits_subtitle() -> None:
    cfg = StreamDefaultsConfig.model_validate(
        {"keep_all_tracks": True, "audio_default": "eng"}
    )
    assert cfg.audio_default == "eng"
    assert cfg.subtitle_default is None
    assert cfg.keep_all_tracks is True


def test_load_breaking_bad_manifest() -> None:
    manifest = load_manifest(ROOT / "configs/shows/breaking-bad-s1-disc1.yaml")
    assert isinstance(manifest, TvDiscManifest)
    assert manifest.meta.id == "breaking-bad-s1-disc1"
    assert manifest.meta.show == "Breaking Bad"
    assert manifest.meta.season == 1
    assert manifest.nas.share == "/mnt/tn-sas/Shows/Breaking Bad"
    assert manifest.nas.subdir == "Season 1"
    assert len(manifest.tracks) == 4
    assert manifest.tracks[0].episode == "01"
    assert manifest.tracks[-1].episode == "04"
    assert manifest.stream_defaults is not None
    assert manifest.stream_defaults.audio_default == "eng"
    assert manifest.stream_defaults.subtitle_default is None
    assert manifest.stream_defaults.keep_all_tracks is True


def test_tv_nas_season_dir_uses_subdir() -> None:
    manifest = TvDiscManifest.model_validate(
        _minimal_tv_manifest(
            output={"season_dir": "Naruto Shippuden/Season 12"},
            nas={"share": "/mnt/tn-sas/Anime/Naruto Shippuden", "subdir": "Season 12"},
        )
    )
    assert tv_nas_season_dir(manifest) == "Season 12"


def test_track_season_override_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    manifest = TvDiscManifest.model_validate(
        {
            "kind": "tv_disc",
            "meta": {
                "id": "naruto-set10-d4",
                "show": "Naruto Shippuden",
                "season": 12,
            },
            "output": {"season_dir": "Naruto Shippuden/Season 12"},
            "nas": {
                "share": "/mnt/tn-sas/Anime/Naruto Shippuden",
                "subdir": "Season 12",
            },
            "tracks": [
                {"slot": 1, "makemkv_title": 0, "episode": "270", "title": "Golden Bonds"},
                {
                    "slot": 7,
                    "makemkv_title": 6,
                    "episode": "276",
                    "title": "Attack of the Gedo Statue",
                    "season": 13,
                },
            ],
        }
    )
    assert tv_output_dir(manifest, 13) == tmp_path / "output/Naruto Shippuden/Season 13"
    assert tv_nas_season_dir(manifest, 13) == "Season 13"
    assert manifest.tracks[-1].season == 13


def test_load_naruto_manifest() -> None:
    manifest = load_manifest(ROOT / "configs/anime/naruto-shippuden-set10-disc1.yaml")
    assert isinstance(manifest, TvDiscManifest)
    assert manifest.meta.id == "naruto-shippuden-set10-disc1"
    assert manifest.meta.season == 12
    assert manifest.output.season_dir == "Naruto Shippuden/Season 12"
    assert manifest.nas.subdir == "Season 12"
    assert len(manifest.tracks) == 7
    assert manifest.tracks[0].episode == "249"
    assert manifest.tracks[-1].episode == "255"
    assert manifest.stream_defaults is not None
    assert manifest.stream_defaults.audio_default == "jpn"
    assert manifest.stream_defaults.subtitle_default == "eng"
    assert manifest.stream_defaults.keep_all_tracks is True
