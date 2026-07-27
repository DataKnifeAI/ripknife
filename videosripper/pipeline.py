"""Pipeline steps as Python functions — serial or parallel depending on step kind."""

from __future__ import annotations

import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from videosripper.devices import device_slug
from videosripper.logging_setup import (
    INSERT_STEPS,
    PROCESS_INSERT,
    PROCESS_RIP,
    RIP_STEP_DONE,
    RIP_STEP_RESCAN,
    RIP_STEP_RIP,
    RIP_STEP_VALIDATE,
    RIP_STEP_VERIFY,
    RIP_STEPS,
    log_progress,
    log_stage,
    process_elapsed,
    setup_pipeline_logger,
)
from videosripper.map_agent import run_map_agent
from videosripper.models import MovieManifest, TvDiscManifest, load_manifest, movie_output_path
from videosripper.monitor import MakeMkvError
from videosripper.movie import rip_movie
from videosripper.paths import STAGING_ROOT
from videosripper.registry import load_registry, manifest_matches_scan
from videosripper.runner import rip_tv_disc
from videosripper.title_verify import format_title_issues, validate_manifest_titles
from videosripper.watcher_util import flock, makemkv_lock_path, scan_device_dict, slugify_disc_id

PENDING_DIR = STAGING_ROOT / "pending"
READY_DIR = STAGING_ROOT / "ready"
DONE_DIR = STAGING_ROOT / "done"
FAILED_DIR = STAGING_ROOT / "failed"

T = TypeVar("T")


class StepKind(Enum):
    """How a pipeline step may be scheduled."""

    MAKEMKV = "makemkv"  # serial — exclusive drive lock
    LOCAL = "local"  # parallel-safe (pure Python / filesystem)
    BACKGROUND = "background"  # fire-and-forget async work


@dataclass
class StepResult:
    name: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingDisc:
    disc_id: str
    disc_name: str
    scan_json: Path
    meta_json: Path
    scan_data: dict[str, Any]


def run_serial(steps: list[tuple[str, Callable[[], StepResult]]]) -> list[StepResult]:
    """Run steps one after another; stop on first failure."""
    results: list[StepResult] = []
    for name, fn in steps:
        result = fn()
        result.name = name
        results.append(result)
        if not result.ok:
            break
    return results


def run_parallel(steps: list[tuple[str, Callable[[], StepResult]]]) -> list[StepResult]:
    """Run independent LOCAL steps concurrently."""
    if not steps:
        return []
    results: list[StepResult | None] = [None] * len(steps)
    with ThreadPoolExecutor(max_workers=min(len(steps), 8)) as pool:
        future_map = {pool.submit(fn): (idx, name) for idx, (name, fn) in enumerate(steps)}
        for future in as_completed(future_map):
            idx, name = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = StepResult(name=name, ok=False, message=str(exc))
            result.name = name
            results[idx] = result
    return [r for r in results if r is not None]


def step_scan_disc(device: str, minlength: int, logger: logging.Logger) -> StepResult:
    """Scan inserted disc (MAKEMKV — serial per drive)."""
    try:
        with flock(makemkv_lock_path(device), blocking=True):
            scan_data = scan_device_dict(device, minlength)
        return StepResult(
            name="scan",
            ok=True,
            message=f"{len(scan_data.get('titles', []))} titles",
            data={"scan_data": scan_data},
        )
    except Exception as exc:
        logger.error("Scan failed: %s", exc)
        return StepResult(name="scan", ok=False, message=str(exc))


def step_write_pending(
    scan_data: dict[str, Any],
    *,
    device: str,
    minlength: int,
) -> PendingDisc:
    """Persist scan JSON + meta to staging/pending (LOCAL).

    When the volume label is generic (or Blu-ray META is available), attach an
    ``identification`` block (bdmt title + runtime DB candidates) so the map
    agent can pick a real title instead of inventing placeholders.
    """
    from videosripper.identify import attach_identification_to_scan, gather_identification_hints

    disc_name = scan_data.get("disc_name", "") or "unknown-disc"
    disc_id = slugify_disc_id(disc_name)
    hints = gather_identification_hints(scan_data, device=device)
    scan_data = attach_identification_to_scan(scan_data, hints)
    # Prefer identified slug for pending filenames when we have a confident title
    best = hints.best_title()
    if best and hints.is_generic_label:
        preferred_id = slugify_disc_id(best)
    else:
        preferred_id = disc_id

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    scan_json = PENDING_DIR / f"{disc_id}.scan.json"
    meta_json = PENDING_DIR / f"{disc_id}.meta.json"
    scan_json.write_text(json.dumps(scan_data, indent=2) + "\n", encoding="utf-8")
    meta: dict[str, Any] = {
        "id": disc_id,
        "disc_name": disc_name,
        "device": device,
        "minlength": minlength,
        "title_count": len(scan_data.get("titles", [])),
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "identification": hints.to_dict(),
    }
    if preferred_id != disc_id:
        meta["preferred_id"] = preferred_id
        meta["identified_title"] = best
    if hints.bluray_title:
        meta["bluray_title"] = hints.bluray_title
    if hints.candidates:
        meta["title_candidates"] = [c.display() for c in hints.candidates]
    obf = scan_data.get("obfuscation")
    if obf:
        meta["obfuscation"] = obf
        if obf.get("recommended_title") is not None:
            meta["recommended_title"] = obf["recommended_title"]
        decoys = obf.get("decoy_indices")
        if decoys:
            meta["decoy_indices"] = decoys
    meta_json.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return PendingDisc(
        disc_id=disc_id,
        disc_name=disc_name,
        scan_json=scan_json,
        meta_json=meta_json,
        scan_data=scan_data,
    )


def step_registry_lookup(disc_name: str):
    """Return registry entry for disc_name, or None (LOCAL)."""
    return load_registry().lookup_disc(disc_name)


def step_promote_registry(entry) -> Path:
    """Copy known manifest from registry to staging/ready (LOCAL)."""
    READY_DIR.mkdir(parents=True, exist_ok=True)
    dest = READY_DIR / f"{entry.id}.yaml"
    shutil.copy2(entry.manifest, dest)
    return dest


def step_trigger_map_async(
    pending: PendingDisc,
    logger: logging.Logger,
) -> StepResult:
    """Launch LLM map agent in background (BACKGROUND)."""
    try:
        run_map_agent(pending.disc_id, pending.scan_json, background=True, logger=logger)
        return StepResult(
            name="map",
            ok=True,
            message="background agent launched",
            data={"disc_id": pending.disc_id},
        )
    except Exception as exc:
        logger.error("Map launch failed: %s", exc)
        return StepResult(name="map", ok=False, message=str(exc))


def validate_manifest_file(
    yaml_path: Path,
    scan_data: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Validate YAML manifest; optional title check against scan (LOCAL)."""
    messages: list[str] = []
    try:
        manifest = load_manifest(yaml_path)
    except ValidationError as exc:
        return False, [str(exc)]
    if scan_data is not None:
        issues = validate_manifest_titles(manifest, scan_data)
        errors = [i.message for i in issues if i.level == "error"]
        if errors:
            return False, errors
    _ = manifest
    return True, messages


def step_validate_manifest(
    yaml_path: Path,
    scan_data: dict[str, Any] | None = None,
) -> StepResult:
    ok, errors = validate_manifest_file(yaml_path, scan_data)
    if ok:
        return StepResult(name="validate", ok=True, message=yaml_path.name)
    return StepResult(
        name="validate",
        ok=False,
        message="; ".join(errors) or "validation failed",
    )


def _rescan_no_lock(
    manifest: TvDiscManifest | MovieManifest,
    logger: logging.Logger,
) -> StepResult:
    """Re-scan disc WITHOUT acquiring the drive lock (caller must hold it)."""
    try:
        scan_data = scan_device_dict(manifest.meta.device, manifest.meta.minlength)
        return StepResult(name="rescan", ok=True, data={"scan_data": scan_data})
    except Exception as exc:
        logger.error("Re-scan failed: %s", exc)
        return StepResult(name="rescan", ok=False, message=str(exc))


def step_rescan_for_manifest(
    manifest: TvDiscManifest | MovieManifest,
    logger: logging.Logger,
) -> StepResult:
    """Re-scan disc for rip verification (MAKEMKV — acquires per-drive lock)."""
    try:
        with flock(makemkv_lock_path(manifest.meta.device), blocking=True):
            return _rescan_no_lock(manifest, logger)
    except Exception as exc:
        logger.error("Re-scan failed: %s", exc)
        return StepResult(name="rescan", ok=False, message=str(exc))


def step_verify_disc_match(
    yaml_path: Path,
    manifest: TvDiscManifest | MovieManifest,
    scan_data: dict[str, Any],
) -> StepResult:
    disc_name = scan_data.get("disc_name", "")
    disc_pattern = getattr(manifest.meta, "disc_pattern", None)
    matched = manifest_matches_scan(
        disc_name=disc_name,
        manifest_path=yaml_path,
        manifest_id=manifest.meta.id,
        disc_name_meta=manifest.meta.disc_name,
        disc_pattern=disc_pattern,
        registry=load_registry(),
    )
    if matched:
        return StepResult(name="disc_match", ok=True, message=disc_name)
    return StepResult(
        name="disc_match",
        ok=False,
        message=f"scan={disc_name!r} manifest_id={manifest.meta.id}",
    )


def step_verify_titles(
    manifest: TvDiscManifest | MovieManifest,
    scan_data: dict[str, Any],
) -> StepResult:
    issues = validate_manifest_titles(manifest, scan_data)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        return StepResult(
            name="title_verify",
            ok=False,
            message=format_title_issues(errors),
        )
    track_count = len(getattr(manifest, "tracks", [])) or 1
    return StepResult(name="title_verify", ok=True, message=f"{track_count} tracks")


def already_ripped(manifest: TvDiscManifest | MovieManifest) -> bool:
    if isinstance(manifest, MovieManifest):
        rules = manifest.validation
        out = movie_output_path(manifest)
        if out.is_file() and out.stat().st_size >= rules.min_size_bytes:
            return True
        if manifest.nas.movies:
            from videosripper.paths import MOVIES_NAS, resolve_path

            nas_base = resolve_path(manifest.nas.share) if manifest.nas.share else MOVIES_NAS
            nas_path = nas_base / manifest.output.filename
            return nas_path.is_file() and nas_path.stat().st_size >= rules.min_size_bytes
    return False


def step_rip_manifest(
    manifest: TvDiscManifest | MovieManifest,
    yaml_path: Path | None = None,
    *,
    dry_run: bool = False,
    start_at: int | None = None,
    on_stall: str | None = None,
    retry: int | None = None,
    retry_delay: float | None = None,
) -> StepResult:
    """Rip from loaded manifest (MAKEMKV — serial; caller must hold lock if needed)."""
    from videosripper.logging_setup import setup_session_logger

    setup_session_logger(yaml_path or Path(manifest.meta.id))
    try:
        if isinstance(manifest, TvDiscManifest):
            rip_tv_disc(
                manifest,
                dry_run=dry_run,
                start_at=start_at,
                on_stall=on_stall,
                retry=retry,
                retry_delay=retry_delay,
            )
        else:
            rip_movie(
                manifest,
                dry_run=dry_run,
                on_stall=on_stall,
                retry=retry,
                retry_delay=retry_delay,
            )
        return StepResult(name="rip", ok=True)
    except (MakeMkvError, RuntimeError, OSError) as exc:
        return StepResult(name="rip", ok=False, message=str(exc))


def move_manifest_yaml(yaml_path: Path, dest_dir: Path, *, reason: str = "") -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / yaml_path.name
    shutil.move(str(yaml_path), str(dest))
    if reason:
        (dest_dir / f"{yaml_path.stem}.reason.txt").write_text(reason + "\n", encoding="utf-8")
    return dest


def _log_identify(pending: PendingDisc) -> None:
    """Unnumbered ``[insert] IDENTIFY`` line: what the bdmt/runtime hints produced.

    Identification runs inside :func:`step_write_pending`; without this line a
    generic-label disc silently jumped from SCAN to PENDING with no trace of why
    the map agent got (or didn't get) a real title.
    """
    ident = pending.scan_data.get("identification") or {}
    if not ident:
        return
    candidates = ident.get("candidates") or []
    bits = [
        f"label={pending.disc_name!r}",
        f"generic={bool(ident.get('is_generic_label'))}",
        f"bdmt={ident.get('bluray_title') or '-'}",
        f"candidates={len(candidates)}",
    ]
    duration = ident.get("feature_duration_sec")
    if duration:
        bits.append(f"feature={duration}s")
    log_stage("IDENTIFY", process=PROCESS_INSERT, detail=" ".join(bits))


def handle_disc_insert(
    device: str,
    minlength: int,
    logger: logging.Logger,
) -> StepResult:
    """Process 1: scan → pending → registry hit OR background map."""
    setup_pipeline_logger()
    log_stage(
        "SCAN",
        process=PROCESS_INSERT,
        step=1,
        total=INSERT_STEPS,
        detail=f"device={device}",
    )
    scan_result = step_scan_disc(device, minlength, logger)
    if not scan_result.ok:
        log_stage("SCAN_FAILED", process=PROCESS_INSERT, detail=scan_result.message)
        return scan_result

    scan_data = scan_result.data["scan_data"]
    from videosripper.post_rip_verify import warn_title_count_explosion

    title_count = len(scan_data.get("titles", []))
    warn_title_count_explosion(title_count)
    log_progress(
        "SCAN",
        title_count,
        title_count,
        process=PROCESS_INSERT,
        detail=f"titles found ({scan_data.get('disc_name', '?')})",
    )

    pending = step_write_pending(scan_data, device=device, minlength=minlength)
    _log_identify(pending)
    log_stage(
        "PENDING",
        process=PROCESS_INSERT,
        step=2,
        total=INSERT_STEPS,
        detail=f"disc={pending.disc_name} id={pending.disc_id} -> {pending.scan_json.name}",
    )
    ready_yaml = READY_DIR / f"{pending.disc_id}.yaml"
    if ready_yaml.is_file():
        logger.info("Ready manifest already exists for %s", pending.disc_id)
        log_stage(
            "READY_EXISTS",
            process=PROCESS_INSERT,
            step=3,
            total=INSERT_STEPS,
            detail=str(ready_yaml),
        )
        return StepResult(
            name="disc_insert",
            ok=True,
            message="ready_exists",
            data={
                "disc_id": pending.disc_id,
                "disc_name": pending.disc_name,
                "ready_yaml": str(ready_yaml),
            },
        )

    log_stage(
        "REGISTRY",
        process=PROCESS_INSERT,
        step=3,
        total=INSERT_STEPS,
        detail=f"lookup disc={pending.disc_name} id={pending.disc_id}",
    )
    entry = step_registry_lookup(pending.disc_name)
    if entry is not None:
        dest = step_promote_registry(entry)
        logger.info("Registry hit: %s -> %s", entry.id, dest)
        log_stage(
            "REGISTRY_HIT",
            process=PROCESS_INSERT,
            step=4,
            total=INSERT_STEPS,
            detail=f"{entry.id} -> {dest}",
        )
        return StepResult(
            name="disc_insert",
            ok=True,
            message="registry_hit",
            data={
                "disc_id": entry.id,
                "disc_name": pending.disc_name,
                "ready_yaml": str(dest),
            },
        )

    log_stage(
        "MAP",
        process=PROCESS_INSERT,
        step=4,
        total=INSERT_STEPS,
        detail=f"unknown disc id={pending.disc_id} ({pending.disc_name})",
    )
    map_result = step_trigger_map_async(pending, logger)
    if map_result.ok:
        log_stage(
            "MAP_LAUNCHED",
            process=PROCESS_INSERT,
            step=5,
            total=INSERT_STEPS,
            detail=f"id={pending.disc_id} background agent; waiting for staging/ready/{pending.disc_id}.yaml",
        )
    else:
        log_stage("MAP_FAILED", process=PROCESS_INSERT, detail=map_result.message)
    return StepResult(
        name="disc_insert",
        ok=map_result.ok,
        message="mapping_triggered",
        data={
            "disc_id": pending.disc_id,
            "disc_name": pending.disc_name,
            "scan_json": str(pending.scan_json),
        },
    )


def _total_suffix() -> str:
    """`` total=1h02m09s`` since [rip 1/7], or ``""`` when step 1 wasn't logged here."""
    total = process_elapsed(PROCESS_RIP)
    return f" total={total}" if total else ""


def _manifest_label(manifest: TvDiscManifest | MovieManifest) -> str:
    return (
        getattr(manifest.meta, "title", None)
        or getattr(manifest.meta, "show", None)
        or manifest.meta.id
    )


def _post_rip_movie_stitch(
    manifest: TvDiscManifest | MovieManifest,
    logger: logging.Logger,
) -> None:
    """Post-rip stitch check (outside makemkv lock). Runs when all parts may be ready."""
    if not isinstance(manifest, MovieManifest):
        return
    if not manifest.stitch or not manifest.stitch.enabled:
        return
    from videosripper.stitch import maybe_stitch_after_part

    rcfg = manifest.retry
    result = maybe_stitch_after_part(
        manifest,
        nas_retries=rcfg.nas_retries,
        nas_delay_sec=rcfg.delay_sec,
    )
    if result.message and result.message not in ("stitch disabled",):
        logger.info("Stitch: %s", result.message)


def _verify_and_rip(
    yaml_path: Path,
    manifest: TvDiscManifest | MovieManifest,
    scan_data: dict[str, Any],
    logger: logging.Logger,
) -> StepResult:
    """Verify disc + titles then rip (caller holds the drive lock, scan done)."""
    label = _manifest_label(manifest)
    log_stage(
        "VERIFY",
        process=PROCESS_RIP,
        step=RIP_STEP_VERIFY,
        total=RIP_STEPS,
        detail=f"disc={scan_data.get('disc_name', '?')} vs manifest={label} ({yaml_path.name})",
    )
    match = step_verify_disc_match(yaml_path, manifest, scan_data)
    if not match.ok:
        move_manifest_yaml(yaml_path, FAILED_DIR, reason=f"disc mismatch: {match.message}")
        log_stage("VERIFY_FAILED", process=PROCESS_RIP, detail=match.message)
        return match

    titles = step_verify_titles(manifest, scan_data)
    if not titles.ok:
        move_manifest_yaml(yaml_path, FAILED_DIR, reason="title validation failed")
        log_stage("TITLE_VERIFY_FAILED", process=PROCESS_RIP, detail=titles.message)
        return titles

    track_total = len(getattr(manifest, "tracks", [])) or 1
    log_stage(
        "RIP",
        process=PROCESS_RIP,
        step=RIP_STEP_RIP,
        total=RIP_STEPS,
        detail=f"{label} ({track_total} track(s)) dev={manifest.meta.device}",
    )
    rip = step_rip_manifest(manifest, yaml_path)
    if rip.ok:
        move_manifest_yaml(yaml_path, DONE_DIR)
        logger.info("Rip succeeded: %s", yaml_path.name)
        log_stage(
            "DONE",
            process=PROCESS_RIP,
            step=RIP_STEP_DONE,
            total=RIP_STEPS,
            detail=(
                f"{label} ({yaml_path.name} -> staging/done; copy/eject above)"
                f"{_total_suffix()}"
            ),
        )
        return StepResult(name="process_ready", ok=True, message="ripped")

    move_manifest_yaml(yaml_path, FAILED_DIR, reason=rip.message)
    logger.error("Rip failed for %s: %s", yaml_path.name, rip.message)
    log_stage("RIP_FAILED", process=PROCESS_RIP, detail=f"{rip.message}{_total_suffix()}")
    return rip


def _load_ready_manifest(
    yaml_path: Path,
    logger: logging.Logger,
) -> TvDiscManifest | MovieManifest | None:
    """Validate + load a ready manifest, moving it to failed on error."""
    log_stage(
        "VALIDATE",
        process=PROCESS_RIP,
        step=RIP_STEP_VALIDATE,
        total=RIP_STEPS,
        detail=yaml_path.name,
    )
    validate = step_validate_manifest(yaml_path)
    if not validate.ok:
        move_manifest_yaml(yaml_path, FAILED_DIR, reason="validation failed")
        log_stage("VALIDATE_FAILED", process=PROCESS_RIP, detail=validate.message)
        return None
    try:
        return load_manifest(yaml_path)
    except ValidationError as exc:
        move_manifest_yaml(yaml_path, FAILED_DIR, reason=str(exc))
        log_stage("VALIDATE_FAILED", process=PROCESS_RIP, detail=str(exc))
        return None


def process_ready_manifest(
    yaml_path: Path,
    logger: logging.Logger,
    *,
    device: str | None = None,
) -> StepResult:
    """Process 2: validate → verify disc → rip (serial makemkv sections).

    ``device`` overrides the manifest's target drive (used by device-bound
    workers so a manifest is ripped on whichever drive currently holds it).
    """
    yaml_path = Path(yaml_path)
    setup_pipeline_logger()
    manifest = _load_ready_manifest(yaml_path, logger)
    if manifest is None:
        return StepResult(name="process_ready", ok=False, message="invalid manifest")

    if device:
        manifest.meta.device = device
    label = _manifest_label(manifest)
    if already_ripped(manifest):
        logger.info("Output already complete for %s", yaml_path.name)
        move_manifest_yaml(yaml_path, DONE_DIR)
        log_stage("ALREADY_RIPPED", process=PROCESS_RIP, detail=str(label))
        _post_rip_movie_stitch(manifest, logger)
        return StepResult(name="process_ready", ok=True, message="already_ripped")

    result: StepResult
    with flock(makemkv_lock_path(manifest.meta.device), blocking=True):
        log_stage(
            "RESCAN",
            process=PROCESS_RIP,
            step=RIP_STEP_RESCAN,
            total=RIP_STEPS,
            detail=f"{label} dev={manifest.meta.device}",
        )
        rescan = _rescan_no_lock(manifest, logger)
        if not rescan.ok:
            move_manifest_yaml(yaml_path, FAILED_DIR, reason="re-scan failed")
            log_stage("RESCAN_FAILED", process=PROCESS_RIP, detail=rescan.message)
            return rescan
        result = _verify_and_rip(yaml_path, manifest, rescan.data["scan_data"], logger)
    if result.ok:
        _post_rip_movie_stitch(manifest, logger)
    return result


def _match_ready_manifest_for_scan(
    scan_data: dict[str, Any],
    logger: logging.Logger,
) -> tuple[Path, TvDiscManifest | MovieManifest] | None:
    """Find a ready manifest whose disc matches the scanned disc, if any."""
    for yaml_path in sorted(READY_DIR.glob("*.yaml")):
        try:
            manifest = load_manifest(yaml_path)
        except (ValidationError, OSError, ValueError):
            continue
        match = step_verify_disc_match(yaml_path, manifest, scan_data)
        if match.ok:
            return yaml_path, manifest
    return None


def run_device_worker_once(device: str, logger: logging.Logger) -> StepResult:
    """Pull-by-disc worker for one drive.

    Scans ``device``; if it holds a disc matching a ready manifest, claims and
    rips it on that drive. Safe to run concurrently across distinct drives.
    """
    setup_pipeline_logger()
    stitch_manifest: MovieManifest | None = None
    result: StepResult | None = None

    with flock(makemkv_lock_path(device), blocking=True):
        try:
            scan_data = scan_device_dict(device, 300)
        except Exception as exc:  # no disc / drive busy — idle
            logger.debug("Worker %s idle: %s", device, exc)
            return StepResult(name="device_worker", ok=True, message="idle")

        if not scan_data.get("titles"):
            return StepResult(name="device_worker", ok=True, message="no titles")

        matched = _match_ready_manifest_for_scan(scan_data, logger)
        if matched is None:
            return StepResult(
                name="device_worker",
                ok=True,
                message=f"no manifest for {scan_data.get('disc_name', '?')}",
            )

        yaml_path, manifest = matched
        # Atomic claim: rename so no other worker picks the same manifest.
        claimed = yaml_path.with_suffix(f".yaml.claimed-{device_slug(device)}")
        try:
            yaml_path.rename(claimed)
        except OSError:
            return StepResult(name="device_worker", ok=True, message="claim lost")

        log_stage(
            "CLAIM",
            process=PROCESS_RIP,
            step=RIP_STEP_VALIDATE,
            total=RIP_STEPS,
            detail=f"{yaml_path.name} claimed by dev={device} (disc={scan_data.get('disc_name', '?')})",
        )
        manifest.meta.device = device
        label = _manifest_label(manifest)
        if already_ripped(manifest):
            move_manifest_yaml(claimed, DONE_DIR)
            log_stage("ALREADY_RIPPED", process=PROCESS_RIP, detail=str(label))
            result = StepResult(name="device_worker", ok=True, message="already_ripped")
            if isinstance(manifest, MovieManifest):
                stitch_manifest = manifest
        else:
            log_stage(
                "RESCAN",
                process=PROCESS_RIP,
                step=RIP_STEP_RESCAN,
                total=RIP_STEPS,
                detail=f"{label} dev={device}",
            )
            result = _verify_and_rip(claimed, manifest, scan_data, logger)
            if result.ok and isinstance(manifest, MovieManifest):
                stitch_manifest = manifest

    if stitch_manifest is not None:
        _post_rip_movie_stitch(stitch_manifest, logger)
    assert result is not None
    return result
