"""Pipeline steps as Python functions — serial or parallel depending on step kind."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from ripknife.devices import device_slug
from ripknife.logging_setup import (
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
from ripknife.map_agent import (
    MAP_FAIL_AMBIGUOUS,
    MAP_FAIL_AUTH,
    supervise_background_map,
)
from ripknife.models import MovieManifest, TvDiscManifest, load_manifest, movie_output_path
from ripknife.monitor import MakeMkvError
from ripknife.movie import rip_movie
from ripknife.paths import OUTPUT_ROOT, STAGING_ROOT
from ripknife.registry import load_registry, manifest_matches_scan
from ripknife.runner import TvRipResult, rip_tv_disc, track_outputs_present
from ripknife.title_verify import format_title_issues, validate_manifest_titles
from ripknife.watcher_util import (
    disc_media_generation,
    flock,
    makemkv_lock_path,
    media_present,
    scan_device_dict,
    slugify_disc_id,
)

# Resume sidecar last_reason values that are watcher interruptions (not rip failures).
_WATCHER_INTERRUPT_REASONS = frozenset({"watcher_stop", "orphaned_claim"})

PENDING_DIR = STAGING_ROOT / "pending"
READY_DIR = STAGING_ROOT / "ready"
DONE_DIR = STAGING_ROOT / "done"
FAILED_DIR = STAGING_ROOT / "failed"

REINSERT_MAX = int(os.environ.get("RIPKNIFE_REINSERT_MAX", "5"))
# Empty tray: one reinsert of a normal rip. Not a sector retry, and not five.
TRAY_REINSERT_MAX = 1
# Bad sector whose in-rip retry has not been recorded yet: one normal rip.
SECTOR_REINSERT_MAX = 1

T = TypeVar("T")


@dataclass
class ResumeInfo:
    resume_from_slot: int
    reinsert_count: int = 0
    last_reason: str = ""
    sector_retries_used: int = 0


def manifest_base_id(yaml_path: Path) -> str:
    """Manifest id from a ready/failed path (handles ``*.yaml.claimed-sr0``)."""
    name = yaml_path.name
    if ".yaml.claimed-" in name:
        return name.split(".yaml.claimed-")[0]
    if name.endswith(".yaml"):
        return name[: -len(".yaml")]
    return yaml_path.stem


def resume_sidecar_path(manifest_id: str, directory: Path) -> Path:
    return directory / f"{manifest_id}.resume.json"


def _load_resume_info(manifest_id: str, directory: Path) -> ResumeInfo | None:
    path = resume_sidecar_path(manifest_id, directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return ResumeInfo(
        resume_from_slot=int(data.get("resume_from_slot", 1)),
        reinsert_count=int(data.get("reinsert_count", 0)),
        last_reason=str(data.get("last_reason", "")),
        sector_retries_used=int(data.get("sector_retries_used", 0)),
    )


def _save_resume_info(manifest_id: str, directory: Path, info: ResumeInfo) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    resume_sidecar_path(manifest_id, directory).write_text(
        json.dumps(
            {
                "resume_from_slot": info.resume_from_slot,
                "reinsert_count": info.reinsert_count,
                "last_reason": info.last_reason,
                "sector_retries_used": info.sector_retries_used,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _resume_records_sector_retries(manifest_id: str, directory: Path) -> bool:
    path = resume_sidecar_path(manifest_id, directory)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return "sector_retries_used" in data


def _read_failure_reason(failed_path: Path, manifest_id: str) -> str:
    for name in (f"{manifest_id}.reason.txt", f"{failed_path.stem}.reason.txt"):
        reason_path = FAILED_DIR / name
        if reason_path.is_file():
            return reason_path.read_text(encoding="utf-8").strip()
    return ""


def _manifest_companion_paths(yaml_path: Path, directory: Path) -> list[Path]:
    """Sidecars keyed by manifest id (and legacy stem-based reason files)."""
    base = manifest_base_id(yaml_path)
    found: list[Path] = []
    for name in (f"{base}.resume.json", f"{base}.reason.txt", f"{base}.partial.json", f"{yaml_path.stem}.reason.txt"):
        path = directory / name
        if path.is_file() and path not in found:
            found.append(path)
    return found


def partial_sidecar_path(manifest_id: str, directory: Path) -> Path:
    return directory / f"{manifest_id}.partial.json"


def _save_partial_sidecar(manifest_id: str, directory: Path, result: TvRipResult) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stats = result.stats
    payload = {
        "episodes_ok": list(stats.episodes_written),
        "failed_slots": [
            {
                "slot": f.slot,
                "makemkv_title": f.makemkv_title,
                "episode": f.episode,
                "error": f.error,
                "sector_retries_used": f.sector_retries_used,
            }
            for f in stats.failed_slots
        ],
        "nas_ok": stats.nas_ok,
        "nas_failed": stats.nas_failed,
    }
    partial_sidecar_path(manifest_id, directory).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _format_tv_partial_failure(result: TvRipResult) -> str:
    failed = result.stats.failed_slots
    if not failed:
        return "TV rip failed"
    parts = [f"slot {f.slot} ({f.episode})" for f in failed]
    return f"partial rip: failed {', '.join(parts)}"


def compute_resume_from_slot(manifest: TvDiscManifest | MovieManifest) -> int:
    """First track slot with missing output files (local or NAS; 1 if all complete)."""
    if isinstance(manifest, MovieManifest):
        return 1
    for track in manifest.tracks:
        if not track_outputs_present(manifest, track):
            return track.slot
    return 1


def tv_disc_outputs_complete(manifest: TvDiscManifest) -> bool:
    return all(track_outputs_present(manifest, track) for track in manifest.tracks)


def list_failed_manifests() -> list[Path]:
    if not FAILED_DIR.is_dir():
        return []
    paths: list[Path] = []
    for path in sorted(FAILED_DIR.iterdir()):
        if not path.is_file():
            continue
        if path.name.endswith(".reason.txt") or path.name.endswith(".resume.json"):
            continue
        if path.name.endswith(".partial.json"):
            continue
        if ".yaml" in path.name:
            paths.append(path)
    return paths


def match_failed_manifest_for_scan(
    scan_data: dict[str, Any],
    *,
    disc_id: str | None = None,
    logger: logging.Logger | None = None,
) -> tuple[Path, TvDiscManifest | MovieManifest] | None:
    """Find a failed manifest matching the scanned disc."""
    disc_name = scan_data.get("disc_name", "")
    scan_slug = slugify_disc_id(disc_name) if disc_name else ""
    matches: list[tuple[int, Path, TvDiscManifest | MovieManifest]] = []

    for path in list_failed_manifests():
        try:
            manifest = load_manifest(path)
        except (ValidationError, OSError, ValueError) as exc:
            if logger:
                logger.debug("Skipping unreadable failed manifest %s: %s", path.name, exc)
            continue

        matched = step_verify_disc_match(path, manifest, scan_data).ok
        score = 0
        if matched:
            score = 10
        elif disc_id and manifest.meta.id == disc_id:
            score = 8
        elif scan_slug and manifest.meta.id == scan_slug:
            score = 6
        elif disc_id and manifest_base_id(path) == disc_id:
            score = 4
        else:
            continue

        matches.append((score, path, manifest))

    if not matches:
        return None

    matches.sort(key=lambda item: (-item[0], item[1].name))
    best_score = matches[0][0]
    top = [item for item in matches if item[0] == best_score]
    if len(top) > 1 and logger:
        logger.warning(
            "Multiple failed manifests match disc %r; using %s",
            disc_name,
            top[0][1].name,
        )
    _, path, manifest = top[0]
    return path, manifest


def begin_sector_recovery(
    manifest: TvDiscManifest | MovieManifest,
    reason: str,
    sector_retries_used: int,
    logger: logging.Logger,
) -> None:
    """Run the planner's capped drive commands after the one bad-sector retry.

    Does nothing for every other decision, while ``makemkvcon`` is running,
    or when the block device is missing. Does not edit MakeMKV settings and
    does not call the model. Stitch and verify stay the planner's next step
    when they have no command tuple.
    """
    from dataclasses import replace

    from ripknife.recover import (
        FAILURE_START_RECOVERY,
        LadderState,
        block_device,
        decide_failure_action,
        dump_ladder_state,
        load_ladder_state,
        makemkvcon_pids,
        parse_ddrescue_map,
        recovery_blocked,
        run_capped_sector_recovery,
        run_recovery_command,
    )

    if (
        decide_failure_action(reason, normal_retries_used=sector_retries_used)
        != FAILURE_START_RECOVERY
    ):
        return
    try:
        pids = makemkvcon_pids()
    except RuntimeError as exc:
        logger.error("Sector recovery not started: %s", exc)
        return
    blocked = recovery_blocked(makemkv_pids=pids, lock_held=False)
    if blocked:
        logger.error("Sector recovery not started: %s", blocked)
        return
    block = block_device(manifest.meta.device)
    if not Path(block).exists():
        logger.error(
            "Sector recovery not started: %s is missing; not imaging an empty tray",
            block,
        )
        return
    iso = OUTPUT_ROOT / f"{manifest.meta.id}-recovery" / f"{manifest.meta.id}.iso"
    iso.parent.mkdir(parents=True, exist_ok=True)
    mapfile = iso.with_suffix(".map")
    state_path = iso.with_suffix(".ladder.json")
    state = LadderState()
    if state_path.is_file():
        try:
            state = load_ladder_state(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.error("Sector recovery state unreadable: %s", exc)
            return
    status = None
    if mapfile.is_file():
        try:
            status = parse_ddrescue_map(mapfile.read_text(encoding="utf-8"))
            state = replace(state, image_done=True)
        except (OSError, ValueError) as exc:
            logger.error("Sector recovery map unreadable: %s", exc)
            return

    def _persist(ladder: LadderState) -> None:
        state_path.write_text(dump_ladder_state(ladder), encoding="utf-8")

    def _read_status():
        if not mapfile.is_file():
            return None
        return parse_ddrescue_map(mapfile.read_text(encoding="utf-8"))

    title = 0
    if isinstance(manifest, MovieManifest) and manifest.title_selection.mode == "fixed":
        if manifest.title_selection.title is not None:
            title = manifest.title_selection.title
    try:
        steps = run_capped_sector_recovery(
            reason,
            normal_retries_used=sector_retries_used,
            device=manifest.meta.device,
            iso=iso,
            mapfile=mapfile,
            state=state,
            status=status,
            title=title,
            minlength=manifest.meta.minlength,
            run=run_recovery_command,
            read_status=_read_status,
            makemkv_pids=pids,
            on_state=_persist,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("Sector recovery stopped: %s", exc)
        return
    kinds = ", ".join(step.kind for step in steps) or "none"
    log_stage(
        "SECTOR_RECOVERY",
        process=PROCESS_RIP,
        detail=f"{manifest.meta.id} steps: {kinds}",
    )
    logger.info("Sector recovery steps for %s: %s", manifest.meta.id, kinds)


def _on_sector_recovery(logger: logging.Logger):
    def _start(
        manifest: TvDiscManifest | MovieManifest,
        reason: str,
        used: int,
    ) -> None:
        begin_sector_recovery(manifest, reason, used, logger)

    return _start


def recover_failed_manifest_for_scan(
    scan_data: dict[str, Any],
    logger: logging.Logger,
    *,
    disc_id: str | None = None,
    on_recovery: Callable[[TvDiscManifest | MovieManifest, str, int], None] | None = None,
) -> Path | None:
    """Promote a matching failed manifest to staging/ready for resume on re-insert.

    A bad-sector failure that already used its one in-rip retry is not
    promoted. ``on_recovery`` continues the bounded ladder instead. An empty
    tray gets one normal reinsert. Ambiguous maps, wrong titles, and NAS
    copy failures are left in ``staging/failed``.
    """
    matched = match_failed_manifest_for_scan(scan_data, disc_id=disc_id, logger=logger)
    if matched is None:
        return None

    failed_path, manifest = matched
    manifest_id = manifest.meta.id
    ready_path = READY_DIR / f"{manifest_id}.yaml"
    if ready_path.is_file():
        logger.info("Ready manifest already queued for resume: %s", manifest_id)
        return ready_path

    if isinstance(manifest, MovieManifest) and already_ripped(manifest):
        dest = move_manifest_yaml(failed_path, DONE_DIR, reason="outputs complete on re-insert")
        log_stage(
            "FAILED_RECOVERED",
            process=PROCESS_INSERT,
            detail=f"{manifest_id} outputs complete -> {dest.name}",
        )
        return dest

    if isinstance(manifest, TvDiscManifest) and tv_disc_outputs_complete(manifest):
        dest = move_manifest_yaml(failed_path, DONE_DIR, reason="outputs complete on re-insert")
        log_stage(
            "FAILED_RECOVERED",
            process=PROCESS_INSERT,
            detail=f"{manifest_id} all episodes exist -> {dest.name}",
        )
        return dest

    info = _load_resume_info(manifest_id, FAILED_DIR) or ResumeInfo(resume_from_slot=1)
    previous_reason = info.last_reason
    reason = _read_failure_reason(failed_path, manifest_id) or previous_reason
    from ripknife.recover import (
        FAILURE_RETRY_NORMAL,
        FAILURE_START_RECOVERY,
        FAILURE_STOP,
        FAILURE_WAIT_REINSERT,
        bad_sector_failure,
        decide_failure_action,
        failure_stops_reread,
    )

    recorded = _resume_records_sector_retries(manifest_id, FAILED_DIR)
    if recorded:
        used = info.sector_retries_used
    elif bad_sector_failure(reason):
        # The manifest is in failed/ because the in-rip retry already returned.
        used = 1
    else:
        used = 0
    action = decide_failure_action(reason, normal_retries_used=used)
    info.last_reason = reason
    info.sector_retries_used = used
    # Watcher stop / orphaned claim is not a disc re-insert — don't burn the cap.
    # A spent bad-sector retry is not another normal attempt either.
    if (
        previous_reason not in _WATCHER_INTERRUPT_REASONS
        and action != FAILURE_START_RECOVERY
    ):
        info.reinsert_count += 1
    if action == FAILURE_START_RECOVERY:
        _save_resume_info(manifest_id, FAILED_DIR, info)
        log_stage(
            "SECTOR_RECOVERY",
            process=PROCESS_INSERT,
            detail=(
                f"{manifest_id} bad-sector retry spent; "
                "not scheduling another full-speed rip"
            ),
        )
        logger.error(
            "Bad-sector retry already used for %s; continuing recovery, not a normal rip",
            manifest_id,
        )
        if on_recovery is not None:
            on_recovery(manifest, reason, used)
        return failed_path
    if action == FAILURE_WAIT_REINSERT and info.reinsert_count > TRAY_REINSERT_MAX:
        _save_resume_info(manifest_id, FAILED_DIR, info)
        log_stage(
            "RESUME_CAP",
            process=PROCESS_INSERT,
            detail=(
                f"{manifest_id} tray re-insert limit ({TRAY_REINSERT_MAX}) exceeded; "
                "not imaging an empty tray"
            ),
        )
        logger.error(
            "Tray re-insert cap reached for %s (%d/%d); not auto-queuing",
            manifest_id,
            info.reinsert_count,
            TRAY_REINSERT_MAX,
        )
        return None
    if action == FAILURE_RETRY_NORMAL and info.reinsert_count > SECTOR_REINSERT_MAX:
        _save_resume_info(manifest_id, FAILED_DIR, info)
        log_stage(
            "RESUME_CAP",
            process=PROCESS_INSERT,
            detail=(
                f"{manifest_id} bad-sector re-insert limit ({SECTOR_REINSERT_MAX}) "
                "exceeded; manual intervention required"
            ),
        )
        return None
    if action == FAILURE_STOP and failure_stops_reread(reason):
        _save_resume_info(manifest_id, FAILED_DIR, info)
        log_stage(
            "RESUME_STOP",
            process=PROCESS_INSERT,
            detail=f"{manifest_id} not a sector failure; not re-ripping and not imaging",
        )
        logger.error(
            "Not re-queuing %s (%s); no sector recovery",
            manifest_id,
            reason,
        )
        return None
    if info.reinsert_count > REINSERT_MAX:
        log_stage(
            "RESUME_CAP",
            process=PROCESS_INSERT,
            detail=(
                f"{manifest_id} re-insert limit ({REINSERT_MAX}) exceeded; "
                "manual intervention required"
            ),
        )
        logger.error(
            "Re-insert resume cap reached for %s (%d/%d); not auto-queuing",
            manifest_id,
            info.reinsert_count,
            REINSERT_MAX,
        )
        return None

    info.resume_from_slot = compute_resume_from_slot(manifest)
    READY_DIR.mkdir(parents=True, exist_ok=True)
    companions = _manifest_companion_paths(failed_path, FAILED_DIR)
    shutil.move(str(failed_path), str(ready_path))
    for companion in companions:
        dest_name = companion.name
        if companion.name.endswith(".reason.txt"):
            dest_name = f"{manifest_id}.reason.txt"
        dest = READY_DIR / dest_name
        if dest.is_file():
            companion.unlink(missing_ok=True)
        else:
            companion.rename(dest)
    _save_resume_info(manifest_id, READY_DIR, info)
    log_stage(
        "FAILED_RECOVERED",
        process=PROCESS_INSERT,
        detail=(
            f"{manifest_id} failed -> ready slot>={info.resume_from_slot} "
            f"reinsert={info.reinsert_count}/{REINSERT_MAX}"
        ),
    )
    logger.info(
        "Recovered failed manifest %s -> %s (resume slot %s, reinsert %s/%s)",
        failed_path.name,
        ready_path.name,
        info.resume_from_slot,
        info.reinsert_count,
        REINSERT_MAX,
    )
    return ready_path


def rip_start_at_for_manifest(yaml_path: Path, manifest: TvDiscManifest | MovieManifest) -> int | None:
    info = _load_resume_info(manifest.meta.id, yaml_path.parent)
    if info is None:
        return None
    return info.resume_from_slot if info.resume_from_slot > 1 else None


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


# Reuse pending scan when the same disc is still loaded (skips slow makemkv info).
SCAN_CACHE_MAX_AGE_SEC = int(os.environ.get("RIPKNIFE_SCAN_CACHE_SEC", "86400"))


def _parse_scanned_at(value: str) -> float | None:
    try:
        from datetime import datetime, timezone

        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def find_cached_pending_scan(
    device: str,
    media_gen: str | None,
    *,
    max_age_sec: int = SCAN_CACHE_MAX_AGE_SEC,
) -> PendingDisc | None:
    """Return a fresh pending scan for the same loaded disc, if any."""
    if media_gen is None or not PENDING_DIR.is_dir():
        return None
    now = time.time()
    for meta_path in sorted(PENDING_DIR.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("device") != device or meta.get("media_generation") != media_gen:
            continue
        scanned_at = _parse_scanned_at(meta.get("scanned_at", ""))
        if scanned_at is None or now - scanned_at > max_age_sec:
            continue
        disc_id = meta.get("id") or meta_path.stem.removesuffix(".meta")
        scan_json = PENDING_DIR / f"{disc_id}.scan.json"
        if not scan_json.is_file():
            continue
        try:
            scan_data = json.loads(scan_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        return PendingDisc(
            disc_id=disc_id,
            disc_name=meta.get("disc_name", scan_data.get("disc_name", "")),
            scan_json=scan_json,
            meta_json=meta_path,
            scan_data=scan_data,
        )
    return None


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
    from ripknife.identify import attach_identification_to_scan, gather_identification_hints

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
    media_gen = disc_media_generation(device)
    if media_gen is not None:
        meta["media_generation"] = media_gen
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
    *,
    device: str | None = None,
) -> StepResult:
    """Map an unknown disc to a ready manifest (OpenRouter JSON).

    A missing key or a bad answer does not write YAML and does not rip.
    At most one extra attempt after ``.env`` merge when the key is missing
    or rejected. Do not restart the watcher mid-rip.
    """
    try:
        ok, message, kind = supervise_background_map(
            pending.disc_id,
            pending.scan_json,
            logger=logger,
            device=device,
        )
        if ok:
            return StepResult(
                name="map",
                ok=True,
                message=message,
                data={"disc_id": pending.disc_id},
            )
        if kind == MAP_FAIL_AUTH:
            status = "map_agent_auth"
        elif kind == MAP_FAIL_AMBIGUOUS:
            status = "map_ambiguous"
        else:
            status = "map_failed"
        return StepResult(
            name="map",
            ok=False,
            message=status,
            data={
                "disc_id": pending.disc_id,
                "last_error": message,
                "map_failure": kind or "failed",
            },
        )
    except Exception as exc:
        logger.error("Map launch failed: %s", exc)
        return StepResult(
            name="map",
            ok=False,
            message="map_failed",
            data={"disc_id": pending.disc_id, "last_error": str(exc), "map_failure": "failed"},
        )


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
            from ripknife.paths import MOVIES_NAS, resolve_path

            nas_base = resolve_path(manifest.nas.share) if manifest.nas.share else MOVIES_NAS
            nas_path = nas_base / out.name
            return nas_path.is_file() and nas_path.stat().st_size >= rules.min_size_bytes
        return False
    return tv_disc_outputs_complete(manifest)


def list_claimed_ready_manifests(*, device_slug_filter: str | None = None) -> list[Path]:
    if not READY_DIR.is_dir():
        return []
    paths = sorted(READY_DIR.glob("*.yaml.claimed-*"))
    if device_slug_filter is None:
        return paths
    suffix = f".claimed-{device_slug_filter}"
    return [p for p in paths if p.name.endswith(suffix)]


def persist_interrupted_claimed_rips(
    *,
    reason: str = "watcher_stop",
    device_slug_filter: str | None = None,
) -> list[str]:
    """On watcher stop: move claimed ready manifests to failed/ with resume sidecars.

    Detects the first missing slot (local **and** NAS when ``copy_to_nas``) so a
    later restart resumes instead of re-ripping completed episodes.
    """
    persisted: list[str] = []
    for claimed in list_claimed_ready_manifests(device_slug_filter=device_slug_filter):
        try:
            manifest = load_manifest(claimed)
        except (ValidationError, OSError, ValueError) as exc:
            log_stage(
                "WATCHER_STOP_RESUME",
                process=PROCESS_RIP,
                detail=f"unreadable claimed {claimed.name}: {exc}",
            )
            continue
        manifest_id = manifest.meta.id
        if isinstance(manifest, MovieManifest) and already_ripped(manifest):
            dest = move_manifest_yaml(claimed, DONE_DIR, reason="outputs complete on stop")
            log_stage(
                "WATCHER_STOP_RESUME",
                process=PROCESS_RIP,
                detail=f"{manifest_id} complete -> {dest.name}",
            )
            persisted.append(manifest_id)
            continue
        if isinstance(manifest, TvDiscManifest) and tv_disc_outputs_complete(manifest):
            dest = move_manifest_yaml(claimed, DONE_DIR, reason="outputs complete on stop")
            log_stage(
                "WATCHER_STOP_RESUME",
                process=PROCESS_RIP,
                detail=f"{manifest_id} all episodes exist -> {dest.name}",
            )
            persisted.append(manifest_id)
            continue

        prev = _load_resume_info(manifest_id, READY_DIR) or _load_resume_info(
            manifest_id, FAILED_DIR
        )
        resume_slot = compute_resume_from_slot(manifest)
        info = ResumeInfo(
            resume_from_slot=resume_slot,
            reinsert_count=prev.reinsert_count if prev else 0,
            last_reason=reason,
        )
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        _save_resume_info(manifest_id, READY_DIR, info)
        dest = move_manifest_yaml(claimed, FAILED_DIR, reason=reason)
        log_stage(
            "WATCHER_STOP_RESUME",
            process=PROCESS_RIP,
            detail=f"id={manifest_id} slot>={resume_slot} -> {dest.name}",
        )
        persisted.append(manifest_id)
    return persisted


def cleanup_orphan_rip_staging() -> list[str]:
    """Remove ``staging/.rip.*`` dirs not referenced by a live process cmdline/cwd."""
    if not STAGING_ROOT.is_dir():
        return []
    live_refs: set[str] = set()
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            for rel in ("cmdline", "cwd"):
                try:
                    if rel == "cwd":
                        target = str((entry / "cwd").resolve())
                    else:
                        target = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                            "utf-8", errors="ignore"
                        )
                except OSError:
                    continue
                live_refs.add(target)

    removed: list[str] = []
    for path in sorted(STAGING_ROOT.glob(".rip.*")):
        if not path.is_dir():
            continue
        resolved = str(path.resolve())
        in_use = any(resolved in ref for ref in live_refs)
        if in_use:
            continue
        try:
            shutil.rmtree(path)
            removed.append(path.name)
        except OSError:
            continue
    if removed:
        log_stage(
            "STAGING_CLEANUP",
            process=PROCESS_RIP,
            detail=f"removed orphan rip dirs: {', '.join(removed)}",
        )
    return removed


def recover_interrupted_rips(
    devices: list[str],
    logger: logging.Logger,
    *,
    hold_locks: bool = True,
) -> list[Path]:
    """Startup recovery: orphan claimed → failed+resume; promote when disc still loaded.

    Call only after the per-device rip-worker flock is held so a second watcher
    instance cannot steal an in-flight claim. Does not require a disc re-insert —
    if the same disc is still in a drive, the job returns to ``staging/ready/``
    with ``resume_from_slot``.
    """
    setup_pipeline_logger()
    cleanup_orphan_rip_staging()

    recovered: list[Path] = []
    for device in devices:
        slug = device_slug(device)
        # Only unclaim when no live makemkvcon holds the drive (avoid racing a
        # resilient-loop restart that still owns an in-flight rip).
        from ripknife.monitor import list_makemkv_processes

        if not list_makemkv_processes(device):
            persist_interrupted_claimed_rips(
                reason="orphaned_claim", device_slug_filter=slug
            )
        if not media_present(device):
            continue

        def _recover_one() -> Path | None:
            try:
                scan_data = scan_device_dict(device, 300)
            except Exception as exc:  # noqa: BLE001 — idle / busy drive
                logger.debug("Startup recover scan %s skipped: %s", device, exc)
                return None
            if not scan_data.get("titles"):
                return None
            return recover_failed_manifest_for_scan(
                scan_data,
                logger,
                on_recovery=_on_sector_recovery(logger),
            )

        try:
            if hold_locks:
                with flock(makemkv_lock_path(device), blocking=False):
                    path = _recover_one()
            else:
                # Caller already holds the makemkv lock (reentrant nested path).
                path = _recover_one()
        except BlockingIOError:
            logger.info(
                "Startup recover skipped for %s (drive lock held — rip in progress)",
                device,
            )
            continue
        if path is not None and path.parent == READY_DIR:
            recovered.append(path)
            log_stage(
                "INTERRUPT_RECOVERED",
                process=PROCESS_RIP,
                detail=f"dev={device} {path.name}",
            )
    return recovered


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
    from ripknife.logging_setup import setup_session_logger

    setup_session_logger(yaml_path or Path(manifest.meta.id))
    try:
        if isinstance(manifest, TvDiscManifest):
            result = rip_tv_disc(
                manifest,
                dry_run=dry_run,
                start_at=start_at,
                on_stall=on_stall,
                retry=retry,
                retry_delay=retry_delay,
            )
            if result.stats.failed_slots:
                message = _format_tv_partial_failure(result)
                used = max(slot.sector_retries_used for slot in result.stats.failed_slots)
                return StepResult(
                    name="rip",
                    ok=False,
                    message=message,
                    data={
                        "partial": result.partial,
                        "tv_result": result,
                        "sector_retries_used": used,
                    },
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
        data: dict[str, Any] = {}
        used = getattr(exc, "sector_retries_used", None)
        if used is not None:
            data["sector_retries_used"] = int(used)
        return StepResult(name="rip", ok=False, message=str(exc), data=data)


def move_manifest_yaml(yaml_path: Path, dest_dir: Path, *, reason: str = "") -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest_id = manifest_base_id(yaml_path)
    dest_name = f"{manifest_id}.yaml"
    dest = dest_dir / dest_name
    shutil.move(str(yaml_path), str(dest))
    source_dir = yaml_path.parent
    for companion in _manifest_companion_paths(yaml_path, source_dir):
        companion_dest = dest_dir / companion.name
        if companion.name.endswith(".reason.txt"):
            companion_dest = dest_dir / f"{manifest_id}.reason.txt"
        if companion_dest.is_file():
            companion.unlink(missing_ok=True)
        else:
            companion.rename(companion_dest)
    if reason:
        (dest_dir / f"{manifest_id}.reason.txt").write_text(reason + "\n", encoding="utf-8")
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
    if ident.get("fingerprint_title"):
        bits.append(f"fingerprint={ident['fingerprint_title']}")
    if ident.get("ambiguous"):
        bits.append("ambiguous=True")
    log_stage("IDENTIFY", process=PROCESS_INSERT, detail=" ".join(bits))


def handle_disc_insert(
    device: str,
    minlength: int,
    logger: logging.Logger,
) -> StepResult:
    """Process 1: scan → pending → registry hit OR background map."""
    setup_pipeline_logger()
    media_gen = disc_media_generation(device)
    cached = find_cached_pending_scan(device, media_gen)
    if cached is not None:
        log_stage(
            "SCAN_CACHED",
            process=PROCESS_INSERT,
            step=1,
            total=INSERT_STEPS,
            detail=f"device={device} id={cached.disc_id} ({cached.disc_name})",
        )
        pending = cached
        scan_data = cached.scan_data
    else:
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
        from ripknife.post_rip_verify import warn_title_count_explosion

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

    recovered = recover_failed_manifest_for_scan(
        scan_data,
        logger,
        disc_id=pending.disc_id,
        on_recovery=_on_sector_recovery(logger),
    )
    if recovered is not None and recovered.parent == FAILED_DIR:
        return StepResult(
            name="disc_insert",
            ok=True,
            message="sector_recovery",
            data={
                "disc_id": pending.disc_id,
                "disc_name": pending.disc_name,
                "failed_yaml": str(recovered),
            },
        )
    if recovered is not None and recovered.parent == READY_DIR:
        return StepResult(
            name="disc_insert",
            ok=True,
            message="failed_recovered",
            data={
                "disc_id": pending.disc_id,
                "disc_name": pending.disc_name,
                "ready_yaml": str(recovered),
            },
        )

    matched_ready = _match_ready_manifest_for_scan(pending.scan_data, logger)
    if matched_ready is not None:
        ready_yaml, _matched_manifest = matched_ready
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
    map_result = step_trigger_map_async(pending, logger, device=device)
    if map_result.ok:
        log_stage(
            "MAP_LAUNCHED",
            process=PROCESS_INSERT,
            step=5,
            total=INSERT_STEPS,
            detail=(
                f"id={pending.disc_id} {map_result.message}; "
                f"staging/ready/{pending.disc_id}.yaml or identified-slug YAML"
            ),
        )
        return StepResult(
            name="disc_insert",
            ok=True,
            message="mapping_triggered",
            data={
                "disc_id": pending.disc_id,
                "disc_name": pending.disc_name,
                "scan_json": str(pending.scan_json),
            },
        )
    failure = (map_result.data or {}).get("map_failure")
    stage = "MAP_AMBIGUOUS" if failure == MAP_FAIL_AMBIGUOUS else "MAP_FAILED"
    log_stage(stage, process=PROCESS_INSERT, detail=map_result.message)
    return StepResult(
        name="disc_insert",
        ok=False,
        message=map_result.message or "map_failed",
        data={
            "disc_id": pending.disc_id,
            "disc_name": pending.disc_name,
            "scan_json": str(pending.scan_json),
            "last_error": map_result.data.get("last_error") or map_result.message,
            "map_failure": map_result.data.get("map_failure") or "failed",
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
    from ripknife.stitch import maybe_stitch_after_part

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
    start_at = rip_start_at_for_manifest(yaml_path, manifest)
    rip = step_rip_manifest(manifest, yaml_path, start_at=start_at)
    if rip.ok:
        resume_sidecar_path(manifest.meta.id, yaml_path.parent).unlink(missing_ok=True)
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

    failure_text = _rip_failure_text(rip)
    used = int(rip.data.get("sector_retries_used") or 0)
    from ripknife.recover import FAILURE_START_RECOVERY, decide_failure_action

    action = decide_failure_action(failure_text, normal_retries_used=used)
    tv_result = rip.data.get("tv_result")
    if isinstance(tv_result, TvRipResult):
        resume_slot = min(f.slot for f in tv_result.stats.failed_slots)
        _save_resume_info(
            manifest.meta.id,
            yaml_path.parent,
            ResumeInfo(
                resume_from_slot=resume_slot,
                last_reason=failure_text,
                sector_retries_used=used,
            ),
        )
        move_manifest_yaml(yaml_path, FAILED_DIR, reason=failure_text)
        _save_partial_sidecar(manifest.meta.id, FAILED_DIR, tv_result)
        logger.error("Rip partial for %s: %s", yaml_path.name, rip.message)
        log_stage(
            "RIP_PARTIAL" if tv_result.partial else "RIP_FAILED",
            process=PROCESS_RIP,
            detail=f"{rip.message}{_total_suffix()}",
        )
        if action == FAILURE_START_RECOVERY:
            begin_sector_recovery(manifest, failure_text, used, logger)
        return StepResult(
            name="process_ready",
            ok=False,
            message=rip.message,
            data={"partial": tv_result.partial},
        )

    _save_resume_info(
        manifest.meta.id,
        yaml_path.parent,
        ResumeInfo(
            resume_from_slot=1,
            last_reason=failure_text,
            sector_retries_used=used,
        ),
    )
    move_manifest_yaml(yaml_path, FAILED_DIR, reason=failure_text)
    logger.error("Rip failed for %s: %s", yaml_path.name, rip.message)
    log_stage("RIP_FAILED", process=PROCESS_RIP, detail=f"{rip.message}{_total_suffix()}")
    if action == FAILURE_START_RECOVERY:
        begin_sector_recovery(manifest, failure_text, used, logger)
    return rip


def _rip_failure_text(rip: StepResult) -> str:
    """Rip message plus slot errors, so a bare title failure still classifies."""
    parts = [rip.message]
    tv_result = rip.data.get("tv_result")
    if isinstance(tv_result, TvRipResult):
        parts.extend(slot.error for slot in tv_result.stats.failed_slots if slot.error)
    return "\n".join(part for part in parts if part)


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
        from ripknife.monitor import ensure_drive_available

        ensure_drive_available(device)
        try:
            scan_data = scan_device_dict(device, 300)
        except Exception as exc:  # no disc / drive busy — idle
            logger.debug("Worker %s idle: %s", device, exc)
            return StepResult(name="device_worker", ok=True, message="idle")

        if not scan_data.get("titles"):
            return StepResult(name="device_worker", ok=True, message="no titles")

        recover_failed_manifest_for_scan(
            scan_data,
            logger,
            on_recovery=_on_sector_recovery(logger),
        )

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
