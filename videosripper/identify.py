"""Disc title identification helpers for generic-labeled discs.

Two sources of identification hints (never overwrite ``disc_name``):

1. **Blu-ray bdmt XML** — ``BDMV/META/DL/bdmt_*.xml`` often carries the real
   movie/show title even when MakeMKV's volume label is generic.
2. **Runtime lookup** — TMDb / OMDb discover/search by main-feature duration,
   returning ranked candidate titles for the LLM mapper (or a human) to pick.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from videosripper.paths import disc_dev

logger = logging.getLogger("videosripper")

GENERIC_DISC_NAMES = frozenset(
    {
        "dvd_video",
        "dvd-video",
        "dvdvideo",
        "video_ts",
        "video-ts",
        "untitled",
        "unknown",
        "unknown-disc",
        "bd_video",
        "bd-video",
        "bdmv",
        "bluray",
        "blu-ray",
    }
)

_GENERIC_RE = re.compile(
    r"^(dvd[_\s-]?video|video[_\s-]?ts|bd[_\s-]?video|untitled|unknown(-\w+)?)$",
    re.IGNORECASE,
)

# TMDb / OMDb keys (optional — lookups skipped when unset)
TMDB_API_KEY = os.environ.get("VIDEOSRIPPER_TMDB_API_KEY") or os.environ.get("TMDB_API_KEY", "")
OMDB_API_KEY = os.environ.get("VIDEOSRIPPER_OMDB_API_KEY") or os.environ.get("OMDB_API_KEY", "")

DEFAULT_RUNTIME_TOLERANCE_SEC = 180  # ±3 minutes
DEFAULT_CANDIDATE_LIMIT = 8
HTTP_TIMEOUT_SEC = 12


@dataclass
class MovieCandidate:
    title: str
    year: int | None = None
    runtime_min: int | None = None
    source: str = ""
    external_id: str = ""
    popularity: float = 0.0

    def display(self) -> str:
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title


@dataclass
class IdentificationHints:
    """Hints attached to scan/pending meta — never replace ``disc_name``."""

    is_generic_label: bool = False
    bluray_title: str | None = None
    bluray_source: str | None = None
    feature_duration_sec: int | None = None
    candidates: list[MovieCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_generic_label": self.is_generic_label,
            "bluray_title": self.bluray_title,
            "bluray_source": self.bluray_source,
            "feature_duration_sec": self.feature_duration_sec,
            "candidates": [asdict(c) for c in self.candidates],
            "notes": list(self.notes),
        }

    def best_title(self) -> str | None:
        if self.bluray_title:
            return self.bluray_title
        if len(self.candidates) == 1:
            return self.candidates[0].display()
        return None


def is_generic_disc_name(disc_name: str | None) -> bool:
    if not disc_name or not disc_name.strip():
        return True
    normalized = disc_name.strip().lower().replace(" ", "_")
    if normalized in GENERIC_DISC_NAMES:
        return True
    return bool(_GENERIC_RE.match(disc_name.strip()))


def feature_duration_sec(titles: list[dict[str, Any]] | dict[int, Any]) -> int | None:
    """Longest title duration in seconds (heuristic for main feature)."""
    durations: list[int] = []
    if isinstance(titles, dict):
        for info in titles.values():
            dur = getattr(info, "duration_sec", None)
            if dur is None and isinstance(info, dict):
                dur = info.get("duration_sec")
            if isinstance(dur, (int, float)) and dur > 0:
                durations.append(int(dur))
    else:
        for t in titles:
            dur = t.get("duration_sec", 0) if isinstance(t, dict) else 0
            if isinstance(dur, (int, float)) and dur > 0:
                durations.append(int(dur))
    return max(durations) if durations else None


def parse_bdmt_xml(xml_path: Path) -> str | None:
    """Extract ``di:name`` (or local-name ``name``) from a Blu-ray bdmt XML file."""
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError) as exc:
        logger.debug("bdmt parse failed for %s: %s", xml_path, exc)
        return None
    root = tree.getroot()
    # Prefer di:name under title; fall back to any element named "name"
    for elem in root.iter():
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local.lower() == "name" and elem.text and elem.text.strip():
            return elem.text.strip()
    return None


def find_bdmt_files(mount_root: Path) -> list[Path]:
    meta_dl = mount_root / "BDMV" / "META" / "DL"
    if not meta_dl.is_dir():
        # Some mounts put BDMV at the root; also try nested
        matches = list(mount_root.glob("**/BDMV/META/DL/bdmt_*.xml"))
        return sorted(matches)
    return sorted(meta_dl.glob("bdmt_*.xml"))


def read_bluray_title_from_mount(mount_root: Path) -> tuple[str | None, str | None]:
    """Return (title, source_path) from bdmt XML under a mounted BD."""
    files = find_bdmt_files(mount_root)
    # Prefer English locale files if present
    preferred = [p for p in files if "eng" in p.name.lower() or "en" in p.stem.lower()]
    ordered = preferred + [p for p in files if p not in preferred]
    for path in ordered:
        title = parse_bdmt_xml(path)
        if title:
            return title, str(path)
    return None, None


def _run(cmd: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def mount_optical_readonly(device: str, mountpoint: Path) -> tuple[bool, Path | None]:
    """Mount optical block device read-only.

    Returns ``(ok, actual_mount_path)``. When ``udisksctl`` mounts the disc,
    ``actual_mount_path`` may differ from ``mountpoint``.
    """
    block = disc_dev(device)
    # Prefer udisksctl — works without root for removable media.
    udisks = _run(["udisksctl", "mount", "-b", block, "-o", "ro"], timeout=45.0)
    if udisks.returncode == 0:
        # stdout like: Mounted /dev/sr0 at /run/media/lee/LABEL
        m = re.search(r"at (/[^\s.]+(?:\.[^\s]+)?)", (udisks.stdout or "") + (udisks.stderr or ""))
        if m:
            return True, Path(m.group(1))
        # Fallback: parse "at ..." more loosely
        m2 = re.search(r"at\s+(\S+)", udisks.stdout or "")
        if m2:
            return True, Path(m2.group(1).rstrip("."))
    mountpoint.mkdir(parents=True, exist_ok=True)
    proc = _run(["mount", "-o", "ro", block, str(mountpoint)], timeout=45.0)
    if proc.returncode == 0:
        return True, mountpoint
    for fstype in ("udf", "iso9660"):
        proc = _run(
            ["mount", "-t", fstype, "-o", "ro", block, str(mountpoint)],
            timeout=45.0,
        )
        if proc.returncode == 0:
            return True, mountpoint
    logger.debug(
        "mount failed for %s: %s",
        block,
        (proc.stderr or proc.stdout or udisks.stderr or "").strip(),
    )
    return False, None


def unmount_path(mountpoint: Path, *, device: str | None = None) -> None:
    block = disc_dev(device) if device else None
    if block:
        _run(["udisksctl", "unmount", "-b", block], timeout=30.0)
    _run(["umount", str(mountpoint)], timeout=30.0)
    try:
        mountpoint.rmdir()
    except OSError:
        pass


def read_bluray_title(device: str) -> tuple[str | None, str | None]:
    """Temporarily mount ``device`` and read bdmt title if present."""
    block = disc_dev(device)
    if not Path(block).exists():
        return None, None
    with tempfile.TemporaryDirectory(prefix="videosripper-bdmt-") as tmp:
        mountpoint = Path(tmp) / "mnt"
        ok, actual = mount_optical_readonly(device, mountpoint)
        if not ok or actual is None:
            return None, None
        try:
            return read_bluray_title_from_mount(actual)
        finally:
            unmount_path(actual, device=device)


def _http_get_json(url: str) -> dict[str, Any] | list[Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": "VideosRipper/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.debug("HTTP GET failed %s: %s", url.split("?")[0], exc)
        return None


def lookup_tmdb_by_runtime(
    duration_sec: int,
    *,
    tolerance_sec: int = DEFAULT_RUNTIME_TOLERANCE_SEC,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    api_key: str | None = None,
) -> list[MovieCandidate]:
    key = api_key if api_key is not None else TMDB_API_KEY
    if not key:
        return []
    runtime_min = max(1, round(duration_sec / 60))
    tol_min = max(1, round(tolerance_sec / 60))
    gte = max(1, runtime_min - tol_min)
    lte = runtime_min + tol_min
    params = urllib.parse.urlencode(
        {
            "api_key": key,
            "with_runtime.gte": gte,
            "with_runtime.lte": lte,
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "page": 1,
        }
    )
    url = f"https://api.themoviedb.org/3/discover/movie?{params}"
    data = _http_get_json(url)
    if not isinstance(data, dict):
        return []
    results = data.get("results") or []
    out: list[MovieCandidate] = []
    for row in results[:limit]:
        title = (row.get("title") or row.get("original_title") or "").strip()
        if not title:
            continue
        year = None
        date = row.get("release_date") or ""
        if len(date) >= 4 and date[:4].isdigit():
            year = int(date[:4])
        out.append(
            MovieCandidate(
                title=title,
                year=year,
                runtime_min=runtime_min,  # discover doesn't return runtime per row
                source="tmdb",
                external_id=str(row.get("id", "")),
                popularity=float(row.get("popularity") or 0.0),
            )
        )
    return out


def lookup_omdb_by_title_hint(
    title_hint: str,
    *,
    duration_sec: int | None = None,
    tolerance_sec: int = DEFAULT_RUNTIME_TOLERANCE_SEC,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    api_key: str | None = None,
) -> list[MovieCandidate]:
    """OMDb search by title string; optionally filter by runtime."""
    key = api_key if api_key is not None else OMDB_API_KEY
    if not key or not title_hint.strip():
        return []
    params = urllib.parse.urlencode({"apikey": key, "s": title_hint.strip(), "type": "movie"})
    data = _http_get_json(f"https://www.omdbapi.com/?{params}")
    if not isinstance(data, dict) or data.get("Response") == "False":
        return []
    out: list[MovieCandidate] = []
    for row in (data.get("Search") or [])[: limit * 2]:
        imdb_id = row.get("imdbID") or ""
        detail = None
        if imdb_id:
            dparams = urllib.parse.urlencode({"apikey": key, "i": imdb_id})
            detail = _http_get_json(f"https://www.omdbapi.com/?{dparams}")
        runtime_min = None
        if isinstance(detail, dict):
            rt = (detail.get("Runtime") or "").replace(" min", "").strip()
            if rt.isdigit():
                runtime_min = int(rt)
        if duration_sec is not None and runtime_min is not None:
            if abs(runtime_min * 60 - duration_sec) > tolerance_sec:
                continue
        year = None
        y = (row.get("Year") or "")[:4]
        if y.isdigit():
            year = int(y)
        title = (row.get("Title") or "").strip()
        if not title:
            continue
        out.append(
            MovieCandidate(
                title=title,
                year=year,
                runtime_min=runtime_min,
                source="omdb",
                external_id=imdb_id,
                popularity=0.0,
            )
        )
        if len(out) >= limit:
            break
    return out


def lookup_runtime_candidates(
    duration_sec: int,
    *,
    title_hint: str | None = None,
    tolerance_sec: int = DEFAULT_RUNTIME_TOLERANCE_SEC,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[MovieCandidate]:
    """Combine TMDb discover-by-runtime and optional OMDb title search."""
    seen: set[str] = set()
    merged: list[MovieCandidate] = []

    def _add(items: list[MovieCandidate]) -> None:
        for c in items:
            key = c.display().lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)

    _add(lookup_tmdb_by_runtime(duration_sec, tolerance_sec=tolerance_sec, limit=limit))
    if title_hint and not is_generic_disc_name(title_hint):
        _add(
            lookup_omdb_by_title_hint(
                title_hint,
                duration_sec=duration_sec,
                tolerance_sec=tolerance_sec,
                limit=limit,
            )
        )
    # If we have a Blu-ray title hint, also search OMDb/TMDb by that name via OMDb
    return merged[:limit]


def gather_identification_hints(
    scan_data: dict[str, Any],
    *,
    device: str | None = None,
    try_bluray_mount: bool = True,
    try_runtime_lookup: bool = True,
) -> IdentificationHints:
    """Build identification hints for a scan payload."""
    disc_name = scan_data.get("disc_name") or ""
    hints = IdentificationHints(is_generic_label=is_generic_disc_name(disc_name))
    titles = scan_data.get("titles") or []
    hints.feature_duration_sec = feature_duration_sec(titles)

    # Blu-ray XML (useful even when disc_name is not generic)
    if try_bluray_mount and device:
        title, source = read_bluray_title(device)
        if title:
            hints.bluray_title = title
            hints.bluray_source = source
            hints.notes.append(f"bdmt title: {title}")

    # Runtime DB lookup when label is generic (or always if no bluray title)
    need_lookup = try_runtime_lookup and (
        hints.is_generic_label or not hints.bluray_title
    )
    if need_lookup and hints.feature_duration_sec:
        if not TMDB_API_KEY and not OMDB_API_KEY:
            hints.notes.append(
                "runtime lookup skipped (set VIDEOSRIPPER_TMDB_API_KEY or "
                "VIDEOSRIPPER_OMDB_API_KEY)"
            )
        else:
            hint_title = hints.bluray_title
            hints.candidates = lookup_runtime_candidates(
                hints.feature_duration_sec,
                title_hint=hint_title,
            )
            if hints.candidates:
                names = ", ".join(c.display() for c in hints.candidates[:5])
                hints.notes.append(f"runtime candidates (~{hints.feature_duration_sec}s): {names}")
            else:
                hints.notes.append("runtime lookup returned no candidates")

    return hints


def attach_identification_to_scan(
    scan_data: dict[str, Any],
    hints: IdentificationHints,
) -> dict[str, Any]:
    """Return a copy of scan_data with an ``identification`` block."""
    out = dict(scan_data)
    out["identification"] = hints.to_dict()
    return out
