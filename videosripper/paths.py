"""Path resolution with environment overrides."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("VIDEOSRIPPER_ROOT", "/mnt/game1/VideosRipper"))
OUTPUT_ROOT = Path(os.environ.get("VIDEOSRIPPER_OUTPUT", ROOT / "output"))
STAGING_ROOT = Path(os.environ.get("VIDEOSRIPPER_STAGING", ROOT / "staging"))
LOG_ROOT = Path(os.environ.get("VIDEOSRIPPER_LOGS", ROOT / "logs"))
NAS_SHARE = Path(
    os.environ.get("VIDEOSRIPPER_NAS", "/mnt/tn-sas/Anime/Spongebob Squarepants")
)
SHOWS_NAS = Path(os.environ.get("VIDEOSRIPPER_SHOWS_NAS", "/mnt/tn-sas/Shows"))
MOVIES_NAS = Path(os.environ.get("VIDEOSRIPPER_MOVIES_NAS", "/mnt/tn-sas/Movies"))
MAKEMKV_CONF = Path.home() / ".config/environment.d/makemkv.conf"
MAKEMKV_BIN = Path.home() / ".local/bin/makemkvcon"
DEFAULT_DEVICE = os.environ.get("VIDEOSRIPPER_DEVICE", "dev:/dev/sr0")


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root = base or ROOT
    return root / path


def disc_dev(device: str) -> str:
    """Return block device path from MakeMKV device string (e.g. dev:/dev/sr0)."""
    if device.startswith("dev:"):
        return device[4:]
    return device
