"""Human-readable status for ``make watch-status``."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from videosripper.monitor import format_eta_remaining
from videosripper.paths import LOG_ROOT
from videosripper.watcher_util import HEARTBEAT_SECTION, STATE_PATH, media_present

DEFAULT_DEVICE = "dev:/dev/sr0"


def _pgrep_watcher(script: str) -> list[int]:
    """Return PIDs matching ``python …/scripts/<script>`` (empty if none)."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", rf"[p]ython.*scripts/{script}"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [int(line) for line in out.splitlines() if line.strip().isdigit()]


def _age_seconds(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        when = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def collect_watch_status(*, device: str = DEFAULT_DEVICE) -> dict:
    state: dict = {}
    if STATE_PATH.is_file():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    rows = {}
    for name in ("disc-watcher", "rip-watcher"):
        pids = _pgrep_watcher(f"{name}.py")
        hb = state.get(f"{HEARTBEAT_SECTION}:{name}", {})
        hb_at = hb.get("heartbeat_at") or hb.get("updated_at")
        rows[name] = {
            "running": bool(pids),
            "pids": pids,
            "heartbeat_at": hb_at,
            "heartbeat_age_sec": _age_seconds(hb_at),
            "heartbeat_status": hb.get("status"),
        }

    present = media_present(device)
    disc_section = state.get("disc_watcher:sr0") or state.get("disc_watcher") or {}
    rp = state.get("rip_progress") or {}
    rip_progress = None
    if rp and rp.get("pct") is not None:
        rip_progress = {
            "pct": rp.get("pct"),
            "title": rp.get("title"),
            "eta_sec": rp.get("eta_sec"),
            "age_sec": _age_seconds(rp.get("updated_at")),
        }
    return {
        "device": device,
        "disc_present": present,
        "disc_name": disc_section.get("disc_name"),
        "disc_status": disc_section.get("status"),
        "rip_progress": rip_progress,
        "watchers": rows,
        "state_path": str(STATE_PATH),
        "log_root": str(LOG_ROOT),
    }


def print_watch_status(*, device: str = DEFAULT_DEVICE) -> None:
    info = collect_watch_status(device=device)
    print(f"device: {info['device']}")
    disc_bits = ["present" if info["disc_present"] else "empty"]
    if info.get("disc_name"):
        disc_bits.append(f"name={info['disc_name']}")
    if info.get("disc_status"):
        disc_bits.append(f"status={info['disc_status']}")
    print(f"disc:   {', '.join(disc_bits)}")
    rp = info.get("rip_progress")
    # Only surface a fresh in-title percent (skip stale values from a prior rip).
    if rp and rp.get("pct") is not None and (rp.get("age_sec") is None or rp["age_sec"] < 300):
        eta_s = format_eta_remaining(rp.get("eta_sec"))
        paren = f"MakeMKV, {eta_s} left" if eta_s else "MakeMKV"
        print(f"rip:    title {rp.get('title')} — {rp['pct']}% ({paren})")
    for name, row in info["watchers"].items():
        if row["running"]:
            pid_s = ",".join(str(p) for p in row["pids"])
            run = f"running (pid {pid_s})"
        else:
            run = "NOT RUNNING"
        age = _fmt_age(row["heartbeat_age_sec"])
        hb_status = row.get("heartbeat_status") or "?"
        print(f"{name}: {run}; heartbeat {age} ({hb_status})")
    stale = [
        name
        for name, row in info["watchers"].items()
        if row["running"]
        and row["heartbeat_age_sec"] is not None
        and row["heartbeat_age_sec"] > 180
    ]
    if stale:
        print(f"warning: stale heartbeat (>3m) for: {', '.join(stale)}")
    dead = [name for name, row in info["watchers"].items() if not row["running"]]
    if dead:
        print("hint: start with `make watch-all` (or enable scripts/systemd/ user units)")
