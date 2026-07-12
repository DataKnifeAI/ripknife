"""Shared helpers for disc/rip watcher daemons."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from videosripper.paths import LOG_ROOT, ROOT, disc_dev

DEFAULT_DEVICE = os.environ.get("VIDEOSRIPPER_DEVICE", "dev:/dev/sr0")
VIDEOSRIPPER_BIN = ROOT / "scripts" / "videosripper"
STATE_PATH = LOG_ROOT / "watcher-state.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify_disc_id(disc_name: str) -> str:
    slug = disc_name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "unknown-disc"


def setup_watcher_logger(name: str, log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(stream)
    return logger


def read_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def update_state(section: str, patch: dict) -> dict:
    state = read_state()
    current = state.get(section, {})
    current.update(patch)
    current["updated_at"] = utc_now()
    state[section] = current
    write_state_atomic(state)
    return state


def write_state_atomic(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


@contextmanager
def flock(lock_path: Path, *, blocking: bool = True) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        fallback = _fallback_lock_path(lock_path)
        if fallback is None:
            raise
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(fallback, os.O_CREAT | os.O_RDWR, 0o644)
        lock_path = fallback
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def media_present(device: str = DEFAULT_DEVICE) -> bool:
    dev_path = Path(disc_dev(device))
    if not dev_path.exists():
        return False
    try:
        with open(dev_path, "rb") as handle:
            handle.read(2048)
        return True
    except OSError:
        return False


_resolved_lock_paths: dict[str, Path] = {}


def _try_lock_path(lock_path: Path) -> bool:
    """Return True if this process can create/open the lock file."""
    lock_dir = lock_path.parent
    if not lock_dir.is_dir() or not os.access(lock_dir, os.W_OK):
        return False
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        os.close(fd)
        return True
    except OSError:
        return False


def _fallback_lock_path(lock_path: Path) -> Path | None:
    stem = lock_path.stem
    if not stem.startswith("videosripper-"):
        return None
    return LOG_ROOT / f"{stem.removeprefix('videosripper-')}.lock"


def _lock_path(name: str) -> Path:
    if name in _resolved_lock_paths:
        return _resolved_lock_paths[name]

    system_lock = Path(f"/var/lock/videosripper-{name}.lock")
    fallback = LOG_ROOT / f"{name}.lock"
    resolved = system_lock if _try_lock_path(system_lock) else fallback
    _resolved_lock_paths[name] = resolved
    return resolved


def makemkv_lock_path() -> Path:
    return _lock_path("makemkv")


def scan_lock_path() -> Path:
    """Same lock as makemkv — all MakeMKV access must be mutually exclusive."""
    return makemkv_lock_path()


def watcher_lock_path(name: str) -> Path:
    """Per-watcher singleton lock (rip-watcher, disc-watcher)."""
    return LOG_ROOT / f"{name}.pid.lock"


def run_videosripper(args: list[str], *, logger: logging.Logger) -> tuple[int, str]:
    cmd = [str(VIDEOSRIPPER_BIN), *args]
    logger.info("Running: %s", " ".join(cmd))
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        logger.error("Command failed (%s): %s", proc.returncode, output.strip())
    return proc.returncode, output


def wait_for_stable_media(
    *,
    device: str,
    debounce_sec: float,
    poll_sec: float,
    logger: logging.Logger,
) -> bool:
    """Wait until media stays present for debounce_sec."""
    stable_since: float | None = None
    while True:
        present = media_present(device)
        now = time.monotonic()
        if present:
            if stable_since is None:
                stable_since = now
                logger.info("Disc detected; debouncing %.0fs", debounce_sec)
            elif now - stable_since >= debounce_sec:
                return True
        else:
            stable_since = None
        time.sleep(poll_sec)
