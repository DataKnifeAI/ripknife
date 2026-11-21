"""Load a YouTube show map and parse one playlist title with its rules.

The mechanism lives here. Show names, libraries, and numbering live in
``youtube.yaml`` (personal config dir, else the tracked example). Disc
watchers do not import this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ripknife.models import NasConfig, sanitize_edition
from ripknife.paths import resolve_config_file


def youtube_map_path() -> Path:
    """Personal map (``RIPKNIFE_CONFIG_DIR`` or sibling checkout) then tracked example."""
    override = os.environ.get("RIPKNIFE_YOUTUBE_MAP")
    if override:
        return Path(override)
    return resolve_config_file("youtube.yaml")


_PLAYLIST_ID = re.compile(r"(?:[?&]|^)list=([A-Za-z0-9_-]+)")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
_NAMED_GROUP = ("season", "episode", "title")


class YoutubeTitleFrom(BaseModel):
    """How one rule builds a title from the raw playlist string."""

    model_config = ConfigDict(extra="forbid")

    split: str | None = "//"
    pick: int | None = Field(default=None, ge=0)
    drop_equal: list[str] = Field(default_factory=list)
    drop_match: str | None = None
    keep_after: str | None = None
    strip_prefix: list[str] = Field(default_factory=list)
    strip_pattern: list[str] = Field(default_factory=list)
    join: str = " "

    @model_validator(mode="after")
    def check_title_from(self) -> YoutubeTitleFrom:
        if self.keep_after is not None and not self.keep_after:
            raise ValueError("keep_after is empty")
        if self.keep_after and not self.drop_match:
            raise ValueError("keep_after requires drop_match")
        for pattern in (self.drop_match, *self.strip_pattern):
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"bad title regex {pattern!r}: {exc}") from exc
        return self


class YoutubeRule(BaseModel):
    """One numbering rule. The first match in a show's list wins."""

    model_config = ConfigDict(extra="forbid")

    pattern: str | None = None
    season: int | str | None = None
    episode: int | str | None = None
    title: str | None = None
    title_from: YoutubeTitleFrom | None = None

    @field_validator("pattern")
    @classmethod
    def pattern_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("pattern is empty")
        return value


class YoutubeMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    show: str | None = None
    title: str | None = None
    year: int | None = Field(default=None, ge=1)

    @field_validator("id")
    @classmethod
    def strip_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("id is empty")
        return cleaned

    @field_validator("show", "title")
    @classmethod
    def blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class YoutubeShow(BaseModel):
    """One mapped show. ``kind`` and ``animated`` select the NAS library."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    meta: YoutubeMeta
    animated: bool
    playlists: list[str] = Field(default_factory=list)
    match: list[str] = Field(default_factory=list)
    nas: NasConfig = Field(default_factory=NasConfig)
    rules: list[YoutubeRule] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def kind_is_series_or_movie(cls, value: str) -> str:
        if value not in ("series", "movie"):
            raise ValueError("kind must be series or movie")
        return value

    @field_validator("playlists", "match")
    @classmethod
    def strip_names(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("empty playlist id or match name")
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def check_show(self) -> YoutubeShow:
        ident = self.meta.id
        if self.kind == "series":
            if not self.meta.show:
                raise ValueError(f"{ident}: series needs meta.show")
            if self.meta.title:
                raise ValueError(f"{ident}: series uses meta.show, not meta.title")
            if not self.rules:
                raise ValueError(f"{ident}: series needs rules")
            if self.nas.movies:
                raise ValueError(f"{ident}: series nas.movies must be false")
        else:
            if not self.meta.title:
                raise ValueError(f"{ident}: movie needs meta.title")
            if self.meta.show:
                raise ValueError(f"{ident}: movie uses meta.title, not meta.show")
            if self.rules:
                raise ValueError(f"{ident}: movie entries do not use title rules")
            if not self.nas.movies:
                raise ValueError(f"{ident}: movie nas.movies must be true")
        share = (self.nas.share or "").strip()
        if not share.startswith("/"):
            raise ValueError(f"{ident}: nas.share must be an absolute path")
        self.nas.share = share
        fallback = [index for index, rule in enumerate(self.rules) if rule.pattern is None]
        if len(fallback) > 1:
            raise ValueError(f"{ident}: only one rule may omit pattern")
        if fallback and fallback[0] != len(self.rules) - 1:
            raise ValueError(f"{ident}: a rule with no pattern must be last")
        for rule in self.rules:
            _require_complete_rule(ident, rule)
        return self

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def show(self) -> str | None:
        return self.meta.show

    @property
    def title(self) -> str | None:
        return self.meta.title

    @property
    def year(self) -> int | None:
        return self.meta.year


class YoutubeMapFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shows: list[YoutubeShow] = Field(min_length=1)


@dataclass(frozen=True)
class YoutubeMap:
    shows: tuple[YoutubeShow, ...]
    path: Path


def load_youtube_map(path: Path | None = None) -> YoutubeMap:
    """Read the YouTube show map. The default file is ``youtube.yaml`` in the config dir."""
    map_path = youtube_map_path() if path is None else Path(path)
    if not map_path.is_file():
        raise ValueError(f"YouTube show map not found: {map_path}")
    data = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"YouTube show map must be a mapping: {map_path}")
    try:
        parsed = YoutubeMapFile.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"YouTube show map {map_path} is invalid: {exc}") from exc
    _reject_duplicates(parsed.shows)
    return YoutubeMap(shows=tuple(parsed.shows), path=map_path)


def match_youtube_show(
    youtube_map: YoutubeMap,
    *,
    show: str | None = None,
    playlist_id: str | None = None,
    url: str | None = None,
) -> YoutubeShow | None:
    """Find a show by id, show or movie title, ``match`` alias, or playlist id.

    ``url`` contributes its ``list=`` id. A show name and a playlist id that
    point at different entries raise ``ValueError``.
    """
    named = _match_name(youtube_map, show) if _clean(show) else None
    list_id = _clean(playlist_id) or _playlist_id_from_url(url)
    listed = _match_playlist(youtube_map, list_id) if list_id else None
    if named is not None and listed is not None and named.id != listed.id:
        raise ValueError(
            f"show {show!r} is {named.id} but playlist {list_id} is {listed.id}"
        )
    return named or listed


def parse_youtube_title(
    raw_title: str,
    show: YoutubeShow,
    *,
    playlist_index: int,
) -> tuple[int, str, str]:
    """Apply ``show.rules`` to one playlist title.

    Returns season, episode, and title. The first matching rule wins. A rule
    with no pattern is the fallback. Literals override named groups.
    ``{show}``, ``{index}``, ``{raw}``, ``{season}``, ``{episode}``, and
    ``{title}`` expand in literals and in title cleanup. The title then goes
    through ``sanitize_edition``.
    """
    if playlist_index < 1:
        raise ValueError(f"{show.id}: playlist index starts at 1")
    if show.kind != "series":
        raise ValueError(f"{show.id}: movie entries have no title rules")
    normalized = " ".join(raw_title.split())
    for rule in show.rules:
        groups = _match_rule(rule, normalized)
        if groups is None:
            continue
        values = _base_values(show, normalized, playlist_index, groups)
        season = _resolve_season(show, rule, groups, values)
        episode = _resolve_episode(show, rule, groups, values)
        values["season"] = str(season)
        values["episode"] = episode
        title = _resolve_title(show, rule, groups, values, normalized)
        return season, episode, _clean_title(title, raw_title)
    raise ValueError(f"{show.id}: no title rule matched {normalized!r}")


def _require_complete_rule(show_id: str, rule: YoutubeRule) -> None:
    groups: set[str] = set()
    if rule.pattern:
        try:
            groups = set(re.compile(rule.pattern).groupindex)
        except re.error as exc:
            raise ValueError(f"{show_id}: bad pattern {rule.pattern!r}: {exc}") from exc
    if rule.season is None and "season" not in groups:
        raise ValueError(f"{show_id}: a rule needs season")
    if rule.episode is None and "episode" not in groups:
        raise ValueError(f"{show_id}: a rule needs episode")
    has_title = rule.title is not None or "title" in groups or rule.title_from is not None
    if not has_title:
        raise ValueError(f"{show_id}: a rule needs a title")


def _reject_duplicates(shows: list[YoutubeShow]) -> None:
    seen_ids: dict[str, str] = {}
    seen_names: dict[str, str] = {}
    seen_lists: dict[str, str] = {}
    for show in shows:
        if show.id in seen_ids:
            raise ValueError(f"duplicate show id {show.id}")
        seen_ids[show.id] = show.id
        for name in _match_names(show):
            key = name.casefold()
            other = seen_names.get(key)
            if other is not None and other != show.id:
                raise ValueError(f"{name!r} matches both {other} and {show.id}")
            seen_names[key] = show.id
        for playlist_id in show.playlists:
            other = seen_lists.get(playlist_id)
            if other is not None:
                raise ValueError(f"playlist {playlist_id} is on {other} and {show.id}")
            seen_lists[playlist_id] = show.id


def _match_names(show: YoutubeShow) -> tuple[str, ...]:
    names = [show.id, *show.match]
    if show.show:
        names.append(show.show)
    if show.title:
        names.append(show.title)
    return tuple(names)


def _match_name(youtube_map: YoutubeMap, show: str | None) -> YoutubeShow | None:
    key = _clean(show)
    if key is None:
        return None
    folded = key.casefold()
    for entry in youtube_map.shows:
        if any(name.casefold() == folded for name in _match_names(entry)):
            return entry
    return None


def _match_playlist(youtube_map: YoutubeMap, playlist_id: str) -> YoutubeShow | None:
    for entry in youtube_map.shows:
        if playlist_id in entry.playlists:
            return entry
    return None


def _playlist_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = _PLAYLIST_ID.search(url.strip())
    if match is None:
        return None
    return match.group(1)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _match_rule(rule: YoutubeRule, normalized: str) -> dict[str, str | None] | None:
    if rule.pattern is None:
        return {}
    match = re.search(rule.pattern, normalized)
    if match is None:
        return None
    return match.groupdict()


def _base_values(
    show: YoutubeShow,
    normalized: str,
    playlist_index: int,
    groups: dict[str, str | None],
) -> dict[str, str]:
    values = {
        "show": show.show or "",
        "index": str(playlist_index),
        "raw": normalized,
        "season": "",
        "episode": "",
        "title": "",
    }
    for name in _NAMED_GROUP:
        group = groups.get(name)
        if group:
            values[name] = group.strip()
    return values


def _substitute(template: str, values: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return values[key]

    return _PLACEHOLDER.sub(repl, template)


def _resolve_season(
    show: YoutubeShow,
    rule: YoutubeRule,
    groups: dict[str, str | None],
    values: dict[str, str],
) -> int:
    if rule.season is not None:
        text = _substitute(str(rule.season), values).strip()
    else:
        text = (groups.get("season") or "").strip()
    if not text:
        raise ValueError(f"{show.id}: matched rule did not set a season")
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{show.id}: season {text!r} is not a number") from exc


def _resolve_episode(
    show: YoutubeShow,
    rule: YoutubeRule,
    groups: dict[str, str | None],
    values: dict[str, str],
) -> str:
    if rule.episode is not None:
        text = _substitute(str(rule.episode), values).strip()
    else:
        text = (groups.get("episode") or "").strip()
    if not text:
        raise ValueError(f"{show.id}: matched rule did not set an episode")
    if text.isdigit():
        return str(int(text))
    return text


def _resolve_title(
    show: YoutubeShow,
    rule: YoutubeRule,
    groups: dict[str, str | None],
    values: dict[str, str],
    normalized: str,
) -> str:
    if rule.title is not None:
        return _substitute(rule.title, values)
    group = (groups.get("title") or "").strip()
    if group:
        return group
    if rule.title_from is not None:
        return _render_title(show, rule.title_from, values, normalized)
    raise ValueError(f"{show.id}: matched rule did not set a title")


def _render_title(
    show: YoutubeShow,
    spec: YoutubeTitleFrom,
    values: dict[str, str],
    normalized: str,
) -> str:
    chunks = normalized.split(spec.split) if spec.split else [normalized]
    segments = [" ".join(chunk.split()) for chunk in chunks if chunk.strip()]
    if spec.pick is not None:
        if spec.pick >= len(segments):
            raise ValueError(f"{show.id}: title segment {spec.pick} missing in {normalized!r}")
        segments = [segments[spec.pick]]
    drop_equal = {_substitute(item, values).casefold() for item in spec.drop_equal}
    drop_re = re.compile(spec.drop_match) if spec.drop_match else None
    kept: list[str] = []
    for segment in segments:
        if segment.casefold() in drop_equal:
            continue
        if drop_re is not None and drop_re.search(segment):
            if spec.keep_after and spec.keep_after in segment:
                tail = " ".join(segment.split(spec.keep_after, 1)[1].split())
                if tail:
                    kept.append(tail)
            continue
        kept.append(segment)
    text = spec.join.join(kept).strip()
    for prefix in spec.strip_prefix:
        text = _strip_prefix(text, _substitute(prefix, values))
    for pattern in spec.strip_pattern:
        text = re.sub(pattern, "", text).strip()
    return " ".join(text.split())


def _strip_prefix(text: str, prefix: str) -> str:
    if not prefix:
        return text
    match = re.match(re.escape(prefix), text, flags=re.IGNORECASE)
    if match is None:
        return text
    return text[match.end() :].strip()


def _clean_title(title: str, raw_title: str) -> str:
    cleaned = sanitize_edition(title)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"title is empty after removing characters SMB cannot store: {raw_title!r}")
    return cleaned
