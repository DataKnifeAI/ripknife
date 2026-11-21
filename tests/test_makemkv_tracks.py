"""Tests for MakeMKV track selection helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife.makemkv import build_selection_string, makemkv_selection_profile
from ripknife.models import StreamDefaultsConfig


def test_build_selection_string_keep_all_with_defaults() -> None:
    cfg = StreamDefaultsConfig(
        keep_all_tracks=True,
        audio_default="jpn",
        subtitle_default="eng",
    )
    assert build_selection_string(cfg) == "+sel:all,=100:all,+50:(jpn&audio),+50:(eng&subtitle)"


def test_build_selection_string_audio_only() -> None:
    cfg = StreamDefaultsConfig(keep_all_tracks=False, audio_default="jpn")
    assert build_selection_string(cfg) == "=100:all,+50:(jpn&audio)"


def test_build_selection_string_keep_all_no_subtitle_default() -> None:
    cfg = StreamDefaultsConfig(keep_all_tracks=True, audio_default="eng")
    assert build_selection_string(cfg) == "+sel:all,=100:all,+50:(eng&audio)"


def test_makemkv_selection_profile_restores_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    makemkv_dir = tmp_path / ".MakeMKV"
    makemkv_dir.mkdir()
    settings = makemkv_dir / "settings.conf"
    settings.write_text('app_Key = "test-key"\n', encoding="utf-8")
    monkeypatch.setattr("ripknife.makemkv.Path.home", lambda: tmp_path)

    with makemkv_selection_profile("+sel:all"):
        during = settings.read_text(encoding="utf-8")
        assert 'app_DefaultSelectionString = "+sel:all"' in during

    after = settings.read_text(encoding="utf-8")
    assert after == 'app_Key = "test-key"\n'
    assert "DefaultSelectionString" not in after
