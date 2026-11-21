"""Post-rip ffprobe validation against MakeMKV scan expectations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ripknife import ffmpeg_ops
from ripknife.makemkv import ScanResult, TitleInfo
from ripknife.models import SplitConfig, TvTrack

logger = logging.getLogger("ripknife")

_DURATION_PCT = 0.05
_DURATION_MIN_SEC = 30
_SIZE_PCT = 0.10
_MIN_SIZE_SCAN_FRACTION = 0.90
# extract_chapter / split size is a CBR estimate (title_size × seg_dur / title_dur).
# VBR titles (anime play-alls) routinely land 12–17% under that line; ±10% then
# deletes good files. Full-title movie TINFO size can also inflate: seamless-
# branch playlists sum every referenced m2ts, not the muxed MKV (a seamless
# branch feature: 19.9 GB mux vs 26.3 GB playlist, duration exact). Duration remains
# the real check; this floor only catches truncated muxes.
_PROPORTIONAL_SIZE_FLOOR = 0.50


class PostRipVerifyError(RuntimeError):
    """Raised when post-rip output does not match scan expectations."""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    duration_actual: int | None = None
    duration_expected: int | None = None
    size_actual: int | None = None
    size_expected: int | None = None
    errors: tuple[str, ...] = ()


def duration_tolerance_sec(expected: int) -> int:
    """±5% or ±30s, whichever is larger."""
    return max(int(expected * _DURATION_PCT), _DURATION_MIN_SEC)


def size_tolerance_bytes(expected: int) -> int:
    """±10% of expected scan size."""
    return int(expected * _SIZE_PCT)


def proportional_size_floor_bytes(expected: int) -> int:
    """Truncation floor for playlist/CBR size estimates (extracts and movies)."""
    return int(expected * _PROPORTIONAL_SIZE_FLOOR)


def effective_min_size_bytes(
    min_size_bytes: int,
    scan_size_bytes: int | None = None,
) -> int:
    """Cap manifest size floor to scan reality when scan size is known.

    Prevents false failures when a manifest sets a rigid 5 GiB floor but the
    disc scan (and valid rip) are slightly smaller — e.g. ~5.0 GB DVD features.
    """
    if scan_size_bytes and scan_size_bytes > 0:
        scan_floor = int(scan_size_bytes * _MIN_SIZE_SCAN_FRACTION)
        return min(min_size_bytes, scan_floor)
    return min_size_bytes


def _split_expected_duration(
    total_sec: int,
    split_config: SplitConfig,
    episode: str,
) -> int | None:
    first_ep, second_ep = split_config.outputs[0].episode, split_config.outputs[1].episode
    at_sec = int(split_config.at_seconds)
    if episode == first_ep:
        return at_sec
    if episode == second_ep:
        return max(0, total_sec - at_sec)
    return None


def _split_expected_size(
    total_bytes: int,
    total_sec: int,
    segment_sec: int,
) -> int:
    if total_sec <= 0:
        return 0
    return int(total_bytes * segment_sec / total_sec)


def _duration_ok(actual: int, expected: int) -> bool:
    tol = duration_tolerance_sec(expected)
    return abs(actual - expected) <= tol


def _size_ok(actual: int, expected: int) -> bool:
    if expected <= 0:
        return True
    tol = size_tolerance_bytes(expected)
    return abs(actual - expected) <= tol


def verify_output(
    scan_title: TitleInfo | None,
    output_path: Path,
    *,
    split_config: SplitConfig | None = None,
    split_episode: str | None = None,
    expected_duration_sec: int | None = None,
    expected_size_bytes: int | None = None,
    min_duration_sec: int | None = None,
    min_size_bytes: int | None = None,
    proportional_size: bool = False,
) -> VerifyResult:
    """Compare ffprobe duration and file size to scan expectations."""
    if not output_path.is_file():
        return VerifyResult(ok=False, errors=(f"output missing: {output_path.name}",))

    actual_dur = ffmpeg_ops.probe_duration(output_path)
    actual_size = output_path.stat().st_size

    exp_dur = expected_duration_sec
    exp_size = expected_size_bytes
    size_is_proportional = proportional_size

    if scan_title is not None:
        if exp_dur is None:
            if split_config is not None and split_episode is not None:
                exp_dur = _split_expected_duration(
                    scan_title.duration_sec,
                    split_config,
                    split_episode,
                )
            else:
                exp_dur = scan_title.duration_sec
        if exp_size is None and scan_title.size_bytes > 0:
            if split_config is not None and split_episode is not None and exp_dur is not None:
                exp_size = _split_expected_size(
                    scan_title.size_bytes,
                    scan_title.duration_sec,
                    exp_dur,
                )
                size_is_proportional = True
            elif expected_duration_sec is not None and scan_title.duration_sec > 0:
                exp_size = _split_expected_size(
                    scan_title.size_bytes,
                    scan_title.duration_sec,
                    expected_duration_sec,
                )
                size_is_proportional = True
            else:
                exp_size = scan_title.size_bytes

    errors: list[str] = []
    duration_ok = True

    if min_duration_sec is not None and actual_dur < min_duration_sec:
        duration_ok = False
        errors.append(
            f"duration {actual_dur}s < minimum {min_duration_sec}s — likely wrong title"
        )
    elif exp_dur is not None and exp_dur > 0 and not _duration_ok(actual_dur, exp_dur):
        duration_ok = False
        tol = duration_tolerance_sec(exp_dur)
        errors.append(
            f"duration {actual_dur}s vs expected {exp_dur}s (scan ±{tol}s) — likely wrong title"
        )

    # The 90% scan cap is the inflated TINFO line (effective_min_size_bytes).
    # A duration match at or above the 50% floor is a complete mux (about
    # 90% and about 70% of the playlist sum). An explicit manifest minimum below that
    # cap stays a hard truncation check.
    scan_cap = (
        int(exp_size * _MIN_SIZE_SCAN_FRACTION)
        if exp_size is not None and exp_size > 0
        else None
    )
    waive_scan_cap = (
        scan_cap is not None
        and min_size_bytes is not None
        and min_size_bytes >= scan_cap
        and duration_ok
        and actual_size >= proportional_size_floor_bytes(exp_size)
    )
    if min_size_bytes is not None and actual_size < min_size_bytes and not waive_scan_cap:
        errors.append(
            f"size {actual_size} bytes < minimum {min_size_bytes} bytes"
        )
    elif exp_size is not None and exp_size > 0:
        # Duration-matched full titles and CBR chapter/split estimates: TINFO /
        # playlist bytes can sum unused seamless-branch clips. Fail only if the
        # file is under 50% (truncated). Keep ±10% when duration is already wrong
        # so a short + undersized rip still reports both.
        if size_is_proportional or duration_ok:
            floor = proportional_size_floor_bytes(exp_size)
            if actual_size < floor:
                pct = int(_PROPORTIONAL_SIZE_FLOOR * 100)
                kind = "CBR estimate" if size_is_proportional else "scan size"
                errors.append(
                    f"size {actual_size} bytes < {floor} bytes "
                    f"({pct}% of {kind} {exp_size}) — likely truncated"
                )
        elif not _size_ok(actual_size, exp_size):
            tol = size_tolerance_bytes(exp_size)
            errors.append(
                f"size {actual_size} bytes vs expected {exp_size} bytes (scan ±{tol})"
            )

    return VerifyResult(
        ok=not errors,
        duration_actual=actual_dur,
        duration_expected=exp_dur,
        size_actual=actual_size,
        size_expected=exp_size,
        errors=tuple(errors),
    )


def verify_track_outputs(
    scan_result: ScanResult,
    track: TvTrack,
    outputs: list[tuple[str, str, Path]],
    *,
    skip_paths: frozenset[Path] | None = None,
    chapter_duration_sec: int | None = None,
) -> VerifyResult:
    """Verify all ripped outputs for one manifest track (skips resume paths)."""
    skip = skip_paths or frozenset()
    scan_info = scan_result.titles.get(track.makemkv_title)
    to_check: list[tuple[str, str, Path]] = [
        (episode, title, path) for episode, title, path in outputs if path not in skip
    ]
    if not to_check:
        return VerifyResult(ok=True)

    combined_errors: list[str] = []
    last: VerifyResult | None = None
    checked_paths: list[Path] = []

    for episode, _title, path in to_check:
        kwargs: dict = {}
        if track.split:
            kwargs["split_config"] = track.split
            kwargs["split_episode"] = episode
            kwargs["proportional_size"] = True
        elif track.extract_chapter and chapter_duration_sec is not None:
            kwargs["expected_duration_sec"] = chapter_duration_sec
            kwargs["proportional_size"] = True
        last = verify_output(scan_info, path, **kwargs)
        checked_paths.append(path)
        if not last.ok:
            combined_errors.extend(last.errors)

    if combined_errors:
        for path in checked_paths:
            path.unlink(missing_ok=True)
        return VerifyResult(
            ok=False,
            duration_actual=last.duration_actual if last else None,
            duration_expected=last.duration_expected if last else None,
            size_actual=last.size_actual if last else None,
            size_expected=last.size_expected if last else None,
            errors=tuple(combined_errors),
        )
    return VerifyResult(ok=True)


def log_verify_failure(
    *,
    slot: int | None = None,
    makemkv_title: int | None = None,
    output_name: str | None = None,
    result: VerifyResult,
) -> None:
    prefix_parts = ["Post-rip verify"]
    if slot is not None:
        prefix_parts.append(f"slot {slot}")
    if makemkv_title is not None:
        prefix_parts.append(f"title {makemkv_title}")
    if output_name:
        prefix_parts.append(output_name)
    prefix = " ".join(prefix_parts)
    for err in result.errors:
        logger.error("ERROR: %s: %s", prefix, err)


def warn_title_count_explosion(title_count: int) -> None:
    """Log when scan returns many titles (obfuscated / multi-playlist disc)."""
    if title_count > 50:
        logger.warning(
            "WARNING: %d titles in scan (>50) — obfuscated or multi-playlist disc; "
            "confirm manifest mapping (post-rip verify will catch wrong picks)",
            title_count,
        )
