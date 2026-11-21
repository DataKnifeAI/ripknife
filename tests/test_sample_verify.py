"""Tests for pre-rip sample preview."""

from __future__ import annotations

from pathlib import Path

import pytest

from ripknife.sample_verify import (
    SampleVerifyResult,
    analyze_sample_slice,
    build_title_spec,
    preview_title,
)


def test_build_title_spec_plain() -> None:
    assert build_title_spec(3) == "3"


def test_build_title_spec_single_chapter() -> None:
    assert build_title_spec(5, chapter=2) == "5:2"


def test_build_title_spec_chapter_range() -> None:
    assert build_title_spec(5, chapter=2, chapter_end=4) == "5:2-4"


def test_analyze_sample_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sample = tmp_path / "sample.mkv"
    sample.write_bytes(b"x")

    monkeypatch.setattr("ripknife.sample_verify.ffmpeg_ops.probe_duration", lambda _p: 45)
    monkeypatch.setattr(
        "ripknife.sample_verify.ffmpeg_ops.black_duration_ratio", lambda _p: 0.1
    )
    monkeypatch.setattr(
        "ripknife.sample_verify.ffmpeg_ops.silence_duration_ratio", lambda _p: 0.05
    )

    result = analyze_sample_slice(sample, expected_seconds=45)
    assert result.ok
    assert result.black_ratio == 0.1
    assert result.silence_ratio == 0.05


def test_analyze_sample_fails_on_black_ratio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sample = tmp_path / "sample.mkv"
    sample.write_bytes(b"x")

    monkeypatch.setattr("ripknife.sample_verify.ffmpeg_ops.probe_duration", lambda _p: 45)
    monkeypatch.setattr(
        "ripknife.sample_verify.ffmpeg_ops.black_duration_ratio", lambda _p: 0.95
    )
    monkeypatch.setattr(
        "ripknife.sample_verify.ffmpeg_ops.silence_duration_ratio", lambda _p: 0.0
    )

    result = analyze_sample_slice(sample, expected_seconds=45)
    assert not result.ok
    assert any("black/static" in r for r in result.reasons)


def test_preview_title_orchestration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    ripped = staging / "ripped.mkv"
    ripped.write_bytes(b"rip")

    monkeypatch.setattr(
        "ripknife.sample_verify.rip_preview_title",
        lambda *_a, **_k: ripped,
    )
    monkeypatch.setattr(
        "ripknife.sample_verify.ffmpeg_ops.extract_slice",
        lambda src, dest, **kwargs: dest.write_bytes(b"slice"),
    )
    monkeypatch.setattr(
        "ripknife.sample_verify.analyze_sample_slice",
        lambda *_a, **_k: SampleVerifyResult(ok=True, duration_sec=30),
    )

    result = preview_title(
        "dev:/dev/sr0",
        2,
        seconds=30,
        offset=10,
        staging=staging,
        keep=False,
    )
    assert result.ok
    assert result.duration_sec == 30
    assert not ripped.exists()


def test_preview_title_keep_sample(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    ripped = staging / "ripped.mkv"
    ripped.write_bytes(b"rip")

    def _extract(src: Path, dest: Path, **kwargs) -> None:
        dest.write_bytes(b"slice")

    monkeypatch.setattr(
        "ripknife.sample_verify.rip_preview_title",
        lambda *_a, **_k: ripped,
    )
    monkeypatch.setattr("ripknife.sample_verify.ffmpeg_ops.extract_slice", _extract)
    monkeypatch.setattr(
        "ripknife.sample_verify.analyze_sample_slice",
        lambda *_a, **_k: SampleVerifyResult(ok=True, duration_sec=45),
    )

    result = preview_title(
        "dev:/dev/sr0",
        1,
        seconds=45,
        offset=60,
        staging=staging,
        keep=True,
    )
    assert result.ok
    assert result.sample_path is not None
    assert result.sample_path.is_file()


def test_cli_preview_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from ripknife.cli import app

    monkeypatch.setattr(
        "ripknife.cli.preview_title",
        lambda *_a, **_k: SampleVerifyResult(
            ok=True,
            duration_sec=45,
            black_ratio=0.05,
            silence_ratio=0.02,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["preview", "--title", "1", "--device", "dev:/dev/sr0", "--offset", "0"],
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_cli_preview_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from ripknife.cli import app

    monkeypatch.setattr(
        "ripknife.cli.preview_title",
        lambda *_a, **_k: SampleVerifyResult(
            ok=False,
            reasons=("sample 90% black/static — likely FBI loop, menu, or wrong title",),
        ),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["preview", "--title", "0"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
