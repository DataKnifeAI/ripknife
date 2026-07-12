#!/usr/bin/env python3
"""Process 1: poll optical drive, scan new discs, route to registry or LLM mapping."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/mnt/game1/VideosRipper")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from videosripper.paths import LOG_ROOT, ROOT as PKG_ROOT  # noqa: E402, I001
from videosripper.registry import load_registry  # noqa: E402
from videosripper.watcher_util import (  # noqa: E402
    DEFAULT_DEVICE,
    flock,
    media_present,
    scan_lock_path,
    setup_watcher_logger,
    slugify_disc_id,
    update_state,
    watcher_lock_path,
)

PENDING_DIR = PKG_ROOT / "staging" / "pending"
READY_DIR = PKG_ROOT / "staging" / "ready"
TRIGGER_MAP = PKG_ROOT / "scripts" / "trigger-map.sh"
LOG_FILE = LOG_ROOT / "disc-watcher.log"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VideosRipper disc insertion watcher")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="MakeMKV device string")
    parser.add_argument("--poll-sec", type=float, default=30.0, help="Drive poll interval")
    parser.add_argument("--debounce-sec", type=float, default=45.0, help="Stability wait after insert")
    parser.add_argument("--minlength", type=int, default=300, help="MakeMKV minlength for scan")
    parser.add_argument("--once", action="store_true", help="Process one disc then exit")
    return parser.parse_args()


def promote_registry_hit(entry, logger) -> Path:
    READY_DIR.mkdir(parents=True, exist_ok=True)
    dest = READY_DIR / f"{entry.id}.yaml"
    shutil.copy2(entry.manifest, dest)
    logger.info("Registry hit: copied %s -> %s", entry.manifest, dest)
    return dest


def trigger_mapping(disc_id: str, scan_json: Path, logger) -> None:
    if not TRIGGER_MAP.is_file():
        logger.error("trigger-map.sh missing at %s", TRIGGER_MAP)
        return
    cmd = [str(TRIGGER_MAP), disc_id, str(scan_json)]
    logger.info("Unknown disc; launching background map: %s", " ".join(cmd))
    subprocess.Popen(
        cmd,
        cwd=PKG_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def handle_disc(args: argparse.Namespace, logger) -> None:
    try:
        with flock(scan_lock_path(), blocking=False):
            code, output = _scan_device_json(args.device, args.minlength)
            if code != 0:
                logger.error("Pre-scan failed: %s", output.strip())
                update_state("disc_watcher", {"status": "scan_failed"})
                return
            scan_data = json.loads(output)
            disc_name = scan_data.get("disc_name", "") or "unknown-disc"
            disc_id = slugify_disc_id(disc_name)

            ready_yaml = READY_DIR / f"{disc_id}.yaml"
            if ready_yaml.is_file():
                logger.info(
                    "Ready manifest already exists for %s; skipping map",
                    disc_id,
                )
                update_state(
                    "disc_watcher",
                    {
                        "status": "ready_exists",
                        "disc_id": disc_id,
                        "disc_name": disc_name,
                        "ready_yaml": str(ready_yaml),
                    },
                )
                return

            scan_json = PENDING_DIR / f"{disc_id}.scan.json"
            meta_json = PENDING_DIR / f"{disc_id}.meta.json"
            PENDING_DIR.mkdir(parents=True, exist_ok=True)
            scan_json.write_text(output, encoding="utf-8")
            meta = {
                "id": disc_id,
                "disc_name": disc_name,
                "device": args.device,
                "minlength": args.minlength,
                "title_count": len(scan_data.get("titles", [])),
                "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            obf = scan_data.get("obfuscation")
            if obf:
                meta["obfuscation"] = obf
                if obf.get("recommended_title") is not None:
                    meta["recommended_title"] = obf["recommended_title"]
                decoys = obf.get("decoy_indices")
                if decoys:
                    meta["decoy_indices"] = decoys
            meta_json.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            logger.info(
                "Scan complete id=%s disc=%s titles=%s",
                disc_id,
                disc_name,
                meta["title_count"],
            )
            from videosripper.post_rip_verify import warn_title_count_explosion  # noqa: I001

            warn_title_count_explosion(meta["title_count"])

            registry = load_registry()
            entry = registry.lookup_disc(disc_name)
            if entry is not None:
                promote_registry_hit(entry, logger)
                update_state(
                    "disc_watcher",
                    {
                        "status": "registry_hit",
                        "disc_id": entry.id,
                        "disc_name": disc_name,
                        "ready_yaml": str(READY_DIR / f"{entry.id}.yaml"),
                    },
                )
            else:
                trigger_mapping(disc_id, scan_json, logger)
                update_state(
                    "disc_watcher",
                    {
                        "status": "mapping_triggered",
                        "disc_id": disc_id,
                        "disc_name": disc_name,
                        "scan_json": str(scan_json),
                    },
                )
    except BlockingIOError:
        logger.warning("Scan lock busy; skipping this cycle")
        update_state("disc_watcher", {"status": "scan_busy"})


def _scan_device_json(device: str, minlength: int) -> tuple[int, str]:
    cmd = [
        str(PKG_ROOT / "scripts" / "videosripper"),
        "scan-device",
        "--json",
        "--device",
        device,
        "--minlength",
        str(minlength),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=PKG_ROOT, check=False)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stdout + proc.stderr


def _poll_loop(args: argparse.Namespace, logger) -> int:
    was_present = media_present(args.device)
    debounce_started: float | None = None
    if was_present:
        debounce_started = time.monotonic()
        logger.info(
            "Disc already present at startup; debouncing %.0fs",
            args.debounce_sec,
        )
        update_state("disc_watcher", {"status": "debouncing", "disc_present": True})

    while True:
        present = media_present(args.device)
        now = time.monotonic()

        if present and not was_present:
            debounce_started = now
            logger.info("Disc inserted; debouncing %.0fs", args.debounce_sec)
            update_state("disc_watcher", {"status": "debouncing", "disc_present": True})

        if present and debounce_started is not None and now - debounce_started >= args.debounce_sec:
            handle_disc(args, logger)
            debounce_started = None
            if args.once:
                return 0

        if not present:
            debounce_started = None
            update_state("disc_watcher", {"status": "idle", "disc_present": False})

        was_present = present
        time.sleep(args.poll_sec)


def main() -> int:
    args = parse_args()
    logger = setup_watcher_logger("disc-watcher", LOG_FILE)
    logger.info(
        "Disc watcher started (device=%s poll=%ss debounce=%ss)",
        args.device,
        args.poll_sec,
        args.debounce_sec,
    )

    try:
        with flock(watcher_lock_path("disc-watcher"), blocking=False):
            while True:
                try:
                    return _poll_loop(args, logger)
                except Exception:
                    logger.exception("Disc watcher crashed; restarting in 10s")
                    time.sleep(10.0)
    except BlockingIOError:
        logger.error("Another disc-watcher instance is already running")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
