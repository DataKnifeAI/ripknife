"""NAS rsync copy helpers."""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from videosripper.monitor import ALERT_PREFIX
from videosripper.paths import MOVIES_NAS, NAS_SHARE
from videosripper.retry import DEFAULT_NAS_RETRIES, DEFAULT_RETRY_DELAY_SEC
from videosripper.watcher_util import media_present

logger = logging.getLogger("videosripper")

DEFAULT_EJECT_ATTEMPTS = 3
DEFAULT_EJECT_VERIFY_TIMEOUT_SEC = 5.0
DEFAULT_EJECT_VERIFY_INITIAL_WAIT_SEC = 1.0
DEFAULT_EJECT_VERIFY_POLL_SEC = 0.5

_EJECT_COMMANDS = (
    ["udisksctl", "eject", "-b"],
    ["udisksctl", "unmount", "-b"],
    ["eject"],
)


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


def _run_eject_commands(device: str) -> bool:
    """Run udisksctl/eject helpers; True when any command exits 0."""
    for prefix in _EJECT_COMMANDS:
        proc = subprocess.run([*prefix, device], capture_output=True, check=False)
        if proc.returncode == 0:
            return True
    return False


def _verify_media_gone(
    device: str,
    *,
    timeout_sec: float = DEFAULT_EJECT_VERIFY_TIMEOUT_SEC,
    initial_wait_sec: float = DEFAULT_EJECT_VERIFY_INITIAL_WAIT_SEC,
    poll_sec: float = DEFAULT_EJECT_VERIFY_POLL_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll until media is absent or timeout (slot loaders can lag after eject OK)."""
    if initial_wait_sec > 0:
        sleep(initial_wait_sec)
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() < deadline:
        if not media_present(device):
            return True
        sleep(poll_sec)
    return not media_present(device)


def eject_disc(
    device: str = "/dev/sr0",
    dry_run: bool = False,
    *,
    attempts: int = DEFAULT_EJECT_ATTEMPTS,
    verify_timeout_sec: float = DEFAULT_EJECT_VERIFY_TIMEOUT_SEC,
    verify_initial_wait_sec: float = DEFAULT_EJECT_VERIFY_INITIAL_WAIT_SEC,
    verify_poll_sec: float = DEFAULT_EJECT_VERIFY_POLL_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Eject ``device`` and verify media is gone.

    Up to ``attempts`` total tries (default 3). Each try runs eject commands then
    polls ``media_present`` briefly — Pioneer BDR-XS07U can report exit 0 while
    the disc is still readable. Does not affect rip success; post-rip only.
    """
    if dry_run:
        logger.info("[dry-run] would eject %s (skipped)", device)
        return False

    total = max(1, attempts)
    logger.info("Ejecting %s...", device)
    for attempt in range(1, total + 1):
        if attempt > 1:
            logger.warning(
                "%s RETRY %d/%d — eject verify failed; retrying %s",
                ALERT_PREFIX,
                attempt - 1,
                total - 1,
                device,
            )
        if not _run_eject_commands(device):
            logger.warning(
                "Eject commands failed (attempt %d/%d): %s",
                attempt,
                total,
                device,
            )
            continue
        if _verify_media_gone(
            device,
            timeout_sec=verify_timeout_sec,
            initial_wait_sec=verify_initial_wait_sec,
            poll_sec=verify_poll_sec,
            sleep=sleep,
        ):
            logger.info("Eject OK: %s", device)
            return True
        logger.warning(
            "Eject reported OK but media still present (attempt %d/%d): %s",
            attempt,
            total,
            device,
        )

    logger.error(
        "%s USER INTERVENTION REQUIRED — eject failed after %d attempts",
        ALERT_PREFIX,
        total,
    )
    logger.error("%s Action: eject manually: eject %s", ALERT_PREFIX, device)
    try:
        from videosripper.logging_setup import PROCESS_RIP, log_stage

        log_stage(
            "EJECT_FAILED",
            process=PROCESS_RIP,
            detail=f"{device} — manual eject needed after {total} attempts",
        )
    except Exception:  # noqa: BLE001 — post-rip alert must not raise
        pass
    return False
