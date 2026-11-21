"""Optical drive discovery and device-string helpers.

A MakeMKV device string looks like ``dev:/dev/sr0``. The block device is
``/dev/sr0`` and the slug (for lock/state names) is ``sr0``.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

from ripknife.paths import DEFAULT_DEVICE, disc_dev


def discover_optical_devices() -> list[str]:
    """Return MakeMKV device strings for present ``/dev/sr*`` block devices.

    Sorted by index (sr0, sr1, …). Empty when no optical drives are present.
    """
    paths = glob.glob("/dev/sr[0-9]*")

    def _index(path: str) -> int:
        m = re.search(r"(\d+)$", path)
        return int(m.group(1)) if m else 0

    return [f"dev:{p}" for p in sorted(paths, key=_index)]


def device_slug(device: str) -> str:
    """Filesystem-safe slug for a device string.

    ``dev:/dev/sr0`` -> ``sr0``. Falls back to a sanitized form for
    non-standard device strings.
    """
    block = disc_dev(device)
    name = Path(block).name or device
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    return slug or "disc"


def normalize_device(device: str) -> str:
    """Ensure a device string carries the ``dev:`` prefix for block paths."""
    if device.startswith(("dev:", "disc:", "iso:", "file:")):
        return device
    if device.startswith("/dev/"):
        return f"dev:{device}"
    return device


def resolve_watch_devices(
    devices: list[str] | None,
    *,
    auto: bool,
) -> list[str]:
    """Resolve the list of devices a watcher should monitor.

    - ``auto=True``: discover all present ``/dev/sr*`` (fallback to default if none).
    - explicit ``devices``: normalized as given.
    - neither: single default device (preserves single-drive behavior).
    """
    if auto:
        found = discover_optical_devices()
        return found or [DEFAULT_DEVICE]
    if devices:
        seen: list[str] = []
        for dev in devices:
            norm = normalize_device(dev)
            if norm not in seen:
                seen.append(norm)
        return seen
    return [DEFAULT_DEVICE]
