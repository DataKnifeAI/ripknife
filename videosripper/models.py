"""Pydantic models for YAML rip manifests."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from videosripper.paths import DEFAULT_DEVICE


class Kind(str, Enum):
    TV_DISC = "tv_disc"
    MOVIE = "movie"


class SkipEntry(BaseModel):
    makemkv_title: int
    reason: str = ""


class SplitOutput(BaseModel):
    episode: str
    title: str


class SplitConfig(BaseModel):
    at_seconds: float
    outputs: list[SplitOutput]

    @field_validator("outputs")
    @classmethod
    def require_two_outputs(cls, value: list[SplitOutput]) -> list[SplitOutput]:
        if len(value) != 2:
            raise ValueError("split.outputs must contain exactly 2 episodes")
        return value


class ExtractChapterConfig(BaseModel):
    chapter_index: int = Field(ge=0, description="0-based ffprobe chapter index")


class TvTrack(BaseModel):
    slot: int = Field(ge=1)
    makemkv_title: int = Field(ge=0)
    episode: str | None = None
    title: str | None = None
    season: int | None = Field(
        default=None,
        ge=1,
        description="Override meta.season for this track (output path and NAS subdir)",
    )
    split: SplitConfig | None = None
    extract_chapter: ExtractChapterConfig | None = None

    @model_validator(mode="after")
    def validate_track_mode(self) -> TvTrack:
        modes = [self.split is not None, self.extract_chapter is not None]
        if sum(modes) > 1:
            raise ValueError("track cannot combine split and extract_chapter")
        if self.split:
            if self.episode or self.title:
                raise ValueError("split tracks must not set episode/title on parent")
            return self
        if self.extract_chapter:
            if not self.episode or not self.title:
                raise ValueError("extract_chapter tracks require episode and title")
            return self
        if not self.episode or not self.title:
            raise ValueError("direct rip tracks require episode and title")
        return self


class MetaBase(BaseModel):
    id: str = Field(min_length=1)
    device: str = DEFAULT_DEVICE
    minlength: int = Field(default=300, ge=0)
    disc_name: str | None = Field(
        default=None,
        description="Expected MakeMKV disc name for watcher verification",
    )


class TvMeta(MetaBase):
    show: str = Field(min_length=1)
    season: int = Field(ge=1)


class MovieMeta(MetaBase):
    title: str = Field(min_length=1)
    disc_pattern: str | None = None
    part: int | None = Field(
        default=None,
        ge=1,
        description="1-based disc part number for multi-disc movies (used with stitch)",
    )


class OutputConfig(BaseModel):
    season_dir: str | None = None
    dir: str | None = None
    filename: str | None = None
    log_file: str | None = Field(
        default=None,
        description=(
            "Deprecated and ignored. Rips append to logs/rip.log (see logging_setup). "
            "Remove from manifests; field kept for backward-compatible parsing only."
        ),
    )


class NasConfig(BaseModel):
    share: str | None = Field(
        default=None,
        description=(
            "NAS destination path. Null or omitted uses paths.NAS_SHARE for tv_disc "
            "(default /mnt/tn-sas/Anime/Spongebob Squarepants) or paths.MOVIES_NAS "
            "when nas.movies is true (default /mnt/tn-sas/Movies)."
        ),
    )
    movies: bool = False
    subdir: str = ""


class PostConfig(BaseModel):
    eject: bool = True
    copy_to_nas: bool = True


class MonitorConfig(BaseModel):
    stall_timeout_sec: int = Field(default=900, ge=60)
    open_timeout_sec: int = Field(default=600, ge=60)
    on_stall: Literal["exit", "warn"] = "exit"


class RetryConfig(BaseModel):
    """Extra attempts after first failure before user intervention is required.

    Default ``count=3`` means 3 retries = **4 total attempts** (initial + 3 retries).
    After exhaustion (especially disc open / stall), the rip ejects the disc and exits.
    """

    count: int = Field(
        default=3,
        ge=0,
        description="Retries after first failure (3 = 4 total attempts) for disc open / title rip",
    )
    delay_sec: float = Field(default=30.0, ge=0, description="Seconds to wait before retry")
    stall_retry: bool = Field(
        default=True,
        description="Retry the same title after a rip stall timeout (uses retry.count)",
    )
    nas_retries: int = Field(
        default=1,
        ge=0,
        description="Extra rsync attempts after first NAS copy failure",
    )


class StreamDefaultsConfig(BaseModel):
    """MakeMKV stream selection and MKV default track flags."""

    audio_default: str | None = Field(
        default=None,
        description="ISO 639-2 language code for default audio track (e.g. jpn, eng)",
    )
    subtitle_default: str | None = Field(
        default=None,
        description=(
            "ISO 639-2 language code for default subtitle track (e.g. eng). "
            "Omit or null with audio_default to clear/disable subtitle default disposition."
        ),
    )
    keep_all_tracks: bool = Field(
        default=False,
        description="Select all audio and subtitle tracks during MakeMKV rip",
    )

    @model_validator(mode="after")
    def require_at_least_one_option(self) -> StreamDefaultsConfig:
        if not self.keep_all_tracks and not self.audio_default and not self.subtitle_default:
            raise ValueError(
                "stream_defaults requires keep_all_tracks and/or audio_default/subtitle_default"
            )
        return self


# Absolute floor for movie size validation — catches empty/truncated rips, not real features.
MIN_SIZE_FLOOR_BYTES = 1024 * 1024 * 1024  # 1 GiB


class TitleSelection(BaseModel):
    mode: Literal["auto_feature", "fixed"] = "auto_feature"
    title: int | None = None
    min_duration_sec: int = Field(default=80 * 60, ge=0)
    min_size_bytes: int = Field(default=MIN_SIZE_FLOOR_BYTES, ge=0)


class ValidationRules(BaseModel):
    min_duration_sec: int = Field(default=80 * 60, ge=0)
    min_size_bytes: int = Field(default=MIN_SIZE_FLOOR_BYTES, ge=0)


class StitchConfig(BaseModel):
    """Multi-disc movie stitch — runs after all part MKVs exist (post-rip, no makemkv lock)."""

    enabled: bool = False
    expected_parts: int = Field(default=2, ge=2)
    group_id: str | None = Field(
        default=None,
        description="Links part manifests; defaults to meta.id with -discN suffix stripped",
    )
    output_filename: str | None = Field(
        default=None,
        description="Combined MKV filename; defaults to meta.title + .mkv",
    )
    copy_to_nas: bool = Field(
        default=True,
        description="Copy stitched file to NAS after join (per-disc post.copy_to_nas stays false)",
    )
    tolerance_pct: float = Field(
        default=2.0,
        ge=0,
        description="Max duration mismatch percent after stitch",
    )


class TvDiscManifest(BaseModel):
    kind: Literal["tv_disc"]
    meta: TvMeta
    output: OutputConfig
    nas: NasConfig = Field(default_factory=NasConfig)
    post: PostConfig = Field(default_factory=PostConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    stream_defaults: StreamDefaultsConfig | None = None
    tracks: list[TvTrack] = Field(min_length=1)
    skips: list[SkipEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_tv_output(self) -> TvDiscManifest:
        if not self.output.season_dir:
            raise ValueError("tv_disc manifests require output.season_dir")
        slots = [t.slot for t in self.tracks]
        if len(slots) != len(set(slots)):
            raise ValueError("track slots must be unique")
        return self

class MovieManifest(BaseModel):
    kind: Literal["movie"]
    meta: MovieMeta
    output: OutputConfig
    nas: NasConfig = Field(default_factory=lambda: NasConfig(movies=True))
    post: PostConfig = Field(default_factory=PostConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    stream_defaults: StreamDefaultsConfig | None = None
    title_selection: TitleSelection = Field(default_factory=TitleSelection)
    validation: ValidationRules = Field(default_factory=ValidationRules)
    skips: list[SkipEntry] = Field(default_factory=list)
    stitch: StitchConfig | None = None

    @model_validator(mode="after")
    def validate_movie_output(self) -> MovieManifest:
        if not self.output.dir or not self.output.filename:
            raise ValueError("movie manifests require output.dir and output.filename")
        if self.title_selection.mode == "fixed" and self.title_selection.title is None:
            raise ValueError("title_selection.mode=fixed requires title_selection.title")
        if self.stitch and self.stitch.enabled:
            if self.meta.part is None:
                raise ValueError("stitch.enabled requires meta.part (1-based disc number)")
            if self.meta.part > self.stitch.expected_parts:
                raise ValueError("meta.part cannot exceed stitch.expected_parts")
        return self


def load_manifest(path: Path) -> TvDiscManifest | MovieManifest:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    kind = data.get("kind")
    if kind == "tv_disc":
        return TvDiscManifest.model_validate(data)
    if kind == "movie":
        return MovieManifest.model_validate(data)
    raise ValueError(f"Unknown manifest kind: {kind!r}")


def track_season(manifest: TvDiscManifest, track: TvTrack) -> int:
    return track.season if track.season is not None else manifest.meta.season


def _season_path(base: str, default_season: int, season: int) -> str:
    if season == default_season:
        return base
    return re.sub(rf"Season {default_season}\b", f"Season {season}", base)


def tv_output_dir(manifest: TvDiscManifest, season: int | None = None) -> Path:
    from videosripper.paths import OUTPUT_ROOT

    target = season if season is not None else manifest.meta.season
    season_dir = _season_path(manifest.output.season_dir, manifest.meta.season, target)
    return OUTPUT_ROOT / season_dir


def tv_nas_season_dir(manifest: TvDiscManifest, season: int | None = None) -> str:
    """Relative season folder on the NAS share (may differ from local output.season_dir)."""
    target = season if season is not None else manifest.meta.season
    base = manifest.nas.subdir or manifest.output.season_dir
    return _season_path(base, manifest.meta.season, target)


def movie_output_path(manifest: MovieManifest) -> Path:
    from videosripper.paths import OUTPUT_ROOT

    return OUTPUT_ROOT / manifest.output.dir / manifest.output.filename


_DISC_ID_SUFFIX = re.compile(r"-disc\d+$", re.IGNORECASE)
_PART_FILENAME = re.compile(r" - Disc (\d+)\.mkv$", re.IGNORECASE)


def stitch_group_id(manifest: MovieManifest) -> str | None:
    if not manifest.stitch or not manifest.stitch.enabled:
        return None
    if manifest.stitch.group_id:
        return manifest.stitch.group_id
    return _DISC_ID_SUFFIX.sub("", manifest.meta.id)


def stitch_output_filename(manifest: MovieManifest) -> str:
    assert manifest.stitch is not None
    if manifest.stitch.output_filename:
        return manifest.stitch.output_filename
    return f"{manifest.meta.title}.mkv"


def stitch_output_path(manifest: MovieManifest) -> Path:
    from videosripper.paths import OUTPUT_ROOT

    assert manifest.output.dir
    return OUTPUT_ROOT / manifest.output.dir / stitch_output_filename(manifest)


def part_filename_for(manifest: MovieManifest, part: int) -> str:
    """Expected per-disc output filename: ``Title (Year) - Disc N.mkv``."""
    if _PART_FILENAME.search(manifest.output.filename or ""):
        return _PART_FILENAME.sub(f" - Disc {part}.mkv", manifest.output.filename or "")
    return f"{manifest.meta.title} - Disc {part}.mkv"
