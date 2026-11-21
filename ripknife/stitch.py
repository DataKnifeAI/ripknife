"""Lossless multi-disc movie stitch (post-rip, outside makemkv lock)."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from ripknife import paths
from ripknife.logging_setup import (
    PROCESS_RIP,
    log_copy_result,
    log_copy_start,
    log_local_cleanup,
    log_stage,
)
from ripknife.models import (
    MovieManifest,
    movie_output_path,
    part_filename_for,
    stitch_group_id,
    stitch_output_path,
)
from ripknife.nas import (
    copy_movie_to_share,
    delete_local_after_nas_copy,
    resolve_delete_local_after_nas,
)
from ripknife.paths import MOVIES_NAS, STAGING_ROOT, resolve_path

logger = logging.getLogger("ripknife")

STITCH_STATE_DIR = STAGING_ROOT / "stitch"
_PART_GLOB = re.compile(r" - Disc (\d+)\.mkv$", re.IGNORECASE)


@dataclass
class StitchResult:
    ok: bool
    output_path: Path | None = None
    message: str = ""
    nas_ok: bool | None = None


def _duration_sec(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    raw = (proc.stdout or "").strip()
    if not raw or raw == "N/A":
        raise RuntimeError(f"could not read duration: {path}")
    return float(raw)


def _stream_summary(path: Path) -> list[str]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _validate_codec_params(part_paths: list[Path]) -> None:
    if len(part_paths) < 2:
        return
    baseline = _stream_summary(part_paths[0])
    for part in part_paths[1:]:
        other = _stream_summary(part)
        if baseline and other and baseline != other:
            logger.warning(
                "Stitch codec/layout mismatch: %s vs %s — mkvmerge/ffmpeg may fail or gap timestamps",
                part_paths[0].name,
                part.name,
            )


def _validate_duration(
    output_path: Path,
    part_paths: list[Path],
    *,
    tolerance_pct: float,
) -> None:
    sum_inputs = sum(_duration_sec(p) for p in part_paths)
    out_dur = _duration_sec(output_path)
    if sum_inputs <= 0:
        raise RuntimeError("invalid input duration sum")
    diff = abs(out_dur - sum_inputs)
    pct = 100.0 * diff / sum_inputs
    if pct > tolerance_pct:
        raise RuntimeError(
            f"duration mismatch: out={out_dur:.3f}s sum={sum_inputs:.3f}s "
            f"({pct:.3f}% > {tolerance_pct}% tolerance)"
        )
    logger.info(
        "Stitch duration check OK (out=%.3fs sum=%.3fs Δ=%.3fs %.3f%%)",
        out_dur,
        sum_inputs,
        diff,
        pct,
    )


def _run_mkvmerge(output_path: Path, part_paths: list[Path]) -> None:
    args = ["mkvmerge", "-o", str(output_path), str(part_paths[0])]
    for part in part_paths[1:]:
        args.extend(["+", str(part)])
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"mkvmerge failed: {(proc.stderr or proc.stdout).strip()}")


def _run_ffmpeg_concat(output_path: Path, part_paths: list[Path]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as handle:
        list_path = Path(handle.name)
        for part in part_paths:
            esc = str(part).replace("'", "'\\''")
            handle.write(f"file '{esc}'\n")
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {(proc.stderr or proc.stdout).strip()}")
    finally:
        list_path.unlink(missing_ok=True)


def stitch_movie_parts(
    output_path: Path,
    part_paths: list[Path],
    *,
    prefer_mkvmerge: bool = True,
    tolerance_pct: float = 2.0,
) -> Path:
    """Losslessly append movie parts into one MKV. Returns ``output_path``."""
    if len(part_paths) < 2:
        raise ValueError("need at least two input parts")
    for part in part_paths:
        if not part.is_file():
            raise FileNotFoundError(f"missing input part: {part}")
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _validate_codec_params(part_paths)

    tool = None
    if prefer_mkvmerge and which("mkvmerge"):
        tool = "mkvmerge"
    elif which("ffmpeg"):
        tool = "ffmpeg"
    else:
        raise RuntimeError(
            "neither mkvmerge nor ffmpeg found in PATH "
            "(install: sudo pacman -S mkvtoolnix-cli)"
        )

    logger.info("Stitching %d parts with %s → %s", len(part_paths), tool, output_path.name)
    if tool == "mkvmerge":
        _run_mkvmerge(output_path, part_paths)
    else:
        _run_ffmpeg_concat(output_path, part_paths)

    _validate_duration(output_path, part_paths, tolerance_pct=tolerance_pct)
    return output_path


def _state_path(group_id: str) -> Path:
    return STITCH_STATE_DIR / f"{group_id}.json"


def _load_state(group_id: str) -> dict:
    path = _state_path(group_id)
    if not path.is_file():
        return {"group_id": group_id, "parts": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(group_id: str, state: dict) -> None:
    STITCH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(group_id).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _register_part(manifest: MovieManifest, part_path: Path) -> dict:
    group = stitch_group_id(manifest)
    assert group is not None
    assert manifest.stitch is not None
    assert manifest.meta.part is not None

    state = _load_state(group)
    state.setdefault("group_id", group)
    state["expected_parts"] = manifest.stitch.expected_parts
    state.setdefault("output_dir", manifest.output.dir)
    state["output_filename"] = stitch_output_path(manifest).name
    parts: dict[str, str] = state.setdefault("parts", {})
    parts[str(manifest.meta.part)] = str(part_path.resolve())
    _save_state(group, state)
    return state


def discover_part_paths(manifest: MovieManifest) -> dict[int, Path]:
    """Find part MKVs on disk for this stitch group (1-based part numbers)."""
    assert manifest.stitch is not None
    assert manifest.output.dir

    found: dict[int, Path] = {}
    out_dir = paths.OUTPUT_ROOT / manifest.output.dir
    if not out_dir.is_dir():
        return found

    title = manifest.meta.title
    for part in range(1, manifest.stitch.expected_parts + 1):
        expected = out_dir / part_filename_for(manifest, part)
        if expected.is_file() and expected.stat().st_size > 0:
            found[part] = expected
            continue
        # Fallback: title stem + Disc N (plain, {edition-X}, or legacy "Extended Edition")
        for candidate in sorted(out_dir.glob(f"{title}*.mkv")):
            match = _PART_GLOB.search(candidate.name)
            if match and int(match.group(1)) == part:
                found[part] = candidate
                break
    return found


def parts_ready(manifest: MovieManifest) -> tuple[bool, dict[int, Path]]:
    """Return whether all expected parts exist and their paths."""
    assert manifest.stitch is not None
    parts = discover_part_paths(manifest)
    expected = manifest.stitch.expected_parts
    ready = len(parts) >= expected and all(n in parts for n in range(1, expected + 1))
    return ready, parts


def _log_stitch(stage: str, detail: str) -> None:
    log_stage(stage, process=PROCESS_RIP, detail=detail)


def maybe_stitch_after_part(
    manifest: MovieManifest,
    part_path: Path | None = None,
    *,
    dry_run: bool = False,
    nas_retries: int = 1,
    nas_delay_sec: float = 30.0,
) -> StitchResult:
    """Post-rip hook: wait until all parts exist, then stitch and optionally copy to NAS."""
    if not manifest.stitch or not manifest.stitch.enabled:
        return StitchResult(ok=True, message="stitch disabled")

    part_path = part_path or movie_output_path(manifest)
    if not part_path.is_file():
        return StitchResult(ok=False, message=f"part file missing: {part_path}")

    group = stitch_group_id(manifest)
    assert group is not None
    _register_part(manifest, part_path)

    ready, parts = parts_ready(manifest)
    expected = manifest.stitch.expected_parts
    have = len(parts)
    if not ready:
        _log_stitch(
            "STITCH_WAITING",
            f"{group} {have}/{expected} parts ({', '.join(p.name for p in parts.values())})",
        )
        return StitchResult(ok=True, message=f"waiting {have}/{expected}")

    ordered = [parts[n] for n in range(1, expected + 1)]
    out_path = stitch_output_path(manifest)
    if out_path.is_file() and out_path.stat().st_size > 0:
        _log_stitch("STITCH_OK", f"{group} already stitched → {out_path.name}")
        return StitchResult(ok=True, output_path=out_path, message="already stitched")

    part_names = ", ".join(p.name for p in ordered)
    _log_stitch("STITCH", f"{group} start ({expected} parts: {part_names}) → {out_path.name}")

    if dry_run:
        return StitchResult(ok=True, output_path=out_path, message="dry-run")

    try:
        stitch_movie_parts(
            out_path,
            ordered,
            tolerance_pct=manifest.stitch.tolerance_pct,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _log_stitch("STITCH_FAILED", f"{group} {exc}")
        logger.error("Stitch failed for %s: %s", group, exc)
        return StitchResult(ok=False, message=str(exc))

    _log_stitch("STITCH_OK", f"{group} → {out_path.name}")

    nas_ok: bool | None = None
    if manifest.stitch.copy_to_nas and manifest.nas.movies:
        movies_nas = resolve_path(manifest.nas.share) if manifest.nas.share else None
        base = movies_nas or MOVIES_NAS
        dest_dir = base / manifest.nas.subdir if manifest.nas.subdir else base
        dest = dest_dir / out_path.name
        log_copy_start(out_path.name, dest, numbered=False)
        delete_local = resolve_delete_local_after_nas(manifest.post.delete_local_after_nas)
        nas_ok = copy_movie_to_share(
            out_path,
            manifest.nas.subdir,
            movies_nas=movies_nas,
            retries=nas_retries,
            delay_sec=nas_delay_sec,
        )
        log_copy_result(nas_ok, out_path.name, dest)
        if nas_ok and delete_local:
            cleanup = delete_local_after_nas_copy(out_path, dest, enabled=True)
            log_local_cleanup(out_path.name, dest, deleted=cleanup)
    elif manifest.stitch.copy_to_nas:
        _log_stitch("COPY", "skipped stitched file (no nas.movies)")

    return StitchResult(ok=True, output_path=out_path, nas_ok=nas_ok, message="stitched")
