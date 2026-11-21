"""Rip monitoring: stall detection, disc preflight, makemkv diagnostics."""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ripknife.logging_setup import log_debug
from ripknife.paths import MAKEMKV_BIN, disc_dev

logger = logging.getLogger("ripknife")

ALERT_PREFIX = "=== ALERT ==="

_MSG_RE = re.compile(r'^MSG:(\d+),\d+,\d+,"([^"]*)"')
_TCOUNT_RE = re.compile(r"^TCOUNT:(\d+)")
_PRGV_RE = re.compile(r"^PRGV:")
# PRGV:current,total,max — current bar, total(overall) bar, constant max.
# Only emitted when makemkvcon runs with a --progress target (e.g.
# --progress=-same); default robot mode prints none.
_PRGV_VALUES_RE = re.compile(r"^PRGV:(\d+),(\d+),(\d+)")

# MakeMKV exit 11 = failed to open disc (common when drive loses the disc).
_EXIT_DISC_OPEN = 11


class MakeMkvError(RuntimeError):
    """Base error for MakeMKV failures surfaced to the user."""


class DiscOpenError(MakeMkvError):
    """Disc could not be opened or has no readable titles."""


class RipStallError(MakeMkvError):
    """MakeMKV process stalled without progress."""


class MakeMkvProcessError(MakeMkvError):
    """MakeMKV subprocess exited with an error."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        messages: list[str] | None = None,
        command: str = "",
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.messages = messages or []
        self.command = command


DEFAULT_TV_STALL_TIMEOUT_SEC = 600


@dataclass
class MonitorSettings:
    stall_timeout_sec: int = 900
    open_timeout_sec: int = 600
    on_stall: str = "exit"  # "exit" | "warn"

    @classmethod
    def resolve(
        cls,
        manifest_monitor: MonitorSettings | None = None,
        *,
        on_stall_cli: str | None = None,
        tv: bool = False,
    ) -> MonitorSettings:
        base = manifest_monitor or cls()
        if "RIPKNIFE_STALL_TIMEOUT_SEC" in os.environ:
            stall = int(os.environ["RIPKNIFE_STALL_TIMEOUT_SEC"])
        elif tv and "RIPKNIFE_TV_STALL_TIMEOUT_SEC" in os.environ:
            stall = int(os.environ["RIPKNIFE_TV_STALL_TIMEOUT_SEC"])
        elif tv and base.stall_timeout_sec == cls().stall_timeout_sec:
            stall = DEFAULT_TV_STALL_TIMEOUT_SEC
        else:
            stall = base.stall_timeout_sec
        open_ = int(os.environ.get("RIPKNIFE_OPEN_TIMEOUT_SEC", base.open_timeout_sec))
        on_stall = on_stall_cli or os.environ.get("RIPKNIFE_ON_STALL", base.on_stall)
        if on_stall not in ("exit", "warn"):
            on_stall = "exit"
        return cls(stall_timeout_sec=stall, open_timeout_sec=open_, on_stall=on_stall)


def parse_msg_text(line: str) -> str | None:
    if m := _MSG_RE.match(line):
        return m.group(2)
    return None


def extract_msg_lines(lines: list[str]) -> list[str]:
    msgs: list[str] = []
    for line in lines:
        if text := parse_msg_text(line):
            msgs.append(text)
    return msgs


def parse_tcount(output: str) -> int | None:
    for line in output.splitlines():
        if m := _TCOUNT_RE.match(line.strip()):
            return int(m.group(1))
    return None


def is_disc_open_failure(returncode: int, output: str) -> bool:
    if returncode == _EXIT_DISC_OPEN:
        return True
    if "Failed to open disc" in output:
        return True
    tcount = parse_tcount(output)
    if tcount is not None and tcount == 0:
        return True
    return False


def format_disc_open_error(returncode: int, output: str) -> str:
    msgs = extract_msg_lines(output.splitlines())
    detail = msgs[-1] if msgs else f"exit {returncode}"
    tcount = parse_tcount(output)
    parts = [f"Failed to open disc ({detail})"]
    if tcount == 0:
        parts.append("TCOUNT:0 — drive sees no titles")
    parts.append(
        "Action: after retries exhaust the disc is ejected — "
        "clean/re-seat, re-insert, confirm /dev/sr0, re-run rip"
    )
    return "; ".join(parts)


def diagnose_process_failure(
    returncode: int,
    lines: list[str],
    *,
    title_index: int | None = None,
    command: str = "",
) -> MakeMkvProcessError:
    msgs = extract_msg_lines(lines)
    if is_disc_open_failure(returncode, "\n".join(lines)):
        msg = format_disc_open_error(returncode, "\n".join(lines))
        return MakeMkvProcessError(
            msg,
            exit_code=returncode,
            messages=msgs,
            command=command,
        )
    label = f"title {title_index}" if title_index is not None else "makemkv"
    summary = f"makemkv failed for {label} (exit {returncode})"
    if msgs:
        summary += f": {'; '.join(msgs[-3:])}"
    return MakeMkvProcessError(summary, exit_code=returncode, messages=msgs, command=command)


def log_makemkv_failure(exc: MakeMkvProcessError) -> None:
    logger.error("%s MAKEKV FAILURE — exit %s", ALERT_PREFIX, exc.exit_code)
    for msg in exc.messages:
        logger.error("%s MSG: %s", ALERT_PREFIX, msg)
    logger.error("%s %s", ALERT_PREFIX, exc)


_WATCHER_MARKERS = (
    "rip-watcher",
    "disc-watcher",
    "ripknife",
    "makemkvcon",
)


@dataclass(frozen=True)
class MakemkvProcess:
    pid: int
    ppid: int
    args: str


def _proc_alive(pid: int) -> bool:
    return pid > 0 and Path(f"/proc/{pid}").is_dir()


def _proc_state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2]
    except (OSError, IndexError):
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _is_active_ripknife_parent(pid: int) -> bool:
    """True when ``pid`` is a live rip-watcher / disc-watcher / CLI parent."""
    if not _proc_alive(pid):
        return False
    if _proc_state(pid) == "Z":
        return False
    cmd = _proc_cmdline(pid)
    return any(marker in cmd for marker in _WATCHER_MARKERS)


def list_makemkv_processes(device: str) -> list[MakemkvProcess]:
    """Return makemkvcon processes targeting ``device``."""
    dev_block = disc_dev(device)
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    matches: list[MakemkvProcess] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "makemkvcon" not in line:
            continue
        if dev_block not in line and device not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        matches.append(MakemkvProcess(pid=pid, ppid=ppid, args=parts[2]))
    return matches


def find_duplicate_makemkv(device: str) -> list[str]:
    """Return command lines of other makemkvcon processes on the same device."""
    return [f"{p.pid} {p.args}" for p in list_makemkv_processes(device)]


def is_orphan_makemkv(proc: MakemkvProcess, *, exclude_pids: set[int] | None = None) -> bool:
    """True for stale makemkvcon after watcher death (PPID=1 / dead parent).

    Live rip-watcher or disc-watcher parents are treated as legitimate concurrent
    use — do not kill those.
    """
    if exclude_pids and proc.pid in exclude_pids:
        return False
    if proc.ppid <= 1:
        return True
    if not _proc_alive(proc.ppid):
        return True
    if _proc_state(proc.ppid) == "Z":
        return True
    if _is_active_ripknife_parent(proc.ppid):
        return False
    return False


def _fuser_holds_device(device: str) -> set[int]:
    dev_path = disc_dev(device)
    try:
        proc = subprocess.run(
            ["fuser", dev_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    pids: set[int] = set()
    for chunk in (proc.stdout, proc.stderr):
        for token in chunk.replace(dev_path, " ").split():
            if token.isdigit():
                pids.add(int(token))
    return pids


def _wait_pids_exit(pids: set[int], *, timeout_sec: float) -> set[int]:
    deadline = time.monotonic() + timeout_sec
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _proc_alive(pid)}
        if not remaining:
            break
        time.sleep(0.25)
    return remaining


def recover_orphan_makemkv(
    device: str,
    *,
    term_wait_sec: float = 5.0,
    kill_wait_sec: float = 3.0,
) -> bool:
    """SIGTERM/SIGKILL orphan makemkvcon on ``device``; return True if any recovered."""
    procs = list_makemkv_processes(device)
    orphans = [p for p in procs if is_orphan_makemkv(p)]
    if not orphans:
        return False

    logger.warning(
        "%s ORPHAN MAKEKV — %d stale makemkvcon on %s; recovering drive",
        ALERT_PREFIX,
        len(orphans),
        device,
    )
    for proc in orphans:
        logger.warning("%s   pid=%d ppid=%d %s", ALERT_PREFIX, proc.pid, proc.ppid, proc.args)

    termed: set[int] = set()
    for proc in orphans:
        try:
            os.kill(proc.pid, signal.SIGTERM)
            termed.add(proc.pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            logger.warning("%s SIGTERM pid=%d failed: %s", ALERT_PREFIX, proc.pid, exc)

    survivors = _wait_pids_exit(termed, timeout_sec=term_wait_sec)
    if survivors:
        logger.warning(
            "%s ORPHAN MAKEKV — SIGKILL %d survivor(s) on %s",
            ALERT_PREFIX,
            len(survivors),
            device,
        )
        for pid in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                survivors.discard(pid)
            except OSError as exc:
                logger.warning("%s SIGKILL pid=%d failed: %s", ALERT_PREFIX, pid, exc)
        survivors = _wait_pids_exit(survivors, timeout_sec=kill_wait_sec)

    if survivors:
        logger.error(
            "%s ORPHAN MAKEKV — drive still held after recovery on %s (pids=%s)",
            ALERT_PREFIX,
            device,
            ", ".join(str(p) for p in sorted(survivors)),
        )
        return False

    holders = _fuser_holds_device(device)
    if holders:
        logger.warning(
            "%s DRIVE STILL HELD — %s users: pids=%s (non-makemkv; not killed)",
            ALERT_PREFIX,
            device,
            ", ".join(str(p) for p in sorted(holders)),
        )
    else:
        logger.info("Drive recovery OK: %s clear", device)
    return True


def ensure_drive_available(device: str) -> bool:
    """Recover orphan makemkvcon; warn on legitimate concurrent use.

    Call before makemkv info/mkv/scan. Returns True when no duplicate remains
    or orphans were cleared; False when a live peer rip still holds the drive.
    """
    recovered = recover_orphan_makemkv(device)
    procs = list_makemkv_processes(device)
    if not procs:
        return True

    live = [p for p in procs if not is_orphan_makemkv(p)]
    if not live:
        return recovered

    logger.warning("%s DUPLICATE MAKEKV — another makemkvcon is using %s", ALERT_PREFIX, device)
    for proc in live:
        logger.warning("%s   pid=%d ppid=%d %s", ALERT_PREFIX, proc.pid, proc.ppid, proc.args)
    if recovered:
        logger.warning(
            "%s Recovered orphan(s) but %d live makemkvcon remain on %s",
            ALERT_PREFIX,
            len(live),
            device,
        )
    else:
        logger.warning(
            "%s Action: wait for the existing rip to finish, or kill it before starting another",
            ALERT_PREFIX,
        )
    return False


def warn_duplicate_makemkv(device: str) -> bool:
    """Legacy entry: recover orphans first, then warn on live duplicates."""
    ensure_drive_available(device)
    return bool(list_makemkv_processes(device))


def preflight_disc_open(
    device: str,
    minlength: int,
    *,
    timeout_sec: int,
    retries: int = 0,
    delay_sec: float = 30.0,
    eject_on_exhaust: bool = True,
) -> int:
    """Run makemkv info with timeout; raise DiscOpenError on failure.

    ``retries`` is extra attempts after the first failure (3 = 4 total tries).
    On exhaustion, logs USER INTERVENTION REQUIRED and ejects the disc (when
    ``eject_on_exhaust``) so the user can clean/re-seat it — never ejects between retries.
    """
    from ripknife.retry import (
        alert_retries_exhausted_and_eject,
        call_with_retries,
        is_disc_retry_exhaustion_failure,
    )

    def _once() -> int:
        return _preflight_disc_open_once(device, minlength, timeout_sec=timeout_sec)

    try:
        if retries <= 0:
            return _once()
        return call_with_retries(
            _once,
            retries=retries,
            delay_sec=delay_sec,
            label="disc preflight",
            should_retry=lambda exc: isinstance(exc, DiscOpenError),
        )
    except BaseException as exc:
        if eject_on_exhaust and is_disc_retry_exhaustion_failure(exc):
            alert_retries_exhausted_and_eject(
                device,
                exc,
                label="disc preflight",
                retries=retries,
            )
        raise


def _preflight_disc_open_once(
    device: str,
    minlength: int,
    *,
    timeout_sec: int,
) -> int:
    """Single makemkv info attempt; raise DiscOpenError on failure."""
    from ripknife.makemkv import makemkv_env

    ensure_drive_available(device)
    logger.info("=== Preflight: disc access (timeout %ds) ===", timeout_sec)
    cmd = [
        str(MAKEMKV_BIN),
        "info",
        "-r",
        f"--minlength={minlength}",
        device,
    ]
    log_debug("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=makemkv_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "%s DISC OPEN TIMEOUT — makemkv info hung after %ds",
            ALERT_PREFIX,
            timeout_sec,
        )
        logger.error(
            "%s Action: check drive LED; re-seat disc; retry or increase RIPKNIFE_OPEN_TIMEOUT_SEC",
            ALERT_PREFIX,
        )
        raise DiscOpenError(
            f"Disc open timed out after {timeout_sec}s — drive may be stuck"
        ) from exc

    output = proc.stdout + proc.stderr
    for line in output.splitlines():
        if line.strip():
            log_debug(line)

    if is_disc_open_failure(proc.returncode, output):
        msg = format_disc_open_error(proc.returncode, output)
        logger.error("%s %s", ALERT_PREFIX, msg)
        raise DiscOpenError(msg)

    tcount = parse_tcount(output)
    if tcount is None:
        logger.warning("%s Preflight: TCOUNT not found in scan output; continuing", ALERT_PREFIX)
        return -1
    logger.info("Preflight OK: %d title(s) visible", tcount)
    from ripknife.makemkv import log_libredrive_status, parse_libredrive_output

    log_libredrive_status(parse_libredrive_output(output, device=device))
    return tcount


@dataclass
class RipWatchdog:
    """Watch a running makemkv process for progress stalls."""

    settings: MonitorSettings
    staging: Path | None = None
    _last_progress: float = field(default_factory=time.monotonic, init=False)
    _rip_phase: bool = field(default=False, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _stall_event: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _last_staging_bytes: int = field(default=0, init=False)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="rip-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def note_progress(self, reason: str = "") -> None:
        self._last_progress = time.monotonic()
        if reason in ("prgv", "file", "msg_save"):
            self._rip_phase = True

    def stalled(self) -> bool:
        return self._stall_event.is_set()

    def phase_label(self) -> str:
        return self._phase_label()

    def active_timeout(self) -> int:
        return self._active_timeout()

    def _active_timeout(self) -> int:
        return self.settings.stall_timeout_sec if self._rip_phase else self.settings.open_timeout_sec

    def _phase_label(self) -> str:
        return "title rip" if self._rip_phase else "disc open"

    def _check_staging_growth(self) -> bool:
        if not self.staging or not self.staging.is_dir():
            return False
        total = sum(f.stat().st_size for f in self.staging.glob("*.mkv") if f.is_file())
        if total > self._last_staging_bytes:
            self._last_staging_bytes = total
            return True
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._check_staging_growth():
                self.note_progress("file")
            elapsed = time.monotonic() - self._last_progress
            limit = self._active_timeout()
            if elapsed >= limit:
                phase = self._phase_label()
                logger.error(
                    "%s RIP STALL — no progress for %ds during %s (limit %ds)",
                    ALERT_PREFIX,
                    int(elapsed),
                    phase,
                    limit,
                )
                logger.error(
                    "%s Diagnosis: makemkv is alive but produced no PRGV lines or file growth",
                    ALERT_PREFIX,
                )
                logger.error(
                    "%s Action: check drive activity; wait, kill the rip, re-seat disc; "
                    "resume with --start-at N",
                    ALERT_PREFIX,
                )
                self._stall_event.set()
                return
            wait = min(15.0, max(1.0, limit - elapsed))
            self._stop.wait(wait)


def _is_prgv_line(stripped: str) -> bool:
    return bool(_PRGV_RE.match(stripped))


def _is_save_titles_line(stripped: str) -> bool:
    return "Saving " in stripped and "titles into directory" in stripped


_PROGRESS_LINE_CHECKS: tuple[Callable[[str], bool], ...] = (
    _is_prgv_line,
    lambda s: s.startswith("MSG:5014"),
    _is_save_titles_line,
)


def is_progress_line(line: str) -> bool:
    stripped = line.strip()
    return any(check(stripped) for check in _PROGRESS_LINE_CHECKS)


def is_save_phase_line(line: str) -> bool:
    """True once MakeMKV enters title-save (after open/analyze).

    Open/scan often ends a phase with ``PRGV:max,max,max`` (100% on the
    overall bar) before real MKV write progress begins. Percent logging should
    wait for ``MSG:5014`` / ``PRGT`` Saving (or the human "Saving N titles…"
    line) so those boundary spikes never become ``RIP title N — 100%``.
    """
    stripped = line.strip()
    if stripped.startswith("MSG:5014"):
        return True
    if stripped.startswith("PRGT:") and "Saving" in stripped:
        return True
    return _is_save_titles_line(stripped)


def parse_prgv(line: str) -> tuple[int, int, int] | None:
    """Parse a MakeMKV robot-mode ``PRGV:current,total,max`` line.

    ``current`` is the current-operation bar, ``total`` is the overall-task bar,
    and ``max`` is the constant full-scale value. Returns ``None`` for non-PRGV
    lines (only appears when makemkv runs with ``--progress``).
    """
    m = _PRGV_VALUES_RE.match(line.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def prgv_percent(line: str) -> int | None:
    """Return overall rip percent (0-100) from a PRGV line, or ``None``.

    Uses the ``total`` (overall-task) bar over ``max`` — for a single-title
    ``mkv`` rip this climbs 0→100 across the whole title. Guards ``max<=0``.
    """
    parsed = parse_prgv(line)
    if parsed is None:
        return None
    _current, total, maximum = parsed
    if maximum <= 0:
        return None
    pct = int(100 * total / maximum)
    return max(0, min(100, pct))


# Early PRGV samples swing wildly; wait for a decile and ~30s of wall time.
_ETA_MIN_PCT = 10
_ETA_MIN_ELAPSED_SEC = 30.0


def estimate_eta_sec(
    pct: int,
    elapsed: float,
    *,
    min_pct: int = _ETA_MIN_PCT,
    min_elapsed: float = _ETA_MIN_ELAPSED_SEC,
) -> int | None:
    """Rough seconds remaining from percent-complete rate, or ``None`` if unstable.

    Extrapolates ``elapsed * (100 - pct) / pct``. Returns ``None`` until
    ``pct >= min_pct`` and ``elapsed >= min_elapsed`` (early samples are noisy),
    and when pct is outside (0, 100).
    """
    if pct < min_pct or pct >= 100 or pct <= 0:
        return None
    if elapsed < min_elapsed:
        return None
    return int(elapsed * (100 - pct) / pct)


def format_eta_remaining(eta_sec: int | None) -> str | None:
    """Human-readable ETA fragment: ``~14m``, ``~1h05m``, ``<1m``, or ``None``."""
    if eta_sec is None:
        return None
    if eta_sec < 60:
        return "<1m"
    hours, rem = divmod(int(eta_sec), 3600)
    minutes = rem // 60
    if hours:
        return f"~{hours}h{minutes:02d}m"
    return f"~{minutes}m"


@dataclass
class RipDecileTracker:
    """Throttle PRGV percents to one milestone per rising 10% step.

    ``milestone_for(pct)`` returns 10/20/…/100 the first time each decile is
    crossed and ``None`` otherwise, so pipeline.log stays at ≤10 progress lines
    per title and oscillation near a boundary never double-logs. 0% is skipped
    (the ``ripping 1/1`` line already marks the start).

    MakeMKV often emits ``PRGV:65536,65536,65536`` at phase boundaries before
    real save progress. 100% is withheld until at least one mid-decile (10–90)
    has been logged, so a first-sample full bar cannot lock the tracker or
    print a false ``RIP title N — 100%``.
    """

    _last_decile: int = field(default=-1, init=False)

    def milestone_for(self, pct: int) -> int | None:
        if pct < 0:
            return None
        decile = min(pct, 100) // 10
        if decile <= self._last_decile:
            return None
        # Never treat 100% as the first progress sample for a title.
        if decile == 10 and self._last_decile < 1:
            return None
        self._last_decile = decile
        milestone = decile * 10
        return milestone if milestone > 0 else None
