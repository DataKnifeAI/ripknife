"""Local-only Plex match hints for curated TV sets.

Plex's TV Anime library scrapes TheTVDB/TMDB. Anthology and theatrical-shorts
collections use official disc order that does not match those catalogs. A
show-folder ``.plexmatch`` with title + year and *no* tvdb/tmdb/imdb
IDs tells the scanner this is its own show so filenames supply episode titles.

Official syntax: https://support.plex.tv/articles/plexmatch/
Unsupported (do not write): ``tvdbid: 0``, ``guid: none``, ``ignoreguid``.
"""

from __future__ import annotations

from pathlib import Path

from ripknife.models import PlexConfig, TvDiscManifest

PLEXMATCH_NAME = ".plexmatch"

_LOCAL_HEADER = (
    "# Local curated set — title/year only (no tvdbid/tmdbid/imdbid).\n"
    "# Keeps Plex from binding a catalog series with different episode order.\n"
)


def render_local_plexmatch(*, title: str, year: int | None = None) -> str:
    """Render a show-level ``.plexmatch`` for unmatched local metadata."""
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("plexmatch title must not be empty")
    lines = [_LOCAL_HEADER, f"title: {cleaned}\n"]
    if year is not None:
        lines.append(f"year: {year}\n")
    return "".join(lines)


def write_local_plexmatch(
    show_dir: Path,
    *,
    title: str,
    year: int | None = None,
) -> Path:
    """Write ``show_dir/.plexmatch``. Idempotent when contents already match."""
    show_dir.mkdir(parents=True, exist_ok=True)
    dest = show_dir / PLEXMATCH_NAME
    text = render_local_plexmatch(title=title, year=year)
    if dest.is_file() and dest.read_text(encoding="utf-8") == text:
        return dest
    tmp = show_dir / ".plexmatch.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    return dest


def plex_title_for(manifest: TvDiscManifest) -> str:
    cfg = manifest.plex
    if cfg and cfg.title and cfg.title.strip():
        return cfg.title.strip()
    return manifest.meta.show


def write_from_manifest(manifest: TvDiscManifest, show_dir: Path) -> Path | None:
    """Write ``.plexmatch`` when ``plex.local`` is set. Returns path or None."""
    cfg: PlexConfig | None = manifest.plex
    if cfg is None or not cfg.local:
        return None
    return write_local_plexmatch(
        show_dir,
        title=plex_title_for(manifest),
        year=cfg.year,
    )
