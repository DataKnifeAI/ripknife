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
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ripknife.paths import configure_process_tmp  # noqa: E402

configure_process_tmp()

from ripknife.devices import device_slug, resolve_watch_devices  # noqa: E402
from ripknife.logging_setup import log_stage, log_watcher_waiting  # noqa: E402
from ripknife.paths import LOG_ROOT  # noqa: E402
from ripknife.monitor import ensure_drive_available
from ripknife.pipeline import (  # noqa: E402
    DONE_DIR,
    FAILED_DIR,
    READY_DIR,
    already_ripped,
    persist_interrupted_claimed_rips,
    process_ready_manifest,
    recover_interrupted_rips,
    run_device_worker_once,
    validate_manifest_file,
)
from ripknife.watcher_util import (  # noqa: E402
    flock,
    install_watcher_signal_handlers,
    media_present,
    run_resilient_loop,
    setup_watcher_logger,
    shutdown_requested,
    touch_heartbeat,
    update_state,
    watcher_lock_path,
)

LOG_FILE = LOG_ROOT / "rip-watcher.log"
WATCHER_NAME = "rip-watcher"

_WORKER_IDLE_MESSAGES = {"idle", "no titles", "claim lost"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ripknife rip queue watcher")
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

    while not shutdown_requested():
        touch_heartbeat(WATCHER_NAME)
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
        deadline = time.monotonic() + args.poll_sec
        while time.monotonic() < deadline and not shutdown_requested():
            time.sleep(min(1.0, deadline - time.monotonic()))
    raise SystemExit(0)


def _device_rip_body(device: str, args: argparse.Namespace, logger) -> int:
    """Inner device-bound loop body (exceptions bubble to resilient wrapper)."""
    section = f"rip_watcher:{device_slug(device)}"
    logger.info("[%s] Rip worker started (poll=%ss)", device, args.poll_sec)
    update_state(section, {"status": "idle", "device": device})
    # After per-device flock: reclaim orphaned claims + resume if disc still loaded.
    recover_interrupted_rips([device], logger)
    last_waiting_msg: str | None = None
    while not shutdown_requested():
        touch_heartbeat(WATCHER_NAME, extra={"device": device})
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
        deadline = time.monotonic() + args.poll_sec
        while time.monotonic() < deadline and not shutdown_requested():
            time.sleep(min(1.0, deadline - time.monotonic()))
    raise SystemExit(0)


def _device_rip_loop(device: str, args: argparse.Namespace, logger) -> int:
    """Device-bound worker: scan this drive, claim + rip the matching manifest."""
    lock_name = f"rip-worker-{device_slug(device)}"
    worker_name = f"{WATCHER_NAME}:{device_slug(device)}"
    try:
        with flock(watcher_lock_path(lock_name), blocking=False):
            # Catch-all: device threads must not die on an unhandled exception
            # while main joins forever (watcher looks alive but does nothing).
            run_resilient_loop(
                worker_name,
                logger,
                lambda: _device_rip_body(device, args, logger),
                heartbeat_name=WATCHER_NAME,
            )
            return 0
    except BlockingIOError:
        logger.error("[%s] Another rip worker is already bound to this drive", device)
        return 1
    except SystemExit:
        raise


def _supervise_threads(threads: list[threading.Thread], logger) -> None:
    try:
        while not shutdown_requested():
            alive = [t for t in threads if t.is_alive()]
            if not alive:
                logger.error("All rip-watcher device threads exited; process staying up for restart tooling")
                touch_heartbeat(WATCHER_NAME, extra={"status": "threads_dead"})
                time.sleep(30.0)
                continue
            touch_heartbeat(WATCHER_NAME, extra={"threads_alive": len(alive)})
            for t in alive:
                t.join(timeout=30.0)
    except KeyboardInterrupt:
        logger.info("Rip workers interrupted; exiting")
        raise SystemExit(0)
    raise SystemExit(0)


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
    _supervise_threads(threads, logger)
    return 0


def _persist_stop_resume() -> None:
    """SIGTERM/SIGINT: write resume sidecars for any in-flight claimed rips."""
    persist_interrupted_claimed_rips(reason="watcher_stop")


def main() -> int:
    args = parse_args()
    logger = setup_watcher_logger(WATCHER_NAME, LOG_FILE)
    install_watcher_signal_handlers(
        WATCHER_NAME, logger, on_shutdown=_persist_stop_resume
    )
    READY_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    touch_heartbeat(WATCHER_NAME)

    if args.devices or args.auto:
        devices = resolve_watch_devices(args.devices, auto=args.auto)
        for dev in devices:
            ensure_drive_available(dev)
        log_watcher_waiting(WATCHER_NAME, devices)
        return _run_workers(devices, args, logger)

    default_dev = os.environ.get("RIPKNIFE_DEVICE", "dev:/dev/sr0")
    ensure_drive_available(default_dev)
    log_watcher_waiting(WATCHER_NAME)
    try:
        with flock(watcher_lock_path("rip-watcher"), blocking=False):
            recover_interrupted_rips([default_dev], logger)

            def _file_driven() -> int:
                return watch_loop(args, logger)

            run_resilient_loop(
                WATCHER_NAME,
                logger,
                _file_driven,
                heartbeat_name=WATCHER_NAME,
            )
            return 0
    except BlockingIOError:
        logger.error("Another rip-watcher instance is already running")
        return 1
    except SystemExit:
        raise


if __name__ == "__main__":
    raise SystemExit(main())


# Re-export for tests that import rip-watcher helpers
def rescan_manifest(manifest, logger):
    from ripknife.pipeline import step_rescan_for_manifest

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
