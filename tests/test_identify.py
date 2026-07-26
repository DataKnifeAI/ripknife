"""Tests for videosripper.identify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videosripper import identify


def test_is_generic_disc_name() -> None:
    assert identify.is_generic_disc_name("DVD_VIDEO")
    assert identify.is_generic_disc_name("VIDEO_TS")
    assert identify.is_generic_disc_name("untitled")
    assert identify.is_generic_disc_name("")
    assert not identify.is_generic_disc_name("Hudson Hawk")
    assert not identify.is_generic_disc_name("John Wick Chapter 2")


def test_feature_duration_sec_from_list() -> None:
    titles = [
        {"index": 0, "duration_sec": 600},
        {"index": 1, "duration_sec": 5979},
    ]
    assert identify.feature_duration_sec(titles) == 5979


def test_parse_bdmt_xml(tmp_path: Path) -> None:
    xml = tmp_path / "bdmt_eng.xml"
    xml.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<disclib xmlns="urn:BDA:bdmv;disclib" xmlns:di="urn:BDA:bdmv;discinfo">
  <di:discinfo>
    <di:title>
      <di:name>Hudson Hawk</di:name>
      <di:numSets>1</di:numSets>
      <di:setNumber>1</di:setNumber>
    </di:title>
  </di:discinfo>
</disclib>
""",
        encoding="utf-8",
    )
    assert identify.parse_bdmt_xml(xml) == "Hudson Hawk"


def test_read_bluray_title_from_mount(tmp_path: Path) -> None:
    dl = tmp_path / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True)
    (dl / "bdmt_eng.xml").write_text(
        '<?xml version="1.0"?><root xmlns:di="urn:BDA:bdmv;discinfo">'
        "<di:name>Scarface</di:name></root>",
        encoding="utf-8",
    )
    title, source = identify.read_bluray_title_from_mount(tmp_path)
    assert title == "Scarface"
    assert source and "bdmt_eng.xml" in source


def test_lookup_tmdb_by_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "results": [
            {
                "id": 1,
                "title": "Hudson Hawk",
                "release_date": "1991-05-24",
                "popularity": 12.0,
            },
            {
                "id": 2,
                "title": "Other Film",
                "release_date": "1990-01-01",
                "popularity": 5.0,
            },
        ]
    }

    def fake_get(url: str):
        assert "discover/movie" in url
        assert "with_runtime.gte" in url
        return payload

    monkeypatch.setattr(identify, "_http_get_json", fake_get)
    monkeypatch.setattr(identify, "TMDB_API_KEY", "test-key")
    candidates = identify.lookup_tmdb_by_runtime(5979, limit=5)
    assert candidates[0].title == "Hudson Hawk"
    assert candidates[0].year == 1991
    assert candidates[0].source == "tmdb"


def test_gather_identification_hints_generic_without_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(identify, "read_bluray_title", lambda _d: (None, None))
    scan = {
        "disc_name": "DVD_VIDEO",
        "titles": [{"index": 0, "duration_sec": 5979}],
    }
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.is_generic_label
    assert hints.feature_duration_sec == 5979
    assert hints.candidates == []
    assert any("VIDEOSRIPPER_TMDB_API_KEY" in n for n in hints.notes)


def test_gather_identification_hints_bluray_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(
        identify, "read_bluray_title", lambda _d: ("Hudson Hawk", "/mnt/x/bdmt_eng.xml")
    )
    scan = {"disc_name": "DVD_VIDEO", "titles": [{"duration_sec": 5979}]}
    hints = identify.gather_identification_hints(
        scan, device="dev:/dev/sr0", try_runtime_lookup=False
    )
    assert hints.bluray_title == "Hudson Hawk"
    assert hints.best_title() == "Hudson Hawk"


def test_step_write_pending_attaches_identification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from videosripper import pipeline

    monkeypatch.setattr(pipeline, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(
        "videosripper.identify.gather_identification_hints",
        lambda scan, device=None, **kw: identify.IdentificationHints(
            is_generic_label=True,
            feature_duration_sec=5979,
            candidates=[
                identify.MovieCandidate(
                    title="Hudson Hawk", year=1991, source="tmdb", external_id="1"
                )
            ],
            notes=["runtime candidates"],
        ),
    )
    pending = pipeline.step_write_pending(
        {"disc_name": "DVD_VIDEO", "titles": [{"index": 0, "duration_sec": 5979}]},
        device="dev:/dev/sr0",
        minlength=300,
    )
    meta = json.loads(pending.meta_json.read_text(encoding="utf-8"))
    scan = json.loads(pending.scan_json.read_text(encoding="utf-8"))
    assert "identification" in meta
    assert meta["title_candidates"] == ["Hudson Hawk (1991)"]
    assert scan["identification"]["is_generic_label"] is True


def test_build_map_prompt_mentions_identification(tmp_path: Path) -> None:
    from videosripper.map_agent import build_map_prompt

    scan_json = tmp_path / "foo.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = build_map_prompt("foo", scan_json)
    assert "identification.bluray_title" in prompt
    assert "identification.candidates" in prompt
