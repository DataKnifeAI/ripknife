"""NAS rsync copy helpers."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from videosripper.monitor import ALERT_PREFIX
from videosripper.paths import MOVIES_NAS, NAS_SHARE
from videosripper.retry import DEFAULT_NAS_RETRIES, DEFAULT_RETRY_DELAY_SEC

logger = logging.getLogger("videosripper")


def _rsync_once(src: Path, dest: Path) -> bool:
    proc = subprocess.run(
        ["rsync", "-ah", "--info=progress2", str(src), str(dest)],
        check=False,
    )
    return proc.returncode == 0


def _copy_with_retries(
    src: Path,
    dest_dir: Path,
    *,
    label: str,
    dry_run: bool,
    retries: int,
    delay_sec: float,
) -> bool:
    dest = dest_dir / src.name
    logger.info("  -> copy to share: %s", dest)
    if dry_run:
        return True
    dest_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(0, retries) + 1
    for attempt in range(1, attempts + 1):
        if _rsync_once(src, dest):
            return True
        if attempt < attempts:
            logger.warning(
                "%s RETRY %d/%d — NAS copy failed; waiting %ss: %s",
                ALERT_PREFIX,
                attempt,
                retries,
                int(delay_sec) if delay_sec == int(delay_sec) else delay_sec,
                dest,
            )
            if delay_sec > 0:
                time.sleep(delay_sec)
    logger.error(
        "%s NAS COPY FAILED — kept local rip; check permissions for %s",
        ALERT_PREFIX,
        dest_dir,
    )
    logger.error("%s Action: fix NAS mount/permissions; re-copy from %s", ALERT_PREFIX, src)
    _ = label
    return False


def copy_episode_to_share(
    src: Path,
    season_dir: str,
    nas_share: Path | None = None,
    dry_run: bool = False,
    *,
    retries: int = DEFAULT_NAS_RETRIES,
    delay_sec: float = DEFAULT_RETRY_DELAY_SEC,
) -> bool:
    share = nas_share or NAS_SHARE
    dest_dir = share / season_dir
    return _copy_with_retries(
        src,
        dest_dir,
        label="episode",
        dry_run=dry_run,
        retries=retries,
        delay_sec=delay_sec,
    )


def copy_movie_to_share(
    src: Path,
    subdir: str = "",
    movies_nas: Path | None = None,
    dry_run: bool = False,
    *,
    retries: int = DEFAULT_NAS_RETRIES,
    delay_sec: float = DEFAULT_RETRY_DELAY_SEC,
) -> bool:
    base = movies_nas or MOVIES_NAS
    dest_dir = base / subdir if subdir else base
    return _copy_with_retries(
        src,
        dest_dir,
        label="movie",
        dry_run=dry_run,
        retries=retries,
        delay_sec=delay_sec,
    )


def eject_disc(device: str = "/dev/sr0", dry_run: bool = False) -> bool:
    if dry_run:
        logger.info("[dry-run] would eject %s (skipped)", device)
        return False
    logger.info("Ejecting %s...", device)
    for cmd in (
        ["udisksctl", "eject", "-b", device],
        ["udisksctl", "unmount", "-b", device],
        ["eject", device],
    ):
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode == 0:
            logger.info("Eject OK: %s", device)
            return True
    logger.warning("  (eject failed — run manually: eject %s)", device)
    return False
