"""Tests for cartoon/animation → Anime NAS routing helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife.nas import (
    anime_movies_destination,
    classify_library,
    nas_share_for,
)
from ripknife.paths import ANIME_NAS, MOVIES_NAS, SHOWS_NAS


def test_classify_library_animation_is_always_anime() -> None:
    assert classify_library(kind="tv_disc", animated=True) == "anime"
    assert classify_library(kind="movie", animated=True) == "anime"


def test_classify_library_live_action() -> None:
    assert classify_library(kind="tv_disc", animated=False) == "shows"
    assert classify_library(kind="movie", animated=False) == "movies"


def test_classify_library_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        classify_library(kind="extra", animated=True)


def test_nas_share_for_cartoon_series() -> None:
    assert nas_share_for(kind="tv_disc", animated=True, show="Example Cartoon") == (
        ANIME_NAS / "Example Cartoon"
    )
    assert nas_share_for(
        kind="tv_disc", animated=True, show="Example Anime"
    ) == ANIME_NAS / "Example Anime"


def test_nas_share_for_live_action_tv() -> None:
    assert nas_share_for(kind="tv_disc", animated=False, show="Example Show") == (
        SHOWS_NAS / "Example Show"
    )


def test_nas_share_for_live_action_movie() -> None:
    assert nas_share_for(kind="movie", animated=False) == MOVIES_NAS


def test_nas_share_for_animated_movie_uses_anime_movies_dir(
    tmp_path: Path,
) -> None:
    dest = nas_share_for(kind="movie", animated=True, anime_root=tmp_path)
    assert dest == tmp_path / "Movies"


def test_nas_share_for_animated_movie_uses_movies_even_if_dir_exists(
    tmp_path: Path,
) -> None:
    movies = tmp_path / "Movies"
    movies.mkdir()
    assert nas_share_for(kind="movie", animated=True, anime_root=tmp_path) == movies


def test_anime_movies_destination_is_movies_subdir(tmp_path: Path) -> None:
    assert anime_movies_destination(tmp_path) == tmp_path / "Movies"


def test_nas_share_for_requires_show_on_tv() -> None:
    with pytest.raises(ValueError, match="show name"):
        nas_share_for(kind="tv_disc", animated=True)
    with pytest.raises(ValueError, match="show name"):
        nas_share_for(kind="tv_disc", animated=False)
