"""Feature film rip with scan validation."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from videosripper import ffmpeg_ops
from videosripper.logging_setup import (
    log_copy_result,
    log_copy_skipped,
    log_copy_start,
    log_debug,
    log_eject_stage,
    log_remux_stage,
)
from videosripper.makemkv import TitleInfo, pick_feature_title, rip_title, scan
from videosripper.models import MovieManifest, movie_output_path
from videosripper.monitor import MonitorSettings, preflight_disc_open, warn_duplicate_makemkv
from videosripper.nas import copy_movie_to_share, eject_disc
from videosripper.paths import MOVIES_NAS, STAGING_ROOT, disc_dev, resolve_path
from videosripper.post_rip_verify import (
    log_verify_failure,
    verify_output,
    warn_title_count_explosion,
)
from videosripper.retry import RetrySettings
from videosripper.sample_verify import preview_title
from videosripper.title_verify import raise_on_errors, validate_manifest_titles

logger = logging.getLogger("videosripper")


def _postprocess_ripped(ripped: Path, staging: Path, manifest: MovieManifest) -> Path:
    """Apply stream_defaults ffmpeg remux when configured; otherwise keep rip as-is."""
    if not manifest.stream_defaults:
        return ripped
    sd = manifest.stream_defaults
    if not sd.audio_default and not sd.subtitle_default:
        return ripped
    sub = sd.subtitle_default or "cleared"
    log_remux_stage(
        f"stream_defaults audio={sd.audio_default} subtitle={sub} — {ripped.name}"
    )
    processed = staging / f".defaults_{ripped.stem}.mkv"
    return ffmpeg_ops.apply_stream_defaults_or_keep(
        ripped,
        processed,
        audio_lang=sd.audio_default,
        subtitle_lang=sd.subtitle_default,
    )


def check_java() -> None:
    try:
        proc = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            line = (proc.stderr or proc.stdout).splitlines()[0]
            log_debug("Java JRE found: %s", line)
            return
    except FileNotFoundError:
        pass
    logger.warning("WARNING: Java JRE not found. BD-Java discs need Java for full MakeMKV support.")
    logger.warning("  Install: sudo pacman -S jre-openjdk")


def verify_rip(
    path: Path,
    scan_info: TitleInfo | None,
    *,
    min_duration_sec: int,
    min_size_bytes: int,
) -> bool:
    result = verify_output(
        scan_info,
        path,
        min_duration_sec=min_duration_sec,
        min_size_bytes=min_size_bytes,
    )
    log_debug(
        "Verify: %ss duration, %s bytes (expected dur=%s size=%s)",
        result.duration_actual,
        result.size_actual,
        result.duration_expected,
        result.size_expected,
    )
    if result.ok:
        return True
    log_verify_failure(makemkv_title=scan_info.index if scan_info else None, result=result)
    return False


def rip_movie(
    manifest: MovieManifest,
    *,
    dry_run: bool = False,
    on_stall: str | None = None,
    retry: int | None = None,
    retry_delay: float | None = None,
    sample_verify: bool = False,
    sample_seconds: int = 45,
    sample_offset: int = 60,
) -> None:
    out_path = movie_output_path(manifest)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    movies_nas = resolve_path(manifest.nas.share) if manifest.nas.share else None
    dev_block = disc_dev(manifest.meta.device)
    cfg = manifest.monitor
    monitor = MonitorSettings.resolve(
        MonitorSettings(
            stall_timeout_sec=cfg.stall_timeout_sec,
            open_timeout_sec=cfg.open_timeout_sec,
            on_stall=cfg.on_stall,
        ),
        on_stall_cli=on_stall,
    )
    rcfg = manifest.retry
    retry_cfg = RetrySettings.resolve(
        RetrySettings(
            count=rcfg.count,
            delay_sec=rcfg.delay_sec,
            stall_retry=rcfg.stall_retry,
            nas_retries=rcfg.nas_retries,
        ),
        retry_cli=retry,
        delay_cli=retry_delay,
    )

    logger.info("=== %s rip started ===", manifest.meta.title)
    log_debug("Output: %s", out_path)
    if manifest.nas.movies:
        log_debug("NAS: %s/%s", movies_nas or "Movies", out_path.name)

    if not Path(dev_block).exists() and not dry_run:
        raise RuntimeError(f"{dev_block} not found — insert disc and re-run")

    if dry_run:
        logger.info("=== DRY RUN COMPLETE ===")
        logger.info("No files written; disc not ejected.")
        sel = manifest.title_selection
        logger.info(
            "Would scan with minlength=%s, pick title via %s",
            manifest.meta.minlength,
            sel.mode,
        )
        return

    log_debug(
        "Monitor: open_timeout=%ds stall_timeout=%ds on_stall=%s",
        monitor.open_timeout_sec,
        monitor.stall_timeout_sec,
        monitor.on_stall,
    )
    log_debug(
        "Retry: count=%d delay=%ss stall_retry=%s nas_retries=%d",
        retry_cfg.count,
        retry_cfg.delay_sec,
        retry_cfg.stall_retry,
        retry_cfg.nas_retries,
    )
    warn_duplicate_makemkv(manifest.meta.device)
    preflight_disc_open(
        manifest.meta.device,
        manifest.meta.minlength,
        timeout_sec=monitor.open_timeout_sec,
        retries=retry_cfg.count,
        delay_sec=retry_cfg.delay_sec,
    )

    check_java()

    logger.info("=== Scanning disc (--minlength=%s) ===", manifest.meta.minlength)
    scan_result = scan(manifest.meta.device, manifest.meta.minlength)
    warn_title_count_explosion(len(scan_result.titles))
    if manifest.meta.disc_pattern and manifest.meta.disc_pattern not in scan_result.raw:
        raise RuntimeError(
            f"Expected disc pattern {manifest.meta.disc_pattern!r}; insert correct disc"
        )
    for warning in scan_result.warnings:
        logger.warning(warning)

    raise_on_errors(validate_manifest_titles(manifest, scan_result))

    sel = manifest.title_selection
    if sel.mode == "fixed":
        title = sel.title
        assert title is not None
    else:
        title = pick_feature_title(scan_result, sel.min_duration_sec)

    info = scan_result.titles.get(title)
    logger.info(
        "Selected title %s duration=%s size=%s",
        title,
        info.duration if info else "?",
        info.size_bytes if info else "?",
    )
    from videosripper.logging_setup import PROCESS_RIP, log_progress

    # 0/1 at start — movie is one title; 100% only after the file lands
    log_progress(
        "RIP",
        0,
        1,
        process=PROCESS_RIP,
        detail=f"ripping 1/1: title {title} → {out_path.name}",
    )

    if sample_verify:
        logger.info(
            "=== Sample-verify title %s (offset=%ss, %ss) ===",
            title,
            sample_offset,
            sample_seconds,
        )
        sample = preview_title(
            manifest.meta.device,
            title,
            seconds=sample_seconds,
            offset=sample_offset,
            minlength=manifest.meta.minlength,
            scan_result=scan_result,
        )
        if not sample.ok:
            raise RuntimeError(
                f"Sample-verify failed for title {title}: {'; '.join(sample.reasons)}"
            )
        logger.info("Sample-verify PASS title %s", title)

    rules = manifest.validation
    if out_path.is_file():
        if out_path.stat().st_size >= rules.min_size_bytes:
            logger.info("Output already exists and looks valid — skipping rip.")
            # Still close steps 5/7 and 6/7 so DONE never follows a gap in numbering.
            log_copy_skipped("local output already complete")
            log_eject_stage(dev_block, skipped="rip skipped (output exists)")
            return
        logger.info("Removing prior bad rip (%s bytes)", out_path.stat().st_size)
        out_path.unlink()

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".rip.", dir=STAGING_ROOT))
    log_debug("Staging: %s", staging)
    nas_ok = False
    ejected = False
    try:
        ripped = rip_title(
            manifest.meta.device,
            title,
            staging,
            manifest.meta.minlength,
            stream_defaults=manifest.stream_defaults,
            monitor=monitor,
            retries=retry_cfg.count,
            delay_sec=retry_cfg.delay_sec,
            stall_retry=retry_cfg.stall_retry,
        )
        if not verify_rip(
            ripped,
            info,
            min_duration_sec=rules.min_duration_sec,
            min_size_bytes=rules.min_size_bytes,
        ):
            ripped.unlink(missing_ok=True)
            raise RuntimeError("Rip validation failed")
        ripped = _postprocess_ripped(ripped, staging, manifest)
        shutil.move(str(ripped), str(out_path))
        logger.info("  -> %s", out_path)
        log_progress(
            "RIP",
            1,
            1,
            process=PROCESS_RIP,
            detail=f"done 1/1: title {title} → {out_path.name}",
        )
        if manifest.nas.movies and manifest.post.copy_to_nas:
            base = movies_nas or MOVIES_NAS
            dest_dir = base / manifest.nas.subdir if manifest.nas.subdir else base
            dest = dest_dir / out_path.name
            log_copy_start(out_path.name, dest, numbered=True)
            nas_ok = copy_movie_to_share(
                out_path,
                manifest.nas.subdir,
                movies_nas=movies_nas,
                retries=retry_cfg.nas_retries,
                delay_sec=retry_cfg.delay_sec,
            )
            log_copy_result(nas_ok, out_path.name, dest)
        elif manifest.nas.movies:
            logger.info("NAS copy: skipped (post.copy_to_nas: false)")
            log_copy_skipped("post.copy_to_nas: false")
        else:
            log_copy_skipped("no nas.movies path")
        if manifest.post.eject:
            ejected = eject_disc(dev_block)
            log_eject_stage(dev_block, ok=ejected)
        else:
            log_eject_stage(dev_block, skipped="post.eject: false")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("=== RIP COMPLETE ===")
    logger.info("Movie written: %s", out_path.name)
    if manifest.nas.movies and manifest.post.copy_to_nas:
        logger.info("NAS copy: %s", "ok" if nas_ok else "failed")
    if manifest.post.eject:
        logger.info("Eject: %s", "success" if ejected else "failed — eject manually")
