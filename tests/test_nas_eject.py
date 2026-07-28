"""Tests for post-rip eject with media verification and retries."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from videosripper import nas


def test_eject_disc_success_when_media_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> MagicMock:
        _ = kwargs
        calls.append(cmd)
        proc = MagicMock()
        proc.returncode = 0
        return proc

    present = {"n": 2}

    def fake_present(device: str) -> bool:
        _ = device
        present["n"] -= 1
        return present["n"] > 0

    monkeypatch.setattr(nas.subprocess, "run", fake_run)
    monkeypatch.setattr(nas, "media_present", fake_present)

    ok = nas.eject_disc(
        "/dev/sr0",
        verify_initial_wait_sec=0.0,
        verify_poll_sec=0.0,
        verify_timeout_sec=0.0,
        sleep=lambda _s: None,
    )
    assert ok is True
    assert calls[0][:3] == ["udisksctl", "eject", "-b"]


def test_eject_disc_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eject_calls = {"n": 0}
    present_after_eject = [True, True, False]

    def fake_run(cmd: list[str], **kwargs) -> MagicMock:
        _ = (cmd, kwargs)
        eject_calls["n"] += 1
        proc = MagicMock()
        proc.returncode = 0
        return proc

    def fake_present(device: str) -> bool:
        _ = device
        return present_after_eject.pop(0) if present_after_eject else False

    monkeypatch.setattr(nas.subprocess, "run", fake_run)
    monkeypatch.setattr(nas, "media_present", fake_present)

    ok = nas.eject_disc(
        "/dev/sr0",
        attempts=3,
        verify_initial_wait_sec=0.0,
        verify_poll_sec=0.0,
        verify_timeout_sec=0.0,
        sleep=lambda _s: None,
    )
    assert ok is True
    assert eject_calls["n"] == 3


def test_eject_disc_fails_after_exhausted_attempts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stages: list[tuple[str, dict]] = []

    def fake_run(cmd: list[str], **kwargs) -> MagicMock:
        _ = (cmd, kwargs)
        proc = MagicMock()
        proc.returncode = 0
        return proc

    monkeypatch.setattr(nas.subprocess, "run", fake_run)
    monkeypatch.setattr(nas, "media_present", lambda _device: True)
    monkeypatch.setattr(
        "videosripper.logging_setup.log_stage",
        lambda stage, **kw: stages.append((stage, kw)),
    )

    with caplog.at_level("ERROR"):
        ok = nas.eject_disc(
            "/dev/sr0",
            attempts=3,
            verify_initial_wait_sec=0.0,
            verify_poll_sec=0.0,
            verify_timeout_sec=0.0,
            sleep=lambda _s: None,
        )

    assert ok is False
    assert any("USER INTERVENTION REQUIRED" in r.message for r in caplog.records)
    assert any("eject failed after 3 attempts" in r.message for r in caplog.records)
    assert stages == [
        (
            "EJECT_FAILED",
            {
                "process": "rip",
                "detail": "/dev/sr0 — manual eject needed after 3 attempts",
            },
        )
    ]


def test_eject_disc_command_failure_still_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [1, 1, 0]
    present = [True, False]

    def fake_run(cmd: list[str], **kwargs) -> MagicMock:
        _ = (cmd, kwargs)
        proc = MagicMock()
        proc.returncode = outcomes.pop(0) if outcomes else 0
        return proc

    def fake_present(device: str) -> bool:
        _ = device
        return present.pop(0) if present else False

    monkeypatch.setattr(nas.subprocess, "run", fake_run)
    monkeypatch.setattr(nas, "media_present", fake_present)

    ok = nas.eject_disc(
        "/dev/sr0",
        attempts=3,
        verify_initial_wait_sec=0.0,
        verify_poll_sec=0.0,
        verify_timeout_sec=0.0,
        sleep=lambda _s: None,
    )
    assert ok is True


def test_eject_disc_dry_run_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"n": 0}

    def fake_run(*args, **kwargs) -> MagicMock:
        _ = (args, kwargs)
        ran["n"] += 1
        return MagicMock(returncode=0)

    monkeypatch.setattr(nas.subprocess, "run", fake_run)
    assert nas.eject_disc("/dev/sr0", dry_run=True) is False
    assert ran["n"] == 0
