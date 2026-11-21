"""YouTube destinations and dry-run command. No network and no yt-dlp process."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from ripknife.cli import app
from ripknife.nas import nas_share_for
from ripknife.paths import ROOT
from ripknife.runner import episode_filename
from ripknife.youtube import (
    PlaylistVideo,
    build_youtube_plan,
    download_playlist,
    download_youtube,
    parse_flat_playlist,
    playlist_plans,
    publish_no_overwrite,
    youtube_destination,
)
from ripknife.youtube_map import load_youtube_map, match_youtube_show, parse_youtube_title

runner = CliRunner()
_EXAMPLE_YOUTUBE = ROOT / "configs" / "youtube.yaml"
_LIBRARY_ROOTS = {
    "anime_root": Path("/path/to/library/Anime"),
    "shows_root": Path("/path/to/library/Shows"),
    "movies_root": Path("/path/to/library/Movies"),
}


@pytest.fixture(autouse=True)
def _use_tracked_youtube_example(monkeypatch: pytest.MonkeyPatch) -> None:
    """Playlist helpers must not read the operator's private show map."""

    def _load(path: Path | None = None):
        return load_youtube_map(_EXAMPLE_YOUTUBE if path is None else path)

    monkeypatch.setattr("ripknife.youtube.load_youtube_map", _load)

_WATCHER_FILES = (
    "Makefile",
    "scripts/disc-watcher.py",
    "scripts/rip-watcher.py",
    "scripts/systemd/ripknife-disc-watcher.service",
    "scripts/systemd/ripknife-rip-watcher.service",
    "ripknife/movie.py",
    "ripknife/pipeline.py",
    "ripknife/recover.py",
    "ripknife/runner.py",
)

_URL = "https://www.youtube.com/watch?v=abc123def45"


def _roots(tmp_path: Path) -> dict[str, Path]:
    return {
        "anime_root": tmp_path / "Anime",
        "shows_root": tmp_path / "Shows",
        "movies_root": tmp_path / "Movies",
    }


def test_animated_series_destination(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    dest = youtube_destination(
        show="GLITCH",
        season=1,
        episode="1",
        title="What's Wrong?",
        movie=False,
        animated=True,
        **roots,
    )
    filename = episode_filename("GLITCH", 1, "01", "What's Wrong")
    assert filename.endswith(".mkv")
    assert dest == roots["anime_root"] / "GLITCH" / "Season 1" / f"{filename[:-4]}.mp4"
    assert "?" not in dest.name
    with pytest.raises(ValueError):
        dest.relative_to(roots["shows_root"])


def test_live_action_series_destination(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    dest = youtube_destination(
        show="Example Show",
        season=2,
        episode="7",
        title="Pilot",
        movie=False,
        animated=False,
        **roots,
    )
    filename = episode_filename("Example Show", 2, "07", "Pilot")
    assert dest == roots["shows_root"] / "Example Show" / "Season 2" / f"{filename[:-4]}.mp4"


def test_animated_movie_goes_to_anime_movies(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    dest = youtube_destination(
        show=None,
        season=None,
        episode=None,
        title="Example Movie",
        year=2001,
        movie=True,
        animated=True,
        **roots,
    )
    assert dest == roots["anime_root"] / "Movies" / "Example Movie (2001).mp4"
    with pytest.raises(ValueError):
        dest.relative_to(roots["shows_root"])


def test_live_action_movie_goes_to_movies(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    dest = youtube_destination(
        show=None,
        season=None,
        episode=None,
        title="Primer?",
        year=2004,
        movie=True,
        animated=False,
        **roots,
    )
    assert dest == roots["movies_root"] / "Primer (2004).mp4"
    assert "?" not in dest.name


def test_refuses_animation_on_shows(tmp_path: Path) -> None:
    shows = tmp_path / "Shows"
    with pytest.raises(ValueError, match="Shows"):
        youtube_destination(
            show="GLITCH",
            season=1,
            episode="1",
            title="Pilot",
            movie=False,
            animated=True,
            anime_root=shows,
            shows_root=shows,
            movies_root=tmp_path / "Movies",
        )


def test_dry_run_command_has_url_and_no_disc_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ripknife.paths.OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr("ripknife.paths.ANIME_NAS", tmp_path / "Anime")
    monkeypatch.setattr("ripknife.paths.SHOWS_NAS", tmp_path / "Shows")
    monkeypatch.setattr("ripknife.paths.MOVIES_NAS", tmp_path / "Movies")

    def fail_run(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"subprocess started: {args!r} {kwargs!r}")

    monkeypatch.setattr("ripknife.youtube.subprocess.run", fail_run)
    url = _URL
    result = runner.invoke(
        app,
        [
            "youtube",
            url,
            "--show",
            "GLITCH",
            "--season",
            "1",
            "--episode",
            "1",
            "--title",
            "Pilot?",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    dest_lines = [line for line in result.stdout.splitlines() if line.startswith("destination:")]
    assert len(dest_lines) == 1
    assert "?" not in dest_lines[0]
    assert "GLITCH - 1x01 - Pilot.mp4" in dest_lines[0]
    assert str(tmp_path / "Anime" / "GLITCH" / "Season 1") in dest_lines[0]
    command = "\n".join(line for line in result.stdout.splitlines() if line not in dest_lines)
    assert url in command
    assert "/dev/sr" not in result.output
    assert "dev:/dev" not in result.output
    assert "bluray:" not in result.output
    assert "staging" not in result.stdout
    assert "--no-playlist" in command
    assert "--no-overwrites" in command
    assert "--merge-output-format" in command
    assert "mp4" in command
    assert "--sleep-requests" in command
    assert "--sleep-requests 1" in command
    assert "bestvideo+bestaudio/best" in command
    assert "--download-archive" in command
    archive = tmp_path / "output" / "youtube" / "download-archive.txt"
    assert str(archive) in command
    assert "%(ext)s" in command
    assert not (tmp_path / "output").exists()
    assert not archive.exists()


def test_watchers_do_not_reference_youtube() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in _WATCHER_FILES:
        text = (root / relative).read_text(encoding="utf-8").lower()
        assert "youtube" not in text, relative
        assert "yt-dlp" not in text, relative
        assert "yt_dlp" not in text, relative
    cli = (root / "ripknife/cli.py").read_text(encoding="utf-8")
    assert "youtube_app" in cli
    assert "add_typer(youtube_app" in cli


def test_existing_destination_skips_download(tmp_path: Path) -> None:
    plan = build_youtube_plan(
        _URL,
        show="GLITCH",
        season=1,
        episode="1",
        title="Pilot",
        movie=False,
        animated=True,
        output_root=tmp_path / "output",
        executable=("yt-dlp",),
        **_roots(tmp_path),
    )
    plan.destination.parent.mkdir(parents=True)
    plan.destination.write_bytes(b"keep")

    def run(_command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("yt-dlp started")

    dest = download_youtube(plan, run=run, probe=lambda _path: True)
    assert dest == plan.destination
    assert dest.read_bytes() == b"keep"
    assert not plan.temp_path.exists()


def test_probe_failure_does_not_publish(tmp_path: Path) -> None:
    plan = build_youtube_plan(
        _URL,
        show="GLITCH",
        season=1,
        episode="1",
        title="Pilot",
        movie=False,
        animated=True,
        output_root=tmp_path / "output",
        executable=("yt-dlp",),
        **_roots(tmp_path),
    )

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert _URL in command
        assert not any(
            part.startswith("/dev/sr") or part.startswith("dev:/dev") for part in command
        )
        plan.temp_path.write_bytes(b"not-a/video")
        return subprocess.CompletedProcess(list(command), 0)

    with pytest.raises(RuntimeError, match="ffprobe"):
        download_youtube(plan, run=run, probe=lambda _path: False)
    assert not plan.destination.exists()
    assert plan.temp_path.is_file()
    assert "staging" not in plan.temp_path.parts
    assert plan.archive.parent == tmp_path / "output" / "youtube"


def test_publish_moves_only_after_probe_and_does_not_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "fresh.mp4"
    existing = tmp_path / "kept.mp4"
    src.write_bytes(b"new")
    existing.write_bytes(b"old")
    assert publish_no_overwrite(src, existing) == existing
    assert existing.read_bytes() == b"old"
    assert src.read_bytes() == b"new"

    plan = build_youtube_plan(
        _URL,
        show="GLITCH",
        season=1,
        episode="1",
        title="Pilot",
        movie=False,
        animated=True,
        output_root=tmp_path / "output",
        executable=("yt-dlp",),
        **_roots(tmp_path),
    )

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        plan.temp_path.write_bytes(b"video")
        return subprocess.CompletedProcess(list(command), 0, "")

    dest = download_youtube(plan, run=run, probe=lambda _path: True)
    assert dest.read_bytes() == b"video"
    assert not plan.temp_path.exists()
    assert dest.parent == tmp_path / "Anime" / "GLITCH" / "Season 1"


_EXAMPLE_CARTOON = (
    (1, "Example Cartoon - SPECIAL: ZERO", 1, "0", "Special Zero"),
    (2, "Example Cartoon - Cold Open // S1: Episode 1", 1, "1", "Cold Open"),
    (8, "Example Cartoon - Last Night // S1: Episode 7 - FINALE", 1, "7", "Last Night"),
    (9, "Example Cartoon - Next One // S1: Episode 8", 1, "8", "Next One"),
    (10, "Example Cartoon - Second Season // S2: Episode 1", 2, "1", "Second Season"),
    (19, "Example Cartoon - Tenth One // S2: Episode 10", 2, "10", "Tenth One"),
    (21, "Example Cartoon - Year End // S2: Episode 12 -FINALE", 2, "12", "Year End"),
    (22, "Example Cartoon SHORTS 1: Side Story // Example Cartoon", 0, "1", "Side Story"),
    (23, "Example Cartoon SHORTS 2 // Extra Bit // Example Cartoon", 0, "2", "Extra Bit"),
    (33, "Bonus Clip // MUSIC VIDEO // Example Cartoon", 0, "33", "Bonus Clip"),
    (36, "Example Cartoon SHORTS 14 // The Puppet // Example Cartoon", 0, "14", "The Puppet"),
)


@pytest.mark.parametrize("index,raw,season,episode,title", _EXAMPLE_CARTOON)
def test_example_cartoon_playlist_titles(index: int, raw: str, season: int, episode: str, title: str) -> None:
    show = match_youtube_show(load_youtube_map(ROOT / "configs" / "youtube.yaml"), show="Example Cartoon")
    assert show is not None
    assert parse_youtube_title(raw, show, playlist_index=index) == (season, episode, title)


def test_playlist_plans_use_watch_urls_and_anime(tmp_path: Path) -> None:
    videos = tuple(
        PlaylistVideo(index, f"vid{index:02d}xxxxx", raw)
        for index, raw, _season, _episode, _title in _EXAMPLE_CARTOON
    )
    plans = playlist_plans(
        videos,
        show="Example Cartoon",
        output_root=tmp_path / "output",
        executable=("yt-dlp",),
        **_roots(tmp_path),
    )
    murder = plans[1]
    assert murder.destination == (
        tmp_path / "Anime" / "Example Cartoon" / "Season 1" / "Example Cartoon - 1x01 - Cold Open.mp4"
    )
    assert "--no-playlist" in murder.command
    assert "https://www.youtube.com/watch?v=vid02xxxxx" in murder.command
    assert "/dev/sr" not in " ".join(murder.command)
    short = plans[7]
    assert short.destination.parent.name == "Season 0"
    assert short.destination.name == "Example Cartoon - 0x01 - Side Story.mp4"
    with pytest.raises(ValueError):
        murder.destination.relative_to(tmp_path / "Shows")
    for plan, (index, _raw, season, episode, title) in zip(plans, _EXAMPLE_CARTOON, strict=True):
        token = f"{int(episode):02d}"
        filename = episode_filename("Example Cartoon", season, token, title)
        assert plan.destination == (
            tmp_path / "Anime" / "Example Cartoon" / f"Season {season}" / f"{filename[:-4]}.mp4"
        )
        assert f"https://www.youtube.com/watch?v=vid{index:02d}xxxxx" in plan.command


def test_parse_flat_playlist_skips_blank_lines() -> None:
    videos = parse_flat_playlist("1\tabc123def45\tExample Cartoon - Cold Open // S1: Episode 1\n\njunk\n")
    assert videos == (
        PlaylistVideo(1, "abc123def45", "Example Cartoon - Cold Open // S1: Episode 1"),
    )


def test_playlist_download_keeps_earlier_files_when_a_later_one_fails(tmp_path: Path) -> None:
    videos = (
        PlaylistVideo(1, "abc123def45", "Example Cartoon - Cold Open // S1: Episode 1"),
        PlaylistVideo(2, "bbb123def45", "Example Cartoon - Second Story // S1: Episode 2"),
    )
    plans = playlist_plans(
        videos,
        show="Example Cartoon",
        output_root=tmp_path / "output",
        executable=("yt-dlp",),
        **_roots(tmp_path),
    )

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        url = command[-1]
        plan = next(item for item in plans if item.url == url)
        if "bbb123def45" in url:
            return subprocess.CompletedProcess(list(command), 1)
        plan.temp_path.write_bytes(b"video")
        return subprocess.CompletedProcess(list(command), 0)

    with pytest.raises(RuntimeError, match="playlist incomplete"):
        download_playlist(plans, run=run, probe=lambda _path: True)
    assert plans[0].destination.is_file()
    assert not plans[1].destination.exists()


def test_playlist_dry_run_does_not_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ripknife.paths.OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr("ripknife.paths.ANIME_NAS", tmp_path / "Anime")
    monkeypatch.setattr("ripknife.paths.SHOWS_NAS", tmp_path / "Shows")
    monkeypatch.setattr("ripknife.paths.MOVIES_NAS", tmp_path / "Movies")
    monkeypatch.setattr(
        "ripknife.youtube.fetch_playlist",
        lambda url, **_kwargs: (
            PlaylistVideo(2, "abc123def45", "Example Cartoon - Cold Open // S1: Episode 1"),
        ),
    )

    def fail_run(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"subprocess started: {args!r} {kwargs!r}")

    monkeypatch.setattr("ripknife.youtube.subprocess.run", fail_run)
    result = runner.invoke(
        app,
        [
            "youtube",
            "https://www.youtube.com/playlist?list=PL-test",
            "--show",
            "Example Cartoon",
            "--playlist",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Example Cartoon - 1x01 - Cold Open.mp4" in result.stdout
    assert "--no-playlist" in result.stdout
    assert "https://www.youtube.com/watch?v=abc123def45" in result.stdout
    assert "/dev/sr" not in result.output
    assert not (tmp_path / "output").exists()


def test_show_map_matches_playlist_id_and_nas_share() -> None:
    ymap = load_youtube_map(ROOT / "configs" / "youtube.yaml")
    assert ymap.path == ROOT / "configs" / "youtube.yaml"
    by_list = match_youtube_show(
        ymap,
        url="https://www.youtube.com/playlist?list=PLexamplecartoon01",
    )
    by_id = match_youtube_show(ymap, show="example-cartoon")
    by_name = match_youtube_show(ymap, show="Example Cartoon")
    assert by_list is not None and by_id is not None and by_name is not None
    assert by_list.id == by_id.id == by_name.id == "example-cartoon"
    assert by_list.kind == "series"
    assert by_list.animated is True
    assert by_list.show == "Example Cartoon"
    for show in ymap.shows:
        kind = "movie" if show.kind == "movie" else "tv_disc"
        name = show.show if kind == "tv_disc" else None
        assert Path(show.nas.share or "") == nas_share_for(
            kind=kind,
            animated=show.animated,
            show=name,
            **_LIBRARY_ROOTS,
        )


def test_unknown_show_does_not_reuse_another_shows_rules(tmp_path: Path) -> None:
    videos = (
        PlaylistVideo(1, "abc123def45", "Example Cartoon - Cold Open // S1: Episode 1"),
    )
    with pytest.raises(ValueError, match="no YouTube show map entry"):
        playlist_plans(
            videos,
            show="Other Show",
            output_root=tmp_path / "output",
            executable=("yt-dlp",),
            **_roots(tmp_path),
        )


def test_python_holds_the_mechanism_not_a_show_grammar() -> None:
    root = Path(__file__).resolve().parents[1]
    blob = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in ("ripknife/youtube.py", "ripknife/youtube_map.py")
    )
    lowered = blob.lower()
    assert "example-cartoon" not in lowered
    assert "SHORTS" not in blob
    assert "MISSION" not in blob
    assert "FINALE" not in blob
    assert "Street Food" not in blob


_SECOND_SHOW = dedent(
    """\
    shows:
      - kind: series
        meta:
          id: street-food
          show: Street Food
        animated: false
        playlists:
          - PLstreetfood
        nas:
          share: /path/to/library/Shows/Street Food
          movies: false
        rules:
          - pattern: '(?i)^S(?P<season>\\d+)E(?P<episode>\\d+)\\s+(?P<title>.+)$'
          - season: 9
            episode: "{index}"
            title: "{raw}"
      - kind: movie
        meta:
          id: night-market
          title: Night Market
          year: 2019
        animated: false
        playlists:
          - PLnightmarket
        nas:
          share: /path/to/library/Movies
          movies: true
    """
)


def test_second_show_is_defined_only_in_yaml(tmp_path: Path) -> None:
    path = tmp_path / "shows.yaml"
    path.write_text(_SECOND_SHOW, encoding="utf-8")
    ymap = load_youtube_map(path)
    street = match_youtube_show(ymap, show="street-food")
    movie = match_youtube_show(
        ymap,
        url="https://www.youtube.com/playlist?list=PLnightmarket",
    )
    assert street is not None and movie is not None
    assert street.kind == "series"
    assert street.animated is False
    assert movie.kind == "movie"
    assert movie.title == "Night Market"
    assert movie.year == 2019
    assert parse_youtube_title("S2E4 Late Service", street, playlist_index=1) == (
        2,
        "4",
        "Late Service",
    )
    assert parse_youtube_title("SHORTS 3", street, playlist_index=5) == (9, "5", "SHORTS 3")
    plans = playlist_plans(
        (PlaylistVideo(1, "abc123def45", "S2E4 Late Service"),),
        entry=street,
        output_root=tmp_path / "output",
        executable=("yt-dlp",),
        **_roots(tmp_path),
    )
    assert plans[0].destination == (
        tmp_path
        / "Shows"
        / "Street Food"
        / "Season 2"
        / "Street Food - 2x04 - Late Service.mp4"
    )
    with pytest.raises(ValueError):
        plans[0].destination.relative_to(tmp_path / "Anime")
    film = youtube_destination(
        show=None,
        season=None,
        episode=None,
        title=movie.title,
        year=movie.year,
        movie=True,
        animated=movie.animated,
        **_roots(tmp_path),
    )
    assert film == tmp_path / "Movies" / "Night Market (2019).mp4"
    assert Path(street.nas.share or "") == nas_share_for(
        kind="tv_disc",
        animated=False,
        show="Street Food",
        **_LIBRARY_ROOTS,
    )
    assert Path(movie.nas.share or "") == nas_share_for(
        kind="movie",
        animated=False,
        **_LIBRARY_ROOTS,
    )
    with pytest.raises(ValueError, match="playlist"):
        match_youtube_show(
            ymap,
            show="Street Food",
            url="https://www.youtube.com/playlist?list=PLnightmarket",
        )


def test_playlist_flags_override_the_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ripknife.paths.OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr("ripknife.paths.ANIME_NAS", tmp_path / "Anime")
    monkeypatch.setattr("ripknife.paths.SHOWS_NAS", tmp_path / "Shows")
    monkeypatch.setattr("ripknife.paths.MOVIES_NAS", tmp_path / "Movies")
    monkeypatch.setattr(
        "ripknife.youtube.fetch_playlist",
        lambda url, **_kwargs: (
            PlaylistVideo(2, "abc123def45", "Example Cartoon - Cold Open // S1: Episode 1"),
        ),
    )

    def fail_run(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"subprocess started: {args!r} {kwargs!r}")

    monkeypatch.setattr("ripknife.youtube.subprocess.run", fail_run)
    result = runner.invoke(
        app,
        [
            "youtube",
            "https://www.youtube.com/playlist?list=PLexamplecartoon01",
            "--show",
            "example-cartoon",
            "--playlist",
            "--season",
            "4",
            "--episode",
            "3",
            "--title",
            "Custom",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Example Cartoon - 4x03 - Custom.mp4" in result.stdout
    assert "/dev/sr" not in result.output
    assert not (tmp_path / "output").exists()


def test_mapped_playlist_url_selects_the_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ripknife.paths.OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr("ripknife.paths.ANIME_NAS", tmp_path / "Anime")
    monkeypatch.setattr("ripknife.paths.SHOWS_NAS", tmp_path / "Shows")
    monkeypatch.setattr("ripknife.paths.MOVIES_NAS", tmp_path / "Movies")
    monkeypatch.setattr(
        "ripknife.youtube.fetch_playlist",
        lambda url, **_kwargs: (
            PlaylistVideo(1, "abc123def45", "Example Cartoon - SPECIAL: ZERO"),
        ),
    )
    result = runner.invoke(
        app,
        [
            "youtube",
            "https://www.youtube.com/playlist?list=PLexamplecartoon01",
            "--playlist",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Example Cartoon - 1x00 - Special Zero.mp4" in result.stdout
    assert str(tmp_path / "Anime" / "Example Cartoon" / "Season 1") in result.stdout
