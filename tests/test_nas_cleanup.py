"""Tests for local cleanup after verified NAS copy."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife import nas


def test_resolve_delete_local_after_nas_manifest_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIPKNIFE_DELETE_LOCAL_AFTER_NAS", raising=False)
    assert nas.resolve_delete_local_after_nas(False) is False
    assert nas.resolve_delete_local_after_nas(True) is True


def test_resolve_delete_local_after_nas_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIPKNIFE_DELETE_LOCAL_AFTER_NAS", "1")
    assert nas.resolve_delete_local_after_nas(False) is True
    monkeypatch.setenv("RIPKNIFE_DELETE_LOCAL_AFTER_NAS", "0")
    assert nas.resolve_delete_local_after_nas(True) is False


def test_verify_nas_copy_requires_matching_size(tmp_path: Path) -> None:
    src = tmp_path / "episode.mkv"
    dest = tmp_path / "nas" / "episode.mkv"
    dest.parent.mkdir()
    src.write_bytes(b"x" * 100)
    dest.write_bytes(b"x" * 100)
    assert nas.verify_nas_copy(src, dest) is True

    dest.write_bytes(b"x" * 99)
    assert nas.verify_nas_copy(src, dest) is False


def test_delete_local_after_nas_copy_disabled(tmp_path: Path) -> None:
    src = tmp_path / "episode.mkv"
    dest = tmp_path / "nas" / "episode.mkv"
    dest.parent.mkdir()
    src.write_bytes(b"data")
    dest.write_bytes(b"data")
    assert nas.delete_local_after_nas_copy(src, dest, enabled=False) is False
    assert src.is_file()


def test_delete_local_after_nas_copy_success(tmp_path: Path) -> None:
    src = tmp_path / "episode.mkv"
    dest = tmp_path / "nas" / "episode.mkv"
    dest.parent.mkdir()
    src.write_bytes(b"episode-bytes")
    dest.write_bytes(b"episode-bytes")
    assert nas.delete_local_after_nas_copy(src, dest, enabled=True) is True
    assert not src.exists()
    assert dest.is_file()


def test_delete_local_after_nas_copy_skips_on_verify_fail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    src = tmp_path / "episode.mkv"
    dest = tmp_path / "nas" / "episode.mkv"
    src.write_bytes(b"local")
    with caplog.at_level("WARNING"):
        assert nas.delete_local_after_nas_copy(src, dest, enabled=True) is False
    assert src.is_file()
    assert any("LOCAL_CLEANUP skipped" in r.message for r in caplog.records)


def test_delete_local_after_nas_copy_dry_run(tmp_path: Path) -> None:
    src = tmp_path / "episode.mkv"
    dest = tmp_path / "nas" / "episode.mkv"
    dest.parent.mkdir()
    src.write_bytes(b"data")
    dest.write_bytes(b"data")
    assert nas.delete_local_after_nas_copy(src, dest, dry_run=True, enabled=True) is True
    assert src.is_file()
