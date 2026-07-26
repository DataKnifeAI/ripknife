"""Unit tests for videosripper.paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from videosripper import paths


def test_resolve_path_absolute(tmp_path: Path) -> None:
    absolute = tmp_path / "output" / "Season 8"
    assert paths.resolve_path(absolute) == absolute


def test_resolve_path_relative_uses_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    assert paths.resolve_path("output/episode.mkv") == tmp_path / "output/episode.mkv"


def test_resolve_path_relative_uses_custom_base(tmp_path: Path) -> None:
    base = tmp_path / "custom"
    assert paths.resolve_path("nested/file.log", base=base) == base / "nested/file.log"


def test_disc_dev_strips_dev_prefix() -> None:
    assert paths.disc_dev("dev:/dev/sr0") == "/dev/sr0"


def test_disc_dev_passthrough() -> None:
    assert paths.disc_dev("/dev/sr0") == "/dev/sr0"


def test_default_device_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEOSRIPPER_DEVICE", "dev:/dev/sr1")
    import importlib

    importlib.reload(paths)
    try:
        assert paths.DEFAULT_DEVICE == "dev:/dev/sr1"
    finally:
        monkeypatch.delenv("VIDEOSRIPPER_DEVICE", raising=False)
        importlib.reload(paths)


def test_movies_nas_default() -> None:
    assert paths.MOVIES_NAS == Path("/mnt/tn-sas/Movies")


def test_ensure_tmp_root_creates_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "game-tmp"
    monkeypatch.setattr(paths, "TMP_ROOT", target)
    assert not target.exists()
    assert paths.ensure_tmp_root() == target
    assert target.is_dir()


def test_tmp_root_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIDEOSRIPPER_TMP", str(tmp_path / "custom-tmp"))
    import importlib

    importlib.reload(paths)
    try:
        assert paths.TMP_ROOT == tmp_path / "custom-tmp"
    finally:
        monkeypatch.delenv("VIDEOSRIPPER_TMP", raising=False)
        importlib.reload(paths)
