"""Recovery commands: argument parsing and plans. No optical hardware."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ripknife.cli import app
from ripknife.recover import (
    BUDGET_SUMMARY,
    IGNORE_READ_KEYS,
    SMALL_BAD_MAX,
    STALL_LIMIT_SEC,
    FAILURE_RETRY_NORMAL,
    FAILURE_START_RECOVERY,
    FAILURE_STOP,
    FAILURE_WAIT_REINSERT,
    LadderState,
    LadderStep,
    MapStatus,
    AlignPiece,
    Span,
    advance_ladder,
    assess_verify,
    assert_resume_paths,
    block_device,
    build_ddrescue_cmd,
    build_ffmpeg_remux_cmd,
    build_ladder_ddrescue_cmd,
    build_makemkv_cmd,
    build_align_plan,
    build_stitch_plan,
    build_verify_plan,
    default_mapfile,
    collect_verify_errors,
    verify_stderr_errors,
    device_lock_held,
    empty_span_decision,
    execute_align,
    estimate_skip,
    execute_ddrescue,
    execute_makemkv,
    makemkv_device,
    assess_drive_remux_verify,
    choose_remux_speed,
    drive_remux_reported_read_errors,
    makemkv_clock_matches,
    format_recovery_plan,
    makemkv_log_succeeded,
    last_rescued_speed,
    map_holes,
    nas_copy_allowed,
    next_recovery_step,
    next_zero_gain_streak,
    bad_sector_failure,
    decide_failure_action,
    execute_capped_step,
    normal_mode_failed,
    run_capped_sector_recovery,
    parse_ddrescue_map,
    parse_ddrescuelog_status,
    parse_skip,
    pass_should_interrupt,
    prepare_image_paths,
    probe_playlist_duration,
    readable_spans,
    recovery_blocked,
    render_recovery_report,
    render_settings,
    resolve_ddrescue,
    scoped_ignore_read_errors,
    span_copy_complete,
    split_span_once,
    under_output,
)

runner = CliRunner()

_WATCHER_FILES = (
    "ripknife/pipeline.py",
    "ripknife/runner.py",
    "ripknife/movie.py",
    "scripts/rip-watcher.py",
    "scripts/disc-watcher.py",
)


def _boom(*_args, **_kwargs):
    raise AssertionError("recovery test tried to spawn a process")


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("ripknife.recover.subprocess.run", _boom)
    monkeypatch.setattr("ripknife.recover.subprocess.Popen", _boom)


def test_help_lists_recovery_commands() -> None:
    result = runner.invoke(app, ["recover", "--help"])
    assert result.exit_code == 0, result.output
    for name in ("makemkv", "ddrescue", "ffmpeg", "stitch", "align", "auto"):
        assert name in result.stdout
    assert "Watchers never" in result.stdout


def test_stitch_help_shows_example_ranges() -> None:
    result = runner.invoke(app, ["recover", "stitch", "--help"])
    assert result.exit_code == 0, result.output
    assert "42:31-43:17" in result.stdout
    assert "44:36-45:05" in result.stdout
    assert "not a default" in result.stdout


def test_missing_device_does_not_run(no_subprocess) -> None:
    result = runner.invoke(app, ["recover", "ffmpeg", "--playlist", "34", "--output", "x.mkv"])
    assert result.exit_code != 0


def test_device_parser() -> None:
    assert block_device("dev:/dev/sr1") == "/dev/sr1"
    assert block_device("/dev/sr1") == "/dev/sr1"
    assert block_device("bluray:/dev/sr1") == "/dev/sr1"
    assert makemkv_device("/dev/sr1") == "dev:/dev/sr1"
    with pytest.raises(ValueError):
        block_device("disc:0")
    with pytest.raises(ValueError):
        block_device("iso:/tmp/disc.iso")


def test_under_output_rejects_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", root)
    assert under_output(Path("clip.mkv")) == Path(os.path.abspath(root / "clip.mkv"))
    with pytest.raises(ValueError):
        under_output(Path("/tmp/clip.mkv"))
    with pytest.raises(ValueError):
        under_output(Path("../outside.mkv"))


def test_under_output_does_not_double_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", root)
    assert under_output(Path("output/clip.mkv")) == Path(os.path.abspath(root / "clip.mkv"))
    nested = under_output(Path("output/example-disc-recovery/DISC0001.iso"))
    assert nested == Path(os.path.abspath(root / "example-disc-recovery/DISC0001.iso"))
    assert under_output(Path("clip.mkv")) == Path(os.path.abspath(root / "clip.mkv"))


def test_span_copy_complete_treats_one_second_short_as_done() -> None:
    assert span_copy_complete(239.6, 240.0)
    assert span_copy_complete(239.0, 240.0)
    assert not span_copy_complete(238.9, 240.0)
    assert not span_copy_complete(0.0, 240.0)


def test_readable_spans_cut_example_holes() -> None:
    holes = [parse_skip("42:31-43:17"), parse_skip("44:36-45:05")]
    spans = readable_spans(9000, holes)
    assert spans == (
        Span(0, 2551),
        Span(2597, 2676),
        Span(2705, 9000),
    )


def test_readable_spans_follow_arguments_not_a_builtin_disc() -> None:
    spans = readable_spans(3600, [parse_skip("10:00-11:00")])
    assert spans == (Span(0, 600), Span(660, 3600))
    assert all(span.start_sec != 2551 for span in spans)


def test_readable_spans_reject_overlap_and_past_end() -> None:
    with pytest.raises(ValueError, match="overlap"):
        readable_spans(3600, [parse_skip("10:00-20:00"), parse_skip("15:00-16:00")])
    with pytest.raises(ValueError, match="past duration"):
        readable_spans(600, [parse_skip("00:09:00-00:12:00")])


def test_unsorted_skips_are_ordered() -> None:
    spans = readable_spans(1000, [parse_skip("100-200"), parse_skip("10-20")])
    assert spans[0].start_sec == 0
    assert spans[1].start_sec == 20
    assert spans[2].start_sec == 200


def test_makemkv_cmd_is_one_title_not_backup(tmp_path: Path) -> None:
    cmd = build_makemkv_cmd("/dev/sr9", 4, tmp_path / "title", minlength=120)
    assert cmd[1] == "mkv"
    assert "backup" not in cmd
    assert "dev:/dev/sr9" in cmd
    assert "4" in cmd
    assert "--minlength=120" in cmd
    assert not any(part.startswith("iso:") for part in cmd)


def test_ddrescue_cmd_sector_size_and_map(tmp_path: Path) -> None:
    iso = tmp_path / "disc.iso"
    mapped = default_mapfile(iso)
    assert mapped.name == "disc.map"
    cmd = build_ddrescue_cmd("/dev/sr9", iso, mapped, binary=Path("ddrescue"))
    assert cmd[:4] == ["ddrescue", "-b", "2048", "-d"]
    assert "-M" not in cmd
    assert "-r" not in cmd
    assert cmd[-3:] == ["/dev/sr9", str(iso), str(mapped)]


def test_ddrescue_retrim_is_one_m_not_a_retry(tmp_path: Path) -> None:
    iso = tmp_path / "disc.iso"
    mapped = default_mapfile(iso)
    cmd = build_ddrescue_cmd(
        "/dev/sr9", iso, mapped, binary=Path("ddrescue"), retrim=True
    )
    assert cmd[:5] == ["ddrescue", "-b", "2048", "-d", "-M"]
    assert "-r" not in cmd
    assert "eject" not in cmd
    assert "sdparm" not in cmd
    assert cmd[-3:] == ["/dev/sr9", str(iso), str(mapped)]


def test_ffmpeg_remux_is_live_bluray(tmp_path: Path) -> None:
    cmd = build_ffmpeg_remux_cmd("/dev/sr9", 34, tmp_path / "out.mkv", ffmpeg="ffmpeg")
    assert "bluray:/dev/sr9" in cmd
    assert "-playlist" in cmd
    assert cmd[cmd.index("-playlist") + 1] == "34"
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    joined = " ".join(cmd)
    assert "iso:" not in joined
    assert "backup" not in joined


def test_stitch_plan_uses_given_ranges(tmp_path: Path) -> None:
    plan = build_stitch_plan(
        "dev:/dev/sr9",
        7,
        [parse_skip("10:00-11:00")],
        tmp_path / "joined.mkv",
        duration_sec=3600,
        segment_dir=tmp_path / "parts",
        ffmpeg="ffmpeg",
    )
    assert len(plan.segment_cmds) == 2
    first = list(plan.segment_cmds[0])
    second = list(plan.segment_cmds[1])
    assert "-ss" not in first
    assert first[first.index("-t") + 1] == "600.000"
    assert second[second.index("-ss") + 1] == "660.000"
    assert "bluray:/dev/sr9" in second
    assert second[second.index("-playlist") + 1] == "7"
    assert plan.concat_cmd[plan.concat_cmd.index("-c") + 1] == "copy"
    text = "\n".join(plan.display_lines())
    assert "2551" not in text
    assert "MAKEMKVCON=" in text


def test_resolve_ddrescue_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda _name: "/usr/bin/ddrescue")
    assert resolve_ddrescue() == Path("/usr/bin/ddrescue")


def test_resolve_ddrescue_falls_back_then_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda _name: None)
    binary = tmp_path / "ddrescue"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("ripknife.recover.KNOWN_DDRESCUE", binary)
    assert resolve_ddrescue() == binary

    monkeypatch.setattr("ripknife.recover.KNOWN_DDRESCUE", tmp_path / "missing")
    with pytest.raises(FileNotFoundError, match="do not vendor"):
        resolve_ddrescue()


def test_render_settings_replaces_ignore_keys_only() -> None:
    original = 'app_Key = "secret"\nio_IgnoreReadErrors = "0"\n'
    rendered = render_settings(original, IGNORE_READ_KEYS)
    assert 'app_Key = "secret"' in rendered
    assert rendered.count("io_IgnoreReadErrors") == 1
    assert 'io_IgnoreReadErrors = "1"' in rendered
    assert 'io_ErrorRetryCount = "1"' in rendered
    assert "app_BackupDecrypted" not in rendered


def test_scoped_settings_restore_and_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = tmp_path / "settings.conf"
    original = 'app_Key = "secret"\napp_DefaultSelectionString = "+sel:all"\n'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setattr("ripknife.recover.makemkvcon_pids", lambda: [])
    with scoped_ignore_read_errors(settings):
        during = settings.read_text(encoding="utf-8")
        assert 'io_IgnoreReadErrors = "1"' in during
        assert "app_BackupDecrypted" not in during
        assert 'app_Key = "secret"' in during
    assert settings.read_text(encoding="utf-8") == original

    monkeypatch.setattr("ripknife.recover.makemkvcon_pids", lambda: [99])
    with pytest.raises(RuntimeError, match="settings.conf"):
        with scoped_ignore_read_errors(settings):
            pass
    assert settings.read_text(encoding="utf-8") == original


def test_execute_makemkv_restores_preexisting_ignore_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "settings.conf"
    original = 'app_Key = "secret"\nio_IgnoreReadErrors = "1"\n'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setattr("ripknife.recover.makemkvcon_pids", lambda: [])

    def fake_run(_cmd, **_kwargs):
        assert 'io_ErrorRetryCount = "1"' in settings.read_text(encoding="utf-8")

        class _Proc:
            returncode = 0

        return _Proc()

    monkeypatch.setattr("ripknife.recover.subprocess.run", fake_run)
    monkeypatch.setattr("ripknife.makemkv.makemkv_env", lambda: {})
    dest = tmp_path / "title"
    execute_makemkv(build_makemkv_cmd("/dev/sr9", 1, dest, minlength=0), settings=settings)
    assert settings.read_text(encoding="utf-8") == original
    assert dest.is_dir()


def test_execute_refuses_while_makemkvcon_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ripknife.recover.makemkvcon_pids", lambda: [2044449])
    monkeypatch.setattr("ripknife.recover.subprocess.run", _boom)
    iso = tmp_path / "nested" / "disc.iso"
    with pytest.raises(RuntimeError, match="makemkvcon is running"):
        execute_ddrescue(["ddrescue", "-b", "2048", "-d", "/dev/sr9", str(iso), str(tmp_path / "disc.map")])
    assert not iso.parent.exists()


def test_probe_command_is_bluray_playlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ripknife.recover.ffmpeg_env", lambda: {})

    def fake_run(cmd, **_kwargs):
        assert cmd[cmd.index("-playlist") + 1] == "34"
        assert "bluray:/dev/sr9" in cmd
        assert not any(str(part).startswith("iso:") for part in cmd)

        class _Proc:
            returncode = 0
            stdout = "5400.5\n"
            stderr = ""

        return _Proc()

    monkeypatch.setattr("ripknife.recover.subprocess.run", fake_run)
    assert probe_playlist_duration("/dev/sr9", 34, ffprobe="ffprobe") == 5400.5


def test_dry_run_prints_plan_without_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda name: name)
    result = runner.invoke(
        app,
        [
            "recover",
            "stitch",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--playlist",
            "34",
            "--skip",
            "10:00-11:00",
            "--duration",
            "1:00:00",
            "--output",
            "joined.mkv",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "bluray:/dev/sr9" in result.stdout
    assert "-playlist" in result.stdout
    assert "600.000" in result.stdout
    assert "660.000" in result.stdout
    assert "joins jump" in result.stdout
    assert not (tmp_path / "joined.mkv").exists()


def test_dry_run_stitch_requires_duration(no_subprocess) -> None:
    result = runner.invoke(
        app,
        [
            "recover",
            "stitch",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--playlist",
            "1",
            "--skip",
            "1:00-2:00",
            "--output",
            "joined.mkv",
        ],
    )
    assert result.exit_code != 0
    assert "duration" in result.output.lower()


def test_ddrescue_help_states_post_stitch_order() -> None:
    result = runner.invoke(app, ["recover", "ddrescue", "--help"])
    assert result.exit_code == 0, result.output
    text = result.stdout
    assert "--retrim" in text
    assert "eject -x 25" in text
    assert "sdparm --command=speed=4500" in text
    assert "--max-read-rate" in text
    assert "does not pass -r" in text
    assert "stamper" in text
    assert "dvdisaster" in text
    assert "freemkv" in text
    assert "AnyDVD HD" in text
    assert "DISC0001" not in text


def test_dry_run_ddrescue_retrim_does_not_set_speed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda _name: "/usr/bin/ddrescue")
    result = runner.invoke(
        app,
        [
            "recover",
            "ddrescue",
            "--dry-run",
            "--retrim",
            "--device",
            "/dev/sr9",
            "--output",
            "disc.iso",
        ],
    )
    assert result.exit_code == 0, result.output
    command = next(line for line in result.stdout.splitlines() if " -b " in line and "2048" in line)
    parts = command.split()
    assert parts[:5] == ["/usr/bin/ddrescue", "-b", "2048", "-d", "-M"]
    assert "-r" not in parts
    assert not any(part == "eject" or part.endswith("/eject") for part in parts)
    assert "sdparm" not in parts
    assert "/dev/sr9" in parts
    assert not (tmp_path / "disc.iso").exists()


def test_dry_run_ddrescue_and_ffmpeg_and_makemkv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda name: f"/usr/bin/{name}")
    binary = tmp_path / "ddrescue"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("ripknife.recover.KNOWN_DDRESCUE", binary)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda _name: None)

    dd = runner.invoke(
        app,
        ["recover", "ddrescue", "--dry-run", "--device", "/dev/sr9", "--output", "disc.iso"],
    )
    assert dd.exit_code == 0, dd.output
    assert "-b" in dd.stdout and "2048" in dd.stdout
    assert "disc.iso" in dd.stdout and "disc.map" in dd.stdout
    assert "/dev/sr9" in dd.stdout

    monkeypatch.setattr("ripknife.recover.shutil.which", lambda name: name)
    ff = runner.invoke(
        app,
        [
            "recover",
            "ffmpeg",
            "--dry-run",
            "--device",
            "dev:/dev/sr9",
            "--playlist",
            "34",
            "--output",
            "feature.mkv",
        ],
    )
    assert ff.exit_code == 0, ff.output
    assert "MAKEMKVCON=" in ff.stdout
    assert "bluray:/dev/sr9" in ff.stdout
    assert "-playlist" in ff.stdout

    mk = runner.invoke(
        app,
        [
            "recover",
            "makemkv",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--title",
            "2",
            "--output",
            "title-dir",
        ],
    )
    assert mk.exit_code == 0, mk.output
    assert "mkv" in mk.stdout
    assert "backup" not in mk.stdout
    assert "io_IgnoreReadErrors" in mk.stdout
    assert "dev:/dev/sr9" in mk.stdout


def test_output_outside_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    result = runner.invoke(
        app,
        [
            "recover",
            "ffmpeg",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--playlist",
            "1",
            "--output",
            "/tmp/not-here.mkv",
        ],
    )
    assert result.exit_code != 0
    assert not Path("/tmp/not-here.mkv").exists()


def test_watchers_do_not_import_recover() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in _WATCHER_FILES:
        if relative == "ripknife/pipeline.py":
            continue
        text = (root / relative).read_text(encoding="utf-8")
        assert "ripknife.recover" not in text
        assert "recover_app" not in text
        assert "next_recovery_step" not in text
        assert "recover auto" not in text
    pipeline = (root / "ripknife/pipeline.py").read_text(encoding="utf-8")
    assert "decide_failure_action" in pipeline
    assert "run_capped_sector_recovery" in pipeline
    assert "recover_app" not in pipeline
    assert "scoped_ignore_read_errors" not in pipeline
    assert "recover auto" not in pipeline
    cli = (root / "ripknife/cli.py").read_text(encoding="utf-8")
    assert "recover_app" in cli
    source = (root / "ripknife/recover.py").read_text(encoding="utf-8")
    assert "DISC0001" not in source


def test_bad_skip_rejected_without_a_process(no_subprocess) -> None:
    result = runner.invoke(
        app,
        [
            "recover",
            "stitch",
            "--device",
            "/dev/sr9",
            "--playlist",
            "1",
            "--skip",
            "later",
            "--duration",
            "1:00:00",
            "--output",
            "joined.mkv",
        ],
    )
    assert result.exit_code != 0
    assert "START-END" in result.output


def _plan(status: MapStatus | None, state: LadderState, tmp_path: Path):
    iso = tmp_path / "disc.iso"
    mapped = tmp_path / "disc.map"
    return next_recovery_step(
        status,
        state,
        device="/dev/sr9",
        iso=iso,
        mapfile=mapped,
        binary=Path("ddrescue"),
    )


def _commands(step) -> list[str]:
    return step.command_lines()


def test_image_step_is_the_scan_and_not_a_prescan(tmp_path: Path) -> None:
    step = _plan(None, LadderState(), tmp_path)
    assert step.kind == "image"
    assert "scan" in step.reason
    assert "pre-scan" in step.reason
    cmd = list(step.ddrescue or [])
    assert cmd[:4] == ["ddrescue", "-b", "2048", "-d"]
    assert "-M" not in cmd
    assert "-r" not in cmd
    assert "-R" not in cmd
    assert step.eject is None
    text = "\n".join(_commands(step))
    assert "ffmpeg" not in text
    assert "makemkv" not in text
    assert "readom" not in text
    assert "ignore-read-errors" not in text
    assert cmd[-2:] == [str(tmp_path / "disc.iso"), str(tmp_path / "disc.map")]


def test_unfinished_resumes_are_25_then_12_then_stop(tmp_path: Path) -> None:
    status = MapStatus(non_scraped=46_522_368, bad_sector=151_552, bad_areas=8)
    first = _plan(status, LadderState(image_done=True), tmp_path)
    assert first.kind == "unfinished"
    assert first.eject == ("eject", "-x", "25", "/dev/sr9")
    assert "-M" not in (first.ddrescue or [])
    assert "-r" not in (first.ddrescue or [])
    second_state = advance_ladder(LadderState(image_done=True), first, bytes_gained=100)
    second = _plan(status, second_state, tmp_path)
    assert second.kind == "unfinished"
    assert second.eject == ("eject", "-x", "12", "/dev/sr9")
    assert second.ddrescue[-2:] == first.ddrescue[-2:]
    spent = advance_ladder(second_state, second, bytes_gained=100)
    assert spent.unfinished_passes == 2
    stopped = _plan(status, spent, tmp_path)
    assert stopped.kind == "stitch"
    assert stopped.ddrescue is None
    assert stopped.restore == ("eject", "-x", "0", "/dev/sr9")


def test_non_trimmed_is_unfinished(tmp_path: Path) -> None:
    step = _plan(
        MapStatus(non_trimmed=4096, bad_sector=100),
        LadderState(image_done=True),
        tmp_path,
    )
    assert step.kind == "unfinished"
    assert step.eject is not None
    assert step.eject[2] == "25"


def test_large_hole_retrim_is_one_m_without_r(tmp_path: Path) -> None:
    step = _plan(
        MapStatus(bad_sector=20 * 1024 * 1024, bad_areas=2),
        LadderState(image_done=True),
        tmp_path,
    )
    assert step.kind == "large_retrim"
    assert step.eject == ("eject", "-x", "12", "/dev/sr9")
    cmd = list(step.ddrescue or [])
    assert cmd[:5] == ["ddrescue", "-b", "2048", "-d", "-M"]
    assert "-r" not in cmd
    assert "-R" not in cmd


def test_small_bad_still_retrims_before_r1(tmp_path: Path) -> None:
    step = _plan(
        MapStatus(bad_sector=151_552, bad_areas=8),
        LadderState(image_done=True),
        tmp_path,
    )
    assert step.kind == "large_retrim"
    assert "-r" not in (step.ddrescue or [])
    after = _plan(
        MapStatus(bad_sector=151_552, bad_areas=8),
        LadderState(image_done=True, large_retrim_done=True),
        tmp_path,
    )
    assert after.kind == "small_retry"
    assert after.eject == ("eject", "-x", "4", "/dev/sr9")
    cmd = list(after.ddrescue or [])
    assert cmd[cmd.index("-r") + 1] == "1"
    assert cmd.count("-r") == 1
    assert "-M" in cmd
    assert "-R" not in cmd
    assert 151_552 <= SMALL_BAD_MAX


def test_large_bad_after_retrim_stops_without_r(tmp_path: Path) -> None:
    step = _plan(
        MapStatus(bad_sector=SMALL_BAD_MAX + 1, bad_areas=1),
        LadderState(image_done=True, large_retrim_done=True),
        tmp_path,
    )
    assert step.kind == "stitch"
    assert step.ddrescue is None
    assert "-r" not in " ".join(_commands(step))
    allowed = _plan(
        MapStatus(bad_sector=SMALL_BAD_MAX, bad_areas=1),
        LadderState(image_done=True, large_retrim_done=True),
        tmp_path,
    )
    assert allowed.kind == "small_retry"


def test_retry_above_one_is_rejected(tmp_path: Path) -> None:
    iso = tmp_path / "disc.iso"
    mapped = tmp_path / "disc.map"
    with pytest.raises(ValueError, match="capped at 1"):
        build_ladder_ddrescue_cmd("/dev/sr9", iso, mapped, binary=Path("ddrescue"), retry=2)
    cmd = build_ladder_ddrescue_cmd(
        "/dev/sr9", iso, mapped, binary=Path("ddrescue"), retrim=True, retry=1
    )
    assert "-R" not in cmd
    assert "--reverse" not in cmd
    assert cmd[cmd.index("-r") + 1] == "1"


def test_two_zero_gain_passes_stop_the_drive_loop(tmp_path: Path) -> None:
    assert next_zero_gain_streak(0, 0) == 1
    assert next_zero_gain_streak(1, 0) == 2
    assert next_zero_gain_streak(1, 46_522_368) == 0
    status = MapStatus(non_scraped=1000, bad_areas=1, bad_sector=1000)
    step = _plan(status, LadderState(image_done=True, zero_gain_streak=2), tmp_path)
    assert step.kind == "stitch"
    assert step.ddrescue is None
    assert "not another speed" in "\n".join(step.display_lines())


def test_stall_interrupts_after_four_minutes_without_new_bytes() -> None:
    assert STALL_LIMIT_SEC == 240
    assert not pass_should_interrupt(seconds_at_zero_bps=239, rescued_increased=False)
    assert pass_should_interrupt(seconds_at_zero_bps=240, rescued_increased=False)
    assert not pass_should_interrupt(seconds_at_zero_bps=3600, rescued_increased=True)


def test_prepare_image_paths_does_not_double_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", root)
    iso, mapped = prepare_image_paths(Path("output/disc.iso"))
    assert iso == Path(os.path.abspath(root / "disc.iso"))
    assert mapped == Path(os.path.abspath(root / "disc.map"))
    assert "output/output" not in str(iso)
    with pytest.raises(ValueError, match="second output path"):
        assert_resume_paths(iso, mapped, previous_iso=root / "other.iso")


def test_clean_map_next_step_is_drive_remux_not_iso(tmp_path: Path) -> None:
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"image-exists")
    absent = tmp_path / "missing.iso"
    status = MapStatus(rescued=48_728_506_368)
    state = LadderState(image_done=True, gain_speeds=(25, 12, 4))
    present = next_recovery_step(
        status,
        state,
        device="/dev/sr9",
        iso=iso,
        mapfile=tmp_path / "disc.map",
        binary=Path("ddrescue"),
        title=0,
    )
    missing = next_recovery_step(
        status,
        state,
        device="/dev/sr9",
        iso=absent,
        mapfile=tmp_path / "disc.map",
        binary=Path("ddrescue"),
        title=0,
    )
    for step in (present, missing):
        assert step.kind == "drive_remux"
        assert step.ddrescue is None
        assert step.restore is None
        assert step.eject == ("eject", "-x", "12", "/dev/sr9")
        assert step.makemkv is not None
        assert step.makemkv[1] == "mkv"
        assert "dev:/dev/sr9" in step.makemkv
        assert not any(str(part).startswith("iso:") for part in step.makemkv)
        assert str(iso) not in step.makemkv
        assert str(absent) not in step.makemkv
        commands = "\n".join(step.command_lines())
        assert "iso:" not in commands
        notes = "\n".join(step.notes)
        assert "not an LLM call" in notes
        assert "not iso:" in notes
        plan = format_recovery_plan(step)
        assert "LLM 4" not in plan
        assert "3 LLM calls" in plan
    assert present.makemkv[-1].endswith("disc-drive")
    assert missing.makemkv[-1].endswith("missing-drive")
    joined = " ".join(present.command_lines())
    assert "-r 1" not in joined
    assert "-r1" not in joined
    robot = present.makemkv[present.makemkv.index("-r") + 1]
    assert robot.startswith("--progress")


def test_remux_speed_keeps_last_gain_and_retries_at_four(tmp_path: Path) -> None:
    assert last_rescued_speed((25, 12, 4)) == 12
    assert last_rescued_speed((12,)) == 12
    assert last_rescued_speed((4,)) == 4
    assert last_rescued_speed(()) is None
    first = choose_remux_speed((25, 12, 4), remux_errors=0)
    assert first.speed == 12 and first.failed is False
    retry = choose_remux_speed((25, 12, 4), remux_errors=1)
    assert retry.speed == 4 and retry.failed is False
    assert choose_remux_speed((4,), remux_errors=1).failed is True
    unfinished = MapStatus(non_scraped=46_522_368, bad_sector=151_552, bad_areas=8)
    state = LadderState(image_done=True, unfinished_passes=1)
    half = _plan(unfinished, state, tmp_path)
    assert half.kind == "unfinished"
    assert half.eject == ("eject", "-x", "12", "/dev/sr9")
    gained = advance_ladder(state, half, bytes_gained=46_522_368)
    assert gained.gain_speeds == (12,)
    small = LadderState(
        image_done=True,
        unfinished_passes=2,
        large_retrim_done=True,
        gain_speeds=gained.gain_speeds,
    )
    retry_step = _plan(MapStatus(bad_sector=151_552, bad_areas=8), small, tmp_path)
    assert retry_step.kind == "small_retry"
    assert retry_step.ddrescue is not None
    assert retry_step.ddrescue[retry_step.ddrescue.index("-r") + 1] == "1"
    after = advance_ladder(small, retry_step, bytes_gained=151_552)
    assert after.gain_speeds == (12, 4)
    clean = _plan(MapStatus(rescued=48_728_506_368), after, tmp_path)
    assert clean.kind == "drive_remux"
    assert clean.eject == ("eject", "-x", "12", "/dev/sr9")
    assert "iso:" not in "\n".join(clean.command_lines())
    assert "dev:/dev/sr9" in (clean.makemkv or ())


def test_stitch_only_after_drive_remux_failed(tmp_path: Path) -> None:
    status = MapStatus(rescued=48_728_506_368)
    state = LadderState(image_done=True, gain_speeds=(12,))
    first = _plan(status, state, tmp_path)
    assert first.kind == "drive_remux"
    errored = advance_ladder(state, first, bytes_gained=0)
    retry = _plan(status, errored, tmp_path)
    assert retry.kind == "drive_remux"
    assert retry.eject == ("eject", "-x", "4", "/dev/sr9")
    failed = advance_ladder(errored, retry, bytes_gained=0)
    stitch = _plan(status, failed, tmp_path)
    assert stitch.kind == "stitch"
    assert "drive remux already failed" in "\n".join(stitch.notes)
    assert "build_align_plan" in "\n".join(stitch.notes)
    explicit = _plan(
        status,
        LadderState(image_done=True, gain_speeds=(12,), drive_remux_failed=True),
        tmp_path,
    )
    assert explicit.kind == "stitch"
    done = _plan(
        status,
        LadderState(image_done=True, gain_speeds=(12,), drive_remux_done=True),
        tmp_path,
    )
    assert done.kind == "verify"
    assert done.ddrescue is None
    assert done.makemkv is None
    notes = "\n".join(done.notes)
    assert "0.5s" in notes
    assert "zero stderr" in notes
    assert "do not stitch" in notes
    assert "NAS copy only after this verify passes" in notes
    assert "not recover align" in notes
    assert "non-monotonic" in notes
    checked = advance_ladder(
        LadderState(image_done=True, gain_speeds=(12,), drive_remux_done=True),
        done,
        bytes_gained=0,
    )
    assert checked.verify_done and not checked.stitch_done
    stop = _plan(status, checked, tmp_path)
    assert stop.kind == "stop"
    assert stop.makemkv is None
    assert "do not stitch" in stop.reason
    assert "iso:" not in "\n".join(stop.command_lines())
    still_missing = _plan(
        MapStatus(bad_sector=SMALL_BAD_MAX + 1, bad_areas=1),
        LadderState(image_done=True, large_retrim_done=True),
        tmp_path,
    )
    assert still_missing.kind == "stitch"


def test_stitch_once_then_verify_once_then_stop(tmp_path: Path) -> None:
    status = MapStatus(bad_sector=SMALL_BAD_MAX + 1, bad_areas=1)
    state = LadderState(image_done=True, large_retrim_done=True)
    stitch = _plan(status, state, tmp_path)
    assert stitch.kind == "stitch"
    assert "build_align_plan" in "\n".join(stitch.notes)
    done = advance_ladder(state, stitch, bytes_gained=0)
    verify = _plan(status, done, tmp_path)
    assert verify.kind == "verify"
    assert verify.ddrescue is None
    checked = advance_ladder(done, verify, bytes_gained=0)
    report = _plan(status, checked, tmp_path)
    assert report.kind == "stop"
    assert "report and stop" in report.reason
    assert report.ddrescue is None
    text = "\n".join(report.display_lines())
    assert "NAS copy" in text
    assert "another speed" in text


def test_map_read_is_status_not_a_drive(tmp_path: Path) -> None:
    text = "\n".join(
        [
            "# Mapfile. Created by GNU ddrescue version 1.30",
            "0x00000000  0x00001000  +",
            "0x00001000  0x00000200  /",
            "0x00001200  0x00000100  -",
            "0x00001300  0x00000050  -",
            "0x00001350  0x00000010  ?",
            "0x00001360  0x00000020  *",
        ]
    )
    status = parse_ddrescue_map(text)
    assert status.rescued == 0x1000
    assert status.non_scraped == 0x200
    assert status.non_tried == 0x10
    assert status.non_trimmed == 0x20
    assert status.bad_sector == 0x150
    assert status.bad_areas == 2
    assert map_holes(text)[0] == (0x1000, 0x1200)
    logged = parse_ddrescuelog_status(
        "rescued: 1,000 B, non-tried: 0 B, non-trimmed: 0 B\n"
        "non-scraped: 46 B, bad-sector: 151552 B, bad areas: 8\n"
    )
    assert logged.rescued == 1000
    assert logged.non_scraped == 46
    assert logged.bad_sector == 151552
    assert logged.bad_areas == 8
    (tmp_path / "disc.map").write_text(text, encoding="utf-8")
    step = _plan(status, LadderState(image_done=True), tmp_path)
    assert step.kind == "unfinished"


def test_estimate_skip_is_cbr_and_empty_span_splits_once() -> None:
    span = estimate_skip(100, 200, feature_bytes=1000, duration_sec=100)
    assert span == Span(10, 20)
    assert empty_span_decision(nbytes=0, already_split=False) == "split_once"
    assert empty_span_decision(nbytes=0, already_split=True) == "skip"
    assert empty_span_decision(nbytes=12, already_split=True) == "keep"
    left, right = split_span_once(Span(0, 10))
    assert (left.start_sec, left.end_sec, right.start_sec, right.end_sec) == (0, 5, 5, 10)
    assert span_copy_complete(9.2, 10.0)


def test_normal_mode_failed_reads_log_signals_only() -> None:
    assert normal_mode_failed("kernel L-EC uncorrectable error")
    assert normal_mode_failed("Failed to save title 4")
    assert normal_mode_failed("hash-check failed for the title")
    assert normal_mode_failed("read error at sector 100")
    assert not normal_mode_failed("0 read errors\nbad-sector: 0 B")
    assert not normal_mode_failed("RIP STALL at offset 9175047168")
    assert normal_mode_failed(
        "RIP STALL at offset 9175047168\nRIP STALL at offset 9175047168"
    )
    assert normal_mode_failed("stalled at the same offset")
    assert not normal_mode_failed("rip finished, 0 read errors")


def test_decide_failure_action_without_a_drive() -> None:
    sector = "Scsi error - MEDIUM ERROR:L-EC UNCORRECTABLE ERROR"
    assert decide_failure_action(sector, normal_retries_used=0) == FAILURE_RETRY_NORMAL
    assert decide_failure_action(sector, normal_retries_used=1) == FAILURE_START_RECOVERY
    assert (
        decide_failure_action("Failed to save title 6", normal_retries_used=0)
        == FAILURE_RETRY_NORMAL
    )
    assert (
        decide_failure_action("hash-check failed for the title", normal_retries_used=1)
        == FAILURE_START_RECOVERY
    )
    assert (
        decide_failure_action("read error at sector 100", normal_retries_used=0)
        == FAILURE_RETRY_NORMAL
    )
    assert (
        decide_failure_action("No MKV produced for title 6", normal_retries_used=0)
        == FAILURE_RETRY_NORMAL
    )
    assert (
        decide_failure_action(
            "L-EC UNCORRECTABLE ERROR\nNo MKV produced for title 6",
            normal_retries_used=1,
        )
        == FAILURE_START_RECOVERY
    )
    # A reinsert does not hand the same disc back to five normal rips.
    assert (
        decide_failure_action("No MKV produced for title 6", normal_retries_used=1)
        == FAILURE_START_RECOVERY
    )
    assert decide_failure_action("tray is open", normal_retries_used=0) == FAILURE_WAIT_REINSERT
    assert decide_failure_action("LIBREDRIVE — no disc", normal_retries_used=3) == (
        FAILURE_WAIT_REINSERT
    )
    assert (
        decide_failure_action("device disappeared: /dev/sr0", normal_retries_used=1)
        == FAILURE_WAIT_REINSERT
    )
    assert (
        decide_failure_action(
            "No MKV produced for title 6\nFailed to open disc",
            normal_retries_used=1,
        )
        == FAILURE_WAIT_REINSERT
    )
    assert not bad_sector_failure("Failed to open disc (no disc)")
    assert decide_failure_action("MAP_AMBIGUOUS: two films at 1:49:32") == FAILURE_STOP
    assert decide_failure_action("duration 100s vs expected 9000s — likely wrong title") == (
        FAILURE_STOP
    )
    assert decide_failure_action("identification failed for generic label") == FAILURE_STOP
    assert (
        decide_failure_action("NAS COPY FAILED — kept local rip; check permissions")
        == FAILURE_STOP
    )
    assert decide_failure_action("NAS copy: failed") == FAILURE_STOP
    assert decide_failure_action("rip finished, 0 read errors") == FAILURE_STOP
    assert decide_failure_action("stalled at the same offset") == FAILURE_STOP
    assert decide_failure_action("slot 5 failed") == FAILURE_STOP


def test_capped_recovery_runs_only_after_the_sector_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    iso = tmp_path / "disc.iso"
    calls: list[tuple[str, ...]] = []

    assert (
        run_capped_sector_recovery(
            "L-EC UNCORRECTABLE ERROR",
            normal_retries_used=0,
            device="/dev/sr9",
            iso=iso,
            makemkv_pids=[],
            run=lambda cmd: calls.append(tuple(cmd)) or "",
        )
        == ()
    )
    assert calls == []
    assert (
        run_capped_sector_recovery(
            "tray is open",
            normal_retries_used=1,
            device="/dev/sr9",
            iso=iso,
            makemkv_pids=[],
            run=lambda cmd: calls.append(tuple(cmd)) or "",
        )
        == ()
    )
    assert (
        run_capped_sector_recovery(
            "MAP_AMBIGUOUS: North Film",
            normal_retries_used=1,
            device="/dev/sr9",
            iso=iso,
            makemkv_pids=[],
        )
        == ()
    )

    planned = run_capped_sector_recovery(
        "No MKV produced for title 6\nL-EC UNCORRECTABLE ERROR",
        normal_retries_used=1,
        device="/dev/sr9",
        iso=iso,
        binary=Path("ddrescue"),
        makemkv_pids=[],
    )
    assert planned[0].kind == "image"
    assert planned[0].ddrescue is not None
    assert "-r" not in planned[0].ddrescue
    assert "-R" not in planned[0].ddrescue
    assert "-M" not in planned[0].ddrescue

    blocked = run_capped_sector_recovery(
        "L-EC UNCORRECTABLE ERROR",
        normal_retries_used=1,
        device="/dev/sr9",
        iso=iso,
        makemkv_pids=[7],
        run=lambda cmd: calls.append(tuple(cmd)) or "",
    )
    assert blocked == ()
    assert calls == []

    maps = iter(
        (
            MapStatus(rescued=100, non_scraped=1000),
            MapStatus(rescued=100, non_scraped=1000),
            MapStatus(rescued=100, non_scraped=1000),
        )
    )

    def _run(cmd: tuple[str, ...]) -> str:
        calls.append(cmd)
        return ""

    steps = run_capped_sector_recovery(
        "Failed to save title 6",
        normal_retries_used=1,
        device="/dev/sr9",
        iso=iso,
        binary=Path("ddrescue"),
        makemkv_pids=[],
        run=_run,
        read_status=lambda: next(maps),
    )
    assert [step.kind for step in steps] == ["image", "unfinished", "unfinished", "stitch"]
    assert steps[-1].ddrescue is None
    assert steps[-1].makemkv is None
    flat = [part for cmd in calls for part in cmd]
    assert "-R" not in flat
    assert "--reverse" not in flat
    assert not any(part.startswith("iso:") for part in flat)
    assert flat.count("ddrescue") == 3
    assert "-r" not in calls[0]
    assert "-M" not in calls[0]


def test_execute_capped_step_rejects_reverse_and_iso() -> None:
    reverse = LadderStep(kind="image", reason="no", ddrescue=("-R", "ddrescue"))
    with pytest.raises(ValueError, match="-R"):
        execute_capped_step(reverse, lambda cmd: "")
    iso = LadderStep(
        kind="drive_remux",
        reason="no",
        makemkv=("makemkvcon", "mkv", "iso:/tmp/disc.iso", "0", "/tmp/out"),
    )
    with pytest.raises(ValueError, match="iso:"):
        execute_capped_step(iso, lambda cmd: "")


def test_device_lock_check_does_not_create_a_file(tmp_path: Path) -> None:
    missing = tmp_path / "makemkv-sr9.lock"
    assert device_lock_held(missing) is False
    assert not missing.exists()
    lock = tmp_path / "held.lock"
    lock.write_text("", encoding="utf-8")
    fd = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert device_lock_held(lock) is True
        assert recovery_blocked(makemkv_pids=[7], lock_held=False) == "makemkvcon is running (pids 7)"
        assert recovery_blocked(makemkv_pids=[], lock_held=True) == "device lock is held"
        assert recovery_blocked(makemkv_pids=[], lock_held=False) is None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert device_lock_held(lock) is False


def test_recovery_report_records_bytes_and_skips_nas(tmp_path: Path) -> None:
    text = render_recovery_report(
        tried=["image", "unfinished eject -x 25", "unfinished eject -x 12"],
        rescued_before=48_681_832_448,
        rescued_after=48_728_354_816,
        bad_before=151_552,
        bad_after=151_552,
        non_scraped_before=46_522_368,
        non_scraped_after=0,
        gaps=(Span(2551, 2597),),
        stitch_path=tmp_path / "gapped.mkv",
        gapped=True,
    )
    assert "rescued before: 48681832448" in text
    assert "rescued after: 48728354816" in text
    assert "bad before: 151552" in text
    assert "non-scraped before: 46522368" in text
    assert "gapped stitch stays in the recovery directory" in text
    assert "NAS copy: not a recovery step" in text
    assert BUDGET_SUMMARY in text


def test_auto_dry_run_prints_budget_without_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", root)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda _name: "/usr/bin/ddrescue")

    def boom(*_args, **_kwargs):
        raise AssertionError("planner touched the lock or a process")

    monkeypatch.setattr("ripknife.recover.device_lock_held", boom)
    monkeypatch.setattr("ripknife.recover.makemkvcon_pids", boom)
    result = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--output",
            "output/disc.iso",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# dry-run" in result.stdout
    assert BUDGET_SUMMARY in result.stdout
    assert "3 LLM calls" in result.stdout
    assert "# not executed" in result.stdout
    assert "eject -x 25" not in result.stdout
    commands = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    dd = next(line for line in commands if "2048" in line)
    assert "-M" not in dd.split()
    assert "-r" not in dd.split()
    assert "ffmpeg" not in dd
    assert "makemkv" not in dd
    assert "readom" not in dd
    assert str(root / "disc.iso") in dd
    assert "output/output" not in dd
    assert not (root / "disc.iso").exists()
    assert not (root / "output" / "disc.iso").exists()


def test_auto_reads_map_and_picks_one_unfinished_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.resolve_ddrescue", lambda: Path("ddrescue"))
    mapped = tmp_path / "disc.map"
    mapped.write_text("0x0 0x100 +\n0x100 0x20 /\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "dev:/dev/sr9",
            "--output",
            "disc.iso",
            "--map",
            "disc.map",
            "--unfinished-passes",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    commands = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    eject = next(line for line in commands if line.startswith("eject "))
    assert eject == "eject -x 12 /dev/sr9"
    dd = next(line for line in commands if "2048" in line).split()
    assert dd[:4] == ["ddrescue", "-b", "2048", "-d"]
    assert "-M" not in dd
    assert "-r" not in dd
    assert dd[-3:] == ["/dev/sr9", str(tmp_path / "disc.iso"), str(mapped)]


def test_auto_clean_map_prints_drive_not_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.resolve_ddrescue", lambda: Path("ddrescue"))
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"image-exists")
    mapped = tmp_path / "disc.map"
    mapped.write_text("0x0 0xB5A000000 +\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--output",
            "disc.iso",
            "--map",
            "disc.map",
            "--gain-speed",
            "25",
            "--gain-speed",
            "12",
            "--gain-speed",
            "4",
            "--title",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# next: drive_remux" in result.stdout
    assert "eject -x 12 /dev/sr9" in result.stdout
    assert "dev:/dev/sr9" in result.stdout
    assert "not an LLM call" in result.stdout
    assert "LLM 4" not in result.stdout
    assert "3 LLM calls" in result.stdout
    assert "# not executed" in result.stdout
    commands = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    blob = "\n".join(commands)
    assert "iso:" not in blob
    assert str(iso) not in blob
    assert not any(line.split()[:1] == ["ddrescue"] for line in commands)
    mkv = next(line for line in commands if line.split()[1:2] == ["mkv"])
    assert "dev:/dev/sr9" in mkv.split()
    assert not any(part.startswith("iso:") for part in mkv.split())
    assert mkv.split()[mkv.split().index("-r") + 1].startswith("--progress")
    assert iso.read_bytes() == b"image-exists"
    failed = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--output",
            "disc.iso",
            "--map",
            "disc.map",
            "--drive-remux-failed",
        ],
    )
    assert failed.exit_code == 0, failed.output
    assert "# next: stitch" in failed.stdout
    failed_commands = [
        line for line in failed.stdout.splitlines() if line and not line.startswith("#")
    ]
    assert "iso:" not in "\n".join(failed_commands)
    assert not any(line.split()[1:2] == ["mkv"] for line in failed_commands)
    assert "dev:/dev/sr9" not in "\n".join(failed_commands)


def test_auto_help_states_it_does_not_open_the_drive() -> None:
    result = runner.invoke(app, ["recover", "auto", "--help"])
    assert result.exit_code == 0, result.output
    assert "Does not open the drive" in result.stdout
    assert "Watchers never" in result.stdout
    assert "--dry-run" in result.stdout


def _align(pieces: list[AlignPiece], dest: Path, feature: float):
    return build_align_plan(pieces, dest, feature_duration_sec=feature, ffmpeg="ffmpeg")


def test_overrun_chunk_is_trimmed_to_its_planned_span(tmp_path: Path) -> None:
    source = tmp_path / "part.mkv"
    plan = _align([AlignPiece(0, 240, 241.4, source)], tmp_path / "aligned.mkv", 240)
    action = plan.actions[0]
    assert action.kind == "trim"
    assert action.trim_cmd is not None
    assert action.trim_cmd[action.trim_cmd.index("-t") + 1] == "240.000"
    assert action.trim_cmd[action.trim_cmd.index("-c") + 1] == "copy"
    assert action.trim_cmd[action.trim_cmd.index("-i") + 1] == str(source)
    assert "libx264" not in action.trim_cmd
    assert plan.aligned_end_sec == pytest.approx(240)
    assert plan.gaps == ()


def test_overruns_align_to_the_feature_not_the_long_concat(tmp_path: Path) -> None:
    feature = 8998.61
    span = 240.0
    overruns = (241.449, 241.449, 241.449, 241.449, 240.325)
    pieces = []
    for index, actual in enumerate(overruns):
        start = span * index
        pieces.append(AlignPiece(start, start + span, actual, tmp_path / f"p{index}.mkv"))
    tail_start = span * len(overruns)
    tail = feature - tail_start
    pieces.append(AlignPiece(tail_start, feature, tail, tmp_path / "tail.mkv"))
    plan = _align(pieces, tmp_path / "aligned.mkv", feature)
    assert plan.raw_duration_sec == pytest.approx(9004.731)
    assert plan.aligned_end_sec == pytest.approx(feature)
    assert abs(plan.aligned_end_sec - 9004.731) > 1
    trim_targets = [
        action.trim_cmd[action.trim_cmd.index("-t") + 1]
        for action in plan.actions
        if action.trim_cmd is not None
    ]
    assert trim_targets == ["240.000"] * 5
    assert plan.actions[-1].kind == "keep"
    assert plan.concat_cmd[plan.concat_cmd.index("-t") + 1] == f"{feature:.3f}"
    assert plan.concat_cmd[plan.concat_cmd.index("-c") + 1] == "copy"
    assert "9004.731" not in "\n".join(plan.display_lines())
    assert plan.gaps == ()


def test_piece_more_than_one_second_short_is_a_gap_and_fails_verify(tmp_path: Path) -> None:
    source = tmp_path / "part.mkv"
    plan = _align([AlignPiece(0, 240, 238.9, source)], tmp_path / "aligned.mkv", 240)
    assert plan.gaps
    assert plan.actions[0].kind == "gap"
    assert plan.actions[0].trim_cmd is None
    text = "\n".join(plan.display_lines())
    assert "tpad" not in text
    assert "anullsrc" not in text
    result = assess_verify(
        feature_duration_sec=240,
        measured_duration_sec=238.9,
        video_streams=1,
        audio_streams=1,
        gaps=plan.gaps,
    )
    assert not result.ok
    assert not result.finished_movie
    assert nas_copy_allowed(verify_passed=False, status=MapStatus(rescued=1)) is False


def test_piece_within_one_second_under_is_kept(tmp_path: Path) -> None:
    source = tmp_path / "part.mkv"
    for actual in (239.0, 239.6):
        plan = _align([AlignPiece(0, 240, actual, source)], tmp_path / "aligned.mkv", 240)
        assert plan.gaps == ()
        assert plan.actions[0].kind == "keep"
        assert plan.actions[0].kept
        assert plan.actions[0].trim_cmd is None
        assert plan.concat_paths == (source,)


def test_duration_outside_half_second_fails_verify() -> None:
    feature = 8998.61
    miss = assess_verify(
        feature_duration_sec=feature,
        measured_duration_sec=9004.731,
        video_streams=1,
        audio_streams=1,
    )
    assert not miss.finished_movie
    edge = assess_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature + 0.5,
        video_streams=1,
        audio_streams=1,
    )
    assert edge.finished_movie
    outside = assess_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature + 0.51,
        video_streams=1,
        audio_streams=1,
    )
    assert not outside.finished_movie
    # A stitch keeps the 0.5s window even when the reference is whole seconds.
    stitch_clock = assess_verify(
        feature_duration_sec=5891.0,
        measured_duration_sec=5891.936,
        video_streams=1,
        audio_streams=1,
    )
    assert not stitch_clock.finished_movie
    missing_audio = assess_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature,
        video_streams=1,
        audio_streams=0,
    )
    assert not missing_audio.finished_movie


def test_one_frame_dts_step_is_not_a_decode_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    noise = (
        "[null] Application provided invalid, non monotonically increasing dts "
        "to muxer in stream 1: 68699192 >= 68699184\n"
        "Last message repeated 2 times\n"
    )

    def fake_run(cmd, **_kwargs):
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = noise

        return _Proc()

    monkeypatch.setattr("ripknife.recover.subprocess.run", fake_run)
    plan = build_verify_plan(
        tmp_path / "aligned.mkv",
        feature_duration_sec=240,
        boundaries=(0.0, 240.0),
        ffmpeg="ffmpeg",
    )
    decode_ok, decode_errors, spot_errors = collect_verify_errors(plan, env={})
    assert decode_ok
    assert decode_errors == ()
    assert spot_errors == ()
    passed = assess_verify(
        feature_duration_sec=8998.615,
        measured_duration_sec=8998.615,
        video_streams=1,
        audio_streams=1,
        decode_ok=decode_ok,
        decode_errors=decode_errors,
    )
    assert passed.finished_movie
    assert verify_stderr_errors(1, noise) == ("exit 1",)
    assert verify_stderr_errors(0, "Error while decoding stream\n") == (
        "Error while decoding stream",
    )
    short = assess_verify(
        feature_duration_sec=8998.615,
        measured_duration_sec=8997.924,
        video_streams=1,
        audio_streams=1,
        decode_ok=True,
    )
    assert not short.finished_movie


def test_decode_errors_fail_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failed = assess_verify(
        feature_duration_sec=240,
        measured_duration_sec=240,
        video_streams=1,
        audio_streams=1,
        decode_errors=("Error while decoding stream",),
    )
    assert not failed.ok
    assert not failed.finished_movie

    def fake_run(cmd, **_kwargs):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "Error while decoding stream"

        return _Proc()

    monkeypatch.setattr("ripknife.recover.subprocess.run", fake_run)
    plan = build_verify_plan(
        tmp_path / "aligned.mkv",
        feature_duration_sec=240,
        boundaries=(0.0, 240.0),
        ffmpeg="ffmpeg",
    )
    decode_ok, decode_errors, spot_errors = collect_verify_errors(plan, env={})
    assert decode_ok is False
    assert decode_errors
    again = assess_verify(
        feature_duration_sec=240,
        measured_duration_sec=240,
        video_streams=1,
        audio_streams=1,
        decode_ok=decode_ok,
        decode_errors=decode_errors,
        spot_ok=not spot_errors,
        spot_errors=spot_errors,
    )
    assert not again.finished_movie
    blob = "\n".join(plan.command_lines())
    assert "-map 0:v:0" in blob
    assert "-map 0:a" in blob
    assert "-f null -" in blob
    assert blob.count("-f null -") == 1 + len(plan.spot_cmds)
    assert "rsync" not in blob
    assert "/path/to/library" not in blob
    assert "eject" not in blob


def test_verify_does_not_emit_a_nas_copy(tmp_path: Path) -> None:
    plan = build_verify_plan(
        tmp_path / "aligned.mkv",
        feature_duration_sec=8998.61,
        boundaries=(0.0, 120.0, 8998.61),
        ffmpeg="ffmpeg",
    )
    commands = plan.command_lines()
    assert commands
    blob = "\n".join(commands)
    assert "rsync" not in blob
    assert "/path/to/library" not in blob
    assert not any(part == "cp" for line in commands for part in line.split())
    assert nas_copy_allowed(verify_passed=True, status=MapStatus(bad_sector=1)) is False
    assert nas_copy_allowed(verify_passed=True, status=MapStatus(rescued=48_728_506_368)) is True
    full = next(line for line in commands if "-ss" not in line)
    assert full.endswith("-map 0:v:0 -map 0:a -f null -") or "-f null -" in full
    assert commands.count(full) == 1


def test_planner_does_not_add_an_llm_call_for_align_or_verify(tmp_path: Path) -> None:
    status = MapStatus(bad_sector=SMALL_BAD_MAX + 1, bad_areas=1)
    state = LadderState(image_done=True, large_retrim_done=True)
    stitch = _plan(status, state, tmp_path)
    assert stitch.kind == "stitch"
    stitch_text = "\n".join(stitch.display_lines())
    assert "build_align_plan" in stitch_text
    assert "not an LLM call" in stitch_text
    assert stitch.ddrescue is None
    verified = advance_ladder(state, stitch, bytes_gained=0)
    verify = _plan(status, verified, tmp_path)
    assert verify.kind == "verify"
    verify_text = format_recovery_plan(verify)
    assert "build_verify_plan" in verify_text
    assert "not an LLM call" in verify_text
    assert "LLM 4" not in verify_text
    assert "align, verify" in verify_text
    assert "3 LLM calls" in verify_text
    assert "1 stitch (including align), 1 verify, 3 LLM calls" in verify_text
    assert verify.ddrescue is None
    with pytest.raises(ValueError, match="verify already used"):
        advance_ladder(advance_ladder(verified, verify, bytes_gained=0), verify, bytes_gained=0)
    with pytest.raises(ValueError, match="verify follows"):
        LadderState(verify_done=True)


def test_align_dry_run_prints_trim_concat_verify_without_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", root)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda name: name)
    result = runner.invoke(
        app,
        [
            "recover",
            "align",
            "--dry-run",
            "--duration",
            "240",
            "--piece",
            "0-240=241.4=output/part.mkv",
            "--output",
            "output/aligned.mkv",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "240.000" in result.stdout
    assert "-c copy" in result.stdout
    assert "-f null -" in result.stdout
    assert "output/output" not in result.stdout
    assert str(root / "part.mkv") in result.stdout
    assert str(root / "aligned.mkv") in result.stdout
    assert "rsync" not in result.stdout
    assert "/path/to/library" not in result.stdout
    assert "# not executed" in result.stdout
    assert "tail trim" in result.stdout
    assert not (root / "aligned.mkv").exists()
    assert not (root / "part.mkv").exists()


def test_align_refuses_existing_output_and_a_doubled_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", root)
    monkeypatch.setattr("ripknife.recover.shutil.which", lambda name: name)
    (root / "aligned.mkv").write_text("keep", encoding="utf-8")
    exists = runner.invoke(
        app,
        [
            "recover",
            "align",
            "--dry-run",
            "--duration",
            "240",
            "--piece",
            "0-240=241.4=part.mkv",
            "--output",
            "aligned.mkv",
        ],
    )
    assert exists.exit_code != 0
    assert "already exists" in exists.output
    assert (root / "aligned.mkv").read_text(encoding="utf-8") == "keep"
    outside = runner.invoke(
        app,
        [
            "recover",
            "align",
            "--dry-run",
            "--duration",
            "240",
            "--piece",
            "0-240=241.4=part.mkv",
            "--output",
            "/tmp/not-here-aligned.mkv",
        ],
    )
    assert outside.exit_code != 0
    assert not Path("/tmp/not-here-aligned.mkv").exists()


def test_auto_after_stitch_names_verify_without_another_speed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.resolve_ddrescue", lambda: Path("ddrescue"))
    mapped = tmp_path / "disc.map"
    mapped.write_text("0x0 0x1000 +\n0x1000 0x500000 -\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--output",
            "disc.iso",
            "--map",
            "disc.map",
            "--large-retrim-done",
            "--stitch-done",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# next: verify" in result.stdout
    assert "build_verify_plan" in result.stdout
    assert "not an LLM call" in result.stdout
    assert "LLM 4" not in result.stdout
    commands = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    assert not any(line.split()[:1] == ["ddrescue"] or " -b 2048 " in f" {line} " for line in commands)
    assert not any(line.split()[:1] == ["ffmpeg"] for line in commands)
    assert not any("rsync" in line or "/path/to/library" in line for line in commands)


def test_makemkv_exit_zero_is_not_success() -> None:
    failed_iso = "Failed to open disc\nTCOUNT:0\n"
    assert makemkv_log_succeeded(failed_iso) is False
    assert makemkv_log_succeeded("TCOUNT:0\n") is False
    assert makemkv_log_succeeded("Failed to open disc\nCopy complete. 1 titles saved.\n") is False
    assert makemkv_log_succeeded("0 titles saved\n") is False
    assert makemkv_log_succeeded("") is False
    assert makemkv_log_succeeded('TCOUNT:26\nTINFO:0,9,0,"2:29:58"\n') is True
    assert makemkv_log_succeeded("Title #0\n") is True
    assert makemkv_log_succeeded("File 00034.mpls was added as title #0\n") is True
    assert makemkv_log_succeeded("Copy complete. 1 titles saved.\n") is True
    assert makemkv_log_succeeded("1 title saved\n") is True


def test_drive_remux_verify_rejects_stitch_dts_line() -> None:
    feature = 100.0
    dts = (
        "[null] Application provided invalid, non monotonically increasing dts "
        "to muxer in stream 1: 68699192 >= 68699184\n"
    )
    assert verify_stderr_errors(0, dts) == ()
    clean = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature + 0.078,
        video_streams=1,
        audio_streams=2,
        decode_exit=0,
        decode_stderr="",
    )
    assert clean.finished_movie
    assert nas_copy_allowed(verify_passed=clean.ok, status=MapStatus(rescued=1)) is True
    noisy = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature,
        video_streams=1,
        audio_streams=1,
        decode_exit=0,
        decode_stderr=dts,
    )
    assert not noisy.finished_movie
    assert nas_copy_allowed(verify_passed=noisy.ok, status=MapStatus(rescued=1)) is False
    # 100.0 is a truncated MakeMKV second. 0.51s past it is the dropped fraction.
    fraction = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature + 0.51,
        video_streams=1,
        audio_streams=1,
        decode_exit=0,
        decode_stderr="",
    )
    assert fraction.finished_movie
    miss = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature + 1.05,
        video_streams=1,
        audio_streams=1,
        decode_exit=0,
        decode_stderr="",
    )
    assert not miss.finished_movie
    short = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=feature - 1.0,
        video_streams=1,
        audio_streams=1,
        decode_exit=0,
        decode_stderr="",
    )
    assert not short.finished_movie
    precise = assess_drive_remux_verify(
        feature_duration_sec=100.4,
        measured_duration_sec=100.4 + 0.51,
        video_streams=1,
        audio_streams=1,
        decode_exit=0,
        decode_stderr="",
    )
    assert not precise.finished_movie


def test_wall_e_truncated_clock_and_dirty_map_still_copy() -> None:
    """Title 3 is 1:38:11 in TINFO and 5891.936s in the container."""
    feature = 1 * 3600 + 38 * 60 + 11
    assert feature == 5891
    assert makemkv_clock_matches(5891.936, feature)
    assert makemkv_clock_matches(5891.0, feature)
    assert not makemkv_clock_matches(5892.0, feature)
    assert not makemkv_clock_matches(5890.0, feature)
    over = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=5892.0,
        video_streams=1,
        audio_streams=1,
        decode_exit=0,
        decode_stderr="",
    )
    assert not over.finished_movie
    verified = assess_drive_remux_verify(
        feature_duration_sec=feature,
        measured_duration_sec=5891.936,
        video_streams=1,
        audio_streams=3,
        decode_exit=0,
        decode_stderr="",
    )
    assert verified.finished_movie
    log = "Copy complete. 1 titles saved.\nNo read errors.\n"
    assert drive_remux_reported_read_errors(log) is False
    assert drive_remux_reported_read_errors("0 read errors\n") is False
    assert drive_remux_reported_read_errors("Copy complete. 1 titles saved.\n") is False
    assert drive_remux_reported_read_errors("2 read errors at 1234\n") is True
    dirty = MapStatus(rescued=29_054_000_000, bad_sector=45_056, bad_areas=21)
    assert dirty.unfinished_bytes() == 0
    assert nas_copy_allowed(
        verify_passed=verified.ok,
        status=dirty,
        source="drive_remux",
        remux_read_errors=drive_remux_reported_read_errors(log),
    ) is True
    assert nas_copy_allowed(
        verify_passed=True,
        status=dirty,
        source="drive_remux",
        remux_read_errors=True,
    ) is False
    assert nas_copy_allowed(verify_passed=True, status=dirty, source="stitch") is False
    assert nas_copy_allowed(
        verify_passed=True,
        status=MapStatus(rescued=1),
        source="stitch",
        gaps=True,
    ) is False
    assert nas_copy_allowed(verify_passed=True, status=MapStatus(rescued=1), source="stitch") is True


def test_auto_drive_remux_done_prints_verify_not_stitch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_subprocess
) -> None:
    monkeypatch.setattr("ripknife.recover.OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr("ripknife.recover.resolve_ddrescue", lambda: Path("ddrescue"))
    mapped = tmp_path / "disc.map"
    mapped.write_text("0x0 0xB5A000000 +\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--output",
            "disc.iso",
            "--map",
            "disc.map",
            "--gain-speed",
            "12",
            "--drive-remux-done",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# next: verify" in result.stdout
    assert "# next: stitch" not in result.stdout
    assert "0.5s" in result.stdout
    assert "fraction under 1s" in result.stdout
    assert "bad bytes outside that read" in result.stdout
    assert "zero stderr" in result.stdout
    assert "not recover align" in result.stdout
    assert "NAS copy only after this verify passes" in result.stdout
    commands = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    assert not any(line.split()[1:2] == ["mkv"] for line in commands)
    assert not any(line.split()[:1] == ["ffmpeg"] for line in commands)
    stopped = runner.invoke(
        app,
        [
            "recover",
            "auto",
            "--dry-run",
            "--device",
            "/dev/sr9",
            "--output",
            "disc.iso",
            "--map",
            "disc.map",
            "--drive-remux-done",
            "--verify-done",
        ],
    )
    assert stopped.exit_code == 0, stopped.output
    assert "# next: stop" in stopped.stdout
    assert "do not stitch" in stopped.stdout
    assert "# next: stitch" not in stopped.stdout


def test_execute_align_streams_copies_and_does_not_copy_to_nas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr("ripknife.recover.subprocess.run", fake_run)
    monkeypatch.setattr("ripknife.recover.ffmpeg_env", lambda: {})
    source = tmp_path / "part.mkv"
    plan = _align([AlignPiece(0, 240, 241.4, source)], tmp_path / "aligned.mkv", 240)
    execute_align(plan)
    blob = "\n".join(" ".join(cmd) for cmd in calls)
    assert "240.000" in blob
    assert "-c copy" in blob
    assert "rsync" not in blob
    assert "/path/to/library" not in blob
    assert "libx264" not in blob
    assert (plan.concat_list).is_file()
