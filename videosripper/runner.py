"""TV disc rip loop with resume and skip-if-exists."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from videosripper import ffmpeg_ops
from videosripper.logging_setup import (
    log_copy_result,
    log_copy_skipped,
    log_copy_start,
    log_copy_summary,
    log_debug,
    log_eject_stage,
    log_remux_stage,
)
from videosripper.makemkv import ScanResult, rip_title, scan
from videosripper.models import (
    TvDiscManifest,
    TvTrack,
    track_season,
    tv_nas_season_dir,
    tv_output_dir,
)
from videosripper.monitor import MonitorSettings, preflight_disc_open, warn_duplicate_makemkv
from videosripper.nas import copy_episode_to_share, eject_disc
from videosripper.paths import NAS_SHARE, STAGING_ROOT, disc_dev, resolve_path
from videosripper.post_rip_verify import (
    PostRipVerifyError,
    log_verify_failure,
    verify_track_outputs,
    warn_title_count_explosion,
)
from videosripper.retry import RetrySettings, call_with_retries
from videosripper.sample_verify import preview_title
from videosripper.title_verify import raise_on_errors, validate_manifest_titles

logger = logging.getLogger("videosripper")


@dataclass
class RipPlanItem:
    slot: int
    action: str
    makemkv_title: int
    episode: str
    title: str
    output: Path
    skip: bool = False


@dataclass
class RipSessionStats:
    episodes_written: list[str] = field(default_factory=list)
    nas_ok: int = 0
    nas_failed: int = 0
    nas_skipped: int = 0
    nas_dest: str = ""  # last NAS destination dir (for the COPY summary line)
    ejected: bool | None = None  # None = not attempted


def episode_filename(show: str, season: int, episode: str, title: str) -> str:
    return f"{show} - {season}x{episode} - {title}.mkv"


def track_outputs(manifest: TvDiscManifest, track: TvTrack) -> list[tuple[str, str, Path]]:
    show = manifest.meta.show
    season = track_season(manifest, track)
    out_dir = tv_output_dir(manifest, season)
    if track.split:
        return [
            (
                out.episode,
                out.title,
                out_dir / episode_filename(show, season, out.episode, out.title),
            )
            for out in track.split.outputs
        ]
    assert track.episode and track.title
    return [
        (
            track.episode,
            track.title,
            out_dir / episode_filename(show, season, track.episode, track.title),
        )
    ]


def build_plan(manifest: TvDiscManifest, start_at: int) -> list[RipPlanItem]:
    plan: list[RipPlanItem] = []
    for track in manifest.tracks:
        if track.slot < start_at:
            continue
        outputs = track_outputs(manifest, track)
        if track.split:
            all_exist = all(p.exists() for _, _, p in outputs)
            if all_exist:
                for ep, title, path in outputs:
                    plan.append(
                        RipPlanItem(
                            slot=track.slot,
                            action="skip",
                            makemkv_title=track.makemkv_title,
                            episode=ep,
                            title=title,
                            output=path,
                            skip=True,
                        )
                    )
                continue
            for ep, title, path in outputs:
                plan.append(
                    RipPlanItem(
                        slot=track.slot,
                        action="split",
                        makemkv_title=track.makemkv_title,
                        episode=ep,
                        title=title,
                        output=path,
                        skip=path.exists(),
                    )
                )
            continue
        ep, title, path = outputs[0]
        action = "extract_chapter" if track.extract_chapter else "rip"
        plan.append(
            RipPlanItem(
                slot=track.slot,
                action=action,
                makemkv_title=track.makemkv_title,
                episode=ep,
                title=title,
                output=path,
                skip=path.exists(),
            )
        )
    return plan


def staging_dir(dry_run: bool) -> Path:
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return STAGING_ROOT / ".dry-run-staging"
    return Path(tempfile.mkdtemp(prefix=".rip.", dir=STAGING_ROOT))


def _monitor_settings(
    manifest: TvDiscManifest,
    on_stall: str | None = None,
) -> MonitorSettings:
    cfg = manifest.monitor
    return MonitorSettings.resolve(
        MonitorSettings(
            stall_timeout_sec=cfg.stall_timeout_sec,
            open_timeout_sec=cfg.open_timeout_sec,
            on_stall=cfg.on_stall,
        ),
        on_stall_cli=on_stall,
    )


def _retry_settings(
    manifest: TvDiscManifest,
    *,
    retry: int | None = None,
    retry_delay: float | None = None,
) -> RetrySettings:
    cfg = manifest.retry
    return RetrySettings.resolve(
        RetrySettings(
            count=cfg.count,
            delay_sec=cfg.delay_sec,
            stall_retry=cfg.stall_retry,
            nas_retries=cfg.nas_retries,
        ),
        retry_cli=retry,
        delay_cli=retry_delay,
    )


def _log_dry_run_complete(manifest: TvDiscManifest, plan: list[RipPlanItem]) -> None:
    pending = [p for p in plan if not p.skip]
    logger.info("=== DRY RUN COMPLETE ===")
    logger.info("No files written; disc not ejected.")
    logger.info(
        "Planned: %d episode file(s) across %d slot(s) (%d already exist)",
        len(pending),
        len({p.slot for p in pending}),
        sum(1 for p in plan if p.skip),
    )


def _maybe_copy(
    final: Path,
    manifest: TvDiscManifest,
    nas_share: Path | None,
    stats: RipSessionStats,
    retry: RetrySettings,
    *,
    season: int | None = None,
) -> None:
    if not manifest.post.copy_to_nas or nas_share is None:
        stats.nas_skipped += 1
        return
    season_rel = tv_nas_season_dir(manifest, season)
    dest = nas_share / season_rel / final.name
    stats.nas_dest = str(dest.parent)
    log_copy_start(final.name, dest, numbered=False)
    if copy_episode_to_share(
        final,
        season_rel,
        nas_share=nas_share,
        retries=retry.nas_retries,
        delay_sec=retry.delay_sec,
    ):
        stats.nas_ok += 1
        log_copy_result(True, final.name, dest)
    else:
        stats.nas_failed += 1
        log_copy_result(False, final.name, dest)


def _postprocess_ripped(
    ripped: Path,
    staging: Path,
    manifest: TvDiscManifest,
) -> Path:
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


def _verify_track_or_raise(
    scan_result: ScanResult,
    track: TvTrack,
    outputs: list[tuple[str, str, Path]],
    *,
    skip_paths: frozenset[Path],
    chapter_duration_sec: int | None = None,
) -> None:
    result = verify_track_outputs(
        scan_result,
        track,
        outputs,
        skip_paths=skip_paths,
        chapter_duration_sec=chapter_duration_sec,
    )
    if result.ok:
        return
    log_verify_failure(
        slot=track.slot,
        makemkv_title=track.makemkv_title,
        result=result,
    )
    raise PostRipVerifyError(
        f"slot {track.slot} title {track.makemkv_title}: {'; '.join(result.errors)}"
    )


def _run_with_verify_retry(
    fn,
    *,
    label: str,
    retry: RetrySettings,
) -> None:
    if retry.count <= 0:
        fn()
        return
    call_with_retries(
        fn,
        retries=retry.count,
        delay_sec=retry.delay_sec,
        label=label,
        should_retry=lambda exc: isinstance(exc, PostRipVerifyError),
    )


def _rip_title_with_retry(
    manifest: TvDiscManifest,
    title_index: int,
    staging: Path,
    monitor: MonitorSettings,
    retry: RetrySettings,
) -> Path:
    ripped = rip_title(
        manifest.meta.device,
        title_index,
        staging,
        manifest.meta.minlength,
        stream_defaults=manifest.stream_defaults,
        monitor=monitor,
        retries=retry.count,
        delay_sec=retry.delay_sec,
        stall_retry=retry.stall_retry,
    )
    assert ripped is not None
    return ripped


def _maybe_sample_verify(
    manifest: TvDiscManifest,
    track: TvTrack,
    *,
    scan_result: ScanResult | None,
    sample_verify: bool,
    sample_seconds: int,
    sample_offset: int,
) -> None:
    if not sample_verify or scan_result is None:
        return
    logger.info(
        "=== Sample-verify title %s (offset=%ss, %ss) ===",
        track.makemkv_title,
        sample_offset,
        sample_seconds,
    )
    result = preview_title(
        manifest.meta.device,
        track.makemkv_title,
        seconds=sample_seconds,
        offset=sample_offset,
        minlength=manifest.meta.minlength,
        scan_result=scan_result,
    )
    if result.ok:
        logger.info("Sample-verify PASS title %s", track.makemkv_title)
        return
    raise RuntimeError(
        f"Sample-verify failed for title {track.makemkv_title}: {'; '.join(result.reasons)}"
    )


def rip_tv_disc(
    manifest: TvDiscManifest,
    *,
    dry_run: bool = False,
    start_at: int | None = None,
    on_stall: str | None = None,
    retry: int | None = None,
    retry_delay: float | None = None,
    sample_verify: bool = False,
    sample_seconds: int = 45,
    sample_offset: int = 60,
) -> list[RipPlanItem]:
    start = start_at if start_at is not None else int(os.environ.get("VIDEOSRIPPER_START_AT", "1"))
    out_dirs = {
        tv_output_dir(manifest, manifest.meta.season),
        *(
            {tv_output_dir(manifest, track.season) for track in manifest.tracks if track.season}
        ),
    }
    for out_dir in out_dirs:
        out_dir.mkdir(parents=True, exist_ok=True)
    nas_share = resolve_path(manifest.nas.share) if manifest.nas.share else NAS_SHARE
    plan = build_plan(manifest, start)
    staging = staging_dir(dry_run)
    monitor = _monitor_settings(manifest, on_stall)
    retry_cfg = _retry_settings(manifest, retry=retry, retry_delay=retry_delay)
    stats = RipSessionStats()
    scan_result: ScanResult | None = None

    logger.info("=== %s rip started ===", manifest.meta.id)
    log_debug("Output dirs: %s", ", ".join(str(d) for d in sorted(out_dirs)))
    log_debug("Staging: %s", staging)
    if dry_run:
        logger.info("DRY RUN — no makemkv/ffmpeg/rsync/eject")
    else:
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
        logger.info("=== Verifying manifest titles against disc scan ===")
        scan_result = scan(manifest.meta.device, manifest.meta.minlength)
        warn_title_count_explosion(len(scan_result.titles))
        raise_on_errors(validate_manifest_titles(manifest, scan_result))
        logger.info("Title validation OK")
    logger.info("Resume from slot: %s", start)

    try:
        # Progress is "tracks completed / tracks in this run", not slot numbers
        # (slots may be non-contiguous or start mid-disc via --start-at).
        work_tracks = [t for t in manifest.tracks if t.slot >= start]
        total_tracks = len(work_tracks)
        completed = 0
        for track in work_tracks:
            outputs = track_outputs(manifest, track)
            from videosripper.logging_setup import PROCESS_RIP, log_progress

            season = track_season(manifest, track)
            ep_label = " + ".join(f"{season}x{ep} {title}" for ep, title, _ in outputs)
            # current=completed → % done before this track; detail names which of N we're on
            log_progress(
                "RIP",
                completed,
                total_tracks,
                process=PROCESS_RIP,
                detail=(
                    f"ripping {completed + 1}/{total_tracks}: "
                    f"slot {track.slot} title {track.makemkv_title} — {ep_label}"
                ),
            )
            if all(p.exists() for _, _, p in outputs):
                logger.info(
                    "=== Skipping slot %s title %s (already exists): %s ===",
                    track.slot,
                    track.makemkv_title,
                    outputs[0][2].name,
                )
                completed += 1
                continue

            _maybe_sample_verify(
                manifest,
                track,
                scan_result=scan_result,
                sample_verify=sample_verify,
                sample_seconds=sample_seconds,
                sample_offset=sample_offset,
            )

            if track.split:
                ep_labels = " + ".join(
                    f"{track_season(manifest, track)}x{o.episode} {o.title}"
                    for o in track.split.outputs
                )
                logger.info(
                    "=== Ripping title %s -> %s (chapter split) ===",
                    track.makemkv_title,
                    ep_labels,
                )
                if dry_run:
                    for _, _, path in outputs:
                        logger.info("  [dry-run] would write %s", path.name)
                    completed += 1
                    continue
                resume_skip = frozenset(p for _, _, p in outputs if p.exists())

                def _rip_split_slot() -> None:
                    ripped = _rip_title_with_retry(
                        manifest, track.makemkv_title, staging, monitor, retry_cfg
                    )
                    ripped = _postprocess_ripped(ripped, staging, manifest)
                    split_dir = staging / "split"
                    ffmpeg_ops.split_at(ripped, track.split, split_dir)
                    for out_cfg, (_, _, final) in zip(
                        track.split.outputs, outputs, strict=True
                    ):
                        if final in resume_skip:
                            continue
                        interim = split_dir / f"part_{out_cfg.episode}.mkv"
                        shutil.move(str(interim), str(final))
                        dur = ffmpeg_ops.probe_duration(final)
                        label = f"{track_season(manifest, track)}x{out_cfg.episode}"
                        stats.episodes_written.append(label)
                        logger.info("  %ss -> %s", dur, final.name)
                    if scan_result is not None:
                        _verify_track_or_raise(
                            scan_result,
                            track,
                            outputs,
                            skip_paths=resume_skip,
                        )
                    for out_cfg, (_, _, final) in zip(
                        track.split.outputs, outputs, strict=True
                    ):
                        if final in resume_skip:
                            continue
                        _maybe_copy(
                            final,
                            manifest,
                            nas_share,
                            stats,
                            retry_cfg,
                            season=track_season(manifest, track),
                        )

                _run_with_verify_retry(
                    _rip_split_slot,
                    label=f"slot {track.slot} title {track.makemkv_title}",
                    retry=retry_cfg,
                )
                completed += 1
                continue

            if track.extract_chapter:
                assert track.episode and track.title
                final = outputs[0][2]
                season = track_season(manifest, track)
                logger.info(
                    "=== Ripping play-all title %s -> %sx%s %s (chapter extract) ===",
                    track.makemkv_title,
                    season,
                    track.episode,
                    track.title,
                )
                if dry_run:
                    logger.info(
                        "  [dry-run] would extract ch %s -> %s",
                        track.extract_chapter.chapter_index,
                        final.name,
                    )
                    completed += 1
                    continue
                resume_skip = frozenset(p for _, _, p in outputs if p.exists())

                def _rip_chapter_slot() -> None:
                    ripped = _rip_title_with_retry(
                        manifest, track.makemkv_title, staging, monitor, retry_cfg
                    )
                    ripped = _postprocess_ripped(ripped, staging, manifest)
                    start, end = ffmpeg_ops.chapter_bounds(
                        ripped, track.extract_chapter.chapter_index
                    )
                    chapter_dur = int(end - start)
                    ffmpeg_ops.extract_chapter(
                        ripped,
                        track.extract_chapter.chapter_index,
                        final,
                    )
                    dur = ffmpeg_ops.probe_duration(final)
                    stats.episodes_written.append(f"{season}x{track.episode}")
                    logger.info("  %ss -> %s", dur, final.name)
                    if scan_result is not None:
                        _verify_track_or_raise(
                            scan_result,
                            track,
                            outputs,
                            skip_paths=resume_skip,
                            chapter_duration_sec=chapter_dur,
                        )
                    _maybe_copy(
                        final,
                        manifest,
                        nas_share,
                        stats,
                        retry_cfg,
                        season=season,
                    )

                _run_with_verify_retry(
                    _rip_chapter_slot,
                    label=f"slot {track.slot} title {track.makemkv_title}",
                    retry=retry_cfg,
                )
                completed += 1
                continue

            ep, title, final = outputs[0]
            season = track_season(manifest, track)
            logger.info(
                "=== Ripping title %s -> %sx%s - %s ===",
                track.makemkv_title,
                season,
                ep,
                title,
            )
            if dry_run:
                logger.info("  [dry-run] would write %s", final.name)
                completed += 1
                continue
            resume_skip = frozenset(p for _, _, p in outputs if p.exists())

            def _rip_direct_slot() -> None:
                ripped = _rip_title_with_retry(
                    manifest, track.makemkv_title, staging, monitor, retry_cfg
                )
                ripped = _postprocess_ripped(ripped, staging, manifest)
                dur = ffmpeg_ops.probe_duration(ripped)
                shutil.move(str(ripped), str(final))
                stats.episodes_written.append(f"{season}x{ep}")
                logger.info("  %ss -> %s", dur, final.name)
                if scan_result is not None:
                    _verify_track_or_raise(
                        scan_result,
                        track,
                        outputs,
                        skip_paths=resume_skip,
                    )
                _maybe_copy(
                    final,
                    manifest,
                    nas_share,
                    stats,
                    retry_cfg,
                    season=season,
                )

            _run_with_verify_retry(
                _rip_direct_slot,
                label=f"slot {track.slot} title {track.makemkv_title}",
                retry=retry_cfg,
            )
            completed += 1

        if total_tracks:
            from videosripper.logging_setup import PROCESS_RIP, log_progress

            log_progress(
                "RIP",
                completed,
                total_tracks,
                process=PROCESS_RIP,
                detail=f"tracks done {completed}/{total_tracks}",
            )

        if dry_run:
            _log_dry_run_complete(manifest, plan)
        else:
            total_mkv = sum(len(list(d.glob("*.mkv"))) for d in out_dirs)
            logger.info("=== RIP COMPLETE ===")
            if stats.episodes_written:
                logger.info(
                    "Episodes written: %d — %s",
                    len(stats.episodes_written),
                    ", ".join(stats.episodes_written),
                )
            else:
                logger.info("Episodes written: 0 (all outputs already existed)")
            logger.info(
                "Output: %d files across %s",
                total_mkv,
                ", ".join(str(d) for d in sorted(out_dirs)),
            )
            if not manifest.post.copy_to_nas:
                logger.info("NAS copy: skipped (post.copy_to_nas: false)")
                log_copy_skipped("post.copy_to_nas: false")
            elif nas_share is None:
                logger.info("NAS copy: skipped (no nas share)")
                log_copy_skipped("no nas share")
            else:
                logger.info(
                    "NAS copy: %d ok, %d failed",
                    stats.nas_ok,
                    stats.nas_failed,
                )
                log_copy_summary(
                    stats.nas_ok,
                    stats.nas_failed,
                    stats.nas_dest or str(nas_share),
                )
            if manifest.post.eject:
                stats.ejected = eject_disc(disc_dev(manifest.meta.device))
                log_eject_stage(disc_dev(manifest.meta.device), ok=stats.ejected)
                if stats.ejected:
                    logger.info("Eject: success")
                    logger.info("Eject OK: %s", disc_dev(manifest.meta.device))
                else:
                    logger.info("Eject: failed — eject manually")
            else:
                log_eject_stage(disc_dev(manifest.meta.device), skipped="post.eject: false")
                logger.info("Eject: not attempted")
    finally:
        if not dry_run and staging.exists() and staging.name.startswith(".rip."):
            shutil.rmtree(staging, ignore_errors=True)

    return plan
