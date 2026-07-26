#!/usr/bin/env python3
"""Process 2: watch staging/ready manifests, verify disc, rip.

Default (no device args): single-drive, file-driven — watches ``staging/ready``
and rips each manifest against whatever drive its ``meta.device`` names.

Multi-drive (``--device`` repeatable or ``--auto``): device-bound workers using
a pull-by-disc model. Each drive scans the disc it holds, claims the matching
ready manifest, and rips it — so multiple drives rip in parallel.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path("/mnt/game1/VideosRipper")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from videosripper.devices import device_slug, resolve_watch_devices  # noqa: E402
from videosripper.logging_setup import log_stage, log_watcher_waiting  # noqa: E402
from videosripper.paths import LOG_ROOT  # noqa: E402
from videosripper.pipeline import (  # noqa: E402
    DONE_DIR,
    FAILED_DIR,
    READY_DIR,
    already_ripped,
    process_ready_manifest,
    run_device_worker_once,
    validate_manifest_file,
)
from videosripper.watcher_util import (  # noqa: E402
    flock,
    media_present,
    setup_watcher_logger,
    update_state,
    watcher_lock_path,
)

LOG_FILE = LOG_ROOT / "rip-watcher.log"

_WORKER_IDLE_MESSAGES = {"idle", "no titles", "claim lost"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VideosRipper rip queue watcher")
    parser.add_argument(
        "--device",
        action="append",
        dest="devices",
        default=None,
        help="Bind a rip worker to this drive (repeatable). Enables device-bound mode.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Discover every /dev/sr* drive and run a rip worker per drive",
    )
    parser.add_argument("--poll-sec", type=float, default=5.0, help="Poll interval when inotify unavailable")
    parser.add_argument("--once", action="store_true", help="Process one ready yaml then exit")
    return parser.parse_args()


def list_ready_yamls() -> list[Path]:
    if not READY_DIR.is_dir():
        return []
    return sorted(READY_DIR.glob("*.yaml"))


def _yaml_seen_key(path: Path) -> tuple[str, float]:
    return str(path.resolve()), path.stat().st_mtime


def _file_stable(path: Path, *, wait_sec: float = 1.0) -> bool:
    if not path.is_file():
        return False
    mtime = path.stat().st_mtime
    time.sleep(wait_sec)
    return path.is_file() and path.stat().st_mtime == mtime


def watch_loop(args: argparse.Namespace, logger) -> int:
    """Single-drive, file-driven loop (legacy default)."""
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
            update_state("rip_watcher", {"status": "processing", "yaml": yaml_path.name})
            result = process_ready_manifest(yaml_path, logger)
            update_state(
                "rip_watcher",
                {
                    "status": "done" if result.ok else "failed",
                    "yaml": yaml_path.name,
                    "reason": result.message,
                },
            )
            if args.once:
                return 0
        time.sleep(args.poll_sec)


def _device_rip_loop(device: str, args: argparse.Namespace, logger) -> int:
    """Device-bound worker: scan this drive, claim + rip the matching manifest."""
    lock_name = f"rip-worker-{device_slug(device)}"
    section = f"rip_watcher:{device_slug(device)}"
    try:
        with flock(watcher_lock_path(lock_name), blocking=False):
            logger.info("[%s] Rip worker started (poll=%ss)", device, args.poll_sec)
            update_state(section, {"status": "idle", "device": device})
            last_waiting_msg: str | None = None
            while True:
                if list_ready_yamls() and media_present(device):
                    update_state(section, {"status": "scanning", "device": device})
                    result = run_device_worker_once(device, logger)
                    # Surface "disc present but no matching manifest" once per state
                    # change so tail -F pipeline.log explains why nothing rips.
                    if result.message.startswith("no manifest") or result.message == "no titles":
                        if result.message != last_waiting_msg:
                            log_stage("WAITING_MANIFEST", detail=f"dev={device} {result.message}")
                        last_waiting_msg = result.message
                    else:
                        last_waiting_msg = None
                    update_state(
                        section,
                        {
                            "status": result.message if result.ok else "failed",
                            "device": device,
                            "reason": result.message,
                        },
                    )
                    processed = result.message not in _WORKER_IDLE_MESSAGES and not result.message.startswith(
                        "no manifest"
                    )
                    if args.once and processed:
                        return 0
                time.sleep(args.poll_sec)
    except BlockingIOError:
        logger.error("[%s] Another rip worker is already bound to this drive", device)
        return 1


def _run_workers(devices: list[str], args: argparse.Namespace, logger) -> int:
    logger.info("Rip workers started (devices=%s)", ", ".join(devices))
    if len(devices) == 1:
        return _device_rip_loop(devices[0], args, logger)

    threads = [
        threading.Thread(
            target=_device_rip_loop,
            args=(dev, args, logger),
            name=f"rip-{device_slug(dev)}",
            daemon=True,
        )
        for dev in devices
    ]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        logger.info("Rip workers interrupted; exiting")
    return 0


def main() -> int:
    args = parse_args()
    logger = setup_watcher_logger("rip-watcher", LOG_FILE)
    READY_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)

    if args.devices or args.auto:
        devices = resolve_watch_devices(args.devices, auto=args.auto)
        log_watcher_waiting("rip-watcher", devices)
        return _run_workers(devices, args, logger)

    log_watcher_waiting("rip-watcher")
    try:
        with flock(watcher_lock_path("rip-watcher"), blocking=False):
            while True:
                try:
                    return watch_loop(args, logger)
                except Exception:
                    logger.exception("Rip watcher crashed; restarting in 10s")
                    time.sleep(10.0)
    except BlockingIOError:
        logger.error("Another rip-watcher instance is already running")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


# Re-export for tests that import rip-watcher helpers
def rescan_manifest(manifest, logger):
    from videosripper.pipeline import step_rescan_for_manifest

    result = step_rescan_for_manifest(manifest, logger)
    if result.ok:
        return result.data["scan_data"]
    return None


__all__ = [
    "already_ripped",
    "validate_manifest_file",
    "rescan_manifest",
    "_yaml_seen_key",
]
