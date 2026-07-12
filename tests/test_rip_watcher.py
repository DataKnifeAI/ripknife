"""Unit tests for scripts/rip-watcher helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_rip_watcher():
    path = ROOT / "scripts" / "rip-watcher.py"
    spec = importlib.util.spec_from_file_location("rip_watcher", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rip_watcher"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rip_watcher():
    return _load_rip_watcher()


def test_yaml_seen_key_changes_when_mtime_changes(
    rip_watcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = tmp_path / "foo.yaml"
    yaml_path.write_text("a: 1\n", encoding="utf-8")
    key1 = rip_watcher._yaml_seen_key(yaml_path)
    yaml_path.write_text("a: 2\n", encoding="utf-8")
    key2 = rip_watcher._yaml_seen_key(yaml_path)
    assert key1[0] == key2[0]
    assert key1[1] != key2[1]


def test_already_ripped_movie_detects_existing_output(
    rip_watcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from videosripper import paths
    from videosripper.models import MovieManifest

    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test-movie", "title": "Test Movie", "minlength": 300},
            "output": {"dir": "Movies", "filename": "Test Movie.mkv"},
            "validation": {"min_duration_sec": 1, "min_size_bytes": 100},
        }
    )
    out = tmp_path / "output" / "Movies" / "Test Movie.mkv"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x" * 200)
    assert rip_watcher.already_ripped(manifest) is True


def test_already_ripped_movie_detects_nas_output(
    rip_watcher, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from videosripper import paths
    from videosripper.models import MovieManifest

    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    nas_root = tmp_path / "nas-movies"
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test-movie", "title": "Test Movie", "minlength": 300},
            "output": {"dir": "Movies", "filename": "Test Movie.mkv"},
            "nas": {"movies": True, "share": str(nas_root)},
            "validation": {"min_duration_sec": 1, "min_size_bytes": 100},
        }
    )
    nas_path = nas_root / "Test Movie.mkv"
    nas_path.parent.mkdir(parents=True)
    nas_path.write_bytes(b"x" * 200)
    assert rip_watcher.already_ripped(manifest) is True
