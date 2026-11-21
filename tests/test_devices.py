"""Unit tests for ripknife.devices."""

from __future__ import annotations

import pytest

from ripknife import devices


def test_device_slug_extracts_block_name() -> None:
    assert devices.device_slug("dev:/dev/sr0") == "sr0"
    assert devices.device_slug("dev:/dev/sr12") == "sr12"


def test_device_slug_sanitizes_nonstandard() -> None:
    slug = devices.device_slug("iso:")
    assert slug and "/" not in slug and ":" not in slug


def test_normalize_device_adds_prefix() -> None:
    assert devices.normalize_device("/dev/sr1") == "dev:/dev/sr1"
    assert devices.normalize_device("dev:/dev/sr1") == "dev:/dev/sr1"
    assert devices.normalize_device("iso:/x.iso") == "iso:/x.iso"


def test_discover_optical_devices_sorted_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        devices.glob,
        "glob",
        lambda pat: ["/dev/sr10", "/dev/sr2", "/dev/sr0"],
    )
    assert devices.discover_optical_devices() == [
        "dev:/dev/sr0",
        "dev:/dev/sr2",
        "dev:/dev/sr10",
    ]


def test_resolve_watch_devices_default_single() -> None:
    assert devices.resolve_watch_devices(None, auto=False) == [devices.DEFAULT_DEVICE]


def test_resolve_watch_devices_explicit_dedup_and_normalize() -> None:
    result = devices.resolve_watch_devices(["/dev/sr0", "dev:/dev/sr0", "/dev/sr1"], auto=False)
    assert result == ["dev:/dev/sr0", "dev:/dev/sr1"]


def test_resolve_watch_devices_auto_discovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices, "discover_optical_devices", lambda: ["dev:/dev/sr0", "dev:/dev/sr1"])
    assert devices.resolve_watch_devices(None, auto=True) == ["dev:/dev/sr0", "dev:/dev/sr1"]


def test_resolve_watch_devices_auto_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices, "discover_optical_devices", lambda: [])
    assert devices.resolve_watch_devices(None, auto=True) == [devices.DEFAULT_DEVICE]
