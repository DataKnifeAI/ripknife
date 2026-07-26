"""MakeMKV scan, rip, and output parsing."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from videosripper.logging_setup import log_debug
from videosripper.models import StreamDefaultsConfig
from videosripper.monitor import (
    DiscOpenError,
    MakeMkvProcessError,
    MonitorSettings,
    RipDecileTracker,
    RipStallError,
    RipWatchdog,
    diagnose_process_failure,
    estimate_eta_sec,
    is_disc_open_failure,
    is_progress_line,
    is_save_phase_line,
    log_makemkv_failure,
    prgv_percent,
)
from videosripper.paths import MAKEMKV_BIN, MAKEMKV_CONF

logger = logging.getLogger("videosripper")

_SELECTION_KEY = "app_DefaultSelectionString"

_TINFO_RE = re.compile(r'^TINFO:(\d+),(\d+),0,"([^"]+)"')
_CINFO_DISC_RE = re.compile(r'^CINFO:2,0,"([^"]+)"')

_SCAN_LINE_WARNINGS: list[tuple[str, str]] = [
    (
        "requires Java runtime (JRE), but none was found",
        "Java JRE not found (BD-Java discs need Java)",
    ),
]


def _set_title_name(info: TitleInfo, value: str) -> None:
    info.name = value


def _set_title_duration(info: TitleInfo, value: str) -> None:
    info.duration = value
    info.duration_sec = duration_to_seconds(value)


def _set_title_size(info: TitleInfo, value: str) -> None:
    info.size_bytes = int(value)


_TINFO_FIELD_HANDLERS: dict[int, Callable[[TitleInfo, str], None]] = {
    2: _set_title_name,
    9: _set_title_duration,
    11: _set_title_size,
}


@dataclass
class TitleInfo:
    index: int
    duration: str = ""
    duration_sec: int = 0
    size_bytes: int = 0
    name: str = ""


@dataclass
class ScanResult:
    raw: str
    disc_name: str = ""
    titles: dict[int, TitleInfo] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def duration_to_seconds(duration: str) -> int:
    parts = duration.split(":")
    if len(parts) == 3:
        h, m, s = (int(p) for p in parts)
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = (int(p) for p in parts)
        return m * 60 + s
    return 0


def makemkv_env() -> dict[str, str]:
    env = os.environ.copy()
    if MAKEMKV_CONF.is_file():
        for line in MAKEMKV_CONF.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            env[key] = value.strip().strip('"').strip("'")
    lib = Path.home() / ".local/lib"
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{lib}{':' + existing if existing else ''}"
    return env


def build_selection_string(cfg: StreamDefaultsConfig) -> str | None:
    """Build MakeMKV app_DefaultSelectionString for track selection/defaults."""
    if not cfg.keep_all_tracks and not cfg.audio_default and not cfg.subtitle_default:
        return None
    parts: list[str] = []
    if cfg.keep_all_tracks:
        parts.append("+sel:all")
    parts.append("=100:all")
    if cfg.audio_default:
        lang = cfg.audio_default.lower()
        parts.append(f"+50:({lang}&audio)")
    if cfg.subtitle_default:
        lang = cfg.subtitle_default.lower()
        parts.append(f"+50:({lang}&subtitle)")
    return ",".join(parts)


@contextmanager
def makemkv_selection_profile(selection: str | None):
    """Temporarily override MakeMKV default track selection in settings.conf."""
    if selection is None:
        yield
        return
    settings = Path.home() / ".MakeMKV/settings.conf"
    original = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    lines = [ln for ln in original.splitlines() if not ln.strip().startswith(_SELECTION_KEY)]
    lines.append(f'{_SELECTION_KEY} = "{selection}"')
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log_debug("MakeMKV selection profile: %s", selection)
    try:
        yield
    finally:
        if original:
            settings.write_text(original, encoding="utf-8")
        elif settings.is_file():
            lines = [
                ln
                for ln in settings.read_text(encoding="utf-8").splitlines()
                if not ln.strip().startswith(_SELECTION_KEY)
            ]
            if lines:
                settings.write_text("\n".join(lines) + "\n", encoding="utf-8")
            else:
                settings.unlink(missing_ok=True)


def parse_scan_output(raw: str) -> ScanResult:
    result = ScanResult(raw=raw)
    for line in raw.splitlines():
        if m := _CINFO_DISC_RE.match(line):
            result.disc_name = m.group(1)
            continue
        if m := _TINFO_RE.match(line):
            idx = int(m.group(1))
            if handler := _TINFO_FIELD_HANDLERS.get(int(m.group(2))):
                info = result.titles.setdefault(idx, TitleInfo(index=idx))
                handler(info, m.group(3))
            continue
        for needle, message in _SCAN_LINE_WARNINGS:
            if needle in line:
                result.warnings.append(message)
                break
    return result


def _write_rip_progress_state(title_index: int | None, pct: int, start: float) -> None:
    """Best-effort update of ``logs/watcher-state.json`` rip_progress section.

    Never raises — progress reporting must not disturb the rip.
    """
    try:
        from videosripper.watcher_util import update_state

        elapsed = max(0.0, time.monotonic() - start)
        update_state(
            "rip_progress",
            {
                "title": title_index,
                "pct": pct,
                "eta_sec": estimate_eta_sec(pct, elapsed),
            },
        )
    except Exception:  # noqa: BLE001 — progress side-channel is non-critical
        pass


def _run_makemkv_streaming(
    cmd: list[str],
    *,
    staging: Path | None,
    monitor: MonitorSettings | None,
    title_index: int | None = None,
) -> tuple[int, list[str]]:
    settings = monitor or MonitorSettings.resolve()
    watchdog = RipWatchdog(settings=settings, staging=staging)
    watchdog.start()
    from videosripper.logging_setup import log_rip_percent

    tracker = RipDecileTracker()
    start = time.monotonic()
    last_state_pct = -1
    last_state_at = 0.0
    save_phase = False
    lines: list[str] = []
    log_debug("$ %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=makemkv_env(),
    )
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)
            log_debug(line)
            # Open/analyze PRGV still feeds the stall watchdog, but percent
            # logging waits for save — early phase bars often hit 100% briefly.
            if is_save_phase_line(line):
                save_phase = True
            if is_progress_line(line):
                reason = "prgv" if line.strip().startswith("PRGV:") else "msg_save"
                watchdog.note_progress(reason)
                pct = prgv_percent(line) if save_phase else None
                if pct is not None:
                    now = time.monotonic()
                    elapsed = max(0.0, now - start)
                    milestone = tracker.milestone_for(pct)
                    if milestone is not None:
                        log_rip_percent(
                            milestone,
                            title=title_index,
                            eta_sec=estimate_eta_sec(milestone, elapsed),
                        )
                    if pct != last_state_pct and (now - last_state_at >= 5.0 or pct == 100):
                        _write_rip_progress_state(title_index, pct, start)
                        last_state_pct = pct
                        last_state_at = now
        proc.wait()
    finally:
        watchdog.stop()

    if watchdog.stalled():
        if settings.on_stall == "exit" and proc.poll() is None:
            proc.kill()
            proc.wait()
        if settings.on_stall == "exit":
            raise RipStallError(
                f"Rip stalled during {watchdog.phase_label()} "
                f"(no progress for {watchdog.active_timeout()}s)"
            )
        logger.warning("%s Stall detected but --on-stall=warn; continuing", "=== ALERT ===")

    returncode = proc.returncode if proc.returncode is not None else -1
    if returncode != 0:
        exc = diagnose_process_failure(
            returncode,
            lines,
            title_index=title_index,
            command=" ".join(cmd),
        )
        log_makemkv_failure(exc)
        if isinstance(exc, MakeMkvProcessError) and is_disc_open_failure(returncode, "\n".join(lines)):
            raise DiscOpenError(str(exc)) from exc
        raise exc
    return returncode, lines


def scan(device: str, minlength: int = 300, *, timeout_sec: int | None = None) -> ScanResult:
    cmd = [
        str(MAKEMKV_BIN),
        "info",
        "-r",
        f"--minlength={minlength}",
        device,
    ]
    log_debug("$ %s", " ".join(cmd))
    if timeout_sec is not None:
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
            raise RuntimeError(f"makemkv info timed out after {timeout_sec}s") from exc
        raw = proc.stdout + proc.stderr
        if proc.returncode != 0 or is_disc_open_failure(proc.returncode, raw):
            from videosripper.monitor import format_disc_open_error

            raise DiscOpenError(format_disc_open_error(proc.returncode, raw))
        _log_scan_raw(raw)
        return parse_scan_output(raw)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=makemkv_env(),
        check=False,
    )
    raw = proc.stdout + proc.stderr
    if proc.returncode != 0:
        _log_scan_raw(raw)
        exc = diagnose_process_failure(proc.returncode, raw.splitlines(), command=" ".join(cmd))
        log_makemkv_failure(exc)
        raise exc
    _log_scan_raw(raw)
    return parse_scan_output(raw)


def _log_scan_raw(raw: str) -> None:
    if raw.strip():
        log_debug("--- makemkv info output ---")
        for line in raw.splitlines():
            log_debug(line)


def rip_title(
    device: str,
    title_index: int | str,
    staging: Path,
    minlength: int = 300,
    *,
    stream_defaults: StreamDefaultsConfig | None = None,
    dry_run: bool = False,
    monitor: MonitorSettings | None = None,
    retries: int = 0,
    delay_sec: float = 30.0,
    stall_retry: bool = True,
    eject_on_exhaust: bool = True,
) -> Path | None:
    """Rip one MakeMKV title into ``staging``.

    ``retries`` is extra attempts after the first failure (3 = 4 total tries).
    Retries apply to disc-open failures, missing MKV output, and (when
    ``stall_retry``) stall timeouts. On exhaustion, logs USER INTERVENTION
    REQUIRED and ejects the disc (when ``eject_on_exhaust``) — never between retries.
    """
    staging.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return None

    from videosripper.retry import (
        alert_retries_exhausted_and_eject,
        call_with_retries,
        is_disc_retry_exhaustion_failure,
    )

    def _should_retry(exc: BaseException) -> bool:
        if isinstance(exc, DiscOpenError):
            return True
        if isinstance(exc, RipStallError):
            return stall_retry
        if isinstance(exc, MakeMkvProcessError) and (
            exc.exit_code == 11 or "Failed to open disc" in str(exc)
        ):
            return True
        if isinstance(exc, RuntimeError) and "No MKV produced" in str(exc):
            return True
        return False

    label = f"title {title_index} rip"
    title_arg = str(title_index)

    def _once() -> Path:
        for old in staging.glob("*.mkv"):
            old.unlink()
        cmd = [
            str(MAKEMKV_BIN),
            "mkv",
            "-r",
            # Route PRGV/PRGT/PRGC progress to stdout (default is no progress
            # output) so within-title percent is parseable and stall detection
            # sees PRGV in addition to file growth / MSG:5014.
            "--progress=-same",
            f"--minlength={minlength}",
            device,
            title_arg,
            str(staging),
        ]
        selection = build_selection_string(stream_defaults) if stream_defaults else None
        rip_idx = title_index if isinstance(title_index, int) else None
        with makemkv_selection_profile(selection):
            _run_makemkv_streaming(
                cmd,
                staging=staging,
                monitor=monitor,
                title_index=rip_idx,
            )
        ripped = next(staging.glob("*.mkv"), None)
        if ripped is None:
            raise RuntimeError(f"No MKV produced for title {title_index}")
        return ripped

    try:
        if retries <= 0:
            return _once()
        return call_with_retries(
            _once,
            retries=retries,
            delay_sec=delay_sec,
            label=label,
            should_retry=_should_retry,
        )
    except BaseException as exc:
        if eject_on_exhaust and is_disc_retry_exhaustion_failure(exc):
            alert_retries_exhausted_and_eject(
                device,
                exc,
                label=label,
                retries=retries,
            )
        raise


def pick_feature_title(scan: ScanResult, min_duration_sec: int) -> int:
    from videosripper.title_verify import analyze_obfuscation

    obf = analyze_obfuscation(scan.titles)
    pool = scan.titles
    if obf.detected:
        pool = {i: scan.titles[i] for i in obf.cluster_indices}

    if obf.detected and obf.recommended_title is not None:
        rec = obf.recommended_title
        if rec in pool and pool[rec].duration_sec >= min_duration_sec:
            return rec

    if 0 in pool and pool[0].duration_sec >= min_duration_sec:
        return 0
    best_idx = -1
    best_size = 0
    for idx, info in pool.items():
        if info.duration_sec >= min_duration_sec and info.size_bytes > best_size:
            best_idx = idx
            best_size = info.size_bytes
    if best_idx < 0:
        raise RuntimeError(
            f"No title >= {min_duration_sec // 60} min found; re-scan disc with matching minlength"
        )
    if obf.detected and best_idx in obf.decoy_indices:
        rec = obf.recommended_title
        hint = f"; use title {rec}" if rec is not None else ""
        raise RuntimeError(f"Selected title {best_idx} is a decoy outlier{hint}")
    return best_idx


def scan_to_json(scan: ScanResult) -> dict:
    from videosripper.title_verify import analyze_obfuscation

    obf = analyze_obfuscation(scan.titles)
    payload: dict = {
        "disc_name": scan.disc_name,
        "warnings": scan.warnings,
        "titles": [
            {
                "index": t.index,
                "name": t.name,
                "duration": t.duration,
                "duration_sec": t.duration_sec,
                "size_bytes": t.size_bytes,
            }
            for t in sorted(scan.titles.values(), key=lambda x: x.index)
        ],
    }
    if obf.detected:
        payload["obfuscation"] = obf.to_dict()
    return payload
