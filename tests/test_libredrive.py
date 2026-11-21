"""Tests for LibreDrive detection parsing."""

from __future__ import annotations

from ripknife.makemkv import (
    LibreDriveStatus,
    format_libredrive_status,
    parse_libredrive_output,
)

_ENABLED = """
DRV:0,0,999,0,"DriveCo","SLOT-DRIVE","1.04","/dev/sr0"
MSG:1011,0,1,"Using LibreDrive mode"
TCOUNT:42
"""

_ABSENT = """
DRV:0,0,999,0,"DriveCo","SLOT-DRIVE","1.04","/dev/sr0"
TCOUNT:42
"""

_NO_DISC = """
DRV:0,0,999,0,"DriveCo","SLOT-DRIVE","1.04","/dev/sr0"
"""

_NO_DISC_TCOUNT0 = """
DRV:0,0,999,0,"DriveCo","SLOT-DRIVE","1.04","/dev/sr0"
TCOUNT:0
"""


def test_parse_libredrive_enabled() -> None:
    status = parse_libredrive_output(_ENABLED, device="dev:/dev/sr0")
    assert status == LibreDriveStatus(
        enabled=True,
        state="enabled",
        message="Using LibreDrive mode",
        drive_name="DriveCo SLOT-DRIVE (1.04)",
        raw_msg_line='MSG:1011,0,1,"Using LibreDrive mode"',
        device="dev:/dev/sr0",
    )


def test_parse_libredrive_not_detected_with_disc() -> None:
    status = parse_libredrive_output(_ABSENT, device="dev:/dev/sr0")
    assert status.enabled is False
    assert status.state == "disabled"
    assert status.drive_name == "DriveCo SLOT-DRIVE (1.04)"
    assert "MSG:1011" in status.message


def test_parse_libredrive_no_disc_empty_tray() -> None:
    status = parse_libredrive_output(_NO_DISC, device="dev:/dev/sr0")
    assert status.enabled is None
    assert status.state == "no_disc"
    assert status.drive_name == "DriveCo SLOT-DRIVE (1.04)"


def test_parse_libredrive_no_disc_tcount_zero() -> None:
    status = parse_libredrive_output(_NO_DISC_TCOUNT0, device="dev:/dev/sr0")
    assert status.enabled is None
    assert status.state == "no_disc"


def test_format_libredrive_status_enabled() -> None:
    status = parse_libredrive_output(_ENABLED, device="dev:/dev/sr0")
    text = format_libredrive_status(status)
    assert "LibreDrive: ENABLED" in text
    assert "DriveCo SLOT-DRIVE" in text
