"""ffmpeg split and chapter extraction helpers."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ripknife.logging_setup import log_debug
from ripknife.models import SplitConfig, SplitOutput

logger = logging.getLogger("ripknife")

_BLACK_DURATION_RE = re.compile(r"black_duration:(\d+(?:\.\d+)?)")
_SILENCE_DURATION_RE = re.compile(r"silence_duration:\s*(\d+(?:\.\d+)?)")


def probe_duration(path: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
    ]
    log_debug("$ %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )
    duration = int(float(proc.stdout.strip()))
    log_debug("ffprobe duration=%ss (%s)", duration, path.name)
    return duration


def _probe_streams(path: Path) -> list[dict]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    log_debug("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)["streams"]


def _stream_language(stream: dict) -> str:
    tags = stream.get("tags") or {}
    return str(tags.get("language", "")).lower()


def _pick_default_stream(
    streams: list[dict],
    lang: str,
    *,
    prefer_lossless: bool = False,
) -> int:
    """Return index into streams list for the preferred default track."""
    lang = lang.lower()
    matches = [i for i, stream in enumerate(streams) if _stream_language(stream).startswith(lang[:3])]
    if not matches:
        raise RuntimeError(f"No {lang} stream found in {streams[0].get('codec_type', 'unknown')} tracks")
    if not prefer_lossless or len(matches) == 1:
        return matches[0]
    lossless_markers = ("truehd", "dts", "flac", "pcm")
    for idx in matches:
        codec = str(streams[idx].get("codec_name", "")).lower()
        if any(marker in codec for marker in lossless_markers):
            return idx
    return matches[0]


def apply_stream_defaults(
    src: Path,
    dest: Path,
    *,
    audio_lang: str | None = None,
    subtitle_lang: str | None = None,
    dry_run: bool = False,
) -> None:
    """Remux MKV to set default audio/subtitle tracks without removing streams.

    When ``audio_lang`` is set and ``subtitle_lang`` is omitted/None, all subtitle
    default dispositions are cleared (no forced default subtitle track).
    """
    if not audio_lang and not subtitle_lang:
        if src != dest:
            shutil.move(str(src), str(dest))
        return

    streams = _probe_streams(src)
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0", "-c", "copy"]
    if audio_lang:
        default_idx = _pick_default_stream(audio_streams, audio_lang, prefer_lossless=True)
        for rel_idx in range(len(audio_streams)):
            flag = "default" if rel_idx == default_idx else "0"
            cmd.extend([f"-disposition:a:{rel_idx}", flag])
    if subtitle_lang:
        default_idx = _pick_default_stream(subtitle_streams, subtitle_lang)
        for rel_idx in range(len(subtitle_streams)):
            flag = "default" if rel_idx == default_idx else "0"
            cmd.extend([f"-disposition:s:{rel_idx}", flag])
    elif subtitle_streams:
        # Omitting subtitle_default while remuxing → disable subtitle default flag
        for rel_idx in range(len(subtitle_streams)):
            cmd.extend([f"-disposition:s:{rel_idx}", "0"])
    cmd.append(str(dest))
    log_debug("$ %s", " ".join(cmd))
    if dry_run:
        return
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        detail = err.splitlines()[-1] if err else f"exit {proc.returncode}"
        raise RuntimeError(f"ffmpeg stream defaults remux failed: {detail}")
    if src != dest and src.exists():
        src.unlink()


def apply_stream_defaults_or_keep(
    src: Path,
    dest: Path,
    *,
    audio_lang: str | None = None,
    subtitle_lang: str | None = None,
    dry_run: bool = False,
) -> Path:
    """Remux with stream defaults; on failure keep original and log WARNING."""
    if not audio_lang and not subtitle_lang:
        if src != dest:
            shutil.move(str(src), str(dest))
            return dest
        return src
    try:
        apply_stream_defaults(
            src,
            dest,
            audio_lang=audio_lang,
            subtitle_lang=subtitle_lang,
            dry_run=dry_run,
        )
        return dest if not dry_run else src
    except Exception as exc:
        logger.warning(
            "WARNING: ffmpeg stream defaults failed — keeping original rip (%s): %s",
            src.name,
            exc,
        )
        if dest.exists() and dest != src:
            dest.unlink(missing_ok=True)
        return src


def _probe_chapter_lines(path: Path) -> list[str]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "chapter=start_time,end_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [ln for ln in proc.stdout.strip().splitlines() if ln]


def _chapter_range_error(
    path: Path,
    chapter_index: int,
    end_index: int,
    chapter_count: int,
    *,
    slot: int | None = None,
) -> RuntimeError:
    """Build a clear out-of-range error for inclusive 0-based chapter indices."""
    where = f"slot {slot} " if slot is not None else ""
    last = chapter_count - 1
    hint = ""
    if chapter_count > 0 and end_index == chapter_count:
        hint = (
            f" chapter_end is inclusive — use {last} for the last chapter, "
            f"not {chapter_count} (chapter count)."
        )
    elif chapter_count > 0 and end_index > last:
        hint = f" Valid inclusive indices are 0..{last}."
    return RuntimeError(
        f"{where}Chapter range {chapter_index}-{end_index} out of range "
        f"({chapter_count} chapters in {path.name}).{hint}"
    )


def chapter_bounds(
    path: Path,
    chapter_index: int,
    chapter_end: int | None = None,
) -> tuple[float, float]:
    """Return (start, end) seconds for one chapter or an inclusive chapter range."""
    lines = _probe_chapter_lines(path)
    end_index = chapter_index if chapter_end is None else chapter_end
    if chapter_index < 0 or end_index < chapter_index or end_index >= len(lines):
        raise _chapter_range_error(path, chapter_index, end_index, len(lines))
    start_s = lines[chapter_index].split(",")[0]
    end_s = lines[end_index].split(",")[1]
    return float(start_s), float(end_s)


def validate_extract_chapter_ranges(
    path: Path,
    ranges: list[tuple[int, int, int]],
) -> int:
    """Fail-fast check that inclusive chapter ranges fit the ripped title.

    ``ranges`` items are ``(slot, chapter_index, chapter_end)`` with inclusive ends.
    Returns the probed chapter count.
    """
    lines = _probe_chapter_lines(path)
    count = len(lines)
    for slot, chapter_index, end_index in ranges:
        if chapter_index < 0 or end_index < chapter_index or end_index >= count:
            raise _chapter_range_error(
                path, chapter_index, end_index, count, slot=slot
            )
    return count


def parse_ffmpeg_out_time_us(line: str) -> int | None:
    """Parse ``out_time_us=`` from ffmpeg ``-progress`` output."""
    stripped = line.strip()
    if not stripped.startswith("out_time_us="):
        return None
    raw = stripped.split("=", 1)[1]
    try:
        return int(raw)
    except ValueError:
        return None


def ffmpeg_time_pct(out_time_us: int, duration_us: int) -> int:
    if duration_us <= 0:
        return 0
    return max(0, min(100, int(100 * out_time_us / duration_us)))


def extract_chapter(
    src: Path,
    chapter_index: int,
    dest: Path,
    dry_run: bool = False,
    chapter_end: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    start, end = chapter_bounds(src, chapter_index, chapter_end=chapter_end)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ss",
        str(start),
        "-to",
        str(end),
        "-map",
        "0",
        "-map",
        "-0:d",
        "-c",
        "copy",
        "-reset_timestamps",
        "1",
        str(dest),
    ]
    if dry_run:
        return
    if on_progress is None:
        subprocess.run(cmd, capture_output=True, check=True)
        return
    progress_cmd = ["ffmpeg", "-y", "-nostats", "-progress", "pipe:1", *cmd[2:]]
    duration_us = max(1, int((end - start) * 1_000_000))
    proc = subprocess.Popen(
        progress_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    last = -1
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            us = parse_ffmpeg_out_time_us(raw)
            if us is not None:
                pct = ffmpeg_time_pct(us, duration_us)
                if pct != last:
                    on_progress(pct)
                    last = pct
                continue
            if raw.strip() == "progress=end" and last < 100:
                on_progress(100)
                last = 100
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode or -1, progress_cmd)


def split_at(src: Path, config: SplitConfig, dest_dir: Path, dry_run: bool = False) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    first, second = config.outputs
    out1 = dest_dir / f"part_{first.episode}.mkv"
    out2 = dest_dir / f"part_{second.episode}.mkv"
    split_at_sec = config.at_seconds
    if dry_run:
        return [out1, out2]
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-map",
            "0",
            "-map",
            "-0:d",
            "-c",
            "copy",
            "-reset_timestamps",
            "1",
            "-to",
            str(split_at_sec),
            str(out1),
        ],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ss",
            str(split_at_sec),
            "-map",
            "0",
            "-map",
            "-0:d",
            "-c",
            "copy",
            "-reset_timestamps",
            "1",
            str(out2),
        ],
        capture_output=True,
        check=True,
    )
    return [out1, out2]


def extract_slice(
    src: Path,
    dest: Path,
    *,
    offset_sec: float = 0,
    duration_sec: float,
    dry_run: bool = False,
) -> None:
    """Remux a time slice from ``src`` without re-encoding."""
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(offset_sec),
        "-i",
        str(src),
        "-t",
        str(duration_sec),
        "-map",
        "0",
        "-map",
        "-0:d",
        "-c",
        "copy",
        "-reset_timestamps",
        "1",
        str(dest),
    ]
    log_debug("$ %s", " ".join(cmd))
    if dry_run:
        return
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        detail = err.splitlines()[-1] if err else f"exit {proc.returncode}"
        raise RuntimeError(f"ffmpeg slice extract failed: {detail}")


def _filter_stderr_duration(stderr: str, pattern: re.Pattern[str]) -> float:
    total = 0.0
    for match in pattern.finditer(stderr):
        total += float(match.group(1))
    return total


def black_duration_ratio(path: Path) -> float | None:
    """Fraction of runtime flagged as black/static by ffmpeg blackdetect."""
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        "blackdetect=d=0.12:pix_th=0.10",
        "-an",
        "-f",
        "null",
        "-",
    ]
    log_debug("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    duration = probe_duration(path)
    if duration <= 0:
        return None
    black_sec = _filter_stderr_duration(proc.stderr, _BLACK_DURATION_RE)
    return min(1.0, black_sec / duration)


def silence_duration_ratio(path: Path) -> float | None:
    """Fraction of runtime flagged as silent by ffmpeg silencedetect."""
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-af",
        "silencedetect=noise=-35dB:d=0.25",
        "-f",
        "null",
        "-",
    ]
    log_debug("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    duration = probe_duration(path)
    if duration <= 0:
        return None
    silent_sec = _filter_stderr_duration(proc.stderr, _SILENCE_DURATION_RE)
    return min(1.0, silent_sec / duration)


def split_outputs_to_paths(
    config: SplitConfig,
    dest_dir: Path,
    season: int,
    show: str,
) -> list[tuple[SplitOutput, Path]]:
    from ripknife.runner import episode_filename

    paths: list[tuple[SplitOutput, Path]] = []
    for out in config.outputs:
        filename = episode_filename(show, season, out.episode, out.title)
        paths.append((out, dest_dir / filename))
    return paths
