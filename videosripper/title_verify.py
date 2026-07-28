"""Validate manifest title indices against a MakeMKV scan."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from videosripper.makemkv import ScanResult, TitleInfo
from videosripper.models import MovieManifest, TvDiscManifest, TvTrack, ValidationRules
from videosripper.post_rip_verify import effective_min_size_bytes

SUSPICIOUS_NAME_RE = re.compile(
    r"\b("
    r"COPYRIGHT|FBI|WARNING|ANTI[- ]?PIRACY|PIRACY|INTERPOL|"
    r"DUPLICATION|UNAUTHORIZED|FEDERAL|DO NOT COPY|"
    r"PLAYBACK PROHIBITED|COPY PROTECTED|RATING|LOGO"
    r")\b",
    re.IGNORECASE,
)

# Standalone TV episode shorter than this is unusual after minlength filter.
_MIN_EPISODE_SEC = 480

# Obfuscated Blu-ray: many playlists share name+duration; decoys differ in length.
_DUPLICATE_CLUSTER_MIN = 5
_DURATION_OUTLIER_SEC = 90


@dataclass(frozen=True)
class ObfuscationSummary:
    detected: bool
    dominant_duration_sec: int
    duplicate_count: int
    cluster_indices: frozenset[int]
    decoy_indices: frozenset[int]
    recommended_title: int | None
    dominant_name: str = ""

    def to_dict(self) -> dict:
        return {
            "detected": self.detected,
            "dominant_duration_sec": self.dominant_duration_sec,
            "duplicate_count": self.duplicate_count,
            "decoy_indices": sorted(self.decoy_indices),
            "recommended_title": self.recommended_title,
            "dominant_name": self.dominant_name,
        }


@dataclass(frozen=True)
class TitleIssue:
    level: str  # "error" or "warn"
    message: str


def titles_from_scan_json(scan_data: dict) -> dict[int, TitleInfo]:
    titles: dict[int, TitleInfo] = {}
    for row in scan_data.get("titles", []):
        idx = int(row["index"])
        titles[idx] = TitleInfo(
            index=idx,
            name=row.get("name", ""),
            duration=row.get("duration", ""),
            duration_sec=int(row.get("duration_sec", 0)),
            size_bytes=int(row.get("size_bytes", 0)),
        )
    return titles


def titles_from_scan_result(scan: ScanResult) -> dict[int, TitleInfo]:
    return dict(scan.titles)


def _resolve_titles(scan_data: dict | ScanResult) -> dict[int, TitleInfo]:
    if isinstance(scan_data, ScanResult):
        return titles_from_scan_result(scan_data)
    return titles_from_scan_json(scan_data)


def _later_title_hint(
    alt_idx: int,
    alt_info: TitleInfo,
    *,
    include_size: bool = False,
) -> str:
    if include_size:
        detail = f"{alt_info.duration}, {alt_info.size_bytes} bytes, {alt_info.name!r}"
    else:
        detail = f"{alt_info.duration}, {alt_info.name!r}"
    return f"check later title {alt_idx} ({detail})"


def _rejected_title_issue(
    subject: str,
    reason: str,
    alt: tuple[int, TitleInfo] | None,
    *,
    include_size: bool = False,
) -> TitleIssue:
    msg = f"{subject} rejected ({reason})"
    if alt:
        alt_idx, alt_info = alt
        msg += f"; {_later_title_hint(alt_idx, alt_info, include_size=include_size)}"
    return TitleIssue("error", msg)


def _duration_clusters(titles: dict[int, TitleInfo]) -> list[tuple[int, list[int]]]:
    """Group title indices by exact duration_sec, largest cluster first."""
    by_dur: dict[int, list[int]] = {}
    for idx, info in titles.items():
        by_dur.setdefault(info.duration_sec, []).append(idx)
    return sorted(by_dur.items(), key=lambda item: (-len(item[1]), item[0]))


def _dominant_name(titles: dict[int, TitleInfo], indices: list[int]) -> str:
    names = [titles[i].name for i in indices if titles[i].name]
    if not names:
        return ""
    return max(set(names), key=names.count)


def analyze_obfuscation(titles: dict[int, TitleInfo]) -> ObfuscationSummary:
    """Detect duplicate-playlist obfuscation and duration decoys."""
    empty = ObfuscationSummary(
        detected=False,
        dominant_duration_sec=0,
        duplicate_count=0,
        cluster_indices=frozenset(),
        decoy_indices=frozenset(),
        recommended_title=None,
    )
    if not titles:
        return empty

    clusters = _duration_clusters(titles)
    dominant_dur, cluster_indices = clusters[0]
    duplicate_count = len(cluster_indices)
    if duplicate_count < _DUPLICATE_CLUSTER_MIN:
        return ObfuscationSummary(
            detected=False,
            dominant_duration_sec=dominant_dur,
            duplicate_count=duplicate_count,
            cluster_indices=frozenset(cluster_indices),
            decoy_indices=frozenset(),
            recommended_title=None,
        )

    dominant_name = _dominant_name(titles, cluster_indices)
    cluster_set = frozenset(cluster_indices)
    decoy_indices: list[int] = []
    for idx, info in titles.items():
        if idx in cluster_set:
            continue
        if abs(info.duration_sec - dominant_dur) <= _DURATION_OUTLIER_SEC:
            continue
        if dominant_name and info.name and info.name != dominant_name:
            continue
        decoy_indices.append(idx)

    return ObfuscationSummary(
        detected=True,
        dominant_duration_sec=dominant_dur,
        duplicate_count=duplicate_count,
        cluster_indices=cluster_set,
        decoy_indices=frozenset(decoy_indices),
        recommended_title=min(cluster_indices),
        dominant_name=dominant_name,
    )


def title_suspicion(info: TitleInfo, *, minlength: int) -> str | None:
    if info.duration_sec < minlength:
        return f"duration {info.duration} ({info.duration_sec}s) below minlength {minlength}s"
    if info.name and SUSPICIOUS_NAME_RE.search(info.name):
        return f"name {info.name!r} looks like copy-protection or menu content"
    return None


def later_title_candidate(
    titles: dict[int, TitleInfo],
    mapped_idx: int,
    *,
    minlength: int,
    min_duration_sec: int = _MIN_EPISODE_SEC,
) -> tuple[int, TitleInfo] | None:
    """Prefer a later index when the mapped title looks like a dummy loop."""
    for later_idx in sorted(titles):
        if later_idx <= mapped_idx:
            continue
        later = titles[later_idx]
        if later.duration_sec < min_duration_sec:
            continue
        if title_suspicion(later, minlength=minlength):
            continue
        return later_idx, later
    return None


def scan_title_hints(titles: dict[int, TitleInfo], *, minlength: int) -> dict[int, str]:
    """Short hints for scan output (suspicious titles, better later index)."""
    hints: dict[int, str] = {}
    obf = analyze_obfuscation(titles)
    if obf.detected:
        dup_note = (
            f"duplicate playlist ({obf.duplicate_count}x "
            f"{obf.dominant_duration_sec // 3600}:{(obf.dominant_duration_sec % 3600) // 60:02d}:"
            f"{obf.dominant_duration_sec % 60:02d})"
        )
        if obf.recommended_title is not None:
            hints[obf.recommended_title] = f"recommended feature; {dup_note}"
        for idx in sorted(obf.decoy_indices):
            info = titles[idx]
            direction = "longer" if info.duration_sec > obf.dominant_duration_sec else "shorter"
            hints[idx] = (
                f"decoy: {direction} outlier vs dominant cluster "
                f"({info.duration}, {obf.duplicate_count} duplicates)"
            )

    for idx, info in titles.items():
        if idx in hints:
            continue
        reason = title_suspicion(info, minlength=minlength)
        if reason:
            hints[idx] = f"suspicious: {reason}"
            continue
        alt = later_title_candidate(titles, idx, minlength=minlength)
        if alt and alt[0] != idx:
            # Only flag early indices when a clearly better later title exists
            if info.duration_sec < _MIN_EPISODE_SEC:
                alt_idx, alt_info = alt
                hints[idx] = (
                    f"short title; later title {alt_idx} "
                    f"({alt_info.duration}, {alt_info.name!r}) may be correct"
                )
    return hints


def _track_label(track: TvTrack) -> str:
    match (track.split, track.extract_chapter):
        case (True, _):
            return f"slot {track.slot} (split)"
        case (_, True):
            return f"slot {track.slot} (chapter extract)"
        case _:
            return f"slot {track.slot}"


def _validate_tv_track(
    track: TvTrack,
    titles: dict[int, TitleInfo],
    minlength: int,
) -> list[TitleIssue]:
    idx = track.makemkv_title
    label = _track_label(track)
    info = titles.get(idx)
    if info is None:
        available = ", ".join(str(i) for i in sorted(titles))
        return [
            TitleIssue(
                "error",
                f"{label}: makemkv_title {idx} missing from scan "
                f"({len(titles)} titles: {available or 'none'})",
            )
        ]

    reason = title_suspicion(info, minlength=minlength)
    if reason:
        return [
            _rejected_title_issue(
                f"{label}: title {idx}",
                reason,
                later_title_candidate(titles, idx, minlength=minlength),
            )
        ]

    if track.split or track.extract_chapter or info.duration_sec >= _MIN_EPISODE_SEC:
        return []

    alt = later_title_candidate(titles, idx, minlength=minlength)
    msg = (
        f"{label}: title {idx} duration {info.duration} "
        f"({info.duration_sec}s) is short for a standalone episode"
    )
    if alt:
        alt_idx, alt_info = alt
        msg += f"; {_later_title_hint(alt_idx, alt_info)}"
    return [TitleIssue("error", msg)]


def _validate_tv_skip(
    skip,
    titles: dict[int, TitleInfo],
    minlength: int,
) -> TitleIssue | None:
    if skip.makemkv_title not in titles:
        return TitleIssue(
            "warn",
            f"skip documents makemkv_title {skip.makemkv_title} "
            f"but it is not in scan (minlength filter?)",
        )
    skip_info = titles[skip.makemkv_title]
    if not title_suspicion(skip_info, minlength=minlength) and skip_info.duration_sec >= 3600:
        return TitleIssue(
            "warn",
            f"skip title {skip.makemkv_title} is long "
            f"({skip_info.duration}) — confirm play-all/extra, not episode",
        )
    return None


def validate_tv_tracks(
    manifest: TvDiscManifest,
    scan_data: dict | ScanResult,
) -> list[TitleIssue]:
    titles = _resolve_titles(scan_data)
    minlength = manifest.meta.minlength
    issues: list[TitleIssue] = []

    for track in manifest.tracks:
        issues.extend(_validate_tv_track(track, titles, minlength))

    for skip in manifest.skips:
        if issue := _validate_tv_skip(skip, titles, minlength):
            issues.append(issue)

    return issues


def _movie_title_index(manifest: MovieManifest, titles: dict[int, TitleInfo]) -> int | None:
    sel = manifest.title_selection
    if sel.mode == "fixed":
        return sel.title
    from videosripper.makemkv import pick_feature_title

    try:
        scan = ScanResult(raw="", titles=titles)
        return pick_feature_title(scan, sel.min_duration_sec)
    except RuntimeError:
        return None


def _obfuscation_movie_issues(
    title_idx: int,
    info: TitleInfo,
    obf: ObfuscationSummary,
    *,
    selection_mode: str,
) -> list[TitleIssue]:
    if not obf.detected:
        return []

    issues: list[TitleIssue] = []
    rec = obf.recommended_title
    rec_hint = f"; use title {rec}" if rec is not None else ""

    if title_idx in obf.decoy_indices:
        issues.append(
            TitleIssue(
                "error",
                f"movie title {title_idx} is a decoy outlier "
                f"(dominant cluster {obf.duplicate_count} playlists at "
                f"{obf.dominant_duration_sec}s){rec_hint}",
            )
        )
        return issues

    if title_idx not in obf.cluster_indices:
        delta = abs(info.duration_sec - obf.dominant_duration_sec)
        level = "error" if delta > _DURATION_OUTLIER_SEC else "warn"
        if level == "error":
            msg = (
                f"movie title {title_idx} duration {info.duration} "
                f"outside dominant cluster "
                f"({obf.dominant_duration_sec}s, {obf.duplicate_count} duplicates)"
                f"{rec_hint}"
            )
        else:
            msg = (
                f"movie title {title_idx} duration {info.duration} "
                f"differs from dominant cluster mode "
                f"({obf.dominant_duration_sec}s){rec_hint}"
            )
        issues.append(TitleIssue(level, msg))
    elif selection_mode == "fixed" and rec is not None and title_idx != rec:
        issues.append(
            TitleIssue(
                "warn",
                f"fixed title {title_idx} is in duplicate cluster; "
                f"title {rec} is equivalent and preferred (lowest index)",
            )
        )

    return issues


def _movie_min_duration_issue(
    info: TitleInfo,
    title_idx: int,
    rules: ValidationRules,
) -> TitleIssue | None:
    if info.duration_sec < rules.min_duration_sec:
        return TitleIssue(
            "error",
            f"movie title {title_idx} duration {info.duration} "
            f"< required {rules.min_duration_sec // 60} min",
        )
    return None


def _movie_min_size_issue(
    info: TitleInfo,
    title_idx: int,
    rules: ValidationRules,
) -> TitleIssue | None:
    required = effective_min_size_bytes(rules.min_size_bytes, info.size_bytes)
    if info.size_bytes and info.size_bytes < required:
        return TitleIssue(
            "error",
            f"movie title {title_idx} size {info.size_bytes} bytes "
            f"< required {required}",
        )
    return None


_MOVIE_SCAN_RULES: tuple[
    Callable[[TitleInfo, int, ValidationRules], TitleIssue | None],
    ...,
] = (
    _movie_min_duration_issue,
    _movie_min_size_issue,
)


def _movie_fixed_title_longer_later_issue(
    title_idx: int,
    info: TitleInfo,
    titles: dict[int, TitleInfo],
    manifest: MovieManifest,
    minlength: int,
) -> TitleIssue | None:
    sel = manifest.title_selection
    if sel.mode != "fixed":
        return None
    longer_later = [
        (i, t)
        for i, t in sorted(titles.items())
        if i > title_idx
        and t.duration_sec > info.duration_sec
        and t.duration_sec >= sel.min_duration_sec
        and not title_suspicion(t, minlength=minlength)
    ]
    if not longer_later:
        return None
    alt_idx, alt_info = longer_later[0]
    return TitleIssue(
        "error",
        f"fixed title {title_idx} ({info.duration}) but later title {alt_idx} "
        f"is longer ({alt_info.duration}) — likely wrong feature",
    )


def validate_movie_title(
    manifest: MovieManifest,
    scan_data: dict | ScanResult,
) -> list[TitleIssue]:
    titles = _resolve_titles(scan_data)
    minlength = manifest.meta.minlength
    issues: list[TitleIssue] = []
    obf = analyze_obfuscation(titles)

    title_idx = _movie_title_index(manifest, titles)
    if title_idx is None:
        sel = manifest.title_selection
        issues.append(
            TitleIssue(
                "error",
                f"no feature title >= {sel.min_duration_sec // 60} min found at minlength={minlength}",
            )
        )
        return issues

    info = titles.get(title_idx)
    if info is None:
        issues.append(TitleIssue("error", f"selected title {title_idx} missing from scan"))
        return issues

    issues.extend(
        _obfuscation_movie_issues(
            title_idx,
            info,
            obf,
            selection_mode=manifest.title_selection.mode,
        )
    )

    reason = title_suspicion(info, minlength=minlength)
    if reason:
        issues.append(
            _rejected_title_issue(
                f"movie title {title_idx}",
                reason,
                later_title_candidate(
                    titles,
                    title_idx,
                    minlength=minlength,
                    min_duration_sec=manifest.title_selection.min_duration_sec,
                ),
                include_size=True,
            )
        )
        return issues

    rules = manifest.validation
    for rule in _MOVIE_SCAN_RULES:
        if issue := rule(info, title_idx, rules):
            issues.append(issue)

    if issue := _movie_fixed_title_longer_later_issue(
        title_idx, info, titles, manifest, minlength
    ):
        issues.append(issue)

    return issues


def validate_manifest_titles(
    manifest: TvDiscManifest | MovieManifest,
    scan_data: dict | ScanResult,
) -> list[TitleIssue]:
    if isinstance(manifest, TvDiscManifest):
        return validate_tv_tracks(manifest, scan_data)
    return validate_movie_title(manifest, scan_data)


def format_title_issues(issues: list[TitleIssue]) -> str:
    if not issues:
        return ""
    lines = []
    for issue in issues:
        prefix = "ERROR" if issue.level == "error" else "WARNING"
        lines.append(f"{prefix}: {issue.message}")
    return "\n".join(lines)


def raise_on_errors(issues: list[TitleIssue]) -> None:
    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise RuntimeError(
            "Title validation failed — fix manifest mapping or re-scan with matching minlength:\n"
            + format_title_issues(errors)
        )
