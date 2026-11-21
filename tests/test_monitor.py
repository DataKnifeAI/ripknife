"""Tests for MakeMKV PRGV progress parsing and decile throttling."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ripknife import logging_setup, paths
from ripknife.monitor import (
    RipDecileTracker,
    estimate_eta_sec,
    format_eta_remaining,
    is_progress_line,
    is_save_phase_line,
    parse_prgv,
    prgv_percent,
)


def _patch_log_paths(monkeypatch: pytest.MonkeyPatch, log_root: Path) -> None:
    monkeypatch.setattr(paths, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "LOG_ROOT", log_root)
    monkeypatch.setattr(logging_setup, "PIPELINE_LOG", log_root / "pipeline.log")
    pl = logging.getLogger(logging_setup._PIPELINE_LOGGER_NAME)
    for handler in pl.handlers[:]:
        pl.removeHandler(handler)
        handler.close()


# --- parse_prgv -------------------------------------------------------------


def test_parse_prgv_returns_three_fields() -> None:
    assert parse_prgv("PRGV:857,730,65536") == (857, 730, 65536)


def test_parse_prgv_ignores_leading_whitespace() -> None:
    assert parse_prgv("   PRGV:0,0,65536") == (0, 0, 65536)


def test_parse_prgv_rejects_non_prgv_lines() -> None:
    assert parse_prgv('MSG:5014,131072,2,"Saving 1 titles"') is None
    assert parse_prgv("PRGC:5017,0,\"Analyzing\"") is None
    assert parse_prgv("") is None


# --- prgv_percent (uses the total/overall bar) ------------------------------


def test_prgv_percent_uses_total_over_max() -> None:
    # total=32768 of max=65536 → 50%
    assert prgv_percent("PRGV:12345,32768,65536") == 50


def test_prgv_percent_zero_and_full() -> None:
    assert prgv_percent("PRGV:0,0,65536") == 0
    assert prgv_percent("PRGV:65536,65536,65536") == 100


def test_prgv_percent_guards_zero_max() -> None:
    assert prgv_percent("PRGV:0,0,0") is None


def test_prgv_percent_clamps_to_100() -> None:
    assert prgv_percent("PRGV:70000,70000,65536") == 100


def test_prgv_percent_none_for_non_prgv() -> None:
    assert prgv_percent('MSG:5011,0,0,"Operation successfully completed"') is None


def test_prgv_lines_still_count_as_progress_for_stall_detection() -> None:
    assert is_progress_line("PRGV:857,730,65536") is True


# --- save-phase detection (ignore open/analyze PRGV for percent) ------------


def test_is_save_phase_line_msg_5014() -> None:
    assert is_save_phase_line(
        'MSG:5014,131072,2,"Saving 1 titles into directory file:///tmp/x"'
    )


def test_is_save_phase_line_prgt_saving() -> None:
    assert is_save_phase_line('PRGT:5024,0,"Saving all titles to MKV files"')


def test_is_save_phase_line_rejects_open_analyze() -> None:
    assert is_save_phase_line('PRGT:3404,0,"Opening Blu-ray disc"') is False
    assert is_save_phase_line('PRGC:5017,0,"Analyzing seamless segments"') is False
    assert is_save_phase_line("PRGV:65536,65536,65536") is False


# --- RipDecileTracker throttling --------------------------------------------


def test_tracker_emits_each_rising_decile_once() -> None:
    tracker = RipDecileTracker()
    emitted = [m for pct in range(0, 101) if (m := tracker.milestone_for(pct)) is not None]
    assert emitted == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]


def test_tracker_skips_zero_percent() -> None:
    # 0% is covered by the existing "ripping 1/1" line — don't double-log.
    assert RipDecileTracker().milestone_for(0) is None
    assert RipDecileTracker().milestone_for(3) is None


def test_tracker_no_duplicate_within_same_decile() -> None:
    tracker = RipDecileTracker()
    assert tracker.milestone_for(41) == 40
    assert tracker.milestone_for(42) is None
    assert tracker.milestone_for(49) is None


def test_tracker_ignores_oscillation_across_boundary() -> None:
    tracker = RipDecileTracker()
    assert tracker.milestone_for(40) == 40
    assert tracker.milestone_for(39) is None  # dipped back below boundary
    assert tracker.milestone_for(41) is None  # already logged decile 4
    assert tracker.milestone_for(50) == 50


def test_tracker_reports_only_current_decile_on_jump() -> None:
    tracker = RipDecileTracker()
    # A large jump logs the reached milestone, not every skipped boundary.
    assert tracker.milestone_for(45) == 40
    assert tracker.milestone_for(100) == 100


def test_tracker_ignores_initial_phase_boundary_100() -> None:
    """MakeMKV often sends PRGV:max,max,max at phase start before real progress."""
    tracker = RipDecileTracker()
    assert tracker.milestone_for(100) is None
    assert tracker.milestone_for(100) is None  # still no mid-decile logged
    assert tracker.milestone_for(12) == 10
    assert tracker.milestone_for(100) == 100


def test_tracker_contraband_open_then_save_sequence() -> None:
    """Early scan full-bar must not lock the tracker before save climbs."""
    tracker = RipDecileTracker()
    # CD-ROM scan phase boundary (false 100% that used to poison pipeline.log).
    assert tracker.milestone_for(prgv_percent("PRGV:65536,65536,65536") or -1) is None
    # Open/analyze overall bar climbs, then save resets — with save-phase gating
    # these open samples never reach the tracker; if they did, mid-deciles are fine.
    assert tracker.milestone_for(3) is None
    assert tracker.milestone_for(11) == 10
    assert tracker.milestone_for(42) == 40
    assert tracker.milestone_for(100) == 100


# --- log_rip_percent line format --------------------------------------------


def test_log_rip_percent_writes_expected_pipeline_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_rip_percent(40, title=0)

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "progress [rip] RIP title 0 — 40% (MakeMKV)" in text
    assert "left" not in text


def test_log_rip_percent_includes_eta_when_stable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_rip_percent(40, title=0, eta_sec=840)

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "progress [rip] RIP title 0 — 40% (MakeMKV, ~14m left)" in text


def test_log_rip_percent_extract_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_root = tmp_path / "logs"
    _patch_log_paths(monkeypatch, log_root)

    logging_setup.log_rip_percent(
        40, source="ffmpeg", stage="EXTRACT", label="2/5"
    )

    text = (log_root / "pipeline.log").read_text(encoding="utf-8")
    assert "progress [rip] EXTRACT 2/5 — 40% (ffmpeg)" in text
    assert "RIP title" not in text


# --- ETA estimate + formatting ----------------------------------------------


def test_estimate_eta_sec_extrapolates_from_rate() -> None:
    # 40% in 600s → remaining 60% at same rate = 900s
    assert estimate_eta_sec(40, 600.0) == 900


def test_estimate_eta_sec_omits_before_min_pct() -> None:
    assert estimate_eta_sec(5, 120.0) is None
    assert estimate_eta_sec(9, 120.0) is None


def test_estimate_eta_sec_omits_before_min_elapsed() -> None:
    assert estimate_eta_sec(40, 10.0) is None
    assert estimate_eta_sec(40, 29.9) is None


def test_estimate_eta_sec_ready_at_thresholds() -> None:
    assert estimate_eta_sec(10, 30.0) == 270
    assert estimate_eta_sec(50, 30.0) == 30


def test_estimate_eta_sec_none_at_bounds() -> None:
    assert estimate_eta_sec(0, 60.0) is None
    assert estimate_eta_sec(100, 60.0) is None


def test_format_eta_remaining_minutes() -> None:
    assert format_eta_remaining(840) == "~14m"
    assert format_eta_remaining(60) == "~1m"
    assert format_eta_remaining(119) == "~1m"


def test_format_eta_remaining_hours() -> None:
    assert format_eta_remaining(3900) == "~1h05m"
    assert format_eta_remaining(3600) == "~1h00m"


def test_format_eta_remaining_under_one_minute() -> None:
    assert format_eta_remaining(0) == "<1m"
    assert format_eta_remaining(59) == "<1m"


def test_format_eta_remaining_none() -> None:
    assert format_eta_remaining(None) is None
