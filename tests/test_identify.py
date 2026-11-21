"""Tests for ripknife.identify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripknife import identify


def test_is_generic_disc_name() -> None:
    # Uninformative volume labels (heuristic / fuzzy)
    for label in (
        "DVDVolume",
        "DVD_VIDEO",
        "DVD VOLUME",
        "DVD_VOLUME",
        "dvd-volume",
        "VIDEO_TS",
        "BD_VIDEO",
        "untitled",
        "UNKNOWN",
        "DISC1",
        "AVCHD",
        "BDMV",
        "blu-ray",
        "LOGICAL_VOLUME_ID",
        "Logical Volume ID",
        "BDROM",
        "BD-ROM",
        "BD_ROM",
        "",
    ):
        assert identify.is_generic_disc_name(label), label

    # Real titles must not be treated as generic
    for title in (
        "Cascade",
        "CASCADE",
        "River",
        "RIVER",
        "Example Movie (1983)",
        "Blue Heron?",
        "BLUEHERON",
        "Blue Heron",
        "DEEPRUN",
        "Example Movie",
        "Night Market Chapter 2",
    ):
        assert not identify.is_generic_disc_name(title), title


def test_is_generic_disc_name_boilerplate_combo() -> None:
    assert identify.is_generic_disc_name("DVD VIDEO VOLUME")
    assert identify.is_generic_disc_name("vol")
    assert identify.is_generic_disc_name("MEDIA")
    assert not identify.is_generic_disc_name("DVD Adventure")  # non-boilerplate token


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
      <di:name>Example Movie</di:name>
      <di:numSets>1</di:numSets>
      <di:setNumber>1</di:setNumber>
    </di:title>
  </di:discinfo>
</disclib>
""",
        encoding="utf-8",
    )
    assert identify.parse_bdmt_xml(xml) == "Example Movie"


def test_read_bluray_title_uses_tmp_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identify mount workdirs must land under TMP_ROOT, not /tmp tmpfs."""
    tmp_root = tmp_path / ".tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(identify, "ensure_tmp_root", lambda: tmp_root)

    created: list[Path] = []

    class FakeTempDir:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("dir") == tmp_root
            self.path = tmp_root / "ripknife-bdmt-test"
            self.path.mkdir()
            created.append(self.path)

        def __enter__(self):
            return str(self.path)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(identify.tempfile, "TemporaryDirectory", FakeTempDir)
    monkeypatch.setattr(identify, "disc_dev", lambda _d: "/dev/null")
    monkeypatch.setattr(identify, "existing_optical_mount", lambda _d: None)
    monkeypatch.setattr(
        identify,
        "mount_optical_readonly",
        lambda _dev, mp: (False, None),
    )
    title, source = identify.read_bluray_title("dev:/dev/null")
    assert title is None and source is None
    assert created and created[0].is_relative_to(tmp_root)


def test_lookup_tmdb_by_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "results": [
            {
                "id": 1,
                "title": "Example Movie",
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
    assert candidates[0].title == "Example Movie"
    assert candidates[0].year == 1991
    assert candidates[0].source == "tmdb"


def test_gather_identification_hints_generic_without_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(
        identify, "read_disc_mount_meta", lambda _d: (None, None, None)
    )
    scan = {
        "disc_name": "DVD_VIDEO",
        "titles": [{"index": 0, "duration_sec": 5979}],
    }
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.is_generic_label
    assert hints.feature_duration_sec == 5979
    assert hints.candidates == []
    assert any("RIPKNIFE_TMDB_API_KEY" in n for n in hints.notes)


def test_gather_identification_hints_bluray_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(
        identify,
        "read_disc_mount_meta",
        lambda _d: ("Example Movie", "/mnt/x/bdmt_eng.xml", None),
    )
    scan = {"disc_name": "DVD_VIDEO", "titles": [{"duration_sec": 5979}]}
    hints = identify.gather_identification_hints(
        scan, device="dev:/dev/sr0", try_runtime_lookup=False
    )
    assert hints.bluray_title == "Example Movie"
    assert hints.best_title() == "Example Movie"


def test_step_write_pending_attaches_identification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ripknife import pipeline

    monkeypatch.setattr(pipeline, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(
        "ripknife.identify.gather_identification_hints",
        lambda scan, device=None, **kw: identify.IdentificationHints(
            is_generic_label=True,
            feature_duration_sec=5979,
            candidates=[
                identify.MovieCandidate(
                    title="Example Movie", year=1991, source="tmdb", external_id="1"
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
    assert meta["title_candidates"] == ["Example Movie (1991)"]
    assert scan["identification"]["is_generic_label"] is True


def test_build_map_prompt_mentions_identification(tmp_path: Path) -> None:
    from ripknife.map_agent import build_map_prompt

    scan_json = tmp_path / "foo.scan.json"
    scan_json.write_text("{}", encoding="utf-8")
    prompt = build_map_prompt("foo", scan_json)
    assert "identification.bluray_title" in prompt
    assert "identification.candidates" in prompt
    assert "identification.fingerprint_title" in prompt
    assert "action is write, stop, or ambiguous" in prompt
    assert "return action ambiguous and manifest null" in prompt
    assert "two well-known films share the feature runtime" in prompt
    assert "BDROM" in prompt
    assert "MAP_AMBIGUOUS:" not in prompt


def _north_film() -> identify.KnownDiscFingerprint:
    return identify.KnownDiscFingerprint(
        title="North Film",
        year=1987,
        duration_sec=6572,
        size_bytes=21_194_059_776,
        content_id_needles=("ABC123",),
        reject_titles=("South Film",),
    )


def test_match_known_fingerprint_needs_size_or_content_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "KNOWN_DISC_FINGERPRINTS", (_north_film(),))
    # Duration alone must not identify — two films can share ~110 min.
    assert (
        identify.match_known_fingerprint(duration_sec=6572, size_bytes=None, content_id=None)
        is None
    )
    by_size = identify.match_known_fingerprint(
        duration_sec=6572,
        size_bytes=21_194_059_776,
        content_id=None,
    )
    assert by_size is not None
    assert by_size.display() == "North Film (1987)"
    assert "South Film" in by_size.reject_titles

    by_aacs = identify.match_known_fingerprint(
        duration_sec=6572,
        size_bytes=None,
        content_id="0xABC12300000000000000000000000000",
    )
    assert by_aacs is not None
    assert by_aacs.display() == "North Film (1987)"


def test_parse_mcmf_content_id(tmp_path: Path) -> None:
    xml = tmp_path / "mcmf.xml"
    xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<mcmf xmlns="urn:AACS:bluray;mcmf" '
        'contentID="0xABC12300000000000000000000000000"/>\n',
        encoding="utf-8",
    )
    assert identify.parse_mcmf_content_id(xml) == "0xABC12300000000000000000000000000"


def test_gather_identification_hints_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "KNOWN_DISC_FINGERPRINTS", (_north_film(),))
    monkeypatch.setattr(identify, "TMDB_API_KEY", "")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(
        identify,
        "read_disc_mount_meta",
        lambda _d: (None, None, "0xABC12300000000000000000000000000"),
    )
    scan = {
        "disc_name": "LOGICAL_VOLUME_ID",
        "titles": [
            {"index": 0, "duration_sec": 6572, "size_bytes": 21_194_059_776}
        ],
    }
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.is_generic_label
    assert hints.fingerprint_title == "North Film (1987)"
    assert hints.best_title() == "North Film (1987)"
    assert hints.ambiguous is False
    assert hints.aacs_content_id.endswith("ABC12300000000000000000000000000")
    assert hints.candidates[0].title == "North Film"
    assert any("not South Film" in n for n in hints.notes)
    assert not any("RIPKNIFE_TMDB_API_KEY" in n for n in hints.notes)


def _runtime_pair() -> list[identify.MovieCandidate]:
    return [
        identify.MovieCandidate(
            title="North Film", year=1987, runtime_min=110, source="tmdb"
        ),
        identify.MovieCandidate(
            title="South Film", year=2006, runtime_min=110, source="tmdb"
        ),
    ]


def test_generic_runtime_collision_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "test")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(identify, "read_disc_mount_meta", lambda _d: (None, None, None))
    monkeypatch.setattr(identify, "lookup_runtime_candidates", lambda *_a, **_k: _runtime_pair())
    scan = {
        "disc_name": "BDROM",
        "titles": [{"index": 0, "duration_sec": 6572, "size_bytes": 1_000}],
    }
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.is_generic_label
    assert hints.fingerprint_title is None
    assert hints.ambiguous is True
    assert hints.best_title() is None
    assert hints.ambiguous_reason is not None
    assert "North Film (1987)" in hints.ambiguous_reason
    assert "South Film (2006)" in hints.ambiguous_reason
    assert hints.to_dict()["ambiguous"] is True


def test_bdmt_title_is_not_a_runtime_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "test")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(
        identify,
        "read_disc_mount_meta",
        lambda _d: ("Example Movie", "/mnt/x/bdmt_eng.xml", None),
    )
    monkeypatch.setattr(identify, "lookup_runtime_candidates", lambda *_a, **_k: _runtime_pair())
    scan = {"disc_name": "DVD_VIDEO", "titles": [{"duration_sec": 6572}]}
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.bluray_title == "Example Movie"
    assert hints.ambiguous is False
    assert hints.best_title() == "Example Movie"


def test_named_volume_label_is_not_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "test")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(identify, "read_disc_mount_meta", lambda _d: (None, None, None))
    monkeypatch.setattr(identify, "lookup_runtime_candidates", lambda *_a, **_k: _runtime_pair())
    scan = {"disc_name": "River", "titles": [{"duration_sec": 6572}]}
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.is_generic_label is False
    assert hints.ambiguous is False


def test_single_runtime_candidate_is_not_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identify, "TMDB_API_KEY", "test")
    monkeypatch.setattr(identify, "OMDB_API_KEY", "")
    monkeypatch.setattr(identify, "read_disc_mount_meta", lambda _d: (None, None, None))
    only = identify.MovieCandidate(
        title="Example Movie", year=1991, runtime_min=100, source="tmdb"
    )
    monkeypatch.setattr(identify, "lookup_runtime_candidates", lambda *_a, **_k: [only])
    scan = {"disc_name": "DVD_VIDEO", "titles": [{"duration_sec": 5979}]}
    hints = identify.gather_identification_hints(scan, device="dev:/dev/sr0")
    assert hints.ambiguous is False
    assert hints.best_title() == "Example Movie (1991)"


def test_step_write_pending_records_runtime_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ripknife import pipeline

    monkeypatch.setattr(pipeline, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(
        "ripknife.identify.gather_identification_hints",
        lambda scan, device=None, **kw: identify.IdentificationHints(
            is_generic_label=True,
            feature_duration_sec=6572,
            candidates=_runtime_pair(),
            ambiguous=True,
            ambiguous_reason="generic volume label; 2 films share ~6572s",
        ),
    )
    pending = pipeline.step_write_pending(
        {"disc_name": "LOGICAL_VOLUME_ID", "titles": [{"index": 0, "duration_sec": 6572}]},
        device="dev:/dev/sr0",
        minlength=300,
    )
    meta = json.loads(pending.meta_json.read_text(encoding="utf-8"))
    scan = json.loads(pending.scan_json.read_text(encoding="utf-8"))
    assert scan["identification"]["ambiguous"] is True
    assert meta["identification"]["ambiguous"] is True
    assert "preferred_id" not in meta
    assert "identified_title" not in meta
