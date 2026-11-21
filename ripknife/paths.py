"""Path resolution with environment overrides.

The install root is the directory that contains this package. Personal
library files live outside the tool repo: ``RIPKNIFE_CONFIG_DIR`` if set,
otherwise a sibling checkout named ``ripknife-configs`` when that directory
exists. Tracked ``configs/`` is the fallback (examples and defaults).

Generated manifests are archived under that sibling checkout for reference.
The rip queue stays ``staging/ready``. This module does not push.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("ripknife.paths")

INSTALL_ROOT = Path(__file__).resolve().parents[1]

PLACEHOLDER_NAS = "/path/to/library/Anime/Example Show"
PLACEHOLDER_ANIME = "/path/to/library/Anime"
PLACEHOLDER_SHOWS = "/path/to/library/Shows"
PLACEHOLDER_MOVIES = "/path/to/library/Movies"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def config_search_dirs() -> list[Path]:
    """Personal config directory first, then the tracked ``configs/`` tree."""
    dirs: list[Path] = []
    if "RIPKNIFE_CONFIG_DIR" in os.environ:
        raw = os.environ["RIPKNIFE_CONFIG_DIR"].strip()
        if raw:
            dirs.append(Path(raw))
    else:
        sibling = INSTALL_ROOT.parent / "ripknife-configs"
        if sibling.is_dir():
            dirs.append(sibling)
    tracked = INSTALL_ROOT / "configs"
    tracked_resolved = tracked.resolve()
    if not any(item.resolve() == tracked_resolved for item in dirs):
        dirs.append(tracked)
    return dirs


def resolve_config_file(name: str | Path) -> Path:
    """Find a config file. Personal dir wins; tracked ``configs/`` is the fallback.

    ``name`` may be ``youtube.yaml``, ``shows/example.yaml``, or
    ``configs/shows/example.yaml``.
    """
    rel = Path(name)
    if rel.is_absolute():
        return rel
    parts = rel.parts
    if parts and parts[0] == "configs":
        rel = Path(*parts[1:]) if len(parts) > 1 else Path()
    for directory in config_search_dirs():
        candidate = directory / rel
        if candidate.is_file():
            return candidate
    return INSTALL_ROOT / "configs" / rel


def _site_values() -> dict[str, str]:
    if "RIPKNIFE_SITE" in os.environ:
        return _read_env_file(Path(os.environ["RIPKNIFE_SITE"]))
    for directory in config_search_dirs():
        candidate = directory / "site.env"
        if candidate.is_file():
            return _read_env_file(candidate)
    return {}


def _pick(env_key: str, fallback: str, site: dict[str, str]) -> str:
    env = os.environ.get(env_key)
    if env:
        return env
    value = site.get(env_key)
    if value:
        return value
    return fallback


_SITE = _site_values()

ROOT = Path(_pick("RIPKNIFE_ROOT", str(INSTALL_ROOT), _SITE))
OUTPUT_ROOT = Path(os.environ.get("RIPKNIFE_OUTPUT", ROOT / "output"))
STAGING_ROOT = Path(os.environ.get("RIPKNIFE_STAGING", ROOT / "staging"))
# Ephemeral work on the install drive — not the /tmp tmpfs. Override with RIPKNIFE_TMP.
TMP_ROOT = Path(os.environ.get("RIPKNIFE_TMP", ROOT / ".tmp"))
# make watch-all → $ROOT/logs. systemd user units set RIPKNIFE_LOGS=/var/log/ripknife.
LOG_ROOT = Path(os.environ.get("RIPKNIFE_LOGS", ROOT / "logs"))
NAS_SHARE = Path(_pick("RIPKNIFE_NAS", PLACEHOLDER_NAS, _SITE))
ANIME_NAS = Path(_pick("RIPKNIFE_ANIME_NAS", PLACEHOLDER_ANIME, _SITE))
SHOWS_NAS = Path(_pick("RIPKNIFE_SHOWS_NAS", PLACEHOLDER_SHOWS, _SITE))
MOVIES_NAS = Path(_pick("RIPKNIFE_MOVIES_NAS", PLACEHOLDER_MOVIES, _SITE))
MAKEMKV_CONF = Path.home() / ".config/environment.d/makemkv.conf"
MAKEMKV_BIN = Path.home() / ".local/bin/makemkvcon"
DEFAULT_DEVICE = os.environ.get("RIPKNIFE_DEVICE", "dev:/dev/sr0")


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root = base or ROOT
    return root / path


def ensure_tmp_root() -> Path:
    """Create and return TMP_ROOT (install-drive temp; never rely on /tmp tmpfs)."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT


def configure_process_tmp() -> Path:
    """Point process-wide temp (TMPDIR, Java) at TMP_ROOT."""
    root = ensure_tmp_root()
    tmp = str(root)
    for key in ("TMPDIR", "TMP", "TEMP", "JAVA_TMPDIR"):
        os.environ.setdefault(key, tmp)
    return root


def generated_archive_dir() -> Path | None:
    """Reference archive for generated manifests. Not the rip queue.

    ``RIPKNIFE_CONFIG_ARCHIVE`` set to an empty string disables the copy.
    A non-empty value is the archive root. When unset, a sibling
    ``ripknife-configs`` directory is used when it exists.
    """
    if "RIPKNIFE_CONFIG_ARCHIVE" in os.environ:
        raw = os.environ["RIPKNIFE_CONFIG_ARCHIVE"].strip()
        return Path(raw) if raw else None
    sibling = INSTALL_ROOT.parent / "ripknife-configs"
    return sibling if sibling.is_dir() else None


def archive_generated_manifest(src: Path) -> Path | None:
    """Copy one generated manifest into ``<archive>/generated``. Never pushes.

    A missing archive or a write error leaves the rip queue file in place.
    """
    root = generated_archive_dir()
    if root is None or not src.is_file():
        return None
    try:
        dest_dir = root / "generated"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(src.read_bytes())
        tmp.replace(dest)
    except OSError:
        logger.warning("config archive copy failed for %s", src.name)
        return None
    return dest


def disc_dev(device: str) -> str:
    """Return block device path from MakeMKV device string (e.g. dev:/dev/sr0)."""
    if device.startswith("dev:"):
        return device[4:]
    return device
