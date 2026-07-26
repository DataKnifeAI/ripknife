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
    install_watcher_signal_handlers,
    media_present,
    run_resilient_loop,
    scan_lock_path,
    setup_watcher_logger,
    shutdown_requested,
    touch_heartbeat,
    update_state,
    watcher_lock_path,
)

LOG_FILE = LOG_ROOT / "disc-watcher.log"
WATCHER_NAME = "disc-watcher"


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

    while not shutdown_requested():
        present = media_present(device)
        now = time.monotonic()
        touch_heartbeat(WATCHER_NAME, extra={"device": device, "disc_present": present})

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
        # Sleep in slices so SIGTERM is noticed promptly between polls.
        deadline = time.monotonic() + args.poll_sec
        while time.monotonic() < deadline and not shutdown_requested():
            time.sleep(min(1.0, deadline - time.monotonic()))
    raise SystemExit(0)


def _device_worker(device: str, args: argparse.Namespace, logger) -> None:
    lock_name = f"disc-watcher-{device_slug(device)}"
    worker_name = f"{WATCHER_NAME}:{device_slug(device)}"
    try:
        with flock(watcher_lock_path(lock_name), blocking=False):
            # Catch-all around the poll loop so a device thread never dies silently.
            run_resilient_loop(
                worker_name,
                logger,
                lambda: _poll_loop(device, args, logger),
                heartbeat_name=WATCHER_NAME,
            )
    except BlockingIOError:
        logger.error("[%s] Another disc-watcher is already watching this drive", device)
    except SystemExit:
        raise


def _supervise_threads(threads: list[threading.Thread], logger) -> None:
    """Join workers; if a non-daemon exit leaves us idle, log and keep waiting.

    Device workers are intended never to exit (except --once / lock conflict).
    Daemon threads dying on an uncaught error would otherwise leave main blocked
    forever with no work happening — resilient wrappers should prevent that, but
    we still wake periodically to refresh the process heartbeat.
    """
    try:
        while not shutdown_requested():
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                logger.error("All disc-watcher device threads exited; process staying up for restart tooling")
                touch_heartbeat(WATCHER_NAME, extra={"status": "threads_dead"})
                time.sleep(30.0)
                continue
            touch_heartbeat(WATCHER_NAME, extra={"threads_alive": len(alive)})
            for t in alive:
                t.join(timeout=30.0)
    except KeyboardInterrupt:
        logger.info("Disc watcher interrupted; exiting")
        raise SystemExit(0)
    raise SystemExit(0)


def main() -> int:
    args = parse_args()
    logger = setup_watcher_logger(WATCHER_NAME, LOG_FILE)
    install_watcher_signal_handlers(WATCHER_NAME, logger)
    devices = resolve_watch_devices(args.devices, auto=args.auto)
    logger.info(
        "Disc watcher started (devices=%s poll=%ss debounce=%ss)",
        ", ".join(devices),
        args.poll_sec,
        args.debounce_sec,
    )
    log_watcher_waiting(WATCHER_NAME, devices)
    touch_heartbeat(WATCHER_NAME, extra={"devices": devices})

    if len(devices) == 1:
        _device_worker(devices[0], args, logger)
        return 0

    threads = [
        threading.Thread(
            target=_device_worker,
            args=(dev, args, logger),
            name=f"watch-{device_slug(dev)}",
            daemon=True,
        )
        for dev in devices
    ]
    for t in threads:
        t.start()
    _supervise_threads(threads, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
