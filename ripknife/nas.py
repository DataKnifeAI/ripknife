"""NAS rsync copy helpers."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ripknife.monitor import ALERT_PREFIX
from ripknife.paths import ANIME_NAS, MOVIES_NAS, NAS_SHARE, SHOWS_NAS
from ripknife.retry import DEFAULT_NAS_RETRIES, DEFAULT_RETRY_DELAY_SEC
from ripknife.watcher_util import media_present

LibraryBucket = Literal["anime", "shows", "movies"]

logger = logging.getLogger("ripknife")

DEFAULT_EJECT_ATTEMPTS = 3
DEFAULT_EJECT_VERIFY_TIMEOUT_SEC = 5.0
DEFAULT_EJECT_VERIFY_INITIAL_WAIT_SEC = 1.0
DEFAULT_EJECT_VERIFY_POLL_SEC = 0.5

_EJECT_COMMANDS = (
    ["udisksctl", "eject", "-b"],
    ["udisksctl", "unmount", "-b"],
    ["eject"],
)


def classify_library(*, kind: str, animated: bool) -> LibraryBucket:
    """NAS library bucket for a mapped title.

    Any cartoon or animation (Western cartoon, Japanese anime, CG series,
    animated feature) belongs on the Anime share. Live-action TV uses Shows;
    live-action films use Movies.
    """
    if kind not in ("tv_disc", "movie"):
        raise ValueError(f"unknown kind: {kind!r}")
    if animated:
        return "anime"
    return "shows" if kind == "tv_disc" else "movies"


def anime_movies_destination(anime_root: Path | None = None) -> Path:
    """Animated features: ``Anime/Movies`` (Plex Movies-type library path)."""
    root = anime_root if anime_root is not None else ANIME_NAS
    return root / "Movies"


def nas_share_for(
    *,
    kind: str,
    animated: bool,
    show: str | None = None,
    anime_root: Path | None = None,
    shows_root: Path | None = None,
    movies_root: Path | None = None,
) -> Path:
    """Absolute NAS destination for a mapped title (future rips; does not move files)."""
    anime = anime_root if anime_root is not None else ANIME_NAS
    shows = shows_root if shows_root is not None else SHOWS_NAS
    movies = movies_root if movies_root is not None else MOVIES_NAS
    bucket = classify_library(kind=kind, animated=animated)
    if bucket == "anime":
        if kind == "tv_disc":
            name = (show or "").strip()
            if not name:
                raise ValueError("animated tv_disc requires show name")
            return anime / name
        return anime_movies_destination(anime)
    if bucket == "shows":
        name = (show or "").strip()
        if not name:
            raise ValueError("live-action tv_disc requires show name")
        return shows / name
    return movies


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


def resolve_delete_local_after_nas(manifest_value: bool) -> bool:
    """Manifest flag unless ``RIPKNIFE_DELETE_LOCAL_AFTER_NAS`` overrides."""
    env = os.environ.get("RIPKNIFE_DELETE_LOCAL_AFTER_NAS", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    return manifest_value


def verify_nas_copy(src: Path, dest: Path) -> bool:
    """True when ``dest`` exists and byte size matches ``src``."""
    if not src.is_file() or not dest.is_file():
        return False
    try:
        return src.stat().st_size == dest.stat().st_size and src.stat().st_size > 0
    except OSError:
        return False


def delete_local_after_nas_copy(
    src: Path,
    dest: Path,
    *,
    dry_run: bool = False,
    enabled: bool = False,
) -> bool:
    """Remove local ``src`` after verified NAS copy. Returns True if deleted (or dry-run)."""
    if not enabled:
        return False
    if not verify_nas_copy(src, dest):
        logger.warning(
            "%s LOCAL_CLEANUP skipped — NAS verify failed (missing or size mismatch): %s",
            ALERT_PREFIX,
            dest,
        )
        return False
    if dry_run:
        logger.info("[dry-run] would delete local after NAS copy: %s", src)
        return True
    try:
        src.unlink()
        logger.info("LOCAL_CLEANUP: deleted %s (NAS verified at %s)", src, dest)
        return True
    except OSError as exc:
        logger.error(
            "%s LOCAL_CLEANUP failed — could not delete %s: %s",
            ALERT_PREFIX,
            src,
            exc,
        )
        return False


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
    polls ``media_present`` briefly — a slot drive can report exit 0 while
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
        from ripknife.logging_setup import PROCESS_RIP, log_stage

        log_stage(
            "EJECT_FAILED",
            process=PROCESS_RIP,
            detail=f"{device} — manual eject needed after {total} attempts",
        )
    except Exception:  # noqa: BLE001 — post-rip alert must not raise
        pass
    return False
