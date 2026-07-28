"""VideosRipper CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from videosripper.paths import configure_process_tmp
from videosripper.logging_setup import (
    log_exception,
    log_scan,
    setup_scan_logger,
    setup_session_logger,
)
from videosripper.makemkv import scan, scan_to_json
from videosripper.models import MovieManifest, TvDiscManifest, load_manifest
from videosripper.monitor import MakeMkvError
from videosripper.movie import rip_movie
from videosripper.post_rip_verify import warn_title_count_explosion
from videosripper.runner import rip_tv_disc
from videosripper.title_verify import (
    format_title_issues,
    scan_title_hints,
    titles_from_scan_result,
    validate_manifest_titles,
)
from videosripper.sample_verify import preview_title, scan_and_preview

app = typer.Typer(no_args_is_help=True, help="Config-driven MakeMKV disc ripping")
console = Console(stderr=True)


def _load(path: Path) -> TvDiscManifest | MovieManifest:
    if not path.is_file():
        raise typer.BadParameter(f"Config not found: {path}")
    try:
        return load_manifest(path)
    except ValidationError as exc:
        console.print("[red]Validation failed:[/red]")
        console.print(exc)
        raise typer.Exit(code=1) from exc


@app.command()
def validate(
    config: Annotated[Path, typer.Argument(help="Path to YAML manifest")],
    with_scan: Annotated[
        bool,
        typer.Option(
            "--with-scan",
            help="Scan inserted disc and verify makemkv_title indices against scan",
        ),
    ] = False,
    scan_json: Annotated[
        Optional[Path],
        typer.Option(
            "--scan-json",
            help="Validate titles against saved scan JSON (from scan --json)",
        ),
    ] = None,
) -> None:
    """Validate a YAML manifest without touching the drive (unless --with-scan)."""
    manifest = _load(config)
    console.print(f"[green]OK[/green] {config} ({manifest.kind})")
    if isinstance(manifest, TvDiscManifest):
        console.print(f"  {manifest.meta.show} S{manifest.meta.season}: {len(manifest.tracks)} tracks")
        if manifest.skips:
            console.print(f"  skips: {len(manifest.skips)} documented")
    else:
        console.print(f"  {manifest.meta.title}")

    scan_data: dict | None = None
    if scan_json is not None:
        if not scan_json.is_file():
            console.print(f"[red]Scan JSON not found:[/red] {scan_json}")
            raise typer.Exit(code=1)
        scan_data = json.loads(scan_json.read_text(encoding="utf-8"))
    elif with_scan:
        device = manifest.meta.device
        minlength = manifest.meta.minlength
        setup_scan_logger(config, console=True)
        console.print(f"Scanning {device} (--minlength={minlength}) for title validation...")
        try:
            result = scan(device, minlength)
        except Exception as exc:
            log_exception(exc, user_message=str(exc))
            console.print(f"[red]Scan failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        scan_data = scan_to_json(result)

    if scan_data is not None:
        issues = validate_manifest_titles(manifest, scan_data)
        errors = [i for i in issues if i.level == "error"]
        warnings = [i for i in issues if i.level == "warn"]
        if warnings:
            for w in warnings:
                console.print(f"[yellow]WARNING:[/yellow] {w.message}")
        if errors:
            console.print("[red]Title validation failed:[/red]")
            console.print(format_title_issues(errors))
            raise typer.Exit(code=1)
        console.print("[green]Title validation OK[/green] against scan")


def _print_scan_table(result, minlength: int) -> None:
    titles = titles_from_scan_result(result)
    hints = scan_title_hints(titles, minlength=minlength)
    table = Table("Idx", "Duration", "Size", "Name", "Notes")
    for t in sorted(result.titles.values(), key=lambda x: x.index):
        size_gb = f"{t.size_bytes / (1024**3):.2f} GB" if t.size_bytes else ""
        note = hints.get(t.index, "")
        log_scan("%s  %s  %s  %s  %s", t.index, t.duration, size_gb, t.name, note)
        table.add_row(str(t.index), t.duration, size_gb, t.name, note)
    log_scan("(full makemkv output in debug.log)")
    console.print(table)


@app.command("scan-device")
def scan_device_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON scan summary")] = False,
    device: Annotated[
        Optional[str],
        typer.Option("--device", help="MakeMKV device (default: env or dev:/dev/sr0)"),
    ] = None,
    minlength: Annotated[
        int,
        typer.Option("--minlength", help="Minimum title length in seconds"),
    ] = 300,
) -> None:
    """Scan the inserted disc without a manifest."""
    target = device or os.environ.get("VIDEOSRIPPER_DEVICE", "dev:/dev/sr0")
    setup_scan_logger(None, console=not as_json)
    log_scan("Scanning %s (--minlength=%s)...", target, minlength)
    if not as_json:
        console.print(f"Scanning {target} (--minlength={minlength})...")
    try:
        result = scan(target, minlength)
    except Exception as exc:
        log_exception(exc, user_message=str(exc))
        console.print(f"[red]Scan failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    warn_title_count_explosion(len(result.titles))
    if as_json:
        typer.echo(json.dumps(scan_to_json(result), indent=2))
        return
    disc_name = result.disc_name or "(unknown)"
    log_scan("Disc: %s", disc_name)
    console.print(f"Disc: {disc_name}")
    if result.warnings:
        for w in result.warnings:
            log_scan("WARNING: %s", w)
            console.print(f"[yellow]WARNING:[/yellow] {w}")
    from videosripper.title_verify import analyze_obfuscation, titles_from_scan_result

    obf = analyze_obfuscation(titles_from_scan_result(result))
    if obf.detected:
        msg = (
            f"Obfuscated disc: {obf.duplicate_count} duplicate playlists at "
            f"{obf.dominant_duration_sec}s; recommended title {obf.recommended_title}; "
            f"decoys: {sorted(obf.decoy_indices) or 'none'}"
        )
        log_scan(msg)
        console.print(f"[yellow]{msg}[/yellow]")
    _print_scan_table(result, minlength)


@app.command()
def scan_cmd(
    config: Annotated[Path, typer.Argument(help="Path to YAML manifest")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON scan summary")] = False,
) -> None:
    """Scan the inserted disc using manifest minlength/device."""
    manifest = _load(config)
    device = manifest.meta.device
    minlength = manifest.meta.minlength
    setup_scan_logger(config, console=not as_json)
    log_scan("Scanning %s (--minlength=%s)...", device, minlength)
    if not as_json:
        console.print(f"Scanning {device} (--minlength={minlength})...")
    try:
        result = scan(device, minlength)
    except Exception as exc:
        log_exception(exc, user_message=str(exc))
        console.print(f"[red]Scan failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    warn_title_count_explosion(len(result.titles))
    if as_json:
        typer.echo(json.dumps(scan_to_json(result), indent=2))
        return
    disc_name = result.disc_name or "(unknown)"
    log_scan("Disc: %s", disc_name)
    console.print(f"Disc: {disc_name}")
    if result.warnings:
        for w in result.warnings:
            log_scan("WARNING: %s", w)
            console.print(f"[yellow]WARNING:[/yellow] {w}")
    from videosripper.title_verify import analyze_obfuscation, titles_from_scan_result

    obf = analyze_obfuscation(titles_from_scan_result(result))
    if obf.detected:
        msg = (
            f"Obfuscated disc: {obf.duplicate_count} duplicate playlists at "
            f"{obf.dominant_duration_sec}s; recommended title {obf.recommended_title}; "
            f"decoys: {sorted(obf.decoy_indices) or 'none'}"
        )
        log_scan(msg)
        console.print(f"[yellow]{msg}[/yellow]")
    _print_scan_table(result, minlength)


@app.command()
def preview(
    title: Annotated[int, typer.Option("--title", help="MakeMKV title index to preview")],
    seconds: Annotated[
        int,
        typer.Option("--seconds", help="Sample length after offset (default 45)"),
    ] = 45,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Skip into title before sampling (skip FBI loops)"),
    ] = 60,
    device: Annotated[
        Optional[str],
        typer.Option("--device", help="MakeMKV device (default: env or dev:/dev/sr0)"),
    ] = None,
    minlength: Annotated[
        int,
        typer.Option("--minlength", help="MakeMKV minlength (match scan/rip)"),
    ] = 300,
    chapter: Annotated[
        Optional[int],
        typer.Option(
            "--chapter",
            help="Rip only this chapter first (DVD manual mode, e.g. 2 to skip FBI ch1)",
        ),
    ] = None,
    chapter_end: Annotated[
        Optional[int],
        typer.Option("--chapter-end", help="Chapter range end (with --chapter)"),
    ] = None,
    scan_json: Annotated[
        Optional[Path],
        typer.Option("--scan-json", help="Scan JSON for duration context"),
    ] = None,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", help="Manifest YAML (uses its device/minlength)"),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option("--keep", help="Keep sample MKV in staging after check"),
    ] = False,
) -> None:
    """Rip a short title slice and verify it looks like real content (not FBI/menu)."""
    target_device = device or os.environ.get("VIDEOSRIPPER_DEVICE", "dev:/dev/sr0")
    target_minlength = minlength
    scan_data: dict | None = None
    if config is not None:
        manifest = _load(config)
        target_device = manifest.meta.device
        target_minlength = manifest.meta.minlength
    if scan_json is not None:
        if not scan_json.is_file():
            console.print(f"[red]Scan JSON not found:[/red] {scan_json}")
            raise typer.Exit(code=1)
        scan_data = json.loads(scan_json.read_text(encoding="utf-8"))

    setup_scan_logger(config, console=True)
    console.print(
        f"Preview title {title} on {target_device} "
        f"(offset={offset}s, sample={seconds}s"
        + (f", chapter={chapter}" if chapter is not None else "")
        + ")..."
    )
    try:
        if scan_data is None and config is not None and scan_json is None:
            result = scan_and_preview(
                target_device,
                title,
                seconds=seconds,
                offset=offset,
                minlength=target_minlength,
                chapter=chapter,
                chapter_end=chapter_end,
                keep=keep,
            )
        else:
            result = preview_title(
                target_device,
                title,
                seconds=seconds,
                offset=offset,
                minlength=target_minlength,
                chapter=chapter,
                chapter_end=chapter_end,
                scan_data=scan_data,
                keep=keep,
            )
    except Exception as exc:
        log_exception(exc, user_message=str(exc))
        console.print(f"[red]Preview failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result.duration_sec is not None:
        console.print(f"  sample duration: {result.duration_sec}s")
    if result.black_ratio is not None:
        console.print(f"  black/static ratio: {result.black_ratio:.0%}")
    if result.silence_ratio is not None:
        console.print(f"  silence ratio: {result.silence_ratio:.0%}")
    if result.sample_path is not None:
        console.print(f"  kept sample: {result.sample_path}")

    if result.ok:
        console.print("[green]PASS[/green] — sample looks like real content")
        return
    console.print("[red]FAIL[/red] — sample verification failed:")
    for reason in result.reasons:
        console.print(f"  - {reason}")
    raise typer.Exit(code=1)


@app.command("rip")
def rip_cmd(
    config: Annotated[Path, typer.Argument(help="Path to YAML manifest")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print plan only")] = False,
    start_at: Annotated[
        Optional[int],
        typer.Option("--start-at", help="Resume from episode slot (tv_disc)"),
    ] = None,
    on_stall: Annotated[
        Optional[str],
        typer.Option(
            "--on-stall",
            help="On rip stall: exit (kill makemkv) or warn (default: manifest/env)",
        ),
    ] = None,
    retry: Annotated[
        Optional[int],
        typer.Option(
            "--retry",
            help=(
                "Retries after first failure before user intervention "
                "(default: 3 = 4 total attempts; then eject + exit)"
            ),
        ),
    ] = None,
    retry_delay: Annotated[
        Optional[float],
        typer.Option(
            "--retry-delay",
            help="Seconds to wait before retry (default: 30)",
        ),
    ] = None,
    sample_verify: Annotated[
        bool,
        typer.Option(
            "--sample-verify",
            help="Pre-rip 45s sample per title (slow; off by default)",
        ),
    ] = False,
    sample_seconds: Annotated[
        int,
        typer.Option("--sample-seconds", help="With --sample-verify: sample length"),
    ] = 45,
    sample_offset: Annotated[
        int,
        typer.Option("--sample-offset", help="With --sample-verify: skip into title"),
    ] = 60,
) -> None:
    """Rip a disc from a validated manifest."""
    manifest = _load(config)
    start = start_at
    if start is None and os.environ.get("VIDEOSRIPPER_START_AT"):
        start = int(os.environ["VIDEOSRIPPER_START_AT"])
    if on_stall is not None and on_stall not in ("exit", "warn"):
        console.print("[red]--on-stall must be 'exit' or 'warn'[/red]")
        raise typer.Exit(code=1)
    if retry is not None and retry < 0:
        console.print("[red]--retry must be >= 0[/red]")
        raise typer.Exit(code=1)
    if retry_delay is not None and retry_delay < 0:
        console.print("[red]--retry-delay must be >= 0[/red]")
        raise typer.Exit(code=1)

    setup_session_logger(config)

    try:
        if isinstance(manifest, TvDiscManifest):
            plan = rip_tv_disc(
                manifest,
                dry_run=dry_run,
                start_at=start,
                on_stall=on_stall,
                retry=retry,
                retry_delay=retry_delay,
                sample_verify=sample_verify,
                sample_seconds=sample_seconds,
                sample_offset=sample_offset,
            )
            if dry_run:
                table = Table("Slot", "Action", "Title", "Output", "Skip")
                for item in plan:
                    table.add_row(
                        str(item.slot),
                        item.action,
                        str(item.makemkv_title),
                        item.output.name,
                        "yes" if item.skip else "",
                    )
                console.print(table)
        else:
            rip_movie(
                manifest,
                dry_run=dry_run,
                on_stall=on_stall,
                retry=retry,
                retry_delay=retry_delay,
                sample_verify=sample_verify,
                sample_seconds=sample_seconds,
                sample_offset=sample_offset,
            )
    except (MakeMkvError, RuntimeError, OSError) as exc:
        log_exception(exc, user_message=str(exc))
        console.print(f"[red]Rip failed:[/red] {exc}")
        from videosripper.retry import is_disc_retry_exhaustion_failure

        if is_disc_retry_exhaustion_failure(exc):
            console.print(
                "[yellow]User intervention required:[/yellow] "
                "disc was ejected after retries — clean/re-seat, re-insert, then re-run the rip "
                "(TV: --start-at N to resume)."
            )
        raise typer.Exit(code=1) from exc


@app.command("identify")
def identify_cmd(
    device: Annotated[
        Optional[str],
        typer.Option("--device", help="MakeMKV device (default: env or dev:/dev/sr0)"),
    ] = None,
    minlength: Annotated[
        int,
        typer.Option("--minlength", help="Minimum title length in seconds"),
    ] = 300,
    scan_json: Annotated[
        Optional[Path],
        typer.Option("--scan-json", help="Use existing scan JSON instead of scanning"),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit identification JSON")] = False,
    no_mount: Annotated[
        bool,
        typer.Option("--no-mount", help="Skip Blu-ray bdmt mount/parse"),
    ] = False,
    no_lookup: Annotated[
        bool,
        typer.Option("--no-lookup", help="Skip TMDb/OMDb runtime lookup"),
    ] = False,
) -> None:
    """Identify a disc: bdmt XML title + runtime DB candidates for generic labels."""
    from videosripper.identify import gather_identification_hints
    from videosripper.makemkv import scan, scan_to_json

    target = device or os.environ.get("VIDEOSRIPPER_DEVICE", "dev:/dev/sr0")
    if scan_json is not None:
        if not scan_json.is_file():
            console.print(f"[red]Scan JSON not found:[/red] {scan_json}")
            raise typer.Exit(code=1)
        scan_data = json.loads(scan_json.read_text(encoding="utf-8"))
    else:
        try:
            scan_data = scan_to_json(scan(target, minlength))
        except Exception as exc:
            console.print(f"[red]Scan failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    hints = gather_identification_hints(
        scan_data,
        device=None if no_mount else target,
        try_bluray_mount=not no_mount,
        try_runtime_lookup=not no_lookup,
    )
    payload = {
        "disc_name": scan_data.get("disc_name"),
        "identification": hints.to_dict(),
        "best_title": hints.best_title(),
    }
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(f"Disc label: {scan_data.get('disc_name') or '(unknown)'}")
    console.print(f"Generic label: {hints.is_generic_label}")
    if hints.feature_duration_sec:
        console.print(f"Feature duration: {hints.feature_duration_sec}s")
    if hints.bluray_title:
        console.print(f"[green]Blu-ray bdmt title:[/green] {hints.bluray_title}")
    if hints.candidates:
        console.print("Runtime candidates:")
        for c in hints.candidates:
            console.print(f"  - {c.display()} [{c.source}]")
    if hints.best_title():
        console.print(f"[bold]Best guess:[/bold] {hints.best_title()}")
    for note in hints.notes:
        console.print(f"[dim]{note}[/dim]")


def main() -> None:
    configure_process_tmp()
    app()


if __name__ == "__main__":
    main()
