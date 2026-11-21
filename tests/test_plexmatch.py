"""Local-only .plexmatch writer."""

from __future__ import annotations

from pathlib import Path

from ripknife.models import PlexConfig, TvDiscManifest
from ripknife.plexmatch import (
    plex_title_for,
    render_local_plexmatch,
    write_from_manifest,
    write_local_plexmatch,
)


def _tv(**overrides: object) -> TvDiscManifest:
    data: dict = {
        "kind": "tv_disc",
        "meta": {"id": "test-disc", "show": "Test Show", "season": 1},
        "output": {"season_dir": "Season 1"},
        "tracks": [
            {"slot": 1, "makemkv_title": 1, "episode": "01", "title": "Pilot"},
        ],
    }
    data.update(overrides)
    return TvDiscManifest.model_validate(data)


def test_render_local_plexmatch_title_year() -> None:
    text = render_local_plexmatch(title="Example Shorts Collection", year=2011)
    assert "title: Example Shorts Collection\n" in text
    assert "year: 2011\n" in text
    assert "tvdbid:" not in text
    assert "tmdbid:" not in text
    assert "guid:" not in text


def test_write_local_plexmatch_idempotent(tmp_path: Path) -> None:
    dest = write_local_plexmatch(tmp_path, title="Curated Show", year=2011)
    assert dest.name == ".plexmatch"
    first = dest.read_text(encoding="utf-8")
    again = write_local_plexmatch(tmp_path, title="Curated Show", year=2011)
    assert again.read_text(encoding="utf-8") == first


def test_write_from_manifest_skips_when_not_local(tmp_path: Path) -> None:
    assert write_from_manifest(_tv(), tmp_path) is None
    assert not (tmp_path / ".plexmatch").exists()


def test_write_from_manifest_uses_show_and_year(tmp_path: Path) -> None:
    manifest = _tv(
        plex={"local": True, "year": 2011},
        meta={"id": "lt", "show": "Example Shorts Collection", "season": 1},
    )
    dest = write_from_manifest(manifest, tmp_path)
    assert dest is not None
    text = dest.read_text(encoding="utf-8")
    assert "title: Example Shorts Collection" in text
    assert "year: 2011" in text


def test_plex_title_override() -> None:
    manifest = _tv(plex={"local": True, "title": "Override Title", "year": 2023})
    assert plex_title_for(manifest) == "Override Title"
    assert manifest.plex == PlexConfig(local=True, title="Override Title", year=2023)
