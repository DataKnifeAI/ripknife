"""Unit tests for videosripper.retry helpers."""

from __future__ import annotations

import pytest

from videosripper.models import RetryConfig
from videosripper.monitor import DiscOpenError, RipStallError, preflight_disc_open
from videosripper.retry import (
    DEFAULT_RETRY_COUNT,
    DEFAULT_RETRY_DELAY_SEC,
    RetrySettings,
    alert_retries_exhausted_and_eject,
    backoff_seconds,
    call_with_retries,
    is_disc_retry_exhaustion_failure,
)


def test_defaults_are_three_retries() -> None:
    """3 retries = 4 total attempts before user intervention."""
    assert DEFAULT_RETRY_COUNT == 3
    assert DEFAULT_RETRY_DELAY_SEC == 30.0
    settings = RetrySettings()
    assert settings.count == 3
    assert settings.delay_sec == 30.0
    assert settings.nas_retries == 1
    assert settings.stall_retry is True
    assert RetryConfig().count == 3


def test_resolve_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOSRIPPER_RETRY_COUNT", "0")
    monkeypatch.setenv("VIDEOSRIPPER_RETRY_DELAY_SEC", "5")
    monkeypatch.setenv("VIDEOSRIPPER_STALL_RETRY", "false")
    monkeypatch.setenv("VIDEOSRIPPER_NAS_RETRIES", "2")
    settings = RetrySettings.resolve()
    assert settings.count == 0
    assert settings.delay_sec == 5.0
    assert settings.stall_retry is False
    assert settings.nas_retries == 2


def test_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOSRIPPER_RETRY_COUNT", "3")
    monkeypatch.setenv("VIDEOSRIPPER_RETRY_DELAY_SEC", "99")
    settings = RetrySettings.resolve(retry_cli=1, delay_cli=10.0)
    assert settings.count == 1
    assert settings.delay_sec == 10.0


def test_backoff_is_fixed_delay() -> None:
    assert backoff_seconds(1, 30.0) == 30.0
    assert backoff_seconds(2, 30.0) == 30.0


def test_call_with_retries_succeeds_on_second_attempt() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return "ok"

    result = call_with_retries(
        flaky,
        retries=1,
        delay_sec=30.0,
        label="unit",
        sleep=sleeps.append,
    )
    assert result == "ok"
    assert calls["n"] == 2
    assert sleeps == [30.0]


def test_call_with_retries_three_retries_is_four_attempts() -> None:
    calls = {"n": 0}

    def always_fail() -> None:
        calls["n"] += 1
        raise RuntimeError(f"fail-{calls['n']}")

    with pytest.raises(RuntimeError, match="fail-4"):
        call_with_retries(
            always_fail,
            retries=3,
            delay_sec=0.0,
            label="unit",
            sleep=lambda _: None,
        )
    assert calls["n"] == 4


def test_call_with_retries_exhausted() -> None:
    calls = {"n": 0}

    def always_fail() -> None:
        calls["n"] += 1
        raise RuntimeError(f"fail-{calls['n']}")

    with pytest.raises(RuntimeError, match="fail-2"):
        call_with_retries(
            always_fail,
            retries=1,
            delay_sec=0.0,
            label="unit",
            sleep=lambda _: None,
        )
    assert calls["n"] == 2


def test_should_retry_false_raises_immediately() -> None:
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        call_with_retries(
            fail,
            retries=1,
            delay_sec=0.0,
            label="unit",
            should_retry=lambda exc: False,
            sleep=lambda _: None,
        )
    assert calls["n"] == 1


def test_is_disc_retry_exhaustion_failure() -> None:
    assert is_disc_retry_exhaustion_failure(DiscOpenError("open failed"))
    assert is_disc_retry_exhaustion_failure(RipStallError("stalled"))
    assert is_disc_retry_exhaustion_failure(RuntimeError("No MKV produced for title 1"))
    assert not is_disc_retry_exhaustion_failure(ValueError("other"))


def test_alert_retries_exhausted_and_eject(monkeypatch: pytest.MonkeyPatch) -> None:
    ejected: list[str] = []

    def fake_eject(device: str, dry_run: bool = False) -> bool:
        _ = dry_run
        ejected.append(device)
        return True

    monkeypatch.setattr("videosripper.nas.eject_disc", fake_eject)
    ok = alert_retries_exhausted_and_eject(
        "dev:/dev/sr0",
        DiscOpenError("Failed to open disc"),
        label="disc preflight",
        retries=3,
    )
    assert ok is True
    assert ejected == ["/dev/sr0"]


def test_preflight_ejects_only_after_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    ejected: list[str] = []

    def fake_once(device: str, minlength: int, *, timeout_sec: int) -> int:
        _ = (device, minlength, timeout_sec)
        calls["n"] += 1
        raise DiscOpenError(f"Failed to open disc attempt {calls['n']}")

    def fake_eject(device: str, dry_run: bool = False) -> bool:
        _ = dry_run
        ejected.append(device)
        return True

    monkeypatch.setattr(
        "videosripper.monitor._preflight_disc_open_once",
        fake_once,
    )
    monkeypatch.setattr("videosripper.nas.eject_disc", fake_eject)

    with pytest.raises(DiscOpenError, match="attempt 4"):
        preflight_disc_open(
            "dev:/dev/sr0",
            300,
            timeout_sec=60,
            retries=3,
            delay_sec=0.0,
        )
    assert calls["n"] == 4
    assert ejected == ["/dev/sr0"]


def test_preflight_no_eject_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    ejected: list[str] = []

    def fake_once(device: str, minlength: int, *, timeout_sec: int) -> int:
        _ = (device, minlength, timeout_sec)
        raise DiscOpenError("Failed to open disc")

    def fake_eject(device: str, dry_run: bool = False) -> bool:
        _ = dry_run
        ejected.append(device)
        return True

    monkeypatch.setattr(
        "videosripper.monitor._preflight_disc_open_once",
        fake_once,
    )
    monkeypatch.setattr("videosripper.nas.eject_disc", fake_eject)

    with pytest.raises(DiscOpenError):
        preflight_disc_open(
            "dev:/dev/sr0",
            300,
            timeout_sec=60,
            retries=1,
            delay_sec=0.0,
            eject_on_exhaust=False,
        )
    assert ejected == []
