"""Disc registry: map scan disc names to known manifests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from ripknife.paths import resolve_config_file


@dataclass(frozen=True)
class RegistryEntry:
    id: str
    manifest: Path
    patterns: tuple[str, ...]

    def matches(self, disc_name: str) -> bool:
        if not disc_name:
            return False
        normalized = disc_name.strip()
        for pattern in self.patterns:
            if pattern.startswith("(?") or pattern.startswith("("):
                if re.search(pattern, normalized, re.IGNORECASE):
                    return True
            elif pattern.lower() == normalized.lower():
                return True
            elif pattern.lower() in normalized.lower():
                return True
        return False


@dataclass
class Registry:
    entries: list[RegistryEntry]
    path: Path

    def lookup_disc(self, disc_name: str) -> RegistryEntry | None:
        for entry in self.entries:
            if entry.matches(disc_name):
                return entry
        return None

    def by_id(self, entry_id: str) -> RegistryEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def by_manifest(self, manifest: Path) -> RegistryEntry | None:
        resolved = _resolve_manifest(manifest)
        for entry in self.entries:
            if entry.manifest == resolved:
                return entry
        return None


def _resolve_manifest(manifest: Path) -> Path:
    path = Path(manifest)
    if path.is_absolute():
        return path.resolve()
    return resolve_config_file(path).resolve()


def default_registry_path() -> Path:
    return resolve_config_file("registry.yaml")


def load_registry(path: Path | None = None) -> Registry:
    registry_path = path or default_registry_path()
    if not registry_path.is_file():
        return Registry(entries=[], path=registry_path)

    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    entries: list[RegistryEntry] = []
    for raw in data.get("entries", []):
        entry_id = raw["id"]
        manifest = _resolve_manifest(raw["manifest"])
        patterns = tuple(raw.get("match", []))
        entries.append(RegistryEntry(id=entry_id, manifest=manifest, patterns=patterns))
    return Registry(entries=entries, path=registry_path)


def manifest_matches_scan(
    *,
    disc_name: str,
    manifest_path: Path,
    manifest_id: str,
    disc_name_meta: str | None = None,
    disc_pattern: str | None = None,
    registry: Registry | None = None,
) -> bool:
    """Return True when the scanned disc name matches the manifest expectations."""
    if disc_name_meta:
        return _names_match(disc_name_meta, disc_name)
    if disc_pattern and disc_pattern.upper() in disc_name.upper():
        return True

    reg = registry or load_registry()
    entry = reg.by_manifest(manifest_path) or reg.by_id(manifest_id)
    if entry is not None:
        return entry.matches(disc_name)
    return False


def _names_match(expected: str, actual: str) -> bool:
    return expected.strip().lower() == actual.strip().lower()
