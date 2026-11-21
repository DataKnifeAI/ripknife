"""Unit tests for ripknife.models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ripknife import paths
from ripknife.models import (
    MIN_SIZE_FLOOR_BYTES,
    MovieManifest,
    RetryConfig,
    SplitConfig,
    SplitOutput,
    StreamDefaultsConfig,
    TitleSelection,
    TvDiscManifest,
    TvTrack,
    ValidationRules,
    apply_edition_to_filename,
    load_manifest,
    movie_output_filename,
    movie_output_path,
    part_filename_for,
    sanitize_edition,
    stitch_output_filename,
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
    assert manifest.post.delete_local_after_nas is False


def test_tv_plex_local_optional() -> None:
    manifest = TvDiscManifest.model_validate(_minimal_tv_manifest())
    assert manifest.plex is None
    with_plex = TvDiscManifest.model_validate(
        _minimal_tv_manifest(plex={"local": True, "year": 2011})
    )
    assert with_plex.plex is not None
    assert with_plex.plex.local is True
    assert with_plex.plex.year == 2011
    assert with_plex.plex.title is None


def test_post_delete_local_after_nas_from_manifest() -> None:
    manifest = TvDiscManifest.model_validate(
        _minimal_tv_manifest(post={"delete_local_after_nas": True})
    )
    assert manifest.post.delete_local_after_nas is True
    assert manifest.retry.count == 1


def test_retry_config_default_is_one_retry() -> None:
    assert RetryConfig().count == 1
    assert RetryConfig().delay_sec == 30.0
    assert RetryConfig().stall_retry is True


def test_tv_disc_requires_season_dir() -> None:
    data = _minimal_tv_manifest(output={})
    with pytest.raises(ValidationError, match="season_dir"):
        TvDiscManifest.model_validate(data)


def test_tv_track_requires_episode_and_title() -> None:
    with pytest.raises(ValidationError, match="episode and title"):
        TvTrack.model_validate({"slot": 1, "makemkv_title": 1})


def test_tv_track_forbids_extra_keys_from_unquoted_comma_title() -> None:
    # Flow YAML `{ title: Zip, Zap }` becomes title="Zip" plus extra key Zap.
    with pytest.raises(ValidationError, match="extra"):
        TvTrack.model_validate(
            {"slot": 22, "makemkv_title": 22, "episode": "22", "title": "Zip", "Zap": None}
        )


def test_split_output_forbids_extra_keys_from_unquoted_comma_title() -> None:
    with pytest.raises(ValidationError, match="extra"):
        SplitOutput.model_validate(
            {"episode": "08", "title": "Farmer-Hunter", "Farmer-Hunted": None}
        )


def test_quoted_comma_title_round_trips() -> None:
    track = TvTrack.model_validate(
        {"slot": 22, "makemkv_title": 22, "episode": "22", "title": "Zip, Zap"}
    )
    assert track.title == "Zip, Zap"


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
    manifest = load_manifest(ROOT / "configs/examples/cartoon-s1-disc1.yaml")
    assert isinstance(manifest, TvDiscManifest)
    assert manifest.meta.season == 1
    assert len(manifest.tracks) == 2


def test_load_manifest_movie() -> None:
    manifest = load_manifest(ROOT / "configs/examples/movie.yaml")
    assert isinstance(manifest, MovieManifest)
    assert manifest.meta.title.startswith("Example Movie")
    assert manifest.stream_defaults is not None
    assert manifest.stream_defaults.audio_default == "eng"
    assert manifest.stream_defaults.subtitle_default is None
    assert manifest.stream_defaults.keep_all_tracks is True


def test_load_movie_stream_defaults_and_skip() -> None:
    manifest = load_manifest(ROOT / "configs/examples/movie.yaml")
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


def test_movie_default_min_size_is_one_gib() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test-movie", "title": "Test Movie"},
            "output": {"dir": "Movies", "filename": "Test.mkv"},
        }
    )
    assert manifest.validation.min_size_bytes == MIN_SIZE_FLOOR_BYTES
    assert manifest.title_selection.min_size_bytes == MIN_SIZE_FLOOR_BYTES
    assert TitleSelection().min_size_bytes == MIN_SIZE_FLOOR_BYTES
    assert ValidationRules().min_size_bytes == MIN_SIZE_FLOOR_BYTES


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


def test_sanitize_edition_strips_braces_and_unsafe() -> None:
    assert sanitize_edition("Extended Edition") == "Extended Edition"
    assert sanitize_edition("Director's Cut") == "Director's Cut"
    assert sanitize_edition("{edition-Final Cut}") == "edition-Final Cut"
    assert sanitize_edition('Unrated / "Special"') == "Unrated Special"
    assert sanitize_edition("  Theatrical  ") == "Theatrical"
    assert sanitize_edition("{}[]") == ""


def test_apply_edition_to_filename_inserts_plex_tag() -> None:
    assert (
        apply_edition_to_filename("Example Movie (1982).mkv", "Director's Cut")
        == "Example Movie (1982) {edition-Director's Cut}.mkv"
    )
    assert (
        apply_edition_to_filename(
            "Example Feature (2001) - Disc 1.mkv", "Extended Edition"
        )
        == "Example Feature (2001) {edition-Extended Edition} - Disc 1.mkv"
    )


def test_apply_edition_to_filename_skips_duplicate_and_empty() -> None:
    already = "Example Movie (1982) {edition-Final Cut}.mkv"
    assert apply_edition_to_filename(already, "Director's Cut") == already
    assert apply_edition_to_filename("Movie (1999).mkv", None) == "Movie (1999).mkv"
    assert apply_edition_to_filename("Movie (1999).mkv", "   ") == "Movie (1999).mkv"
    assert apply_edition_to_filename("Movie (1999).mkv", "{}") == "Movie (1999).mkv"


def test_apply_edition_to_filename_replaces_plain_suffix() -> None:
    assert (
        apply_edition_to_filename(
            "Example Feature (2001) Extended Edition.mkv", "Extended Edition"
        )
        == "Example Feature (2001) {edition-Extended Edition}.mkv"
    )
    assert (
        apply_edition_to_filename(
            "Example Feature (2001) Extended Edition - Disc 1.mkv", "Extended Edition"
        )
        == "Example Feature (2001) {edition-Extended Edition} - Disc 1.mkv"
    )


def test_movie_output_filename_uses_edition() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "example-movie-1982", "title": "Example Movie (1982)"},
            "output": {
                "dir": "Movies",
                "filename": "Example Movie (1982).mkv",
                "edition": "Director's Cut",
            },
        }
    )
    assert movie_output_filename(manifest) == (
        "Example Movie (1982) {edition-Director's Cut}.mkv"
    )


def test_part_and_stitch_filenames_include_edition() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {
                "id": "example-feature-2001-extended-disc1",
                "title": "Example Feature (2001)",
                "part": 1,
            },
            "output": {
                "dir": "Movies",
                "filename": (
                    "Example Feature (2001) - Disc 1.mkv"
                ),
                "edition": "Extended Edition",
            },
            "post": {"copy_to_nas": False},
            "stitch": {
                "enabled": True,
                "group_id": "example-feature-2001-extended",
                "expected_parts": 2,
                "output_filename": (
                    "Example Feature (2001).mkv"
                ),
                "copy_to_nas": True,
            },
        }
    )
    assert part_filename_for(manifest, 1) == (
        "Example Feature (2001) "
        "{edition-Extended Edition} - Disc 1.mkv"
    )
    assert part_filename_for(manifest, 2) == (
        "Example Feature (2001) "
        "{edition-Extended Edition} - Disc 2.mkv"
    )
    assert stitch_output_filename(manifest) == (
        "Example Feature (2001) "
        "{edition-Extended Edition}.mkv"
    )
    assert " - Disc " not in stitch_output_filename(manifest)


def test_stitch_output_filename_strips_disc_n() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {
                "id": "example-feature-2001-extended-disc1",
                "title": "Example Feature (2001)",
                "part": 1,
            },
            "output": {
                "dir": "Movies",
                "filename": (
                    "Example Feature (2001) - Disc 1.mkv"
                ),
                "edition": "Extended Edition",
            },
            "post": {"copy_to_nas": False},
            "stitch": {
                "enabled": True,
                "expected_parts": 2,
                "output_filename": (
                    "Example Feature (2001) - Disc 1.mkv"
                ),
            },
        }
    )
    assert stitch_output_filename(manifest) == (
        "Example Feature (2001) "
        "{edition-Extended Edition}.mkv"
    )
    assert " - Disc " not in stitch_output_filename(manifest)


def test_output_edition_blank_becomes_none() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test-movie", "title": "Test Movie (2020)"},
            "output": {"dir": "Movies", "filename": "Test Movie (2020).mkv", "edition": ""},
        }
    )
    assert manifest.output.edition is None
    assert movie_output_filename(manifest) == "Test Movie (2020).mkv"


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


def test_load_example_show_manifest() -> None:
    manifest = load_manifest(ROOT / "configs/examples/show-s1-disc1.yaml")
    assert isinstance(manifest, TvDiscManifest)
    assert manifest.meta.id == "example-show-s1-disc1"
    assert manifest.meta.show == "Example Show"
    assert manifest.meta.season == 1
    assert manifest.nas.share == "/path/to/library/Shows/Example Show"
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
            output={"season_dir": "Example Anime/Season 12"},
            nas={"share": "/path/to/library/Anime/Example Anime", "subdir": "Season 12"},
        )
    )
    assert tv_nas_season_dir(manifest) == "Season 12"


def test_track_season_override_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    manifest = TvDiscManifest.model_validate(
        {
            "kind": "tv_disc",
            "meta": {
                "id": "example-anime-d4",
                "show": "Example Anime",
                "season": 12,
            },
            "output": {"season_dir": "Example Anime/Season 12"},
            "nas": {
                "share": "/path/to/library/Anime/Example Anime",
                "subdir": "Season 12",
            },
            "tracks": [
                {"slot": 1, "makemkv_title": 0, "episode": "270", "title": "Earlier Chapter"},
                {
                    "slot": 7,
                    "makemkv_title": 6,
                    "episode": "276",
                    "title": "Later Chapter",
                    "season": 13,
                },
            ],
        }
    )
    assert tv_output_dir(manifest, 13) == tmp_path / "output/Example Anime/Season 13"
    assert tv_nas_season_dir(manifest, 13) == "Season 13"
    assert manifest.tracks[-1].season == 13


def test_load_example_anime_manifest() -> None:
    manifest = load_manifest(ROOT / "configs/examples/anime-s1-disc1.yaml")
    assert isinstance(manifest, TvDiscManifest)
    assert manifest.meta.id == "example-anime-s1-disc1"
    assert manifest.meta.season == 12
    assert manifest.output.season_dir == "Example Anime/Season 12"
    assert manifest.nas.subdir == "Season 12"
    assert len(manifest.tracks) == 7
    assert manifest.tracks[0].episode == "249"
    assert manifest.tracks[-1].episode == "255"
    assert manifest.stream_defaults is not None
    assert manifest.stream_defaults.audio_default == "jpn"
    assert manifest.stream_defaults.subtitle_default == "eng"
    assert manifest.stream_defaults.keep_all_tracks is True
