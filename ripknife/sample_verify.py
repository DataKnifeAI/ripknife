"""Pre-rip title slice preview: partial rip + ffmpeg content checks."""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ripknife import ffmpeg_ops
from ripknife.logging_setup import log_debug
from ripknife.makemkv import ScanResult, TitleInfo, rip_title, scan
from ripknife.paths import STAGING_ROOT
from ripknife.title_verify import titles_from_scan_json

logger = logging.getLogger("ripknife")

# Sample mostly black/static → likely FBI loop, menu, or wrong title.
_MAX_BLACK_RATIO = 0.75
# Sample mostly silent → likely protection loop or bad rip.
_MAX_SILENCE_RATIO = 0.90


@dataclass(frozen=True)
class SampleVerifyResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    sample_path: Path | None = None
    ripped_path: Path | None = None
    black_ratio: float | None = None
    silence_ratio: float | None = None
    duration_sec: int | None = None


def build_title_spec(
    title_index: int,
    *,
    chapter: int | None = None,
    chapter_end: int | None = None,
) -> str:
    """MakeMKV title argument; chapter suffix limits rip to chapter range (DVD manual mode)."""
    if chapter is None:
        return str(title_index)
    end = chapter_end if chapter_end is not None else chapter
    if end == chapter:
        return f"{title_index}:{chapter}"
    return f"{title_index}:{chapter}-{end}"


def _resolve_scan_title(
    scan_result: ScanResult | None,
    scan_data: dict | None,
    title_index: int,
) -> TitleInfo | None:
    if scan_result is not None:
        return scan_result.titles.get(title_index)
    if scan_data is not None:
        return titles_from_scan_json(scan_data).get(title_index)
    return None


def _estimate_rip_seconds(
    title_info: TitleInfo | None,
    *,
    chapter: int | None,
    seconds: int,
    offset: int,
) -> int:
    """Rough upper bound for preview rip time (used for logging only)."""
    if chapter is not None:
        return min(120, seconds + offset + 30)
    if title_info is not None and title_info.duration_sec > 0:
        # Full title rip — warn when likely >> 2 min target.
        return title_info.duration_sec
    return seconds + offset + 60


def rip_preview_title(
    device: str,
    title_index: int,
    staging: Path,
    *,
    minlength: int = 300,
    chapter: int | None = None,
    chapter_end: int | None = None,
) -> Path:
    """Rip a title (or chapter range) into staging for preview analysis."""
    spec = build_title_spec(title_index, chapter=chapter, chapter_end=chapter_end)
    log_debug("Preview rip spec: %s", spec)
    ripped = rip_title(
        device,
        spec,
        staging,
        minlength=minlength,
        retries=0,
        eject_on_exhaust=False,
    )
    assert ripped is not None
    return ripped


def analyze_sample_slice(
    sample_path: Path,
    *,
    expected_seconds: int,
    title_info: TitleInfo | None = None,
) -> SampleVerifyResult:
    """Run ffprobe + blackframe/silence heuristics on an extracted slice."""
    reasons: list[str] = []
    if not sample_path.is_file():
        return SampleVerifyResult(ok=False, reasons=(f"sample missing: {sample_path.name}",))

    duration = ffmpeg_ops.probe_duration(sample_path)
    tol = max(5, int(expected_seconds * 0.15))
    if abs(duration - expected_seconds) > tol:
        reasons.append(
            f"sample duration {duration}s vs requested {expected_seconds}s (±{tol}s)"
        )

    black_ratio = ffmpeg_ops.black_duration_ratio(sample_path)
    if black_ratio is not None and black_ratio > _MAX_BLACK_RATIO:
        reasons.append(
            f"sample {black_ratio:.0%} black/static — likely FBI loop, menu, or wrong title"
        )

    silence_ratio = ffmpeg_ops.silence_duration_ratio(sample_path)
    if silence_ratio is not None and silence_ratio > _MAX_SILENCE_RATIO:
        reasons.append(
            f"sample {silence_ratio:.0%} silent — likely protection loop or bad audio"
        )

    if title_info is not None and title_info.duration_sec >= 480 and duration < 20:
        reasons.append(
            f"sample only {duration}s from title expecting ~{title_info.duration_sec}s feature content"
        )

    return SampleVerifyResult(
        ok=not reasons,
        reasons=tuple(reasons),
        sample_path=sample_path,
        black_ratio=black_ratio,
        silence_ratio=silence_ratio,
        duration_sec=duration,
    )


def preview_title(
    device: str,
    title_index: int,
    *,
    seconds: int = 45,
    offset: int = 60,
    minlength: int = 300,
    chapter: int | None = None,
    chapter_end: int | None = None,
    scan_result: ScanResult | None = None,
    scan_data: dict | None = None,
    keep: bool = False,
    staging: Path | None = None,
) -> SampleVerifyResult:
    """Rip a short slice from a disc title and verify it looks like real content."""
    title_info = _resolve_scan_title(scan_result, scan_data, title_index)
    est = _estimate_rip_seconds(title_info, chapter=chapter, seconds=seconds, offset=offset)
    if chapter is None and title_info is not None and est > 180:
        logger.warning(
            "Preview will rip full title %s (~%ss) before slicing — use --chapter to speed up",
            title_index,
            est,
        )

    own_staging = staging is None
    if staging is None:
        STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".preview.", dir=STAGING_ROOT))

    ripped: Path | None = None
    sample_path: Path | None = None
    try:
        ripped = rip_preview_title(
            device,
            title_index,
            staging,
            minlength=minlength,
            chapter=chapter,
            chapter_end=chapter_end,
        )
        sample_path = staging / f"sample_t{title_index}.mkv"
        ffmpeg_ops.extract_slice(ripped, sample_path, offset_sec=offset, duration_sec=seconds)
        result = analyze_sample_slice(
            sample_path,
            expected_seconds=seconds,
            title_info=title_info,
        )
        if keep:
            kept = staging / f"preview_t{title_index}_{offset}s_{seconds}s.mkv"
            shutil.copy2(sample_path, kept)
            return SampleVerifyResult(
                ok=result.ok,
                reasons=result.reasons,
                sample_path=kept,
                ripped_path=ripped if keep else None,
                black_ratio=result.black_ratio,
                silence_ratio=result.silence_ratio,
                duration_sec=result.duration_sec,
            )
        return SampleVerifyResult(
            ok=result.ok,
            reasons=result.reasons,
            sample_path=None,
            ripped_path=None,
            black_ratio=result.black_ratio,
            silence_ratio=result.silence_ratio,
            duration_sec=result.duration_sec,
        )
    finally:
        if not keep:
            if sample_path is not None and sample_path.exists():
                sample_path.unlink(missing_ok=True)
            if ripped is not None and ripped.exists():
                ripped.unlink(missing_ok=True)
        if own_staging and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def preview_title_from_scan_file(
    device: str,
    title_index: int,
    scan_json_path: Path,
    **kwargs,
) -> SampleVerifyResult:
    import json

    scan_data = json.loads(scan_json_path.read_text(encoding="utf-8"))
    return preview_title(
        device,
        title_index,
        scan_data=scan_data,
        **kwargs,
    )


def scan_and_preview(
    device: str,
    title_index: int,
    *,
    minlength: int = 300,
    **kwargs,
) -> SampleVerifyResult:
    """Scan disc then preview one title (mapper workflow)."""
    scan_result = scan(device, minlength)
    return preview_title(
        device,
        title_index,
        minlength=minlength,
        scan_result=scan_result,
        **kwargs,
    )
