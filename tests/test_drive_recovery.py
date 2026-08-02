"""Tests for orphan makemkvcon drive recovery."""

from __future__ import annotations

import signal

import pytest

from videosripper.monitor import (
    MakemkvProcess,
    ensure_drive_available,
    is_orphan_makemkv,
    list_makemkv_processes,
    recover_orphan_makemkv,
)


def test_is_orphan_makemkv_ppid_init() -> None:
    proc = MakemkvProcess(pid=100, ppid=1, args="makemkvcon mkv dev:/dev/sr0")
    assert is_orphan_makemkv(proc) is True


def test_is_orphan_makemkv_dead_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MakemkvProcess(pid=100, ppid=9999, args="makemkvcon info /dev/sr0")
    monkeypatch.setattr("videosripper.monitor._proc_alive", lambda pid: pid == 100)
    assert is_orphan_makemkv(proc) is True


def test_is_orphan_makemkv_live_watcher_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MakemkvProcess(pid=100, ppid=200, args="makemkvcon mkv dev:/dev/sr0")
    monkeypatch.setattr("videosripper.monitor._proc_alive", lambda pid: True)
    monkeypatch.setattr("videosripper.monitor._proc_state", lambda pid: "S")
    monkeypatch.setattr(
        "videosripper.monitor._proc_cmdline",
        lambda pid: "python scripts/rip-watcher.py" if pid == 200 else "",
    )
    assert is_orphan_makemkv(proc) is False


def test_is_orphan_makemkv_manual_shell_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MakemkvProcess(pid=100, ppid=200, args="makemkvcon mkv dev:/dev/sr0")
    monkeypatch.setattr("videosripper.monitor._proc_alive", lambda pid: True)
    monkeypatch.setattr("videosripper.monitor._proc_state", lambda pid: "S")
    monkeypatch.setattr(
        "videosripper.monitor._proc_cmdline",
        lambda pid: "/bin/bash" if pid == 200 else "",
    )
    assert is_orphan_makemkv(proc) is False


def test_list_makemkv_processes_parses_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    ps_out = (
        "  100   1 makemkvcon mkv -r dev:/dev/sr0 0 /tmp\n"
        "  200  50 makemkvcon info -r /dev/sr1\n"
    )

    class _Proc:
        returncode = 0
        stdout = ps_out

    monkeypatch.setattr("videosripper.monitor.subprocess.run", lambda *a, **k: _Proc())
    procs = list_makemkv_processes("dev:/dev/sr0")
    assert len(procs) == 1
    assert procs[0].pid == 100
    assert procs[0].ppid == 1


def test_recover_orphan_makemkv_sends_term_then_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    orphan = MakemkvProcess(pid=4242, ppid=1, args="makemkvcon mkv dev:/dev/sr0")
    monkeypatch.setattr(
        "videosripper.monitor.list_makemkv_processes",
        lambda device: [orphan],
    )
    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    alive = {4242}

    def fake_alive(pid: int) -> bool:
        return pid in alive

    def fake_wait(pids, *, timeout_sec):
        if signal.SIGTERM in [k for _, k in killed]:
            alive.discard(4242)
        return set()

    monkeypatch.setattr("videosripper.monitor.os.kill", fake_kill)
    monkeypatch.setattr("videosripper.monitor._proc_alive", fake_alive)
    monkeypatch.setattr("videosripper.monitor._wait_pids_exit", fake_wait)
    monkeypatch.setattr("videosripper.monitor._fuser_holds_device", lambda device: set())

    assert recover_orphan_makemkv("dev:/dev/sr0") is True
    assert (4242, signal.SIGTERM) in killed


def test_ensure_drive_available_clears_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_recover(device: str) -> bool:
        calls.append(device)
        return True

    monkeypatch.setattr("videosripper.monitor.recover_orphan_makemkv", fake_recover)
    monkeypatch.setattr("videosripper.monitor.list_makemkv_processes", lambda device: [])
    assert ensure_drive_available("dev:/dev/sr0") is True
    assert calls == ["dev:/dev/sr0"]


def test_ensure_drive_available_warns_on_live_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    live = MakemkvProcess(pid=100, ppid=200, args="makemkvcon mkv dev:/dev/sr0")
    monkeypatch.setattr("videosripper.monitor.recover_orphan_makemkv", lambda device: False)
    monkeypatch.setattr("videosripper.monitor.list_makemkv_processes", lambda device: [live])
    monkeypatch.setattr("videosripper.monitor.is_orphan_makemkv", lambda proc, **kw: False)
    assert ensure_drive_available("dev:/dev/sr0") is False
