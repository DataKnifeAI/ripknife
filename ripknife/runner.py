"""TV disc rip loop with resume and skip-if-exists."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ripknife import ffmpeg_ops
from ripknife.logging_setup import (
    PROCESS_RIP,
    log_copy_result,
    log_copy_skipped,
    log_copy_start,
    log_copy_summary,
    log_debug,
    log_eject_stage,
    log_local_cleanup,
    log_progress,
    log_remux_stage,
    log_rip_percent,
)
from ripknife.makemkv import (
    ScanResult,
    clear_rip_progress,
    playall_overall_pct,
    rip_progress_context,
    rip_title,
    scan,
    write_rip_progress,
)
from ripknife.models import (
    TvDiscManifest,
    TvTrack,
    track_season,
    tv_nas_season_dir,
    tv_output_dir,
)
from ripknife.monitor import (
    ALERT_PREFIX,
    MonitorSettings,
    RipDecileTracker,
    preflight_disc_open,
    warn_duplicate_makemkv,
)
from ripknife.nas import (
    copy_episode_to_share,
    delete_local_after_nas_copy,
    eject_disc,
    resolve_delete_local_after_nas,
)
from ripknife.plexmatch import write_from_manifest
from ripknife.paths import NAS_SHARE, STAGING_ROOT, disc_dev, resolve_path
from ripknife.post_rip_verify import (
    PostRipVerifyError,
    log_verify_failure,
    verify_track_outputs,
    warn_title_count_explosion,
)
from ripknife.retry import RetrySettings, call_with_retries
from ripknife.sample_verify import preview_title
from ripknife.title_verify import raise_on_errors, validate_manifest_titles

logger = logging.getLogger("ripknife")


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
class SlotFailure:
    slot: int
    makemkv_title: int
    episode: str
    error: str
    sector_retries_used: int = 0


@dataclass
class RipSessionStats:
    episodes_written: list[str] = field(default_factory=list)
    failed_slots: list[SlotFailure] = field(default_factory=list)
    nas_ok: int = 0
    nas_failed: int = 0
    nas_skipped: int = 0
    nas_dest: str = ""  # last NAS destination dir (for the COPY summary line)
    ejected: bool | None = None  # None = not attempted


@dataclass
class TvRipResult:
    plan: list["RipPlanItem"]
    stats: RipSessionStats

    @property
    def partial(self) -> bool:
        return bool(self.stats.failed_slots) and bool(self.stats.episodes_written)

    @property
    def had_failures(self) -> bool:
        return bool(self.stats.failed_slots)


def _filename_component(value: str) -> str:
    """One path segment. ``/`` and ``\\`` become `` - ``; whitespace collapses.

    ``?`` and ``:`` stay. Season folders are separate components and are not
    passed through here.
    """
    cleaned = value.replace("/", " - ").replace("\\", " - ")
    return " ".join(cleaned.split())


def episode_filename(show: str, season: int, episode: str, title: str) -> str:
    show_name = _filename_component(show)
    episode_token = _filename_component(episode)
    title_name = _filename_component(title)
    return f"{show_name} - {season}x{episode_token} - {title_name}.mkv"


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


def _file_size_ok(path: Path, *, min_size: int = 1) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_size
    except OSError:
        return False


def tv_episode_nas_path(manifest: TvDiscManifest, local_path: Path, season: int) -> Path:
    """NAS destination for a TV episode (same layout as post-rip copy)."""
    nas_share = resolve_path(manifest.nas.share) if manifest.nas.share else NAS_SHARE
    return nas_share / tv_nas_season_dir(manifest, season) / local_path.name


def episode_output_present(
    path: Path,
    manifest: TvDiscManifest,
    season: int,
    *,
    min_size: int = 1,
) -> bool:
    """True when episode exists locally or (if copy_to_nas) on NAS with good size.

    Critical with ``delete_local_after_nas``: local MKVs are removed after copy, so
    skip/resume must treat a verified-looking NAS file as done.
    """
    if _file_size_ok(path, min_size=min_size):
        return True
    if not manifest.post.copy_to_nas:
        return False
    return _file_size_ok(tv_episode_nas_path(manifest, path, season), min_size=min_size)


def track_outputs_present(manifest: TvDiscManifest, track: TvTrack, *, min_size: int = 1) -> bool:
    season = track_season(manifest, track)
    return all(
        episode_output_present(path, manifest, season, min_size=min_size)
        for _, _, path in track_outputs(manifest, track)
    )


def _playall_siblings(work_tracks: list[TvTrack], makemkv_title: int) -> list[TvTrack]:
    return [
        t
        for t in work_tracks
        if t.makemkv_title == makemkv_title and t.extract_chapter is not None
    ]


def _chapter_range_label(track: TvTrack) -> str:
    ch = track.extract_chapter
    assert ch is not None
    if ch.chapter_end is None:
        return str(ch.chapter_index)
    return f"{ch.chapter_index}–{ch.chapter_end}"


def _playall_feeds_label(siblings: list[TvTrack], slot: int) -> str:
    if len(siblings) > 1:
        return f"feeds slots {siblings[0].slot}–{siblings[-1].slot}"
    return f"slot {slot}"


def build_plan(manifest: TvDiscManifest, start_at: int) -> list[RipPlanItem]:
    plan: list[RipPlanItem] = []
    for track in manifest.tracks:
        if track.slot < start_at:
            continue
        outputs = track_outputs(manifest, track)
        season = track_season(manifest, track)
        if track.split:
            all_exist = track_outputs_present(manifest, track)
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
                        skip=episode_output_present(path, manifest, season),
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
                skip=episode_output_present(path, manifest, season),
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
        tv=True,
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
    delete_local = resolve_delete_local_after_nas(manifest.post.delete_local_after_nas)
    if copy_episode_to_share(
        final,
        season_rel,
        nas_share=nas_share,
        retries=retry.nas_retries,
        delay_sec=retry.delay_sec,
    ):
        stats.nas_ok += 1
        log_copy_result(True, final.name, dest)
        if delete_local:
            cleanup = delete_local_after_nas_copy(final, dest, enabled=True)
            log_local_cleanup(final.name, dest, deleted=cleanup)
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


def _run_slot_with_continue(
    fn,
    *,
    label: str,
    retry: RetrySettings,
    stats: RipSessionStats,
    slot: int,
    makemkv_title: int,
    episode: str,
) -> bool:
    """Run one slot with verify retries; on failure log, record, and continue disc."""
    try:
        _run_with_verify_retry(fn, label=label, retry=retry)
        return True
    except BaseException as exc:
        logger.error("%s SLOT FAILED — %s: %s", ALERT_PREFIX, label, exc)
        stats.failed_slots.append(
            SlotFailure(
                slot=slot,
                makemkv_title=makemkv_title,
                episode=episode,
                error=str(exc),
                sector_retries_used=int(getattr(exc, "sector_retries_used", 0) or 0),
            )
        )
        return False


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
        eject_on_exhaust=False,
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
) -> TvRipResult:
    start = start_at if start_at is not None else int(os.environ.get("RIPKNIFE_START_AT", "1"))
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
    if not dry_run:
        clear_rip_progress()
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
    if (
        not dry_run
        and manifest.post.copy_to_nas
        and nas_share is not None
        and manifest.plex
        and manifest.plex.local
    ):
        plex_path = write_from_manifest(manifest, nas_share)
        if plex_path is not None:
            logger.info("Plex local match: wrote %s", plex_path)

    try:
        # Progress is "tracks completed / tracks in this run", not slot numbers
        # (slots may be non-contiguous or start mid-disc via --start-at).
        work_tracks = [t for t in manifest.tracks if t.slot >= start]
        total_tracks = len(work_tracks)
        completed = 0
        # Shared play-all MKVs for extract_chapter slots (subdir survives rip_title wipe).
        playall_cache: dict[int, Path] = {}
        all_extract = bool(work_tracks) and all(
            t.extract_chapter is not None for t in work_tracks
        )
        for track in work_tracks:
            outputs = track_outputs(manifest, track)
            season = track_season(manifest, track)
            ep_label = " + ".join(f"{season}x{ep} {title}" for ep, title, _ in outputs)
            if track.extract_chapter is None:
                # RIP N/M is MakeMKV disc reads only — not ffmpeg chapter extracts.
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
            if track_outputs_present(manifest, track):
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
                resume_skip = frozenset(
                    p
                    for _, _, p in outputs
                    if episode_output_present(p, manifest, season)
                )

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

                ok = _run_slot_with_continue(
                    _rip_split_slot,
                    label=f"slot {track.slot} title {track.makemkv_title}",
                    retry=retry_cfg,
                    stats=stats,
                    slot=track.slot,
                    makemkv_title=track.makemkv_title,
                    episode=ep_labels,
                )
                if ok:
                    completed += 1
                continue

            if track.extract_chapter:
                assert track.episode and track.title
                final = outputs[0][2]
                season = track_season(manifest, track)
                siblings = _playall_siblings(work_tracks, track.makemkv_title)
                extract_n = len(siblings) or 1
                extract_idx = next(
                    (i for i, t in enumerate(siblings, 1) if t.slot == track.slot),
                    1,
                )
                feeds = _playall_feeds_label(siblings, track.slot)
                ch_label = _chapter_range_label(track)
                cached = playall_cache.get(track.makemkv_title)
                cache_ready = cached is not None and cached.is_file()
                if cache_ready:
                    logger.info(
                        "=== EXTRACT %s/%s — slot %s chapters %s from cached title %s ===",
                        extract_idx,
                        extract_n,
                        track.slot,
                        ch_label,
                        track.makemkv_title,
                    )
                    log_progress(
                        "EXTRACT",
                        extract_idx,
                        extract_n,
                        process=PROCESS_RIP,
                        detail=(
                            f"slot {track.slot} chapters {ch_label} "
                            f"from cached title {track.makemkv_title}"
                        ),
                        include_percent=False,
                    )
                else:
                    logger.info(
                        "=== RIP play-all title %s (%s) — slow MakeMKV pass "
                        "(~90%% of disc time) ===",
                        track.makemkv_title,
                        feeds,
                    )
                    log_progress(
                        f"RIP play-all title {track.makemkv_title} ({feeds})",
                        0,
                        0,
                        process=PROCESS_RIP,
                    )
                if dry_run:
                    logger.info(
                        "  [dry-run] would extract ch %s -> %s",
                        ch_label,
                        final.name,
                    )
                    completed += 1
                    continue
                resume_skip = frozenset(
                    p
                    for _, _, p in outputs
                    if episode_output_present(p, manifest, season)
                )

                def _rip_chapter_slot() -> None:
                    ripped_path: Path
                    did_makemkv = False
                    cached_now = playall_cache.get(track.makemkv_title)
                    if cached_now is not None and cached_now.is_file():
                        ripped_path = cached_now
                        logger.info(
                            "  reusing cached play-all title %s (%s)",
                            track.makemkv_title,
                            ripped_path.name,
                        )
                    else:
                        playall_label = (
                            f"play-all title {track.makemkv_title} ({feeds})"
                        )
                        with rip_progress_context(
                            phase="makemkv",
                            label=playall_label,
                            extracts_total=extract_n,
                        ):
                            ripped_path = _rip_title_with_retry(
                                manifest,
                                track.makemkv_title,
                                staging,
                                monitor,
                                retry_cfg,
                            )
                        ripped_path = _postprocess_ripped(
                            ripped_path, staging, manifest
                        )
                        cache_dir = staging / ".playall"
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        cached_path = cache_dir / f"title_{track.makemkv_title}.mkv"
                        if ripped_path.resolve() != cached_path.resolve():
                            if cached_path.exists():
                                cached_path.unlink()
                            shutil.move(str(ripped_path), str(cached_path))
                        playall_cache[track.makemkv_title] = cached_path
                        ripped_path = cached_path
                        did_makemkv = True
                    if did_makemkv:
                        logger.info(
                            "=== EXTRACT %s/%s — slot %s chapters %s from title %s ===",
                            extract_idx,
                            extract_n,
                            track.slot,
                            ch_label,
                            track.makemkv_title,
                        )
                        log_progress(
                            "EXTRACT",
                            extract_idx,
                            extract_n,
                            process=PROCESS_RIP,
                            detail=(
                                f"slot {track.slot} chapters {ch_label} "
                                f"from title {track.makemkv_title}"
                            ),
                            include_percent=False,
                        )
                    # Fail-fast: validate every extract_chapter range for this
                    # play-all title before spending time on any episode cut.
                    sibling_ranges = [
                        (
                            t.slot,
                            t.extract_chapter.chapter_index,
                            (
                                t.extract_chapter.chapter_index
                                if t.extract_chapter.chapter_end is None
                                else t.extract_chapter.chapter_end
                            ),
                        )
                        for t in manifest.tracks
                        if t.makemkv_title == track.makemkv_title
                        and t.extract_chapter is not None
                    ]
                    chapter_count = ffmpeg_ops.validate_extract_chapter_ranges(
                        ripped_path, sibling_ranges
                    )
                    logger.info(
                        "  play-all title %s: %s chapters; ranges OK for %s slot(s)",
                        track.makemkv_title,
                        chapter_count,
                        len(sibling_ranges),
                    )
                    start, end = ffmpeg_ops.chapter_bounds(
                        ripped_path,
                        track.extract_chapter.chapter_index,
                        chapter_end=track.extract_chapter.chapter_end,
                    )
                    chapter_dur = int(end - start)
                    extract_started = time.monotonic()
                    extract_tracker = RipDecileTracker()

                    def _on_extract_progress(pct: int) -> None:
                        overall = playall_overall_pct(
                            makemkv_pct=100,
                            extracts_done=extract_idx - 1,
                            extracts_total=extract_n,
                            extract_pct=pct,
                        )
                        write_rip_progress(
                            track.makemkv_title,
                            pct,
                            extract_started,
                            extra={
                                "phase": "extract",
                                "source": "ffmpeg",
                                "detail": (
                                    f"EXTRACT {extract_idx}/{extract_n} "
                                    f"slot {track.slot}"
                                ),
                                "overall_pct": overall,
                            },
                        )
                        milestone = extract_tracker.milestone_for(pct)
                        if milestone is not None:
                            log_rip_percent(
                                milestone,
                                source="ffmpeg",
                                stage="EXTRACT",
                                label=f"{extract_idx}/{extract_n}",
                            )

                    ffmpeg_ops.extract_chapter(
                        ripped_path,
                        track.extract_chapter.chapter_index,
                        final,
                        chapter_end=track.extract_chapter.chapter_end,
                        on_progress=_on_extract_progress,
                    )
                    clear_rip_progress()
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

                ok = _run_slot_with_continue(
                    _rip_chapter_slot,
                    label=f"slot {track.slot} title {track.makemkv_title}",
                    retry=retry_cfg,
                    stats=stats,
                    slot=track.slot,
                    makemkv_title=track.makemkv_title,
                    episode=f"{season}x{track.episode} {track.title}",
                )
                if ok:
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
            resume_skip = frozenset(
                p
                for _, _, p in outputs
                if episode_output_present(p, manifest, season)
            )

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

            ok = _run_slot_with_continue(
                _rip_direct_slot,
                label=f"slot {track.slot} title {track.makemkv_title}",
                retry=retry_cfg,
                stats=stats,
                slot=track.slot,
                makemkv_title=track.makemkv_title,
                episode=f"{season}x{ep} {title}",
            )
            if ok:
                completed += 1

        if total_tracks:
            done_stage = "EXTRACT" if all_extract else "RIP"
            log_progress(
                done_stage,
                completed,
                total_tracks,
                process=PROCESS_RIP,
                detail=f"tracks done {completed}/{total_tracks}",
            )

        if dry_run:
            _log_dry_run_complete(manifest, plan)
        else:
            total_mkv = sum(len(list(d.glob("*.mkv"))) for d in out_dirs)
            if stats.failed_slots:
                logger.error("=== RIP PARTIAL ===")
                logger.error(
                    "Failed slot(s): %s",
                    "; ".join(
                        f"slot {f.slot} ({f.episode}): {f.error}"
                        for f in stats.failed_slots
                    ),
                )
                if stats.episodes_written:
                    logger.info(
                        "Episodes written before failure(s): %d — %s",
                        len(stats.episodes_written),
                        ", ".join(stats.episodes_written),
                    )
            else:
                logger.info("=== RIP COMPLETE ===")
            if stats.episodes_written and not stats.failed_slots:
                logger.info(
                    "Episodes written: %d — %s",
                    len(stats.episodes_written),
                    ", ".join(stats.episodes_written),
                )
            elif stats.episodes_written and stats.failed_slots:
                logger.info(
                    "Episodes written: %d — %s",
                    len(stats.episodes_written),
                    ", ".join(stats.episodes_written),
                )
            elif not stats.failed_slots:
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
        if not dry_run:
            clear_rip_progress()
        if not dry_run and staging.exists() and staging.name.startswith(".rip."):
            shutil.rmtree(staging, ignore_errors=True)

    return TvRipResult(plan=plan, stats=stats)
