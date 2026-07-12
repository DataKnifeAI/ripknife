"""Rip monitoring: stall detection, disc preflight, makemkv diagnostics."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from videosripper.logging_setup import log_debug
from videosripper.paths import MAKEMKV_BIN, disc_dev

logger = logging.getLogger("videosripper")

ALERT_PREFIX = "=== ALERT ==="

_MSG_RE = re.compile(r'^MSG:(\d+),\d+,\d+,"([^"]*)"')
_TCOUNT_RE = re.compile(r"^TCOUNT:(\d+)")
_PRGV_RE = re.compile(r"^PRGV:")

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
    ) -> MonitorSettings:
        base = manifest_monitor or cls()
        stall = int(os.environ.get("VIDEOSRIPPER_STALL_TIMEOUT_SEC", base.stall_timeout_sec))
        open_ = int(os.environ.get("VIDEOSRIPPER_OPEN_TIMEOUT_SEC", base.open_timeout_sec))
        on_stall = on_stall_cli or os.environ.get("VIDEOSRIPPER_ON_STALL", base.on_stall)
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


def find_duplicate_makemkv(device: str) -> list[str]:
    """Return command lines of other makemkvcon processes on the same device."""
    dev_block = disc_dev(device)
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    matches: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "makemkvcon" not in line:
            continue
        if dev_block in line or device in line:
            matches.append(line)
    return matches


def warn_duplicate_makemkv(device: str) -> bool:
    dupes = find_duplicate_makemkv(device)
    if not dupes:
        return False
    logger.warning("%s DUPLICATE MAKEKV — another makemkvcon is using %s", ALERT_PREFIX, device)
    for proc_line in dupes:
        logger.warning("%s   %s", ALERT_PREFIX, proc_line)
    logger.warning(
        "%s Action: wait for the existing rip to finish, or kill it before starting another",
        ALERT_PREFIX,
    )
    return True


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
    from videosripper.retry import (
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
    from videosripper.makemkv import makemkv_env

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
            "%s Action: check drive LED; re-seat disc; retry or increase VIDEOSRIPPER_OPEN_TIMEOUT_SEC",
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


def is_progress_line(line: str) -> bool:
    stripped = line.strip()
    if _PRGV_RE.match(stripped):
        return True
    if stripped.startswith("MSG:5014"):
        return True
    if "Saving " in stripped and "titles into directory" in stripped:
        return True
    return False
