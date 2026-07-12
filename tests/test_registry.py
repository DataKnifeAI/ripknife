"""Unit tests for videosripper.registry."""

from __future__ import annotations

from pathlib import Path

import yaml

from videosripper.registry import RegistryEntry, load_registry, manifest_matches_scan


def _write_registry(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")
    return path


def test_registry_entry_exact_match() -> None:
    entry = RegistryEntry(
        id="breaking-bad-s1-disc1",
        manifest=Path("/mnt/game1/VideosRipper/configs/shows/breaking-bad-s1-disc1.yaml"),
        patterns=("Breaking Bad: Season 1: Disc 1",),
    )
    assert entry.matches("Breaking Bad: Season 1: Disc 1")
    assert entry.matches("breaking bad: season 1: disc 1")
    assert not entry.matches("Breaking Bad: Season 2: Disc 1")


def test_registry_entry_regex_match() -> None:
    entry = RegistryEntry(
        id="naruto-set10-d2",
        manifest=Path("/tmp/naruto.yaml"),
        patterns=(r"(?i)naruto shippuden.*256.*262",),
    )
    assert entry.matches("Naruto Shippuden 256 - 262")
    assert not entry.matches("Naruto Shippuden 249 - 255")


def test_load_registry_lookup(tmp_path: Path, monkeypatch) -> None:
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "id": "breaking-bad-s1-disc1",
                "manifest": "configs/shows/breaking-bad-s1-disc1.yaml",
                "match": ["Breaking Bad: Season 1: Disc 1"],
            }
        ],
    )
    registry = load_registry(registry_path)
    hit = registry.lookup_disc("Breaking Bad: Season 1: Disc 1")
    assert hit is not None
    assert hit.id == "breaking-bad-s1-disc1"
    assert registry.lookup_disc("Unknown Disc") is None


def test_manifest_matches_scan_via_disc_name_meta() -> None:
    assert manifest_matches_scan(
        disc_name="Breaking Bad: Season 1: Disc 1",
        manifest_path=Path("configs/shows/breaking-bad-s1-disc1.yaml"),
        manifest_id="breaking-bad-s1-disc1",
        disc_name_meta="Breaking Bad: Season 1: Disc 1",
    )


def test_manifest_matches_scan_via_disc_pattern() -> None:
    assert manifest_matches_scan(
        disc_name="GHOST_RIDER",
        manifest_path=Path("configs/movies/ghost-rider.yaml"),
        manifest_id="ghost-rider",
        disc_pattern="GHOST_RIDER",
    )
