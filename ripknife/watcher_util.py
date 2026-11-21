"""Shared helpers for disc/rip watcher daemons."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import signal
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, TypeVar

from ripknife.paths import LOG_ROOT, ROOT, disc_dev

DEFAULT_DEVICE = os.environ.get("RIPKNIFE_DEVICE", "dev:/dev/sr0")
RIPKNIFE_BIN = ROOT / "scripts" / "ripknife"
STATE_PATH = LOG_ROOT / "watcher-state.json"
ERROR_LOG = LOG_ROOT / "error.log"
HEARTBEAT_SECTION = "heartbeat"

_T = TypeVar("_T")

# Set by SIGTERM/SIGINT so poll loops / backoff can exit without restarting.
_shutdown = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def shutdown_requested() -> bool:
    """True after SIGTERM/SIGINT (or an explicit request_shutdown)."""
    return _shutdown.is_set()


def request_shutdown() -> None:
    """Mark watchers for clean exit (also used by tests)."""
    _shutdown.set()


def clear_shutdown() -> None:
    """Reset shutdown flag (tests only)."""
    _shutdown.clear()


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
        # ERROR+ (including logger.exception) also land in logs/error.log
        from ripknife.logging_setup import _attach_error_handler

        _attach_error_handler(logger)
    return logger


def install_watcher_signal_handlers(
    watcher: str,
    logger: logging.Logger,
    *,
    on_shutdown: Callable[[], None] | None = None,
) -> None:
    """Log ``WATCHER_STOPPED`` on SIGTERM/SIGINT so session kills are explainable.

    Without this, ``pkill`` or a parent process-group teardown kills watchers
    with no log line — the silent death mode that made 20:12–20:14 hard to debug.

    ``on_shutdown`` runs before ``SystemExit`` (e.g. persist resume sidecars for
    in-flight claimed rips). Must not raise.
    """

    def _handler(signum: int, _frame) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)
        detail = f"{watcher} received {sig_name}"
        logger.info("Watcher stopping: %s", detail)
        try:
            from ripknife.logging_setup import log_stage

            log_stage("WATCHER_STOPPED", detail=detail)
        except Exception:  # noqa: BLE001 — shutdown path must not raise
            pass
        if on_shutdown is not None:
            try:
                on_shutdown()
            except Exception:  # noqa: BLE001 — shutdown path must not raise
                logger.exception("%s on_shutdown failed", watcher)
        request_shutdown()
        # Raise SystemExit so finally/flock cleanup can run on the main thread.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def log_watcher_crash(watcher: str, logger: logging.Logger, exc: BaseException) -> None:
    """Log full traceback to watcher log + error.log and a STAGE WATCHER_CRASH line."""
    logger.exception("%s crashed: %s", watcher, exc)
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    try:
        error_log = ERROR_LOG
        error_log.parent.mkdir(parents=True, exist_ok=True)
        with error_log.open("a", encoding="utf-8") as fh:
            fh.write(f"=== WATCHER_CRASH — {watcher} ===\n")
            fh.write(tb + "\n")
    except OSError:
        logger.error("Failed writing watcher crash to error.log")
    try:
        from ripknife.logging_setup import log_stage

        # Keep the pipeline line one-shot readable; traceback is in the watcher/error logs.
        brief = f"{type(exc).__name__}: {exc}".replace("\n", " ")
        log_stage("WATCHER_CRASH", detail=f"{watcher} — {brief}")
    except Exception:  # noqa: BLE001
        logger.error("Failed to write WATCHER_CRASH stage line:\n%s", traceback.format_exc())


def touch_heartbeat(watcher: str, *, extra: dict | None = None) -> None:
    """Update ``logs/watcher-state.json`` heartbeat so a dead watcher is detectable."""
    patch: dict = {
        "watcher": watcher,
        "pid": os.getpid(),
        "status": "alive",
        "heartbeat_at": utc_now(),
    }
    if extra:
        patch.update(extra)
    update_state(f"{HEARTBEAT_SECTION}:{watcher}", patch)


def run_resilient_loop(
    name: str,
    logger: logging.Logger,
    fn: Callable[[], _T],
    *,
    restart_sec: float = 10.0,
    heartbeat_name: str | None = None,
) -> _T | None:
    """Run ``fn``, logging crashes and restarting after ``restart_sec``.

    Returns when ``fn`` returns normally (e.g. ``--once``). ``SystemExit`` /
    ``KeyboardInterrupt`` propagate so signal handlers can stop cleanly.
    Does not restart after a shutdown signal.
    """
    while not shutdown_requested():
        try:
            if heartbeat_name:
                touch_heartbeat(heartbeat_name)
            return fn()
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:
            if shutdown_requested():
                raise SystemExit(0) from exc
            log_watcher_crash(name, logger, exc)
            if heartbeat_name:
                touch_heartbeat(
                    heartbeat_name,
                    extra={"status": "restarting", "last_error": type(exc).__name__},
                )
            deadline = time.monotonic() + restart_sec
            while time.monotonic() < deadline and not shutdown_requested():
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    raise SystemExit(0)


def read_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def update_state(section: str, patch: dict) -> dict:
    # Serialize read-modify-write across disc/rip processes (shared state file).
    with flock(STATE_PATH.with_suffix(".json.lock")):
        state = read_state()
        current = state.get(section, {})
        current.update(patch)
        current["updated_at"] = utc_now()
        state[section] = current
        write_state_atomic(state)
        return state


def replace_state_section(section: str, value: dict) -> dict:
    """Overwrite a watcher-state section (drops stale keys such as ready_yaml)."""
    with flock(STATE_PATH.with_suffix(".json.lock")):
        state = read_state()
        current = dict(value)
        current["updated_at"] = utc_now()
        state[section] = current
        write_state_atomic(state)
        return state


def clear_state_section(section: str) -> dict:
    """Remove a watcher-state section entirely."""
    with flock(STATE_PATH.with_suffix(".json.lock")):
        state = read_state()
        state.pop(section, None)
        write_state_atomic(state)
        return state


def write_state_atomic(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per writer — shared ``.json.tmp`` races (one replace steals the
    # file) caused FileNotFoundError / WATCHER_CRASH when both watchers started.
    tmp = STATE_PATH.with_name(f"{STATE_PATH.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


# Lock paths currently held by this thread. flock() treats each open fd as a
# distinct owner, so re-acquiring the same lock file in a nested `with flock(...)`
# (e.g. disc-watcher holds the scan lock, then step_scan_disc grabs the same
# makemkv lock) would deadlock the process against itself without this.
_held_locks = threading.local()


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
    held: set[str] = getattr(_held_locks, "paths", None) or set()
    _held_locks.paths = held
    key = str(lock_path)
    if key in held:  # reentrant: this thread already holds the lock
        os.close(fd)
        yield
        return
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
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
    if not stem.startswith("ripknife-"):
        return None
    return LOG_ROOT / f"{stem.removeprefix('ripknife-')}.lock"


def _lock_path(name: str) -> Path:
    if name in _resolved_lock_paths:
        return _resolved_lock_paths[name]

    system_lock = Path(f"/var/lock/ripknife-{name}.lock")
    fallback = LOG_ROOT / f"{name}.lock"
    resolved = system_lock if _try_lock_path(system_lock) else fallback
    _resolved_lock_paths[name] = resolved
    return resolved


def makemkv_lock_path(device: str | None = None) -> Path:
    """MakeMKV access lock.

    With ``device=None`` returns the global lock (single-drive default).
    With a device string returns a per-drive lock (e.g. ``…-makemkv-sr0.lock``)
    so two physical drives can run MakeMKV concurrently while a single drive
    stays mutually exclusive.
    """
    if device is None:
        return _lock_path("makemkv")
    from ripknife.devices import device_slug

    return _lock_path(f"makemkv-{device_slug(device)}")


def scan_lock_path(device: str | None = None) -> Path:
    """Same lock as makemkv — MakeMKV access on a drive must be mutually exclusive."""
    return makemkv_lock_path(device)


def watcher_lock_path(name: str) -> Path:
    """Per-watcher singleton lock (rip-watcher, disc-watcher, per-device workers)."""
    return LOG_ROOT / f"{name}.pid.lock"


def run_ripknife(args: list[str], *, logger: logging.Logger) -> tuple[int, str]:
    cmd = [str(RIPKNIFE_BIN), *args]
    logger.info("Running: %s", " ".join(cmd))
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        logger.error("Command failed (%s): %s", proc.returncode, output.strip())
    return proc.returncode, output


def disc_media_generation(device: str) -> str | None:
    """Kernel counter that bumps on each disc insert/eject (for scan cache keys)."""
    block = Path(disc_dev(device)).name
    for name in ("media_change", "event"):
        path = Path(f"/sys/block/{block}/device/{name}")
        if path.is_file():
            try:
                return f"{name}={path.read_text().strip()}"
            except OSError:
                continue
    return None


def scan_device_dict(device: str, minlength: int = 300) -> dict:
    """Scan disc via MakeMKV Python API; return JSON-serializable summary."""
    from ripknife.makemkv import scan, scan_to_json

    return scan_to_json(scan(device, minlength))


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
        if shutdown_requested():
            return False
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
