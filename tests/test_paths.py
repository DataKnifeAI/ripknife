"""Unit tests for ripknife.paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ripknife import paths


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
    monkeypatch.setenv("RIPKNIFE_DEVICE", "dev:/dev/sr1")
    import importlib

    importlib.reload(paths)
    try:
        assert paths.DEFAULT_DEVICE == "dev:/dev/sr1"
    finally:
        monkeypatch.delenv("RIPKNIFE_DEVICE", raising=False)
        importlib.reload(paths)


def test_library_placeholders_without_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RIPKNIFE_SITE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("RIPKNIFE_CONFIG_DIR", str(tmp_path))
    for key in ("RIPKNIFE_NAS", "RIPKNIFE_ANIME_NAS", "RIPKNIFE_SHOWS_NAS", "RIPKNIFE_MOVIES_NAS"):
        monkeypatch.delenv(key, raising=False)
    import importlib

    importlib.reload(paths)
    try:
        assert paths.MOVIES_NAS == Path("/path/to/library/Movies")
        assert paths.ANIME_NAS == Path("/path/to/library/Anime")
        assert paths.SHOWS_NAS == Path("/path/to/library/Shows")
        assert paths.NAS_SHARE == Path("/path/to/library/Anime/Example Show")
    finally:
        monkeypatch.delenv("RIPKNIFE_SITE", raising=False)
        monkeypatch.delenv("RIPKNIFE_CONFIG_DIR", raising=False)
        importlib.reload(paths)


def test_config_dir_is_searched_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "youtube.yaml").write_text("shows: []\n", encoding="utf-8")
    monkeypatch.setenv("RIPKNIFE_CONFIG_DIR", str(personal))
    assert paths.resolve_config_file("youtube.yaml") == personal / "youtube.yaml"
    assert paths.resolve_config_file("configs/examples/movie.yaml").is_file()


def test_archive_generated_manifest_copies_into_generated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "example-movie-1991.yaml"
    src.write_text("kind: movie\n", encoding="utf-8")
    archive = tmp_path / "archive"
    monkeypatch.setenv("RIPKNIFE_CONFIG_ARCHIVE", str(archive))
    dest = paths.archive_generated_manifest(src)
    assert dest == archive / "generated" / "example-movie-1991.yaml"
    assert dest is not None
    assert dest.read_text(encoding="utf-8") == "kind: movie\n"


def test_empty_archive_env_skips_the_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "example-movie-1991.yaml"
    src.write_text("kind: movie\n", encoding="utf-8")
    monkeypatch.setenv("RIPKNIFE_CONFIG_ARCHIVE", "")
    assert paths.generated_archive_dir() is None
    assert paths.archive_generated_manifest(src) is None


def test_ensure_tmp_root_creates_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "game-tmp"
    monkeypatch.setattr(paths, "TMP_ROOT", target)
    assert not target.exists()
    assert paths.ensure_tmp_root() == target
    assert target.is_dir()


def test_log_root_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RIPKNIFE_LOGS", str(tmp_path / "var-log-ripknife"))
    import importlib

    importlib.reload(paths)
    try:
        assert paths.LOG_ROOT == tmp_path / "var-log-ripknife"
    finally:
        monkeypatch.delenv("RIPKNIFE_LOGS", raising=False)
        importlib.reload(paths)


def test_tmp_root_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RIPKNIFE_TMP", str(tmp_path / "custom-tmp"))
    import importlib

    importlib.reload(paths)
    try:
        assert paths.TMP_ROOT == tmp_path / "custom-tmp"
    finally:
        monkeypatch.delenv("RIPKNIFE_TMP", raising=False)
        importlib.reload(paths)


def test_configure_process_tmp_sets_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "game-tmp"
    monkeypatch.setattr(paths, "TMP_ROOT", target)
    for key in ("TMPDIR", "TMP", "TEMP", "JAVA_TMPDIR"):
        monkeypatch.delenv(key, raising=False)
    assert paths.configure_process_tmp() == target
    assert target.is_dir()
    for key in ("TMPDIR", "TMP", "TEMP", "JAVA_TMPDIR"):
        assert os.environ[key] == str(target)


def test_configure_process_tmp_respects_existing_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom = tmp_path / "custom-tmp"
    custom.mkdir()
    monkeypatch.setenv("TMPDIR", str(custom))
    monkeypatch.setattr(paths, "TMP_ROOT", tmp_path / "default-tmp")
    paths.configure_process_tmp()
    assert os.environ["TMPDIR"] == str(custom)
