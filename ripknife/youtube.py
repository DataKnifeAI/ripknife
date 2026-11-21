"""Download a released YouTube episode, movie, or show playlist into Plex.

Manual CLI only. Disc watcher, rip watcher, recover, and ``make watch-*`` do
not call this. Show numbering and library kind come from ``configs/youtube.yaml``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from ripknife import paths as pathmod
from ripknife.models import sanitize_edition
from ripknife.nas import nas_share_for
from ripknife.runner import episode_filename
from ripknife.youtube_map import (
    YoutubeMap,
    YoutubeShow,
    load_youtube_map,
    match_youtube_show,
    parse_youtube_title,
)

console = Console(stderr=True)

youtube_app = typer.Typer(
    no_args_is_help=True,
    help="Download a released YouTube episode, movie, or playlist. Watchers never call this.",
)

_VIDEO_ID = re.compile(
    r"(?:[?&]v=|/vi/|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})(?=[^A-Za-z0-9_-]|$)"
)
_ARCHIVE_NAME = "download-archive.txt"
_FLAT_PRINT = "%(playlist_index)s\t%(id)s\t%(title)s"


@dataclass(frozen=True)
class PlaylistVideo:
    index: int
    video_id: str
    title: str


@dataclass(frozen=True)
class YoutubePlan:
    url: str
    destination: Path
    temp_path: Path
    archive: Path
    command: tuple[str, ...]

    def display(self) -> str:
        return f"destination: {self.destination}\n{shlex.join(self.command)}"


def resolve_ytdlp() -> tuple[str, ...]:
    """``yt-dlp`` on PATH, otherwise ``python -m yt_dlp``."""
    found = shutil.which("yt-dlp")
    if found:
        return (found,)
    return (sys.executable, "-m", "yt_dlp")


def video_id(url: str) -> str:
    match = _VIDEO_ID.search(url)
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _component(value: str, *, what: str) -> str:
    cleaned = sanitize_edition(value)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"{what} is empty after removing characters SMB cannot store")
    return cleaned


def _episode_token(episode: str) -> str:
    cleaned = _component(episode, what="episode")
    if cleaned.isdigit():
        return f"{int(cleaned):02d}"
    return cleaned


def series_filename(show: str, season: int, episode: str, title: str) -> str:
    """Disc/Plex episode grammar from ``episode_filename``, in an mp4 container."""
    mkv_name = episode_filename(show, season, episode, title)
    suffix = ".mkv"
    if not mkv_name.endswith(suffix):
        raise RuntimeError(f"episode_filename returned {mkv_name}")
    return f"{mkv_name[: -len(suffix)]}.mp4"


def _movie_filename(title: str, year: int | None) -> str:
    safe = _component(title, what="title")
    if year is None:
        return f"{safe}.mp4"
    return f"{safe} ({int(year)}).mp4"


def _refuse_animation_on_shows(path: Path, *, animated: bool, shows_root: Path) -> None:
    if not animated:
        return
    try:
        path.relative_to(shows_root)
    except ValueError:
        return
    raise ValueError(f"refusing to put animation on Shows: {path}")


def _library_roots(
    anime_root: Path | None,
    shows_root: Path | None,
    movies_root: Path | None,
) -> tuple[Path, Path, Path]:
    anime = pathmod.ANIME_NAS if anime_root is None else anime_root
    shows = pathmod.SHOWS_NAS if shows_root is None else shows_root
    movies = pathmod.MOVIES_NAS if movies_root is None else movies_root
    return anime, shows, movies


def youtube_destination(
    *,
    show: str | None,
    season: int | None,
    episode: str | None,
    title: str | None,
    movie: bool,
    animated: bool,
    year: int | None = None,
    anime_root: Path | None = None,
    shows_root: Path | None = None,
    movies_root: Path | None = None,
) -> Path:
    """NAS file path for one YouTube download. Does not create directories."""
    anime, shows, movies = _library_roots(anime_root, shows_root, movies_root)
    if movie:
        if title is None or not title.strip():
            raise ValueError("movie needs a title")
        share = nas_share_for(
            kind="movie",
            animated=animated,
            anime_root=anime,
            shows_root=shows,
            movies_root=movies,
        )
        path = share / _movie_filename(title, year)
    else:
        if show is None or not show.strip():
            raise ValueError("series episode needs a show name")
        if season is None:
            raise ValueError("series episode needs a season")
        if episode is None or not str(episode).strip():
            raise ValueError("series episode needs an episode number")
        if title is None or not title.strip():
            raise ValueError("series episode needs a title")
        show_name = _component(show, what="show")
        title_name = _component(title, what="title")
        episode_name = _episode_token(episode)
        share = nas_share_for(
            kind="tv_disc",
            animated=animated,
            show=show_name,
            anime_root=anime,
            shows_root=shows,
            movies_root=movies,
        )
        path = share / f"Season {season}" / series_filename(
            show_name, season, episode_name, title_name
        )
    _refuse_animation_on_shows(path, animated=animated, shows_root=shows)
    return path


def _assert_no_disc_device(parts: Sequence[str]) -> None:
    for part in parts:
        if (
            part.startswith("/dev/sr")
            or part.startswith("dev:/dev/")
            or part.startswith("bluray:/dev/sr")
        ):
            raise ValueError("refusing to pass a disc device to yt-dlp")


def parse_flat_playlist(text: str) -> tuple[PlaylistVideo, ...]:
    videos: list[PlaylistVideo] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        index_text, video, title = (part.strip() for part in parts)
        if not index_text.isdigit() or not video or not title:
            continue
        videos.append(PlaylistVideo(int(index_text), video, title))
    if not videos:
        raise ValueError("playlist has no videos")
    return tuple(videos)


def flat_playlist_command(url: str, *, executable: Sequence[str]) -> tuple[str, ...]:
    _assert_no_disc_device((url, *executable))
    command = (
        *executable,
        "--flat-playlist",
        "--no-warnings",
        "--print",
        _FLAT_PRINT,
        url,
    )
    _assert_no_disc_device(command)
    return command


def _mapped_series(
    *,
    show: str | None,
    entry: YoutubeShow | None,
    youtube_map: YoutubeMap | None,
    url: str | None,
) -> YoutubeShow:
    resolved = entry
    if resolved is None:
        loaded = youtube_map if youtube_map is not None else load_youtube_map()
        resolved = match_youtube_show(loaded, show=show, url=url)
    if resolved is None:
        label = show or url or "playlist"
        raise ValueError(f"no YouTube show map entry for {label}")
    if resolved.kind != "series" or not resolved.show:
        raise ValueError(f"{resolved.id} is not a series in the YouTube show map")
    return resolved


def playlist_plans(
    videos: Sequence[PlaylistVideo],
    *,
    show: str | None = None,
    entry: YoutubeShow | None = None,
    youtube_map: YoutubeMap | None = None,
    url: str | None = None,
    season: int | None = None,
    episode: str | None = None,
    title: str | None = None,
    anime_root: Path | None = None,
    shows_root: Path | None = None,
    movies_root: Path | None = None,
    output_root: Path | None = None,
    executable: Sequence[str] | None = None,
) -> tuple[YoutubePlan, ...]:
    """Build one ``YoutubePlan`` per playlist video from the show map.

    ``season``, ``episode``, and ``title`` replace the parsed values when set.
    Library kind and the show folder come from the matched map entry.
    """
    mapped = _mapped_series(show=show, entry=entry, youtube_map=youtube_map, url=url)
    show_name = mapped.show
    assert show_name is not None
    plans: list[YoutubePlan] = []
    for video in videos:
        parsed_season, parsed_episode, parsed_title = parse_youtube_title(
            video.title,
            mapped,
            playlist_index=video.index,
        )
        if season is not None:
            parsed_season = season
        if episode is not None and episode.strip():
            parsed_episode = episode.strip()
        if title is not None and title.strip():
            parsed_title = title.strip()
        plans.append(
            build_youtube_plan(
                f"https://www.youtube.com/watch?v={video.video_id}",
                show=show_name,
                season=parsed_season,
                episode=parsed_episode,
                title=parsed_title,
                movie=False,
                animated=mapped.animated,
                year=mapped.year,
                anime_root=anime_root,
                shows_root=shows_root,
                movies_root=movies_root,
                output_root=output_root,
                executable=executable,
            )
        )
    return tuple(plans)


def ytdlp_command(
    url: str,
    *,
    output_template: Path,
    archive: Path,
    executable: Sequence[str],
) -> tuple[str, ...]:
    _assert_no_disc_device((url, *executable))
    command = (
        *executable,
        "--no-playlist",
        "--no-overwrites",
        "--sleep-requests",
        "1",
        "--merge-output-format",
        "mp4",
        "--download-archive",
        str(archive),
        "-f",
        "bestvideo+bestaudio/best",
        "-o",
        str(output_template),
        url,
    )
    _assert_no_disc_device(command)
    return command


def _work_dir(output_root: Path | None) -> Path:
    root = pathmod.OUTPUT_ROOT if output_root is None else output_root
    work = root / "youtube"
    try:
        work.relative_to(pathmod.STAGING_ROOT)
    except ValueError:
        return work
    raise ValueError(f"refusing to download under staging: {work}")


def build_youtube_plan(
    url: str,
    *,
    show: str | None,
    season: int | None,
    episode: str | None,
    title: str | None,
    movie: bool,
    animated: bool,
    year: int | None = None,
    anime_root: Path | None = None,
    shows_root: Path | None = None,
    movies_root: Path | None = None,
    output_root: Path | None = None,
    executable: Sequence[str] | None = None,
) -> YoutubePlan:
    text = url.strip()
    if not text:
        raise ValueError("URL is empty")
    _assert_no_disc_device((text,))
    destination = youtube_destination(
        show=show,
        season=season,
        episode=episode,
        title=title,
        movie=movie,
        animated=animated,
        year=year,
        anime_root=anime_root,
        shows_root=shows_root,
        movies_root=movies_root,
    )
    work = _work_dir(output_root)
    ident = video_id(text)
    archive = work / _ARCHIVE_NAME
    argv = tuple(executable) if executable is not None else resolve_ytdlp()
    command = ytdlp_command(
        text,
        output_template=work / f"{ident}.%(ext)s",
        archive=archive,
        executable=argv,
    )
    return YoutubePlan(
        url=text,
        destination=destination,
        temp_path=work / f"{ident}.mp4",
        archive=archive,
        command=command,
    )


def run_ytdlp(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), check=False)


def _returncode(completed: object) -> int:
    code = getattr(completed, "returncode", None)
    if isinstance(code, int):
        return code
    if isinstance(completed, int):
        return completed
    raise RuntimeError("yt-dlp runner did not return an exit code")


def ffprobe_has_video_and_audio(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    kinds = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return "video" in kinds and "audio" in kinds


def publish_no_overwrite(src: Path, dest: Path) -> Path:
    """Copy ``src`` onto ``dest`` unless ``dest`` already exists.

    ``O_EXCL`` leaves an existing file untouched, including one another
    download is still writing.
    """
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    try:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return dest
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    src.unlink()
    return dest


def download_youtube(
    plan: YoutubePlan,
    *,
    run: Callable[[Sequence[str]], object] | None = None,
    probe: Callable[[Path], bool] | None = None,
) -> Path:
    """Download to the game drive, then publish only after video and audio exist."""
    if plan.destination.is_file():
        return plan.destination
    runner = run_ytdlp if run is None else run
    checker = ffprobe_has_video_and_audio if probe is None else probe
    plan.temp_path.parent.mkdir(parents=True, exist_ok=True)
    code = _returncode(runner(plan.command))
    if code != 0:
        raise RuntimeError(f"yt-dlp exited {code}")
    if not plan.temp_path.is_file():
        raise RuntimeError(f"yt-dlp exited 0 but did not write {plan.temp_path}")
    if not checker(plan.temp_path):
        raise RuntimeError(f"ffprobe did not see video and audio; left {plan.temp_path}")
    if plan.destination.is_file():
        return plan.destination
    return publish_no_overwrite(plan.temp_path, plan.destination)


def download_playlist(
    plans: Sequence[YoutubePlan],
    *,
    run: Callable[[Sequence[str]], object] | None = None,
    probe: Callable[[Path], bool] | None = None,
) -> list[Path]:
    """Download each plan. Keep files that already landed if a later one fails."""
    saved: list[Path] = []
    errors: list[str] = []
    for plan in plans:
        try:
            saved.append(download_youtube(plan, run=run, probe=probe))
        except (OSError, RuntimeError) as exc:
            errors.append(f"{plan.url}: {exc}")
    if errors:
        raise RuntimeError("playlist incomplete: " + "; ".join(errors))
    return saved


def fetch_playlist(
    url: str,
    *,
    run: Callable[[Sequence[str]], object] | None = None,
    executable: Sequence[str] | None = None,
) -> tuple[PlaylistVideo, ...]:
    argv = tuple(executable) if executable is not None else resolve_ytdlp()
    command = flat_playlist_command(url, executable=argv)
    if run is None:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        completed = run(command)
    code = _returncode(completed)
    if code != 0:
        raise RuntimeError(f"yt-dlp exited {code}")
    text = getattr(completed, "stdout", "")
    if not isinstance(text, str):
        raise RuntimeError("yt-dlp runner did not return playlist text")
    return parse_flat_playlist(text)


def _require_identity(
    *,
    show: str | None,
    season: int | None,
    episode: str | None,
    title: str | None,
    movie: bool,
) -> None:
    if movie:
        if title is None or not title.strip():
            raise typer.BadParameter("A movie needs --title")
        return
    missing: list[str] = []
    if show is None or not show.strip():
        missing.append("--show")
    if season is None:
        missing.append("--season")
    if episode is None or not episode.strip():
        missing.append("--episode")
    if title is None or not title.strip():
        missing.append("--title")
    if missing:
        raise typer.BadParameter("A series episode needs " + ", ".join(missing))


@youtube_app.command("youtube", no_args_is_help=True)
def youtube_cmd(
    url: Annotated[str, typer.Argument(help="YouTube watch or playlist URL")],
    show: Annotated[
        Optional[str],
        typer.Option(
            "--show",
            help="Series name, or a show id from configs/youtube.yaml",
        ),
    ] = None,
    season: Annotated[
        Optional[int],
        typer.Option("--season", min=0, help="Season number"),
    ] = None,
    episode: Annotated[
        Optional[str],
        typer.Option("--episode", help="Episode number (zero-padded in the filename)"),
    ] = None,
    title: Annotated[
        Optional[str],
        typer.Option("--title", help="Episode or movie title used in the Plex filename"),
    ] = None,
    year: Annotated[
        Optional[int],
        typer.Option("--year", min=1, help="Movie year for Title (Year).mp4"),
    ] = None,
    movie: Annotated[
        bool,
        typer.Option("--movie/--series", help="Feature film, or a series episode"),
    ] = False,
    animated: Annotated[
        bool,
        typer.Option(
            "--animated/--live-action",
            help=(
                "Animated (Anime) or live-action (Shows / Movies) for one episode "
                "or a movie. A mapped playlist uses configs/youtube.yaml."
            ),
        ),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the destination and yt-dlp command; do not download"),
    ] = False,
    playlist: Annotated[
        bool,
        typer.Option(
            "--playlist",
            help=(
                "Download each video in the playlist. Season, episode, and title "
                "come from configs/youtube.yaml. --season, --episode, and --title override."
            ),
        ),
    ] = False,
) -> None:
    """Download a released YouTube episode, movie, or show playlist.

    Watchers and make watch-* never call this. A single episode uses the flags
    below. ``--playlist`` loads ``configs/youtube.yaml`` (``load_youtube_map``,
    ``match_youtube_show``, ``parse_youtube_title``) and builds a plan per video.
    ``--season``, ``--episode``, and ``--title`` override the map.
    """
    if playlist and movie:
        raise typer.BadParameter("--playlist is a series, not a movie")
    if playlist:
        try:
            entry = match_youtube_show(load_youtube_map(), show=show, url=url)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if entry is None:
            raise typer.BadParameter(
                "No show map entry matches this playlist. Add the show to configs/youtube.yaml."
            )
        if entry.kind != "series":
            raise typer.BadParameter(f"{entry.id} is a movie in the show map, not a playlist")
        try:
            videos = fetch_playlist(url)
            plans = playlist_plans(
                videos,
                entry=entry,
                season=season,
                episode=episode,
                title=title,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if dry_run:
            typer.echo("\n".join(plan.display() for plan in plans))
            return
        try:
            saved = download_playlist(plans)
        except (OSError, RuntimeError) as exc:
            console.print(f"[red]YouTube download failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        for dest in saved:
            typer.echo(dest)
        return
    _require_identity(show=show, season=season, episode=episode, title=title, movie=movie)
    try:
        plan = build_youtube_plan(
            url,
            show=show,
            season=season,
            episode=episode,
            title=title,
            movie=movie,
            animated=animated,
            year=year,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if dry_run:
        typer.echo(plan.display())
        return
    if plan.destination.is_file():
        typer.echo(plan.destination)
        return
    try:
        dest = download_youtube(plan)
    except (OSError, RuntimeError) as exc:
        console.print(f"[red]YouTube download failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(dest)
