"""Tests for videosripper.stitch."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from videosripper import paths, stitch
from videosripper.models import MovieManifest, load_manifest, stitch_group_id, stitch_output_path


def _stitch_manifest(part: int, *, copy_to_nas: bool = True) -> MovieManifest:
    return MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {
                "id": f"pearl-harbor-2001-disc{part}",
                "title": "Pearl Harbor (2001)",
                "part": part,
            },
            "output": {
                "dir": "Movies",
                "filename": f"Pearl Harbor (2001) - Disc {part}.mkv",
            },
            "nas": {"movies": True, "share": "/mnt/nas/Movies"},
            "post": {"copy_to_nas": False},
            "stitch": {
                "enabled": True,
                "group_id": "pearl-harbor-2001",
                "expected_parts": 2,
                "output_filename": "Pearl Harbor (2001).mkv",
                "copy_to_nas": copy_to_nas,
            },
        }
    )


def test_load_pearl_harbor_manifests() -> None:
    root = Path(__file__).resolve().parents[1]
    m1 = load_manifest(root / "configs/movies/pearl-harbor-2001-disc1.yaml")
    m2 = load_manifest(root / "configs/movies/pearl-harbor-2001-disc2.yaml")
    assert isinstance(m1, MovieManifest)
    assert m1.meta.part == 1
    assert m1.stitch is not None
    assert m1.stitch.expected_parts == 2
    assert m1.post.copy_to_nas is False
    assert m2.meta.part == 2
    assert stitch_group_id(m1) == "pearl-harbor-2001"


def test_stitch_requires_meta_part() -> None:
    with pytest.raises(ValueError, match="meta.part"):
        MovieManifest.model_validate(
            {
                "kind": "movie",
                "meta": {"id": "test-disc1", "title": "Test (2020)"},
                "output": {"dir": "Movies", "filename": "Test (2020) - Disc 1.mkv"},
                "stitch": {"enabled": True, "expected_parts": 2},
            }
        )


def test_discover_part_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    movies = tmp_path / "output" / "Movies"
    movies.mkdir(parents=True)
    (movies / "Pearl Harbor (2001) - Disc 1.mkv").write_bytes(b"x" * 100)
    (movies / "Pearl Harbor (2001) - Disc 2.mkv").write_bytes(b"y" * 100)

    manifest = _stitch_manifest(1)
    ready, parts = stitch.parts_ready(manifest)
    assert ready is True
    assert len(parts) == 2
    assert parts[1].name.endswith("Disc 1.mkv")
    assert parts[2].name.endswith("Disc 2.mkv")


def test_maybe_stitch_waiting_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(stitch, "STITCH_STATE_DIR", tmp_path / "stitch")
    movies = tmp_path / "output" / "Movies"
    movies.mkdir(parents=True)
    part1 = movies / "Pearl Harbor (2001) - Disc 1.mkv"
    part1.write_bytes(b"x" * 100)

    stages: list[tuple[str, str]] = []

    def capture(stage: str, *, process: str | None = None, detail: str = "", **kw: object) -> None:
        stages.append((stage, detail))

    monkeypatch.setattr(stitch, "log_stage", capture)

    manifest = _stitch_manifest(1)
    result = stitch.maybe_stitch_after_part(manifest, part1)
    assert result.ok
    assert "waiting" in result.message
    assert stages[0][0] == "STITCH_WAITING"
    assert "1/2" in stages[0][1]

    state = json.loads((tmp_path / "stitch" / "pearl-harbor-2001.json").read_text())
    assert state["parts"]["1"] == str(part1.resolve())


def test_stitch_movie_parts_mkvmerge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parts = [tmp_path / "a.mkv", tmp_path / "b.mkv"]
    for p in parts:
        p.write_bytes(b"mkv")
    out = tmp_path / "combined.mkv"

    monkeypatch.setattr(stitch, "which", lambda name: "/usr/bin/mkvmerge" if name == "mkvmerge" else None)
    monkeypatch.setattr(stitch, "_validate_codec_params", lambda _: None)
    monkeypatch.setattr(stitch, "_validate_duration", lambda *a, **k: None)

    with patch.object(stitch.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = stitch.stitch_movie_parts(out, parts)
        assert result == out
        args = run.call_args[0][0]
        assert args[0] == "mkvmerge"
        assert args[2] == str(out)
        assert str(parts[0]) in args
        assert "+" in args


def test_stitch_movie_parts_ffmpeg_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parts = [tmp_path / "a.mkv", tmp_path / "b.mkv"]
    for p in parts:
        p.write_bytes(b"mkv")
    out = tmp_path / "combined.mkv"

    def fake_which(name: str) -> str | None:
        return "/usr/bin/ffmpeg" if name == "ffmpeg" else None

    monkeypatch.setattr(stitch, "which", fake_which)
    monkeypatch.setattr(stitch, "_validate_codec_params", lambda _: None)
    monkeypatch.setattr(stitch, "_validate_duration", lambda *a, **k: None)

    with patch.object(stitch.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        stitch.stitch_movie_parts(out, parts)
        args = run.call_args[0][0]
        assert args[0] == "ffmpeg"
        assert "-f" in args
        assert "concat" in args


def test_maybe_stitch_runs_and_copies_nas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(stitch, "STITCH_STATE_DIR", tmp_path / "stitch")
    movies = tmp_path / "output" / "Movies"
    movies.mkdir(parents=True)
    part1 = movies / "Pearl Harbor (2001) - Disc 1.mkv"
    part2 = movies / "Pearl Harbor (2001) - Disc 2.mkv"
    part1.write_bytes(b"x" * 100)
    part2.write_bytes(b"y" * 100)

    stitched = stitch_output_path(_stitch_manifest(2))
    copy_calls: list[Path] = []

    def fake_stitch(out: Path, parts: list[Path], **kw: object) -> Path:
        out.write_bytes(b"stitched")
        return out

    def fake_copy(src: Path, subdir: str = "", **kw: object) -> bool:
        copy_calls.append(src)
        return True

    monkeypatch.setattr(stitch, "stitch_movie_parts", fake_stitch)
    monkeypatch.setattr(stitch, "copy_movie_to_share", fake_copy)
    monkeypatch.setattr(stitch, "log_copy_start", lambda *a, **k: None)
    monkeypatch.setattr(stitch, "log_copy_result", lambda *a, **k: None)
    monkeypatch.setattr(stitch, "log_stage", lambda *a, **k: None)

    manifest = _stitch_manifest(2)
    result = stitch.maybe_stitch_after_part(manifest, part2)
    assert result.ok
    assert result.output_path == stitched
    assert stitched.is_file()
    assert copy_calls == [stitched]


def test_maybe_stitch_skips_nas_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(paths, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(stitch, "STITCH_STATE_DIR", tmp_path / "stitch")
    movies = tmp_path / "output" / "Movies"
    movies.mkdir(parents=True)
    part1 = movies / "Pearl Harbor (2001) - Disc 1.mkv"
    part2 = movies / "Pearl Harbor (2001) - Disc 2.mkv"
    part1.write_bytes(b"x" * 100)
    part2.write_bytes(b"y" * 100)

    monkeypatch.setattr(
        stitch,
        "stitch_movie_parts",
        lambda out, parts, **kw: out.write_bytes(b"ok") or out,
    )
    copied = {"called": False}
    monkeypatch.setattr(
        stitch,
        "copy_movie_to_share",
        lambda *a, **k: copied.__setitem__("called", True) or True,
    )
    monkeypatch.setattr(stitch, "log_stage", lambda *a, **k: None)

    manifest = _stitch_manifest(2, copy_to_nas=False)
    stitch.maybe_stitch_after_part(manifest, part2)
    assert copied["called"] is False
