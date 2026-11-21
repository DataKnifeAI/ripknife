"""Episode filenames must be a single path component inside Season N."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife import paths
from ripknife.models import TvDiscManifest
from ripknife.runner import episode_filename, track_outputs


def _manifest(*, show: str, title: str) -> TvDiscManifest:
    return TvDiscManifest.model_validate(
        {
            "kind": "tv_disc",
            "meta": {
                "id": "slash-title",
                "show": show,
                "season": 1,
                "device": "dev:/dev/sr0",
                "minlength": 300,
            },
            "output": {"season_dir": f"{show}/Season 1"},
            "tracks": [
                {
                    "slot": 1,
                    "makemkv_title": 0,
                    "episode": "01",
                    "title": title,
                }
            ],
            "post": {"eject": False, "copy_to_nas": False},
        }
    )


def test_slashed_title_is_one_file_in_season_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    show = "Example Show"
    title = "Left Half / Right Half"
    manifest = _manifest(show=show, title=title)
    _, _, path = track_outputs(manifest, manifest.tracks[0])[0]

    season_dir = tmp_path / "output" / show / "Season 1"
    expected = "Example Show - 1x01 - Left Half - Right Half.mkv"
    assert path.parent == season_dir
    assert path.name == expected
    assert "Left Half" in path.name
    assert "Right Half" in path.name
    assert "/" not in path.name
    assert "\\" not in path.name

    path.parent.mkdir(parents=True)
    path.write_bytes(b"mkv")
    entries = list(season_dir.iterdir())
    assert entries == [path]
    assert path.is_file()
    assert not any(entry.is_dir() for entry in entries)


def test_show_slash_and_punctuation_stay_one_component() -> None:
    name = episode_filename("A / B", 1, "02", r"Who? What: C \ D")
    assert name == "A - B - 1x02 - Who? What: C - D.mkv"
    assert Path(name).name == name
    assert "?" in name
    assert ":" in name
