"""Tests for title verification against MakeMKV scans."""

from __future__ import annotations

import pytest

from ripknife.makemkv import ScanResult, TitleInfo, pick_feature_title
from ripknife.models import MovieManifest, TvDiscManifest
from ripknife.title_verify import (
    analyze_obfuscation,
    format_title_issues,
    raise_on_errors,
    scan_title_hints,
    title_suspicion,
    validate_manifest_titles,
    validate_tv_tracks,
)


def _tv_manifest(**track_overrides) -> TvDiscManifest:
    return TvDiscManifest.model_validate(
        {
            "kind": "tv_disc",
            "meta": {
                "id": "test-disc",
                "show": "Test Show",
                "season": 1,
                "minlength": 300,
            },
            "output": {"season_dir": "Season 1"},
            "tracks": [
                {
                    "slot": 1,
                    "makemkv_title": 1,
                    "episode": "01",
                    "title": "Pilot",
                    **track_overrides,
                }
            ],
        }
    )


def _scan_json(*titles: tuple[int, str, int, str]) -> dict:
    return {
        "disc_name": "TEST_DISC",
        "warnings": [],
        "titles": [
            {
                "index": idx,
                "duration": dur,
                "duration_sec": sec,
                "size_bytes": 700_000_000,
                "name": name,
            }
            for idx, dur, sec, name in titles
        ],
    }


def test_title_suspicion_flags_copyright_name() -> None:
    info = TitleInfo(index=0, name="FBI WARNING", duration="0:05:00", duration_sec=300)
    assert "copy-protection" in title_suspicion(info, minlength=300)


def test_title_suspicion_allows_normal_episode() -> None:
    info = TitleInfo(index=1, name="Pilot", duration="0:11:30", duration_sec=690)
    assert title_suspicion(info, minlength=300) is None


def test_validate_tv_rejects_missing_title_index() -> None:
    manifest = _tv_manifest()
    scan = _scan_json((0, "2:00:00", 7200, "Play All"))
    issues = validate_tv_tracks(manifest, scan)
    assert any("missing from scan" in i.message for i in issues)


def test_validate_tv_rejects_suspicious_title_suggests_later() -> None:
    manifest = _tv_manifest(makemkv_title=0)
    scan = _scan_json(
        (0, "0:05:00", 300, "FBI WARNING"),
        (1, "0:11:30", 690, "Real Episode"),
    )
    issues = validate_tv_tracks(manifest, scan)
    assert len(issues) == 1
    assert "check later title 1" in issues[0].message


def test_validate_tv_accepts_good_mapping() -> None:
    manifest = _tv_manifest()
    scan = _scan_json(
        (0, "2:00:00", 7200, "Play All"),
        (1, "0:11:30", 690, "Pilot"),
    )
    issues = validate_tv_tracks(manifest, scan)
    assert not [i for i in issues if i.level == "error"]


def test_validate_tv_short_episode_suggests_later_title() -> None:
    manifest = _tv_manifest(makemkv_title=0)
    scan = _scan_json(
        (0, "0:06:00", 360, "Menu Loop"),
        (1, "0:11:30", 690, "Pilot"),
    )
    issues = validate_tv_tracks(manifest, scan)
    assert any("check later title 1" in i.message for i in issues)


def test_validate_tv_split_skips_short_standalone_check() -> None:
    manifest = TvDiscManifest.model_validate(
        {
            "kind": "tv_disc",
            "meta": {"id": "split-disc", "show": "Test", "season": 1, "minlength": 300},
            "output": {"season_dir": "Season 1"},
            "tracks": [
                {
                    "slot": 1,
                    "makemkv_title": 1,
                    "split": {
                        "at_seconds": 660.0,
                        "outputs": [
                            {"episode": "01", "title": "Part A"},
                            {"episode": "02", "title": "Part B"},
                        ],
                    },
                }
            ],
        }
    )
    scan = _scan_json((1, "0:22:00", 1320, "Double Episode"))
    issues = validate_tv_tracks(manifest, scan)
    assert not [i for i in issues if i.level == "error"]


def test_validate_tv_min_episode_sec_allows_theatrical_shorts() -> None:
    """Classic theatrical cartoons are often ~6–8 min; default floor is 480s."""
    manifest = TvDiscManifest.model_validate(
        {
            "kind": "tv_disc",
            "meta": {
                "id": "shorts-disc",
                "show": "Example Shorts",
                "season": 1,
                "minlength": 300,
                "min_episode_sec": 360,
            },
            "output": {"season_dir": "Season 1"},
            "tracks": [
                {
                    "slot": 1,
                    "makemkv_title": 1,
                    "episode": "01",
                    "title": "First Short",
                },
                {
                    "slot": 2,
                    "makemkv_title": 2,
                    "episode": "02",
                    "title": "Second Short",
                },
            ],
        }
    )
    scan = _scan_json(
        (0, "0:15:52", 952, "Play All"),
        (1, "0:08:18", 498, "First Short"),
        (2, "0:07:34", 454, "Second Short"),
    )
    issues = validate_tv_tracks(manifest, scan)
    assert not [i for i in issues if i.level == "error"]


def test_validate_tv_default_min_episode_sec_still_rejects_short() -> None:
    manifest = _tv_manifest(makemkv_title=1)
    scan = _scan_json(
        (1, "0:07:34", 454, "Short Dummy"),
        (2, "0:11:30", 690, "Real Episode"),
    )
    issues = validate_tv_tracks(manifest, scan)
    assert any("short for a standalone episode" in i.message for i in issues)

def test_scan_title_hints_flags_suspicious() -> None:
    titles = {
        0: TitleInfo(index=0, name="COPYRIGHT", duration="0:05:00", duration_sec=300),
        1: TitleInfo(index=1, name="Episode", duration="0:11:00", duration_sec=660),
    }
    hints = scan_title_hints(titles, minlength=300)
    assert "suspicious" in hints[0]


def test_validate_movie_fixed_title_warns_when_later_is_longer() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "test-movie", "title": "Test Movie", "minlength": 300},
            "output": {"dir": "Movies", "filename": "Test Movie.mkv"},
            "title_selection": {"mode": "fixed", "title": 0},
            "validation": {"min_duration_sec": 4800, "min_size_bytes": 1},
        }
    )
    scan = _scan_json(
        (0, "0:27:00", 1620, "Promo"),
        (1, "2:30:00", 9000, "Feature"),
    )
    issues = validate_manifest_titles(manifest, scan)
    assert any("later title 1 is longer" in i.message for i in issues)


def test_validate_movie_fixed_near_longest_is_not_a_stereo_reject() -> None:
    """Example Feature title 3 is the feature. Title 6 is 13s longer and stereo only."""
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "example-feature-2008", "title": "Example Feature", "minlength": 300},
            "output": {"dir": "Movies", "filename": "Example Feature (2008).mkv"},
            "title_selection": {"mode": "fixed", "title": 3},
            "validation": {"min_duration_sec": 4800, "min_size_bytes": 1},
        }
    )
    scan = _scan_json(
        (0, "1:37:25", 5845, "20000.m2ts"),
        (3, "1:38:11", 5891, "00081.mpls"),
        (4, "1:38:11", 5891, "00081 stereo"),
        (6, "1:38:24", 5904, "00095.mpls"),
    )
    issues = validate_manifest_titles(manifest, scan)
    assert not any("longer" in i.message for i in issues)


def test_raise_on_errors_raises() -> None:
    from ripknife.title_verify import TitleIssue

    with pytest.raises(RuntimeError, match="Title validation failed"):
        raise_on_errors([TitleIssue("error", "bad title")])


def test_format_title_issues() -> None:
    from ripknife.title_verify import TitleIssue

    text = format_title_issues([TitleIssue("error", "slot 1: oops")])
    assert "ERROR: slot 1: oops" in text


def test_validate_from_scan_result() -> None:
    manifest = _tv_manifest()
    scan = ScanResult(
        raw="",
        titles={
            1: TitleInfo(index=1, name="Pilot", duration="0:11:30", duration_sec=690),
        },
    )
    issues = validate_tv_tracks(manifest, scan)
    assert not [i for i in issues if i.level == "error"]


def _john_wick_scan(*, decoy_index: int = 169) -> dict:
    """Synthetic Night Market Chapter 2: many duplicate playlists + one decoy."""
    titles = []
    for idx in range(330):
        if idx == decoy_index:
            dur_sec = 7448
            dur = "2:04:08"
            size = 36_707_008_512
        else:
            dur_sec = 7342
            dur = "2:02:22"
            size = 36_369_334_272
        titles.append(
            {
                "index": idx,
                "name": "Night Market Chapter 2",
                "duration": dur,
                "duration_sec": dur_sec,
                "size_bytes": size,
            }
        )
    return {
        "disc_name": "Night Market Chapter 2",
        "warnings": [],
        "titles": titles,
        "obfuscation": {
            "detected": True,
            "dominant_duration_sec": 7342,
            "duplicate_count": 329,
            "decoy_indices": [decoy_index],
            "recommended_title": 0,
            "dominant_name": "Night Market Chapter 2",
        },
    }


def test_analyze_obfuscation_john_wick_pattern() -> None:
    titles = {
        row["index"]: TitleInfo(
            index=row["index"],
            name=row["name"],
            duration=row["duration"],
            duration_sec=row["duration_sec"],
            size_bytes=row["size_bytes"],
        )
        for row in _john_wick_scan()["titles"]
    }
    obf = analyze_obfuscation(titles)
    assert obf.detected
    assert obf.dominant_duration_sec == 7342
    assert obf.duplicate_count == 329
    assert 169 in obf.decoy_indices
    assert obf.recommended_title == 0


def test_pick_feature_title_prefers_dominant_cluster() -> None:
    scan = ScanResult(
        raw="",
        titles={
            row["index"]: TitleInfo(
                index=row["index"],
                name=row["name"],
                duration=row["duration"],
                duration_sec=row["duration_sec"],
                size_bytes=row["size_bytes"],
            )
            for row in _john_wick_scan()["titles"]
        },
    )
    assert pick_feature_title(scan, min_duration_sec=4800) == 0


def test_validate_movie_rejects_decoy_fixed_title() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "night-market-2", "title": "Night Market Chapter 2", "minlength": 300},
            "output": {"dir": "Movies", "filename": "Night Market Chapter 2.mkv"},
            "title_selection": {"mode": "fixed", "title": 169},
            "validation": {"min_duration_sec": 4800, "min_size_bytes": 1},
        }
    )
    issues = validate_manifest_titles(manifest, _john_wick_scan())
    assert any("decoy outlier" in i.message for i in issues if i.level == "error")


def test_validate_movie_auto_feature_accepts_dominant_cluster() -> None:
    manifest = MovieManifest.model_validate(
        {
            "kind": "movie",
            "meta": {"id": "night-market-2", "title": "Night Market Chapter 2", "minlength": 300},
            "output": {"dir": "Movies", "filename": "Night Market Chapter 2.mkv"},
            "title_selection": {"mode": "auto_feature"},
            "validation": {"min_duration_sec": 4800, "min_size_bytes": 1},
        }
    )
    issues = validate_manifest_titles(manifest, _john_wick_scan())
    assert not [i for i in issues if i.level == "error"]


def test_scan_title_hints_marks_decoy_and_recommended() -> None:
    titles = {
        row["index"]: TitleInfo(
            index=row["index"],
            name=row["name"],
            duration=row["duration"],
            duration_sec=row["duration_sec"],
        )
        for row in _john_wick_scan()["titles"]
    }
    hints = scan_title_hints(titles, minlength=300)
    assert "recommended feature" in hints[0]
    assert "decoy" in hints[169]
