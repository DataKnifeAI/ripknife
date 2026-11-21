"""Disc title identification helpers for generic-labeled discs.

Generic labels are detected heuristically (patterns + boilerplate tokens),
not via an exhaustive allowlist of full volume-label strings.

Three sources of identification hints (never overwrite ``disc_name``):

1. **Known-disc fingerprints** — duration + feature size and/or AACS
   ``contentID``. Authoritative when they match. Runtime alone is too weak
   (two different films can share a ~110 min runtime).
2. **Blu-ray bdmt XML** — ``BDMV/META/DL/bdmt_*.xml`` often carries the real
   movie/show title even when MakeMKV's volume label is generic.
3. **Runtime lookup** — TMDb / OMDb discover/search by main-feature duration,
   returning ranked candidate titles for the LLM mapper (or a human) to pick.
   Two or more hits on a generic volume label are a collision, not a pick:
   the map fails with ``MAP_AMBIGUOUS`` until a fingerprint, bdmt title,
   or the user names the disc.
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

import yaml

from ripknife.paths import disc_dev, ensure_tmp_root

logger = logging.getLogger("ripknife")

# Seed vocabulary of uninformative volume-label tokens (not an exhaustive
# list of full disc names). Used by fuzzy/heuristic matching.
BOILERPLATE_TOKENS = frozenset(
    {
        "dvd",
        "video",
        "volume",
        "vol",
        "disc",
        "bd",
        "bluray",
        "blu",
        "ray",
        "udf",
        "title",
        "untitled",
        "unknown",
        "media",
        "ts",
        "bdmv",
        "avchd",
    }
)

# Structural patterns for common manufacturer/OS default volume labels.
_GENERIC_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^DVD[\s_-]*VOL(UME)?\d*$", re.IGNORECASE),
    re.compile(r"^DVD[\s_-]*VIDEO\d*$", re.IGNORECASE),
    re.compile(r"^VIDEO[\s_-]*TS\d*$", re.IGNORECASE),
    re.compile(r"^BD[\s_-]*VIDEO\d*$", re.IGNORECASE),
    re.compile(r"^BDMV\d*$", re.IGNORECASE),
    re.compile(r"^AVCHD\d*$", re.IGNORECASE),
    re.compile(r"^UNTITLED\b.*", re.IGNORECASE),
    re.compile(r"^UNKNOWN\b.*", re.IGNORECASE),
    re.compile(r"^DISC\d*$", re.IGNORECASE),
    re.compile(r"^VOL(UME)?\d*$", re.IGNORECASE),
    re.compile(r"^BLU[\s_-]*RAY\d*$", re.IGNORECASE),
    re.compile(r"^BLURAY\d*$", re.IGNORECASE),
    re.compile(r"^UDF\d*$", re.IGNORECASE),
    re.compile(r"^MEDIA\d*$", re.IGNORECASE),
    re.compile(r"^LOGICAL[\s_-]*VOLUME[\s_-]*ID\d*$", re.IGNORECASE),
    re.compile(r"^BD[\s_-]*ROM\d*$", re.IGNORECASE),
)

_VOWELS = frozenset("AEIOUY")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")
_SEPARATOR_RE = re.compile(r"[\s_-]+")
_EDGE_PUNCT_RE = re.compile(r"^[^\w]+|[^\w]+$", re.UNICODE)
_DIGITS_RE = re.compile(r"\d+")

# TMDb / OMDb keys (optional — lookups skipped when unset)
TMDB_API_KEY = os.environ.get("RIPKNIFE_TMDB_API_KEY") or os.environ.get("TMDB_API_KEY", "")
OMDB_API_KEY = os.environ.get("RIPKNIFE_OMDB_API_KEY") or os.environ.get("OMDB_API_KEY", "")

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
    aacs_content_id: str | None = None
    fingerprint_title: str | None = None
    fingerprint_source: str | None = None
    feature_duration_sec: int | None = None
    candidates: list[MovieCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ambiguous: bool = False
    ambiguous_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_generic_label": self.is_generic_label,
            "bluray_title": self.bluray_title,
            "bluray_source": self.bluray_source,
            "aacs_content_id": self.aacs_content_id,
            "fingerprint_title": self.fingerprint_title,
            "fingerprint_source": self.fingerprint_source,
            "feature_duration_sec": self.feature_duration_sec,
            "candidates": [asdict(c) for c in self.candidates],
            "notes": list(self.notes),
            "ambiguous": self.ambiguous,
            "ambiguous_reason": self.ambiguous_reason,
        }

    def best_title(self) -> str | None:
        if self.fingerprint_title:
            return self.fingerprint_title
        if self.bluray_title:
            return self.bluray_title
        if self.ambiguous:
            return None
        if len(self.candidates) == 1:
            return self.candidates[0].display()
        return None


@dataclass(frozen=True)
class KnownDiscFingerprint:
    """Pressed-disc identity that runtime-only matching must not override."""

    title: str
    year: int
    duration_sec: int
    size_bytes: int
    content_id_needles: tuple[str, ...]
    reject_titles: tuple[str, ...] = ()
    duration_tol_sec: int = 5
    size_tol_bytes: int = 250_000_000

    def display(self) -> str:
        return f"{self.title} ({self.year})"


def load_fingerprints(path: Path | None = None) -> tuple[KnownDiscFingerprint, ...]:
    """Load pressed-disc fingerprints from the personal config dir.

    The tool repo ships none. A ``fingerprints.yaml`` next to the operator's
    manifests supplies title, year, duration, size, and content-id needles.
    Duration alone never matches.
    """
    if path is None:
        from ripknife.paths import config_search_dirs

        found: Path | None = None
        for directory in config_search_dirs():
            candidate = directory / "fingerprints.yaml"
            if candidate.is_file():
                found = candidate
                break
        if found is None:
            return ()
        path = found
    elif not Path(path).is_file():
        return ()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_items = data.get("fingerprints", []) if isinstance(data, dict) else []
    loaded: list[KnownDiscFingerprint] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        loaded.append(
            KnownDiscFingerprint(
                title=str(raw["title"]),
                year=int(raw["year"]),
                duration_sec=int(raw["duration_sec"]),
                size_bytes=int(raw["size_bytes"]),
                content_id_needles=tuple(str(item) for item in raw.get("content_id_needles") or ()),
                reject_titles=tuple(str(item) for item in raw.get("reject_titles") or ()),
                duration_tol_sec=int(raw.get("duration_tol_sec", 5)),
                size_tol_bytes=int(raw.get("size_tol_bytes", 250_000_000)),
            )
        )
    return tuple(loaded)


KNOWN_DISC_FINGERPRINTS: tuple[KnownDiscFingerprint, ...] = load_fingerprints()


def _normalize_disc_label(disc_name: str) -> tuple[str, str, list[str]]:
    """Uppercase, strip, collapse separators → (spaced, compact, tokens)."""
    raw = disc_name.strip().upper()
    spaced = _SEPARATOR_RE.sub(" ", raw).strip()
    spaced = re.sub(r"\bBLU\s+RAY\b", "BLURAY", spaced)
    compact = _SEPARATOR_RE.sub("", raw)
    tokens: list[str] = []
    for part in spaced.split():
        cleaned = _EDGE_PUNCT_RE.sub("", part)
        if cleaned:
            tokens.append(cleaned.lower())
    return spaced, compact, tokens


def _matches_generic_pattern(raw: str, spaced: str, compact: str) -> bool:
    for pat in _GENERIC_LABEL_PATTERNS:
        if pat.match(raw) or pat.match(spaced) or pat.match(compact):
            return True
    return False


def _tokens_are_boilerplate(tokens: list[str]) -> bool:
    """True when every letter-bearing token is media boilerplate vocabulary."""
    if not tokens:
        return True
    letter_tokens: list[str] = []
    for tok in tokens:
        alpha = _DIGITS_RE.sub("", tok)
        if alpha:
            letter_tokens.append(alpha)
    if not letter_tokens:
        # Digits-only labels (e.g. "01") — treat short ones as uninformative.
        return len(tokens) <= 2
    return all(t in BOILERPLATE_TOKENS for t in letter_tokens)


def _looks_like_opaque_serial(compact: str) -> bool:
    """Flag long vowel-less / digit-heavy serials; spare real short titles."""
    s = _NON_ALNUM_RE.sub("", compact.upper())
    if len(s) < 8:
        return False
    letters = re.sub(r"[^A-Z]", "", s)
    digits = re.sub(r"[^0-9]", "", s)
    if not letters:
        return True
    if not any(c in _VOWELS for c in letters):
        return bool(digits) or len(letters) >= 10
    return False


def is_generic_disc_name(disc_name: str | None) -> bool:
    """Return True when ``disc_name`` is an uninformative volume label.

    Uses heuristics (patterns + boilerplate-token vocabulary), not an
    exhaustive allowlist of full label strings. Short real titles must not match.
    """
    if not disc_name or not disc_name.strip():
        return True
    raw = disc_name.strip()
    spaced, compact, tokens = _normalize_disc_label(raw)

    if _matches_generic_pattern(raw, spaced, compact):
        return True

    # Entirely boilerplate tokens after stripping digits/separators
    # (also covers short opaque labels of ≤3 boilerplate tokens).
    if tokens and _tokens_are_boilerplate(tokens):
        return True

    if _looks_like_opaque_serial(compact):
        return True

    return False


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


def feature_size_bytes(titles: list[dict[str, Any]] | dict[int, Any]) -> int | None:
    """Size of the longest-duration title (main feature), if present."""
    best_dur = -1
    best_size: int | None = None
    rows = titles.values() if isinstance(titles, dict) else titles
    for info in rows:
        if isinstance(info, dict):
            dur = info.get("duration_sec") or 0
            size = info.get("size_bytes")
        else:
            dur = getattr(info, "duration_sec", 0) or 0
            size = getattr(info, "size_bytes", None)
        if not isinstance(dur, (int, float)) or int(dur) <= best_dur:
            continue
        best_dur = int(dur)
        if isinstance(size, (int, float)) and int(size) > 0:
            best_size = int(size)
        else:
            best_size = None
    return best_size


def _content_id_matches(content_id: str | None, needles: tuple[str, ...]) -> bool:
    if not content_id or not needles:
        return False
    raw = content_id.upper()
    compact = re.sub(r"[^0-9A-F]", "", raw)
    for needle in needles:
        n = needle.upper()
        n_hex = re.sub(r"[^0-9A-F]", "", n)
        if n in raw or (n_hex and n_hex in compact):
            return True
    return False


def match_known_fingerprint(
    *,
    duration_sec: int | None,
    size_bytes: int | None = None,
    content_id: str | None = None,
) -> KnownDiscFingerprint | None:
    """Match a pressed disc. Duration alone is never enough."""
    if duration_sec is None:
        return None
    for fp in KNOWN_DISC_FINGERPRINTS:
        if abs(int(duration_sec) - fp.duration_sec) > fp.duration_tol_sec:
            continue
        cid_ok = _content_id_matches(content_id, fp.content_id_needles)
        size_ok = (
            size_bytes is not None
            and abs(int(size_bytes) - fp.size_bytes) <= fp.size_tol_bytes
        )
        if cid_ok or size_ok:
            return fp
    return None


def mark_runtime_ambiguity(hints: IdentificationHints) -> None:
    """Flag a generic label when two films share the feature runtime.

    A fingerprint or bdmt title is stronger evidence and stays unambiguous.
    One runtime candidate is not a collision. Extras and studio stamps are
    not considered here — they are not an identification.
    """
    if hints.fingerprint_title or hints.bluray_title or not hints.is_generic_label:
        return
    feature = hints.feature_duration_sec
    titles: list[str] = []
    seen: set[str] = set()
    for cand in hints.candidates:
        if cand.source == "fingerprint":
            continue
        if feature is not None and cand.runtime_min is not None:
            delta = abs(cand.runtime_min * 60 - int(feature))
            if delta > DEFAULT_RUNTIME_TOLERANCE_SEC:
                continue
        key = cand.display().lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(cand.display())
    if len(titles) < 2:
        return
    shown = ", ".join(titles[:6])
    extra = f" (+{len(titles) - 6} more)" if len(titles) > 6 else ""
    duration = f"~{feature}s" if feature is not None else "the feature runtime"
    hints.ambiguous = True
    hints.ambiguous_reason = (
        f"generic volume label; {len(titles)} films share {duration} "
        f"({shown}{extra}). Runtime, extras, and studio stamps are not an ID."
    )
    hints.notes.append(f"MAP_AMBIGUOUS: {hints.ambiguous_reason}")


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


def parse_mcmf_content_id(xml_path: Path) -> str | None:
    """Extract AACS ``contentID`` from ``AACS/mcmf.xml``."""
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError) as exc:
        logger.debug("mcmf parse failed for %s: %s", xml_path, exc)
        return None
    root = tree.getroot()
    for key, val in root.attrib.items():
        local = key.split("}")[-1] if "}" in key else key
        if local.lower() == "contentid" and val and str(val).strip():
            return str(val).strip()
    return None


def find_mcmf_files(mount_root: Path) -> list[Path]:
    primary = mount_root / "AACS" / "mcmf.xml"
    if primary.is_file():
        return [primary]
    return sorted(p for p in mount_root.glob("**/AACS/mcmf.xml") if "DUPLICATE" not in p.parts)


def read_aacs_content_id_from_mount(mount_root: Path) -> str | None:
    for path in find_mcmf_files(mount_root):
        cid = parse_mcmf_content_id(path)
        if cid:
            return cid
    return None


def existing_optical_mount(device: str) -> Path | None:
    """Return an already-mounted path for ``device``, if any."""
    block = disc_dev(device)
    proc = _run(["findmnt", "-n", "-o", "TARGET", block], timeout=5.0)
    for line in (proc.stdout or "").splitlines():
        target = line.strip()
        if target:
            path = Path(target)
            if path.is_dir():
                return path
    return None


def _mount_meta_from_root(mount_root: Path) -> tuple[str | None, str | None, str | None]:
    title, source = read_bluray_title_from_mount(mount_root)
    return title, source, read_aacs_content_id_from_mount(mount_root)


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


def read_disc_mount_meta(device: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(bdmt_title, bdmt_source, aacs_content_id)`` from a BD mount.

    Prefers an existing mount (MakeMKV may already hold the drive).
    """
    existing = existing_optical_mount(device)
    if existing is not None:
        return _mount_meta_from_root(existing)
    block = disc_dev(device)
    if not Path(block).exists():
        return None, None, None
    # Mount point on TMP_ROOT (game drive), not /tmp tmpfs.
    with tempfile.TemporaryDirectory(
        prefix="ripknife-bdmt-", dir=ensure_tmp_root()
    ) as tmp:
        mountpoint = Path(tmp) / "mnt"
        ok, actual = mount_optical_readonly(device, mountpoint)
        if not ok or actual is None:
            return None, None, None
        try:
            return _mount_meta_from_root(actual)
        finally:
            unmount_path(actual, device=device)


def read_bluray_title(device: str) -> tuple[str | None, str | None]:
    """Temporarily mount ``device`` and read bdmt title if present."""
    title, source, _cid = read_disc_mount_meta(device)
    return title, source


def _http_get_json(url: str) -> dict[str, Any] | list[Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Ripknife/0.1"})
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
    size_bytes = feature_size_bytes(titles)

    # Blu-ray XML + AACS (useful even when disc_name is not generic)
    if try_bluray_mount and device:
        title, source, cid = read_disc_mount_meta(device)
        if title:
            hints.bluray_title = title
            hints.bluray_source = source
            hints.notes.append(f"bdmt title: {title}")
        if cid:
            hints.aacs_content_id = cid
            hints.notes.append(f"aacs contentID: {cid}")

    fp = match_known_fingerprint(
        duration_sec=hints.feature_duration_sec,
        size_bytes=size_bytes,
        content_id=hints.aacs_content_id,
    )
    if fp:
        hints.fingerprint_title = fp.display()
        hints.fingerprint_source = "known_disc_fingerprint"
        hints.candidates = [
            MovieCandidate(
                title=fp.title,
                year=fp.year,
                runtime_min=max(1, round(fp.duration_sec / 60)),
                source="fingerprint",
                external_id=",".join(fp.content_id_needles),
            )
        ]
        note = f"fingerprint: {fp.display()} (duration+size/AACS)"
        if fp.reject_titles:
            note += f"; not {', '.join(fp.reject_titles)}"
        hints.notes.append(note)

    # Runtime DB lookup when label is generic (or always if no bluray title).
    # Skip when a known fingerprint already identified the disc.
    need_lookup = (
        try_runtime_lookup
        and fp is None
        and (hints.is_generic_label or not hints.bluray_title)
    )
    if need_lookup and hints.feature_duration_sec:
        if not TMDB_API_KEY and not OMDB_API_KEY:
            hints.notes.append(
                "runtime lookup skipped (set RIPKNIFE_TMDB_API_KEY or "
                "RIPKNIFE_OMDB_API_KEY)"
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

    mark_runtime_ambiguity(hints)
    return hints


def attach_identification_to_scan(
    scan_data: dict[str, Any],
    hints: IdentificationHints,
) -> dict[str, Any]:
    """Return a copy of scan_data with an ``identification`` block."""
    out = dict(scan_data)
    out["identification"] = hints.to_dict()
    return out
