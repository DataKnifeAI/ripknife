#!/usr/bin/env python3
"""Process 1: poll optical drive(s), scan new discs, route to registry or LLM mapping.

Single drive (default): watches ``dev:/dev/sr0``.
Multiple drives: pass ``--device`` repeatedly, or ``--auto`` to discover every
``/dev/sr*`` present. Each drive runs in its own thread with its own scan lock
and state section, so discs in different drives are handled concurrently.
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
from videosripper.pipeline import handle_disc_insert  # noqa: E402
from videosripper.watcher_util import (  # noqa: E402
    flock,
    media_present,
    scan_lock_path,
    setup_watcher_logger,
    update_state,
    watcher_lock_path,
)

LOG_FILE = LOG_ROOT / "disc-watcher.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VideosRipper disc insertion watcher")
    parser.add_argument(
        "--device",
        action="append",
        dest="devices",
        default=None,
        help="MakeMKV device string (repeatable). Default: dev:/dev/sr0",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Discover and watch every /dev/sr* optical drive present",
    )
    parser.add_argument("--poll-sec", type=float, default=30.0, help="Drive poll interval")
    parser.add_argument("--debounce-sec", type=float, default=45.0, help="Stability wait after insert")
    parser.add_argument("--minlength", type=int, default=300, help="MakeMKV minlength for scan")
    parser.add_argument("--once", action="store_true", help="Process one disc then exit")
    return parser.parse_args()


def _state_section(device: str) -> str:
    return f"disc_watcher:{device_slug(device)}"


def handle_disc(device: str, args: argparse.Namespace, logger) -> None:
    section = _state_section(device)
    try:
        with flock(scan_lock_path(device), blocking=False):
            result = handle_disc_insert(device, args.minlength, logger)
            status = result.message if result.ok else "scan_failed"
            patch = {"status": status, "device": device}
            for key in ("disc_id", "disc_name", "scan_json", "ready_yaml"):
                if key in result.data:
                    patch[key] = result.data[key]
            update_state(section, patch)
            if not result.ok:
                logger.error("[%s] Disc insert pipeline failed: %s", device, result.message)
    except BlockingIOError:
        logger.warning("[%s] Scan lock busy; skipping this cycle", device)
        update_state(section, {"status": "scan_busy", "device": device})


def _poll_loop(device: str, args: argparse.Namespace, logger) -> int:
    section = _state_section(device)
    was_present = media_present(device)
    debounce_started: float | None = None
    if was_present:
        debounce_started = time.monotonic()
        logger.info("[%s] Disc already present at startup; debouncing %.0fs", device, args.debounce_sec)
        log_stage("DISC_DETECTED", detail=f"device={device} (present at startup); debouncing {args.debounce_sec:.0f}s")
        update_state(section, {"status": "debouncing", "disc_present": True, "device": device})

    while True:
        present = media_present(device)
        now = time.monotonic()

        if present and not was_present:
            debounce_started = now
            logger.info("[%s] Disc inserted; debouncing %.0fs", device, args.debounce_sec)
            log_stage("DISC_DETECTED", detail=f"device={device}; debouncing {args.debounce_sec:.0f}s")
            update_state(section, {"status": "debouncing", "disc_present": True, "device": device})

        if present and debounce_started is not None and now - debounce_started >= args.debounce_sec:
            handle_disc(device, args, logger)
            debounce_started = None
            if args.once:
                return 0

        if not present:
            if was_present:
                log_stage("IDLE", detail=f"device={device} — no disc; waiting for insert")
            debounce_started = None
            update_state(section, {"status": "idle", "disc_present": False, "device": device})

        was_present = present
        time.sleep(args.poll_sec)


def _device_worker(device: str, args: argparse.Namespace, logger) -> None:
    lock_name = f"disc-watcher-{device_slug(device)}"
    try:
        with flock(watcher_lock_path(lock_name), blocking=False):
            while True:
                try:
                    if _poll_loop(device, args, logger) == 0 and args.once:
                        return
                except Exception:
                    logger.exception("[%s] Disc watcher crashed; restarting in 10s", device)
                    time.sleep(10.0)
    except BlockingIOError:
        logger.error("[%s] Another disc-watcher is already watching this drive", device)


def main() -> int:
    args = parse_args()
    logger = setup_watcher_logger("disc-watcher", LOG_FILE)
    devices = resolve_watch_devices(args.devices, auto=args.auto)
    logger.info(
        "Disc watcher started (devices=%s poll=%ss debounce=%ss)",
        ", ".join(devices),
        args.poll_sec,
        args.debounce_sec,
    )
    log_watcher_waiting("disc-watcher", devices)

    if len(devices) == 1:
        _device_worker(devices[0], args, logger)
        return 0

    threads = [
        threading.Thread(target=_device_worker, args=(dev, args, logger), name=f"watch-{device_slug(dev)}", daemon=True)
        for dev in devices
    ]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        logger.info("Disc watcher interrupted; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
