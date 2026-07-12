#!/usr/bin/env python3
"""Process 2: watch staging/ready manifests, verify disc, rip."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/mnt/game1/VideosRipper")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from videosripper.models import (  # noqa: E402
    MovieManifest,
    TvDiscManifest,
    load_manifest,
    movie_output_path,
)
from videosripper.paths import LOG_ROOT  # noqa: E402
from videosripper.paths import ROOT as PKG_ROOT  # noqa: E402
from videosripper.registry import load_registry, manifest_matches_scan  # noqa: E402
from videosripper.title_verify import format_title_issues, validate_manifest_titles  # noqa: E402
from videosripper.watcher_util import (  # noqa: E402
    flock,
    makemkv_lock_path,
    scan_device_dict,
    setup_watcher_logger,
    update_state,
    watcher_lock_path,
)

READY_DIR = PKG_ROOT / "staging" / "ready"
DONE_DIR = PKG_ROOT / "staging" / "done"
FAILED_DIR = PKG_ROOT / "staging" / "failed"
LOG_FILE = LOG_ROOT / "rip-watcher.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VideosRipper rip queue watcher")
    parser.add_argument("--poll-sec", type=float, default=5.0, help="Poll interval when inotify unavailable")
    parser.add_argument("--once", action="store_true", help="Process one ready yaml then exit")
    return parser.parse_args()


def list_ready_yamls() -> list[Path]:
    if not READY_DIR.is_dir():
        return []
    return sorted(READY_DIR.glob("*.yaml"))


def validate_manifest(yaml_path: Path, logger) -> bool:
    code, output = _run_cli(["validate", str(yaml_path)])
    if code == 0:
        logger.info("Validated %s", yaml_path.name)
        return True
    logger.error("Validation failed for %s: %s", yaml_path.name, output.strip())
    return False


def rescan_manifest(manifest: TvDiscManifest | MovieManifest, logger) -> dict | None:
    device = manifest.meta.device
    minlength = manifest.meta.minlength
    try:
        return scan_device_dict(device, minlength)
    except Exception as exc:
        logger.error("Re-scan failed: %s", exc)
        return None


def verify_match(
    yaml_path: Path,
    manifest: TvDiscManifest | MovieManifest,
    scan_data: dict,
    logger,
) -> bool:
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
        logger.info("Disc match OK: %s", disc_name)
        return True
    logger.error(
        "Disc mismatch: scan=%r manifest_id=%s disc_name_meta=%s",
        disc_name,
        manifest.meta.id,
        manifest.meta.disc_name,
    )
    return False


def verify_titles(
    manifest: TvDiscManifest | MovieManifest,
    scan_data: dict,
    logger,
) -> bool:
    issues = validate_manifest_titles(manifest, scan_data)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warn"]
    for w in warnings:
        logger.warning("Title verify: %s", w.message)
    if errors:
        logger.error("Title validation failed:\n%s", format_title_issues(errors))
        return False
    logger.info("Title validation OK (%d tracks)", len(getattr(manifest, "tracks", [])) or 1)
    return True


def rip_manifest(yaml_path: Path, logger) -> tuple[bool, str]:
    logger.info("Starting REAL rip (not dry-run): %s", yaml_path)
    update_state("rip_watcher", {"status": "ripping", "yaml": str(yaml_path)})
    code, output = _run_cli(["rip", str(yaml_path)])
    if code == 0:
        logger.info("Rip succeeded: %s", yaml_path.name)
        return True, ""
    reason = output.strip() or f"rip exited {code}"
    logger.error("Rip failed for %s: %s", yaml_path.name, reason)
    return False, reason


def move_yaml(yaml_path: Path, dest_dir: Path, *, reason: str = "") -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / yaml_path.name
    shutil.move(str(yaml_path), str(dest))
    if reason:
        reason_path = dest_dir / f"{yaml_path.stem}.reason.txt"
        reason_path.write_text(reason + "\n", encoding="utf-8")


def already_ripped(manifest: TvDiscManifest | MovieManifest) -> bool:
    """True when output already satisfies manifest validation floors (local or NAS)."""
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


def process_yaml(yaml_path: Path, logger) -> None:
    if not validate_manifest(yaml_path, logger):
        move_yaml(yaml_path, FAILED_DIR, reason="validation failed")
        update_state("rip_watcher", {"status": "failed", "yaml": yaml_path.name, "reason": "validation"})
        return

    manifest = load_manifest(yaml_path)

    if already_ripped(manifest):
        logger.info("Output already complete for %s — moving to done", yaml_path.name)
        move_yaml(yaml_path, DONE_DIR)
        update_state("rip_watcher", {"status": "done", "yaml": yaml_path.name, "reason": "already_ripped"})
        return

    with flock(makemkv_lock_path(), blocking=True):
        scan_data = rescan_manifest(manifest, logger)
        if scan_data is None:
            move_yaml(yaml_path, FAILED_DIR, reason="re-scan failed")
            update_state("rip_watcher", {"status": "failed", "yaml": yaml_path.name, "reason": "scan"})
            return

        if not verify_match(yaml_path, manifest, scan_data, logger):
            move_yaml(
                yaml_path,
                FAILED_DIR,
                reason=f"disc mismatch: scan disc_name={scan_data.get('disc_name', '')!r}",
            )
            update_state("rip_watcher", {"status": "failed", "yaml": yaml_path.name, "reason": "mismatch"})
            return

        if not verify_titles(manifest, scan_data, logger):
            move_yaml(yaml_path, FAILED_DIR, reason="title validation failed")
            update_state(
                "rip_watcher",
                {"status": "failed", "yaml": yaml_path.name, "reason": "title_validation"},
            )
            return

        ok, reason = rip_manifest(yaml_path, logger)
        if ok:
            move_yaml(yaml_path, DONE_DIR)
            update_state("rip_watcher", {"status": "done", "yaml": yaml_path.name})
        else:
            move_yaml(yaml_path, FAILED_DIR, reason=reason)
            update_state("rip_watcher", {"status": "failed", "yaml": yaml_path.name, "reason": reason})


def _run_cli(args: list[str]) -> tuple[int, str]:
    cmd = [str(PKG_ROOT / "scripts" / "videosripper"), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=PKG_ROOT, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def _yaml_seen_key(path: Path) -> tuple[str, float]:
    return str(path.resolve()), path.stat().st_mtime


def watch_loop(args: argparse.Namespace, logger) -> int:
    seen: set[tuple[str, float]] = set()
    logger.info("Rip watcher started (poll=%ss)", args.poll_sec)
    update_state("rip_watcher", {"status": "idle"})

    while True:
        for yaml_path in list_ready_yamls():
            if not _file_stable(yaml_path):
                continue
            key = _yaml_seen_key(yaml_path)
            if key in seen:
                continue
            seen.add(key)
            process_yaml(yaml_path, logger)
            if args.once:
                return 0
        time.sleep(args.poll_sec)


def _file_stable(path: Path, *, wait_sec: float = 1.0) -> bool:
    if not path.is_file():
        return False
    mtime = path.stat().st_mtime
    time.sleep(wait_sec)
    return path.is_file() and path.stat().st_mtime == mtime


def main() -> int:
    args = parse_args()
    logger = setup_watcher_logger("rip-watcher", LOG_FILE)
    READY_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with flock(watcher_lock_path("rip-watcher"), blocking=False):
            while True:
                try:
                    code = watch_loop(args, logger)
                    return code
                except Exception:
                    logger.exception("Rip watcher crashed; restarting in 10s")
                    time.sleep(10.0)
    except BlockingIOError:
        logger.error("Another rip-watcher instance is already running")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
