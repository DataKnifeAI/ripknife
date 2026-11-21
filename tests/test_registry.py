"""Unit tests for ripknife.registry."""

from __future__ import annotations

from pathlib import Path

import yaml

from ripknife.registry import RegistryEntry, load_registry, manifest_matches_scan


def _write_registry(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")
    return path


def test_registry_entry_exact_match() -> None:
    entry = RegistryEntry(
        id="example-show-s1-disc1",
        manifest=Path("/path/to/ripknife/configs/examples/show-s1-disc1.yaml"),
        patterns=("Example Show: Season 1: Disc 1",),
    )
    assert entry.matches("Example Show: Season 1: Disc 1")
    assert entry.matches("example show: season 1: disc 1")
    assert not entry.matches("Example Show: Season 2: Disc 1")


def test_registry_entry_regex_match() -> None:
    entry = RegistryEntry(
        id="example-set-d2",
        manifest=Path("/tmp/example.yaml"),
        patterns=(r"(?i)example anime.*256.*262",),
    )
    assert entry.matches("Example Anime 256 - 262")
    assert not entry.matches("Example Anime 249 - 255")


def test_load_registry_lookup(tmp_path: Path, monkeypatch) -> None:
    registry_path = _write_registry(
        tmp_path,
        [
            {
                "id": "example-show-s1-disc1",
                "manifest": "configs/examples/show-s1-disc1.yaml",
                "match": ["Example Show: Season 1: Disc 1"],
            }
        ],
    )
    registry = load_registry(registry_path)
    hit = registry.lookup_disc("Example Show: Season 1: Disc 1")
    assert hit is not None
    assert hit.id == "example-show-s1-disc1"
    assert registry.lookup_disc("Unknown Disc") is None


def test_manifest_matches_scan_via_disc_name_meta() -> None:
    assert manifest_matches_scan(
        disc_name="Example Show: Season 1: Disc 1",
        manifest_path=Path("configs/examples/show-s1-disc1.yaml"),
        manifest_id="example-show-s1-disc1",
        disc_name_meta="Example Show: Season 1: Disc 1",
    )


def test_manifest_matches_scan_via_disc_pattern() -> None:
    assert manifest_matches_scan(
        disc_name="EXAMPLE_MOVIE",
        manifest_path=Path("configs/examples/movie.yaml"),
        manifest_id="example-movie",
        disc_pattern="EXAMPLE_MOVIE",
    )
