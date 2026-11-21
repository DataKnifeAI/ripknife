"""Opt-in damaged-disc recovery.

The CLI commands stay manual. ``recover auto`` prints one next ladder step
and does not open the drive. After one bad-sector retry the pipeline may
call :func:`decide_failure_action` and :func:`run_capped_sector_recovery`.
That path is a function: it asks the planner for the next capped step and
runs only that step's commands. It does not LLM-loop. None of this replaces
a bad pressing.

The first ``ddrescue -b 2048 -d`` image is the scan. Later steps resume that
same ISO and map. There is no separate pre-scan. ``recover auto`` has no
execute switch.

A clean map (0 bad, 0 unfinished) is a drive remux of the feature
(``makemkvcon mkv`` on ``dev:/dev/srN``), not ``makemkvcon iso:`` and not a
stitch. The spindle is the last speed that increased rescued bytes. A
trailing small-area gain does not replace an earlier one; ``eject -x 4`` is
only the retry after that remux fails. ``eject -x`` sets spindle speed;
``eject -x 0`` restores it. ``eject -t`` is not a load command on a slot
drive. Process exit 0 is not success. ``info`` succeeds when the feature
title is listed. ``mkv`` succeeds on ``Copy complete`` or ``titles saved``.
``Failed to open disc`` or ``TCOUNT:0`` fails either one. After the remux,
verify the file (video and audio, one ffmpeg null decode with zero stderr).
MakeMKV's HH:MM:SS is truncated whole seconds: the same displayed second,
or a fraction under 1s past that integer, matches. A fractional feature
duration stays inside 0.5s. That file is the movie. Do not stitch it. A
finished drive remux with no read errors may copy to the share while the
image still has bad bytes outside that read. A stitch, and a remux that
hit read errors, still need a clean map. A gapped stitch is never a NAS
copy. A one-frame non-monotonic DTS line is the stitch verify, not this
one. Stitch and align run only after the drive remux fails. The image is
the sector copy, not the movie. ``--title`` is the operator's index. This
module does not pick it. The longest playlist can be a stereo alternate.

The one stitch includes a stream-copy align: trim each piece that overran
its planned span, then cut the new file at the feature duration. A tail trim
of the concat is not that align. One stitch verify follows. Neither reads
the drive and neither is an LLM call. ``recover`` does not copy to the NAS.

MakeMKV reads ``io_IgnoreReadErrors`` only from ``~/.MakeMKV/settings.conf``.
``recover makemkv`` sets that key for one title and restores the previous
file. It refuses to edit the file while any ``makemkvcon`` is running
(including libmmbd ``guiserver``). The ladder does not schedule that rip.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer
from rich.console import Console

from ripknife.logging_setup import log_debug, setup_scan_logger
from ripknife.paths import MAKEMKV_BIN, OUTPUT_ROOT, ROOT, TMP_ROOT, disc_dev

console = Console(stderr=True)

# GNU ddrescue is not vendored. PATH wins; this is the unpacked copy already
# on the game drive from a local package extract.
KNOWN_DDRESCUE = ROOT / ".tmp/tools/ddrescue-pkg/usr/bin/ddrescue"

# Keys a recovery MakeMKV invocation may set. app_BackupDecrypted is not one
# of them: disc backup is not a recovery path.
IGNORE_READ_KEYS: dict[str, str] = {
    "io_IgnoreReadErrors": "1",
    "io_ErrorRetryCount": "1",
}

recover_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Manual damaged-disc recovery. Watchers never call these. "
        "Does not replace a bad pressing."
    ),
)


@dataclass(frozen=True)
class Span:
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class StitchPlan:
    output: Path
    segment_dir: Path
    spans: tuple[Span, ...]
    segment_cmds: tuple[tuple[str, ...], ...]
    segment_paths: tuple[Path, ...]
    concat_list: Path
    concat_cmd: tuple[str, ...]

    def display_lines(self) -> list[str]:
        prefix = f"MAKEMKVCON={shlex.quote(str(MAKEMKV_BIN))} "
        lines: list[str] = []
        for span, cmd in zip(self.spans, self.segment_cmds, strict=True):
            lines.append(
                f"# span {span.start_sec:.3f}-{span.end_sec:.3f} ({span.duration_sec:.3f}s)"
            )
            lines.append(prefix + shlex.join(cmd))
        lines.append("# concat")
        lines.append(shlex.join(self.concat_cmd))
        return lines


def block_device(device: str) -> str:
    """``/dev/srN`` from ``/dev/srN``, ``dev:/dev/srN``, or ``bluray:/dev/srN``."""
    text = device.strip()
    if text.startswith("bluray:"):
        text = text[len("bluray:") :]
    path = disc_dev(text)
    if not path.startswith("/dev/"):
        raise ValueError(
            f"device must be /dev/srN or dev:/dev/srN (got {device!r})"
        )
    return path


def makemkv_device(device: str) -> str:
    return "dev:" + block_device(device)


def under_output(path: Path) -> Path:
    """Resolve ``path`` and require it to stay inside ``OUTPUT_ROOT``.

    Relative paths are relative to the output root, not the working directory.
    ``clip.mkv`` becomes ``$RIPKNIFE_OUTPUT/clip.mkv``. A relative path
    that already starts with the output directory's name (``output/clip.mkv``
    when the root is named ``output``) is that same file, not
    ``output/output/clip.mkv``.
    """
    root = Path(os.path.abspath(OUTPUT_ROOT))
    if path.is_absolute():
        candidate = Path(os.path.abspath(path))
    else:
        relative = Path(os.path.normpath(str(path)))
        if relative.parts and relative.parts[0] == root.name:
            candidate = Path(os.path.abspath(root.parent / relative))
        else:
            candidate = Path(os.path.abspath(root / relative))
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path must stay under {root}: {candidate}")
    if candidate == root:
        raise ValueError(f"path must be a file or directory inside {root}")
    return candidate


def parse_clock(text: str) -> float:
    """Seconds from ``SS``, ``MM:SS``, or ``HH:MM:SS`` (fractional seconds ok)."""
    raw = text.strip()
    if not raw:
        raise ValueError("empty timestamp")
    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp {text!r}")
    try:
        nums = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"bad timestamp {text!r}") from exc
    if any(num < 0 for num in nums):
        raise ValueError(f"bad timestamp {text!r}")
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]


def parse_skip(text: str) -> Span:
    raw = text.strip()
    if "-" not in raw:
        raise ValueError(f"skip range must be START-END, got {text!r}")
    left, right = raw.split("-", 1)
    start = parse_clock(left)
    end = parse_clock(right)
    if end <= start:
        raise ValueError(f"skip range end must be after start: {text}")
    return Span(start, end)


def readable_spans(duration_sec: float, skips: Sequence[Span]) -> tuple[Span, ...]:
    """Spans to copy, in order, with each skip range cut out.

    ``skips`` are holes. They are sorted. Overlaps are an error. A leading or
    trailing hole produces no empty span. The picture jumps at each join.
    """
    if duration_sec <= 0:
        raise ValueError("duration must be positive")
    if not skips:
        raise ValueError("at least one skip range is required")
    ordered = sorted(skips, key=lambda span: (span.start_sec, span.end_sec))
    prev_end = 0.0
    for span in ordered:
        if span.start_sec < prev_end:
            raise ValueError(
                "skip ranges overlap: "
                f"{prev_end:.3f}s meets {span.start_sec:.3f}-{span.end_sec:.3f}s"
            )
        if span.end_sec > duration_sec + 0.05:
            raise ValueError(
                f"skip {span.start_sec:.3f}-{span.end_sec:.3f}s "
                f"extends past duration {duration_sec:.3f}s"
            )
        prev_end = max(prev_end, span.end_sec)
    covered = 0.0
    spans: list[Span] = []
    for hole in ordered:
        if hole.start_sec - covered > 0.05:
            spans.append(Span(covered, hole.start_sec))
        covered = max(covered, hole.end_sec)
    if duration_sec - covered > 0.05:
        spans.append(Span(covered, duration_sec))
    if not spans:
        raise ValueError("skip ranges cover the whole title")
    return tuple(spans)


def span_copy_complete(reached_sec: float, requested_sec: float, *, slack_sec: float = 1.0) -> bool:
    """True when a stream copy got within ``slack_sec`` of the requested length.

    ``ffmpeg -c copy -t`` on a Blu-ray often stops advancing ``out_time`` about
    a second short of ``-t`` and then never exits. That file is done. It is
    not a stall and it is not a reason to delete the chunk.
    """
    if requested_sec <= 0 or slack_sec < 0:
        raise ValueError("requested duration must be positive and slack must be >= 0")
    return reached_sec >= requested_sec - slack_sec


def resolve_ddrescue() -> Path:
    """``ddrescue`` on ``PATH``, else the known temp binary, else an error."""
    found = shutil.which("ddrescue")
    if found:
        return Path(found)
    if KNOWN_DDRESCUE.is_file() and os.access(KNOWN_DDRESCUE, os.X_OK):
        return KNOWN_DDRESCUE
    raise FileNotFoundError(
        "ddrescue not on PATH and not at "
        f"{KNOWN_DDRESCUE}. Install GNU ddrescue; do not vendor the binary."
    )


def resolve_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise FileNotFoundError("ffmpeg not on PATH")
    return found


def resolve_ffprobe() -> str:
    found = shutil.which("ffprobe")
    if not found:
        raise FileNotFoundError("ffprobe not on PATH")
    return found


def makemkvcon_pids() -> list[int]:
    """PIDs of ``makemkvcon``, including libmmbd ``guiserver``. Empty if none.

    ``pgrep`` exit 1 means no match. Any other failure refuses the edit rather
    than guessing that the settings file is free.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "makemkvcon"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            "cannot check for makemkvcon; refusing to edit settings.conf or start a recovery read"
        ) from exc
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise RuntimeError(
            "pgrep makemkvcon failed; refusing to edit settings.conf or start a recovery read"
        )
    return [int(part) for part in proc.stdout.split() if part.strip().isdigit()]


def assert_makemkvcon_idle() -> None:
    pids = makemkvcon_pids()
    if not pids:
        return
    listed = ", ".join(str(pid) for pid in pids)
    raise RuntimeError(
        "makemkvcon is running "
        f"(pids {listed}), including a libmmbd guiserver if that is all you see. "
        "Refusing to edit ~/.MakeMKV/settings.conf or start another recovery read. "
        "Leave it alone while a disc read is in progress."
    )


def _settings_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key


def render_settings(original: str, updates: dict[str, str]) -> str:
    """Drop ``updates`` keys from ``original`` and append quoted values."""
    kept = [line for line in original.splitlines() if _settings_key(line) not in updates]
    for key, value in updates.items():
        kept.append(f'{key} = "{value}"')
    text = "\n".join(kept)
    if not text.endswith("\n"):
        text += "\n"
    return text


def settings_path() -> Path:
    return Path.home() / ".MakeMKV" / "settings.conf"


@contextmanager
def scoped_ignore_read_errors(path: Path | None = None) -> Iterator[None]:
    """Set ignore-read-errors for one MakeMKV run and restore the previous bytes.

    Refuses while any makemkvcon is running. Does not set ``app_BackupDecrypted``.
    A ``SIGKILL`` during the run can skip the restore; check the two ``io_*`` keys
    afterward.
    """
    assert_makemkvcon_idle()
    target = path if path is not None else settings_path()
    original = target.read_text(encoding="utf-8") if target.is_file() else None
    updated = render_settings(original or "", IGNORE_READ_KEYS)
    assert_makemkvcon_idle()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    os.chmod(target, 0o600)
    try:
        yield
    finally:
        if original is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(original, encoding="utf-8")
            os.chmod(target, 0o600)


def ffmpeg_env() -> dict[str, str]:
    from ripknife.makemkv import makemkv_env

    env = makemkv_env()
    env["MAKEMKVCON"] = str(MAKEMKV_BIN)
    return env


def build_makemkv_cmd(
    device: str,
    title: int,
    output_dir: Path,
    *,
    minlength: int,
) -> list[str]:
    if title < 0:
        raise ValueError("--title must be >= 0")
    if minlength < 0:
        raise ValueError("--minlength must be >= 0")
    return [
        str(MAKEMKV_BIN),
        "mkv",
        "-r",
        "--progress=-same",
        f"--minlength={minlength}",
        makemkv_device(device),
        str(title),
        str(output_dir),
    ]


def build_ddrescue_cmd(
    device: str,
    iso: Path,
    mapfile: Path,
    *,
    binary: Path,
    sector_size: int = 2048,
    retrim: bool = False,
) -> list[str]:
    """Resume command: ``-b 2048 -d`` plus the existing map.

    ``retrim`` adds one ``-M`` (check hole edges). Never passes ``-r``.
    Does not set spindle speed; ``--max-read-rate`` would not either.
    """
    if sector_size <= 0:
        raise ValueError("--sector-size must be positive")
    cmd = [
        str(binary),
        "-b",
        str(sector_size),
        "-d",
    ]
    if retrim:
        cmd.append("-M")
    cmd.extend(
        [
            block_device(device),
            str(iso),
            str(mapfile),
        ]
    )
    return cmd


def default_mapfile(iso: Path) -> Path:
    """``disc.iso`` → ``disc.map`` (suffix replaced, not appended)."""
    return iso.with_suffix(".map")


# Worst case for one disc. The first image is the scan; nothing before it.
BUDGET_SUMMARY = (
    "worst case: 1 normal rip, 1 image (the scan), "
    "2 unfinished slow resumes, 1 large-hole retrim, "
    "1 small-area -r1, 1 drive remux, 1 stitch (including align), 1 verify, 3 LLM calls"
)
# Printed rules only. The three decisions are ripknife.llm; this planner does not call them.
LLM_RULE_LINES = (
    "LLM calls: at most 3 for one disc, none inside the byte loop",
    "LLM 1: bad sectors vs wrong title vs tray vs tooling; else do not start ddrescue",
    "LLM 2: other owned disc, different-brand drive, or accept a gap; not another speed",
    "LLM 3: if verify fails or the stitch has gaps, keep it for review or discard it; never a NAS copy",
    "functions: map read, speed, -r cap, path check, zero-gain stop, empty-span, skip estimate, drive remux speed, align, verify",
    "handoff: one bad-sector retry, then this planner; no LLM loop; recover auto stays dry-run",
)
# Same window as ``span_copy_complete``. Short by more than this is a gap.
ALIGN_KEEP_SLACK_SEC = 1.0
# Final file versus a known fractional feature duration. Outside this, verify fails.
VERIFY_TOLERANCE_SEC = 0.5
# MakeMKV TINFO drops the fraction. A container may sit this far past that integer.
MAKEMKV_CLOCK_FRACTION_SEC = 1.0
SPOT_DECODE_SEC = 2.0
SPEED_UNFINISHED = (25, 12)
SPEED_LARGE_RETRIM = 12
SPEED_SMALL_RETRY = 4
SPEED_RESTORE = 0
_EJECT_SPEEDS = frozenset({SPEED_RESTORE, SPEED_SMALL_RETRY, *SPEED_UNFINISHED})
_GAIN_SPEEDS = frozenset({*SPEED_UNFINISHED, SPEED_LARGE_RETRIM, SPEED_SMALL_RETRY})
STALL_LIMIT_SEC = 4 * 60
SMALL_BAD_MAX = 4 * 1024 * 1024
MAX_UNFINISHED_PASSES = 2
MAX_ZERO_GAIN_PASSES = 2
MAX_RETRY = 1
# image, two unfinished, large retrim, small -r1, two drive remuxes.
# Stitch and verify are planner steps without a command tuple.
MAX_AUTO_COMMAND_STEPS = 8
LLM_CALL_BUDGET = 3

_MAP_STATUS = {
    "?": "non_tried",
    "*": "non_trimmed",
    "/": "non_scraped",
    "-": "bad_sector",
    "+": "rescued",
}
_STATUS_FIELD_RE = re.compile(
    r"(rescued|non-tried|non-trimmed|non-scraped|bad-sector):\s*([0-9][0-9,]*)\s*B\b",
    re.IGNORECASE,
)
_BAD_AREAS_RE = re.compile(r"bad areas:\s*([0-9]+)", re.IGNORECASE)
_READ_ERROR_RE = re.compile(r"read errors?", re.IGNORECASE)
_ZERO_READ_ERRORS_RE = re.compile(r"\b0\s+read errors?\b", re.IGNORECASE)
_POSITIVE_READ_ERRORS_RE = re.compile(r"\b[1-9]\d*\s+read errors?\b", re.IGNORECASE)
_READ_ERROR_AT_RE = re.compile(r"read errors?\s+at\b", re.IGNORECASE)
_NO_READ_ERRORS_RE = re.compile(r"\bno read errors?\b", re.IGNORECASE)
_HASH_CHECK_RE = re.compile(
    r"hash[- ]check[^\n]{0,80}(?:error|fail)|(?:error|fail)[^\n]{0,80}hash[- ]check",
    re.IGNORECASE,
)
_L_EC_RE = re.compile(r"L-EC", re.IGNORECASE)
_FAILED_SAVE_RE = re.compile(r"Failed to save title", re.IGNORECASE)
_FAILED_OPEN_DISC_RE = re.compile(r"Failed to open disc", re.IGNORECASE)
_NO_MKV_RE = re.compile(r"No MKV produced", re.IGNORECASE)
_MISSING_DISC_RE = re.compile(
    r"tray(?: is)? open"
    r"|no disc"
    r"|no medium"
    r"|device disappeared"
    r"|device not found"
    r"|no such device"
    r"|not found — insert disc"
    r"|Failed to open disc",
    re.IGNORECASE,
)
_NONRECOVERY_STOP_RE = re.compile(
    r"MAP_AMBIGUOUS"
    r"|wrong[- ]title"
    r"|identification failed"
    r"|identification\.ambiguous"
    r"|ambiguous map"
    r"|NAS COPY FAILED"
    r"|NAS copy failed"
    r"|NAS copy: failed",
    re.IGNORECASE,
)

FAILURE_RETRY_NORMAL = "retry_normal"
FAILURE_START_RECOVERY = "start_recovery"
FAILURE_WAIT_REINSERT = "wait_reinsert"
FAILURE_STOP = "stop"
_TCOUNT_VALUE_RE = re.compile(r"TCOUNT:\s*(\d+)\b", re.IGNORECASE)
_TINFO_TITLE_RE = re.compile(r"TINFO:\s*\d+,", re.IGNORECASE)
_TITLE_LISTED_RE = re.compile(
    r"(?:Title\s+#\d+\b|was added as title\s+#?\d+\b)",
    re.IGNORECASE,
)
_COPY_COMPLETE_RE = re.compile(r"Copy complete", re.IGNORECASE)
_TITLES_SAVED_RE = re.compile(r"\b([1-9]\d*)\s+titles?\s+saved\b", re.IGNORECASE)
_STALL_LINE_RE = re.compile(r"stall|no progress|stuck", re.IGNORECASE)
_STALL_OFFSET_RE = re.compile(
    r"(?:offset|at|position)\s*[:=]?\s*(\d{4,})",
    re.IGNORECASE,
)
_SAME_OFFSET_PHRASE_RE = re.compile(r"stall(?:ed)? at the same offset", re.IGNORECASE)

REASON_IMAGE = "first ddrescue image is the scan; no separate pre-scan"
REASON_ZERO = "two zero-gain passes; stop the drive loop"
REASON_UNFINISHED_CAP = "two unfinished resumes already spent; stop the drive loop"
REASON_STILL_LARGE = "bad bytes still a large hole after the one retrim; stop the drive loop"
REASON_CLEAN = "map has 0 bad and 0 unfinished; stop the sector loop"
REASON_REMUX = "map has 0 bad and 0 unfinished; drive remux of the feature, not an ISO"
REASON_REMUX_RETRY = "first drive remux errored; one retry at eject -x 4"
REASON_REMUX_VERIFY = "drive remux reported success; verify that file before any NAS copy"
REASON_REMUX_DONE = "verified drive remux is the movie; do not stitch it"
REASON_STITCH = "one stitch after the drive loop; do not start another speed cycle"
REASON_VERIFY = "one verify after align; do not start another speed cycle"
REASON_REPORT = "report and stop; do not start another speed cycle"
REASON_BUDGET = "drive budget is spent; stop the drive loop"


@dataclass(frozen=True)
class MapStatus:
    """Byte counts from a ddrescue map. No drive I/O."""

    rescued: int = 0
    non_tried: int = 0
    non_trimmed: int = 0
    non_scraped: int = 0
    bad_sector: int = 0
    bad_areas: int = 0

    def unfinished_bytes(self) -> int:
        """Bytes a plain resume can still read. Bad sectors are not unfinished."""
        return self.non_tried + self.non_trimmed + self.non_scraped


@dataclass(frozen=True)
class LadderState:
    """How much of the budget is already spent. Counts only, no drive I/O."""

    image_done: bool = False
    unfinished_passes: int = 0
    large_retrim_done: bool = False
    small_retry_done: bool = False
    zero_gain_streak: int = 0
    gain_speeds: tuple[int, ...] = ()
    remux_errors: int = 0
    drive_remux_done: bool = False
    drive_remux_failed: bool = False
    stitch_done: bool = False
    verify_done: bool = False

    def __post_init__(self) -> None:
        if self.unfinished_passes < 0 or self.unfinished_passes > MAX_UNFINISHED_PASSES:
            raise ValueError(
                f"unfinished resumes are capped at {MAX_UNFINISHED_PASSES}"
            )
        if self.zero_gain_streak < 0:
            raise ValueError("zero-gain streak must be >= 0")
        if self.remux_errors < 0:
            raise ValueError("remux errors must be >= 0")
        if self.drive_remux_done and self.drive_remux_failed:
            raise ValueError("drive remux cannot be both done and failed")
        for speed in self.gain_speeds:
            if speed not in _GAIN_SPEEDS:
                raise ValueError(f"gain speed must be one of {sorted(_GAIN_SPEEDS)}")
        if self.verify_done and not (self.stitch_done or self.drive_remux_done):
            raise ValueError("verify follows a drive remux or the stitch")


@dataclass(frozen=True)
class LadderStep:
    kind: str
    reason: str
    eject: tuple[str, ...] | None = None
    ddrescue: tuple[str, ...] | None = None
    makemkv: tuple[str, ...] | None = None
    restore: tuple[str, ...] | None = None
    notes: tuple[str, ...] = ()

    def display_lines(self) -> list[str]:
        lines = [f"# next: {self.kind}", f"# {self.reason}"]
        lines.extend(f"# {note}" for note in self.notes)
        for cmd in (self.eject, self.ddrescue, self.makemkv, self.restore):
            if cmd:
                lines.append(shlex.join(cmd))
        return lines

    def command_lines(self) -> list[str]:
        return [line for line in self.display_lines() if not line.startswith("#")]


@dataclass(frozen=True)
class NormalFailEvidence:
    """Signals that the one normal rip already failed. Not a cause judgment."""

    read_error: bool = False
    hash_check: bool = False
    l_ec: bool = False
    failed_to_save_title: bool = False
    stalled_same_offset: bool = False

    @property
    def failed(self) -> bool:
        return bool(
            self.read_error
            or self.hash_check
            or self.l_ec
            or self.failed_to_save_title
            or self.stalled_same_offset
        )


def prepare_image_paths(iso: Path, mapfile: Path | None = None) -> tuple[Path, Path]:
    """One ISO and its map under the output root. ``output/file`` is not doubled."""
    dest = under_output(iso)
    mapped = under_output(mapfile) if mapfile is not None else under_output(default_mapfile(dest))
    return dest, mapped


def assert_resume_paths(
    iso: Path,
    mapfile: Path,
    *,
    previous_iso: Path | None = None,
    previous_map: Path | None = None,
) -> None:
    """Later passes reuse the image. A second output path is refused."""
    if previous_iso is not None and os.path.abspath(iso) != os.path.abspath(previous_iso):
        raise ValueError("refusing a second output path; resume the same ISO and map")
    if previous_map is not None and os.path.abspath(mapfile) != os.path.abspath(previous_map):
        raise ValueError("refusing a second map; resume the same ISO and map")


def build_eject_speed_cmd(device: str, speed: int) -> list[str]:
    """``eject -x`` sets spindle speed. ``ddrescue --max-read-rate`` does not."""
    if speed not in _EJECT_SPEEDS:
        raise ValueError(f"spindle step must be one of {sorted(_EJECT_SPEEDS)}")
    return ["eject", "-x", str(speed), block_device(device)]


def build_ladder_ddrescue_cmd(
    device: str,
    iso: Path,
    mapfile: Path,
    *,
    binary: Path,
    sector_size: int = 2048,
    retrim: bool = False,
    retry: int = 0,
) -> list[str]:
    """Same ISO and map. ``retry`` is 0 or 1. Never ``-R``."""
    if retry < 0 or retry > MAX_RETRY:
        raise ValueError(f"ddrescue -r is capped at {MAX_RETRY}")
    cmd = build_ddrescue_cmd(
        device,
        iso,
        mapfile,
        binary=binary,
        sector_size=sector_size,
        retrim=retrim,
    )
    if retry:
        cmd = [*cmd[:-3], "-r", str(retry), *cmd[-3:]]
    if "-R" in cmd or "--reverse" in cmd:
        raise ValueError("ddrescue -R is not a recovery step")
    return cmd


def parse_ddrescue_map(text: str) -> MapStatus:
    """Sum mapfile extents. Does not open a drive."""
    totals = {
        "rescued": 0,
        "non_tried": 0,
        "non_trimmed": 0,
        "non_scraped": 0,
        "bad_sector": 0,
    }
    bad_areas = 0
    saw = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            size = int(parts[1], 16)
        except ValueError:
            continue
        status = parts[2]
        if status not in _MAP_STATUS:
            continue
        totals[_MAP_STATUS[status]] += size
        if status == "-":
            bad_areas += 1
        saw = True
    if not saw:
        raise ValueError("mapfile has no data lines")
    return MapStatus(bad_areas=bad_areas, **totals)


def parse_ddrescuelog_status(text: str) -> MapStatus:
    """Parse ``ddrescuelog --show-status`` fields. Integer byte counts only."""
    found: dict[str, int] = {}
    for match in _STATUS_FIELD_RE.finditer(text):
        key = match.group(1).lower().replace("-", "_")
        found[key] = int(match.group(2).replace(",", ""))
    areas = _BAD_AREAS_RE.search(text)
    required = {"rescued", "non_tried", "non_trimmed", "non_scraped", "bad_sector"}
    if areas is None or set(found) != required:
        raise ValueError(
            "status must include rescued, non-tried, non-trimmed, "
            "non-scraped, bad-sector (bytes), and bad areas"
        )
    return MapStatus(
        rescued=found["rescued"],
        non_tried=found["non_tried"],
        non_trimmed=found["non_trimmed"],
        non_scraped=found["non_scraped"],
        bad_sector=found["bad_sector"],
        bad_areas=int(areas.group(1)),
    )


def map_holes(text: str) -> tuple[tuple[int, int], ...]:
    """``(start, end)`` byte holes for every map extent that is not rescued."""
    holes: list[tuple[int, int]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3 or parts[2] not in _MAP_STATUS or parts[2] == "+":
            continue
        try:
            start = int(parts[0], 16)
            size = int(parts[1], 16)
        except ValueError:
            continue
        holes.append((start, start + size))
    return tuple(holes)


def _map_clean(status: MapStatus) -> bool:
    return status.unfinished_bytes() == 0 and status.bad_sector == 0 and status.bad_areas == 0


def _restore(device: str) -> tuple[str, ...]:
    return tuple(build_eject_speed_cmd(device, SPEED_RESTORE))


def next_drive_step(
    status: MapStatus | None,
    state: LadderState,
    *,
    device: str,
    iso: Path,
    mapfile: Path,
    binary: Path,
) -> LadderStep:
    """One drive attempt, or stop. Does not run it.

    Order: the image (the scan); unfinished plain resumes at ``eject -x 25``
    then ``12``; one ``-M`` with no ``-r`` at ``12``; one ``-M -r1`` at ``4``
    only when the remaining bad bytes are at most 4 MiB. Two zero-gain passes
    stop the loop. A clean map stops the sector loop. It does not remux.
    """
    if state.zero_gain_streak >= MAX_ZERO_GAIN_PASSES:
        return LadderStep(kind="stop", reason=REASON_ZERO, restore=_restore(device))
    if not state.image_done:
        cmd = build_ladder_ddrescue_cmd(device, iso, mapfile, binary=binary)
        return LadderStep(
            kind="image",
            reason=REASON_IMAGE,
            ddrescue=tuple(cmd),
            notes=(
                "do not pre-scan with ffmpeg, MakeMKV, readom, or a second ddrescue",
                "later passes resume this ISO and map; rescued bytes are not recopied",
                "do not repeat the normal rip with ignore-read-errors",
            ),
        )
    if status is None:
        raise ValueError("map status is required after the image")
    if _map_clean(status):
        return LadderStep(kind="stop", reason=REASON_CLEAN, restore=_restore(device))
    if status.unfinished_bytes() > 0:
        if state.unfinished_passes >= MAX_UNFINISHED_PASSES:
            return LadderStep(
                kind="stop",
                reason=REASON_UNFINISHED_CAP,
                restore=_restore(device),
            )
        speed = SPEED_UNFINISHED[state.unfinished_passes]
        cmd = build_ladder_ddrescue_cmd(device, iso, mapfile, binary=binary)
        which = state.unfinished_passes + 1
        return LadderStep(
            kind="unfinished",
            reason=f"unfinished resume {which}/2 at eject -x {speed}",
            eject=tuple(build_eject_speed_cmd(device, speed)),
            ddrescue=tuple(cmd),
            notes=(
                "plain resume reads non-tried, non-trimmed, and non-scraped only",
                "bad sectors stay bad; do not pass -M or -r on this step",
                f"interrupt after {STALL_LIMIT_SEC}s at 0 B/s with no rescued-byte increase",
            ),
        )
    if status.bad_sector > 0 and not state.large_retrim_done:
        cmd = build_ladder_ddrescue_cmd(
            device, iso, mapfile, binary=binary, retrim=True, retry=0
        )
        return LadderStep(
            kind="large_retrim",
            reason="one large-hole retrim (-M, no -r) at eject -x 12",
            eject=tuple(build_eject_speed_cmd(device, SPEED_LARGE_RETRIM)),
            ddrescue=tuple(cmd),
            notes=(
                "this is the one attempt on sectors the map already marked bad",
                "it runs before -r1, including when the remainder is already small",
                f"interrupt after {STALL_LIMIT_SEC}s at 0 B/s with no rescued-byte increase",
            ),
        )
    if status.bad_sector > SMALL_BAD_MAX:
        return LadderStep(kind="stop", reason=REASON_STILL_LARGE, restore=_restore(device))
    if status.bad_sector > 0 and not state.small_retry_done:
        cmd = build_ladder_ddrescue_cmd(
            device, iso, mapfile, binary=binary, retrim=True, retry=1
        )
        return LadderStep(
            kind="small_retry",
            reason="one small-area retrim (-M -r1) at eject -x 4; -r capped at 1",
            eject=tuple(build_eject_speed_cmd(device, SPEED_SMALL_RETRY)),
            ddrescue=tuple(cmd),
            notes=("no -R", f"only when bad bytes are <= {SMALL_BAD_MAX}"),
        )
    return LadderStep(kind="stop", reason=REASON_BUDGET, restore=_restore(device))


@dataclass(frozen=True)
class RemuxSpeed:
    """Spindle for one drive remux. ``failed`` means stitch is next."""

    speed: int | None
    failed: bool


def last_rescued_speed(gain_speeds: Sequence[int]) -> int | None:
    """Last ``eject -x`` that increased rescued bytes. Not an LLM call.

    Speeds are in pass order and include only passes that gained bytes.
    A trailing small-area speed does not replace an earlier gain. That
    slower speed is the one retry after the first drive remux errors.
    """
    if not gain_speeds:
        return None
    if gain_speeds[-1] == SPEED_SMALL_RETRY and any(
        speed != SPEED_SMALL_RETRY for speed in gain_speeds
    ):
        for speed in reversed(gain_speeds):
            if speed != SPEED_SMALL_RETRY:
                return speed
    return gain_speeds[-1]


def choose_remux_speed(gain_speeds: Sequence[int], *, remux_errors: int) -> RemuxSpeed:
    """Speed for the next drive remux. Not an LLM call.

    The first try uses :func:`last_rescued_speed`. No recorded gain means
    remux the drive without inventing a spindle speed. One error retries at
    the small-area speed when the first try was faster. A further error, or
    an error when the first try was already that slow, means the remux failed.
    """
    if remux_errors < 0:
        raise ValueError("remux errors must be >= 0")
    primary = last_rescued_speed(gain_speeds)
    if remux_errors == 0:
        return RemuxSpeed(speed=primary, failed=False)
    if remux_errors == 1 and primary is not None and primary != SPEED_SMALL_RETRY:
        return RemuxSpeed(speed=SPEED_SMALL_RETRY, failed=False)
    return RemuxSpeed(speed=None, failed=True)


def _drive_remux_failed(state: LadderState) -> bool:
    if state.drive_remux_failed:
        return True
    if state.drive_remux_done:
        return False
    return choose_remux_speed(state.gain_speeds, remux_errors=state.remux_errors).failed


def _eject_speed(step: LadderStep) -> int | None:
    if step.eject is None or len(step.eject) < 3 or step.eject[1] != "-x":
        return None
    return int(step.eject[2])


def drive_remux_dir(iso: Path, mkv_output: Path | None) -> Path:
    """Directory for the drive mkv. Never the image path."""
    if mkv_output is not None:
        return mkv_output
    return iso.parent / f"{iso.stem}-drive"


def build_drive_remux_cmd(
    device: str,
    title: int,
    output_dir: Path,
    *,
    minlength: int,
    iso: Path,
) -> list[str]:
    """Normal ``makemkvcon mkv`` of the drive title. Not ``iso:``.

    Whether ``iso`` exists is irrelevant. The image is not an argument.
    """
    cmd = build_makemkv_cmd(device, title, output_dir, minlength=minlength)
    if any(str(part).startswith("iso:") for part in cmd):
        raise ValueError("drive remux uses the drive, not iso:")
    if any(str(part) == str(iso) for part in cmd):
        raise ValueError("drive remux does not read the image")
    if makemkv_device(device) not in cmd:
        raise ValueError("drive remux must name the drive device")
    return cmd


def _remux_notes(choice: RemuxSpeed) -> tuple[str, ...]:
    if choice.speed is not None and choice.speed != SPEED_SMALL_RETRY:
        retry = (
            "if the log is not Copy complete or titles saved, "
            "the next step is one drive remux at eject -x 4"
        )
    else:
        retry = (
            "if the log is not Copy complete or titles saved, "
            "the drive remux has failed and the next step is stitch"
        )
    spindle = (
        "no slow pass increased rescued bytes; do not invent a spindle speed"
        if choice.speed is None
        else f"function: last speed that increased rescued bytes is eject -x {choice.speed}, not an LLM call"
    )
    return (
        spindle,
        "normal makemkvcon mkv of the feature on the drive; not iso:",
        "the image is only the sector copy; do not pass it to makemkvcon",
        "not ignore-read-errors and not an ISO remux",
        "function: makemkv_log_succeeded; exit 0 is not an argument and is not success",
        "info succeeds when the feature title is listed",
        "a drive mkv succeeds on Copy complete or titles saved",
        "Failed to open disc or TCOUNT 0 is a failure even when the process exits 0",
        "do not stitch while the drive title is still the next step",
        "after Copy complete or titles saved, verify that file; do not stitch it when that verify passes",
        retry,
        "this print does not open the drive",
    )


def _drive_remux_verify_notes() -> tuple[str, ...]:
    """File check after a drive remux. Not ``recover align`` and not a stitch."""
    return (
        "function: assess_drive_remux_verify, one pass, not an LLM call and not a disc read",
        "not recover align and not a stitch",
        "duration within 0.5s of a fractional feature duration",
        "MakeMKV HH:MM:SS is truncated; the same displayed second or a fraction under 1s past that integer matches",
        "a miss of more than about 1s, or a short file, still fails",
        "expected video and audio streams present",
        "one ffmpeg -v error -map 0:v:0 -map 0:a -f null -",
        "zero stderr bytes; a one-frame non-monotonic DTS line is the stitch path, not this one",
        "0 stderr and a duration match means this file is the movie",
        "do not stitch a drive remux that verifies",
        "if verify fails, stop and report; do not stitch and do not copy to NAS",
        "NAS copy only after this verify passes",
        "a drive remux with no read errors may copy while the image still has bad bytes outside that read",
        "a remux that hit read errors still needs a clean map",
        "do not copy a stereo-only alternate of the feature",
        "recover does not copy to NAS",
    )


def _drive_remux_step(
    state: LadderState,
    *,
    device: str,
    iso: Path,
    title: int,
    mkv_output: Path | None,
    minlength: int,
) -> LadderStep:
    choice = choose_remux_speed(state.gain_speeds, remux_errors=state.remux_errors)
    if choice.failed or state.drive_remux_failed:
        raise ValueError("drive remux already failed")
    dest = drive_remux_dir(iso, mkv_output)
    cmd = tuple(
        build_drive_remux_cmd(device, title, dest, minlength=minlength, iso=iso)
    )
    eject = None
    if choice.speed is not None:
        eject = tuple(build_eject_speed_cmd(device, choice.speed))
    if state.remux_errors:
        reason = REASON_REMUX_RETRY
    elif choice.speed is None:
        reason = REASON_REMUX + "; no slow pass increased rescued bytes"
    else:
        reason = f"{REASON_REMUX}; eject -x {choice.speed}"
    return LadderStep(
        kind="drive_remux",
        reason=reason,
        eject=eject,
        makemkv=cmd,
        notes=_remux_notes(choice),
    )


def next_recovery_step(
    status: MapStatus | None,
    state: LadderState,
    *,
    device: str,
    iso: Path,
    mapfile: Path,
    binary: Path,
    title: int = 0,
    mkv_output: Path | None = None,
    minlength: int = 300,
) -> LadderStep:
    """Next function step: one drive attempt, one drive remux, one stitch, or stop.

    A clean map (0 bad, 0 unfinished) prints a drive remux of the feature.
    The command uses the drive, not ``iso:``, even when the image file
    exists. After that remux reports success, the next step is a verify of
    that file, then the normal NAS copy is allowed. Stitch is not that
    path. Stitch runs once when bytes are still missing, or when that drive
    remux has already failed. Align is part of that stitch: it does not
    read the disc and it is not an LLM call. The stitch verify runs once
    after the stitch. A failed verify stops the ladder.
    """
    drive = next_drive_step(
        status,
        state,
        device=device,
        iso=iso,
        mapfile=mapfile,
        binary=binary,
    )
    if drive.kind != "stop":
        return drive
    if status is not None and _map_clean(status) and not _drive_remux_failed(state):
        if state.drive_remux_done:
            if not state.verify_done:
                return LadderStep(
                    kind="verify",
                    reason=REASON_REMUX_VERIFY,
                    restore=drive.restore,
                    notes=_drive_remux_verify_notes(),
                )
            return LadderStep(
                kind="stop",
                reason=REASON_REMUX_DONE,
                restore=drive.restore,
                notes=(
                    "verified drive remux is the movie; do not stitch it",
                    "do not open the image with makemkvcon iso:",
                    "NAS copy only if that verify passed; recover does not copy",
                    "bad bytes outside a drive remux that reported no read errors do not block that copy",
                    "do not start another speed cycle or another remux",
                ),
            )
        return _drive_remux_step(
            state,
            device=device,
            iso=iso,
            title=title,
            mkv_output=mkv_output,
            minlength=minlength,
        )
    if status is None:
        return drive
    stitch_notes = (
        "split a 0-byte span once; if both halves are empty, skip it",
        "do not retry an empty span",
        "a chunk within 1 second of the requested duration is done",
        "do not overwrite an existing stitch output; align writes a new file",
        "estimate skips from hole offsets, feature size, and duration",
        "the LLM does not place each skip",
        "function: build_align_plan, part of this stitch, not an LLM call and not a disc read",
        "stream-copy trim each overrun to its planned span; a tail trim is not alignment",
        "cut the aligned output at the feature duration",
        "a piece short by more than 1 second is a gap; do not invent picture",
        "gapped output stays in the recovery directory; never a NAS copy",
        "LLM 2 decides another disc, another brand, or a gap; not another speed",
    )
    if _map_clean(status):
        stitch_notes = ("drive remux already failed; stitch is the fallback",) + stitch_notes
    if not state.stitch_done:
        return LadderStep(
            kind="stitch",
            reason=REASON_STITCH,
            restore=drive.restore,
            notes=stitch_notes,
        )
    if not state.verify_done:
        return LadderStep(
            kind="verify",
            reason=REASON_VERIFY,
            restore=drive.restore,
            notes=(
                "function: build_verify_plan, one pass, not an LLM call and not a loop",
                "duration within 0.5s of the feature duration",
                "expected video and audio streams present",
                "ffmpeg -v error -map 0:v:0 -map 0:a -f null - with zero errors",
                "a one-frame non-monotonic DTS line is not an error on this stitch verify",
                "that DTS skip is not the drive-remux verify; that path needs zero stderr bytes",
                "short spot-decodes at the planned boundaries",
                "any error, a duration miss, or a recorded gap is not a finished movie",
                "if verify fails, stop and report; do not start another speed cycle",
                "NAS copy only after verify passes and the map has no missing bytes",
                "this command does not copy to NAS",
            ),
        )
    return LadderStep(
        kind="stop",
        reason=REASON_REPORT,
        restore=drive.restore,
        notes=(
            "verify already ran once; do not run it again",
            "LLM 3 only if verify failed or the stitch has gaps: keep the file for review or discard it",
            "NAS copy only after verify passes and the map has no missing bytes",
            "recover does not copy to NAS",
            "do not scan again and do not start another speed cycle",
        ),
    )


def format_recovery_plan(step: LadderStep) -> str:
    """Budget, the function/LLM split, and the one next step. Nothing is run."""
    lines = [BUDGET_SUMMARY, *LLM_RULE_LINES, *step.display_lines(), "# not executed"]
    return "\n".join(lines) + "\n"


def pass_should_interrupt(*, seconds_at_zero_bps: float, rescued_increased: bool) -> bool:
    """Stop a pass that sits at 0 B/s with no rescued-byte increase."""
    if seconds_at_zero_bps < 0:
        raise ValueError("seconds must be >= 0")
    if rescued_increased:
        return False
    return seconds_at_zero_bps >= STALL_LIMIT_SEC


def next_zero_gain_streak(streak: int, bytes_gained: int) -> int:
    """Two passes in a row that add zero bytes end the drive loop."""
    if streak < 0:
        raise ValueError("zero-gain streak must be >= 0")
    if bytes_gained < 0:
        raise ValueError("bytes gained must be >= 0")
    if bytes_gained > 0:
        return 0
    return streak + 1


def _with_gain(state: LadderState, step: LadderStep, *, bytes_gained: int) -> tuple[int, tuple[int, ...]]:
    """Zero-gain streak, plus the speed when this pass increased rescued bytes."""
    streak = next_zero_gain_streak(state.zero_gain_streak, bytes_gained)
    speed = _eject_speed(step)
    if bytes_gained > 0 and speed in _GAIN_SPEEDS:
        return streak, (*state.gain_speeds, speed)
    return streak, state.gain_speeds


def advance_ladder(state: LadderState, step: LadderStep, *, bytes_gained: int) -> LadderState:
    """Record one finished attempt. A stop is not an attempt.

    A drive-remux advance means that attempt errored. A finished movie is
    ``drive_remux_done``, not another pass. A ddrescue pass that gains bytes
    appends its ``eject -x`` speed.
    """
    if step.kind == "stop":
        raise ValueError("a stop step is not a pass")
    if step.kind == "stitch":
        if state.stitch_done:
            raise ValueError("stitch already used")
        return replace(state, stitch_done=True)
    if step.kind == "verify":
        if not state.stitch_done and not state.drive_remux_done:
            raise ValueError("verify follows a drive remux or the stitch")
        if state.verify_done:
            raise ValueError("verify already used")
        return replace(state, verify_done=True)
    if step.kind == "drive_remux":
        choice = choose_remux_speed(state.gain_speeds, remux_errors=state.remux_errors)
        if choice.failed or state.drive_remux_failed or state.drive_remux_done:
            raise ValueError("drive remux already failed or finished")
        return replace(state, remux_errors=state.remux_errors + 1)
    streak, gains = _with_gain(state, step, bytes_gained=bytes_gained)
    if step.kind == "image":
        return replace(state, image_done=True, zero_gain_streak=streak, gain_speeds=gains)
    if step.kind == "unfinished":
        done = state.unfinished_passes + 1
        if done > MAX_UNFINISHED_PASSES:
            raise ValueError(f"unfinished resumes are capped at {MAX_UNFINISHED_PASSES}")
        return replace(
            state,
            unfinished_passes=done,
            zero_gain_streak=streak,
            gain_speeds=gains,
        )
    if step.kind == "large_retrim":
        if state.large_retrim_done:
            raise ValueError("large-hole retrim already used")
        return replace(
            state,
            large_retrim_done=True,
            zero_gain_streak=streak,
            gain_speeds=gains,
        )
    if step.kind == "small_retry":
        if state.small_retry_done:
            raise ValueError("small-area -r1 already used")
        return replace(
            state,
            small_retry_done=True,
            zero_gain_streak=streak,
            gain_speeds=gains,
        )
    raise ValueError(f"unknown step {step.kind}")


def _line_has_read_error(line: str) -> bool:
    if not _READ_ERROR_RE.search(line):
        return False
    if _POSITIVE_READ_ERRORS_RE.search(line) or _READ_ERROR_AT_RE.search(line):
        return True
    if _ZERO_READ_ERRORS_RE.search(line):
        remainder = _ZERO_READ_ERRORS_RE.sub("", line)
        return _READ_ERROR_RE.search(remainder) is not None
    return True


def _stalled_same_offset(text: str) -> bool:
    if _SAME_OFFSET_PHRASE_RE.search(text):
        return True
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if not _STALL_LINE_RE.search(line):
            continue
        for match in _STALL_OFFSET_RE.finditer(line):
            offset = match.group(1)
            counts[offset] = counts.get(offset, 0) + 1
    return any(count >= 2 for count in counts.values())


def normal_mode_evidence(text: str) -> NormalFailEvidence:
    """Read the rip log. Does not decide why it failed."""
    return NormalFailEvidence(
        read_error=any(_line_has_read_error(line) for line in text.splitlines()),
        hash_check=_HASH_CHECK_RE.search(text) is not None,
        l_ec=_L_EC_RE.search(text) is not None,
        failed_to_save_title=_FAILED_SAVE_RE.search(text) is not None,
        stalled_same_offset=_stalled_same_offset(text),
    )


def normal_mode_failed(text: str) -> bool:
    return normal_mode_evidence(text).failed


def _strong_sector_signals(text: str) -> bool:
    """L-EC, MakeMKV read errors, hash-check, or Failed to save title."""
    evidence = normal_mode_evidence(text)
    return bool(
        evidence.l_ec
        or evidence.read_error
        or evidence.hash_check
        or evidence.failed_to_save_title
    )


def bad_sector_failure(text: str) -> bool:
    """Bad-sector class, including ``No MKV produced`` after those errors.

    An empty tray wins: ``No MKV produced`` plus a missing disc is not a
    sector failure and must not be imaged.
    """
    if _strong_sector_signals(text):
        return True
    if _NO_MKV_RE.search(text) and _MISSING_DISC_RE.search(text) is None:
        return True
    return False


def failure_stops_reread(text: str) -> bool:
    """Ambiguous map, wrong title, identification, or a NAS copy failure."""
    return _NONRECOVERY_STOP_RE.search(text) is not None


def decide_failure_action(text: str, *, normal_retries_used: int = 0) -> str:
    """One of ``retry_normal``, ``start_recovery``, ``wait_reinsert``, ``stop``.

    No drive I/O. ``normal_retries_used`` is how many in-rip retries already
    ran. A reinsert does not pass 0 once that retry is spent: the same text
    then stays ``start_recovery`` instead of another full-speed rip.
    ``wait_reinsert`` is not a bad-sector retry. ``stop`` does not enter
    sector recovery.
    """
    if normal_retries_used < 0:
        raise ValueError("normal_retries_used must be >= 0")
    if failure_stops_reread(text):
        return FAILURE_STOP
    # An empty tray is not a sector retry, even if the same text mentions L-EC.
    if _MISSING_DISC_RE.search(text):
        return FAILURE_WAIT_REINSERT
    if bad_sector_failure(text):
        if normal_retries_used >= 1:
            return FAILURE_START_RECOVERY
        return FAILURE_RETRY_NORMAL
    return FAILURE_STOP


def _command_tuples(step: LadderStep) -> tuple[tuple[str, ...], ...]:
    found: list[tuple[str, ...]] = []
    for cmd in (step.eject, step.ddrescue, step.makemkv):
        if cmd:
            found.append(tuple(cmd))
    return tuple(found)


def assert_capped_command(cmd: Sequence[str]) -> None:
    """The planner's argv only: no ``-R``, no ``iso:``, ``-r`` stays 1."""
    if "-R" in cmd or "--reverse" in cmd:
        raise ValueError("ddrescue -R is not a recovery step")
    if any(str(part).startswith("iso:") for part in cmd):
        raise ValueError("recovery remux uses the drive, not iso:")
    if "-r" in cmd:
        values = list(cmd)
        retry = values[values.index("-r") + 1]
        if retry != "1":
            raise ValueError("ddrescue -r stays 1")


def execute_capped_step(
    step: LadderStep,
    run: Callable[[Sequence[str]], str],
) -> str:
    """Run one planner step. A stop, stitch, or verify has no command here."""
    logs: list[str] = []
    for cmd in _command_tuples(step):
        assert_capped_command(cmd)
        logs.append(run(tuple(cmd)) or "")
    return "\n".join(logs)


def run_recovery_command(cmd: Sequence[str]) -> str:
    """One planner command. Refuses to start while ``makemkvcon`` is running."""
    assert_capped_command(cmd)
    assert_makemkvcon_idle()
    log_debug("$ %s", shlex.join(cmd))
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"command exited {proc.returncode}: {shlex.join(list(cmd))}"
        )
    return output


def dump_ladder_state(state: LadderState) -> str:
    payload = {
        "image_done": state.image_done,
        "unfinished_passes": state.unfinished_passes,
        "large_retrim_done": state.large_retrim_done,
        "small_retry_done": state.small_retry_done,
        "zero_gain_streak": state.zero_gain_streak,
        "gain_speeds": list(state.gain_speeds),
        "remux_errors": state.remux_errors,
        "drive_remux_done": state.drive_remux_done,
        "drive_remux_failed": state.drive_remux_failed,
        "stitch_done": state.stitch_done,
        "verify_done": state.verify_done,
    }
    return json.dumps(payload, indent=2) + "\n"


def load_ladder_state(text: str) -> LadderState:
    data = json.loads(text)
    speeds = data.get("gain_speeds") or ()
    return LadderState(
        image_done=bool(data.get("image_done")),
        unfinished_passes=int(data.get("unfinished_passes", 0)),
        large_retrim_done=bool(data.get("large_retrim_done")),
        small_retry_done=bool(data.get("small_retry_done")),
        zero_gain_streak=int(data.get("zero_gain_streak", 0)),
        gain_speeds=tuple(int(speed) for speed in speeds),
        remux_errors=int(data.get("remux_errors", 0)),
        drive_remux_done=bool(data.get("drive_remux_done")),
        drive_remux_failed=bool(data.get("drive_remux_failed")),
        stitch_done=bool(data.get("stitch_done")),
        verify_done=bool(data.get("verify_done")),
    )


def run_capped_sector_recovery(
    text: str,
    *,
    normal_retries_used: int,
    device: str,
    iso: Path,
    mapfile: Path | None = None,
    binary: Path | None = None,
    state: LadderState | None = None,
    status: MapStatus | None = None,
    title: int = 0,
    minlength: int = 300,
    run: Callable[[Sequence[str]], str] | None = None,
    read_status: Callable[[], MapStatus | None] | None = None,
    makemkv_pids: Sequence[int] | None = None,
    on_state: Callable[[LadderState], None] | None = None,
) -> tuple[LadderStep, ...]:
    """Planner steps after the one bad-sector retry. Nothing otherwise.

    ``run`` executes one planned command. Omit it and the function returns
    the next step without opening the drive. ``recover auto`` stays that
    dry-run. Stitch and verify have no argv on the step; the loop stops
    there instead of inventing a playlist or an ffmpeg line. A second image
    is the planner's refusal, not another cycle. ``-r`` stays 1. No ``-R``.
    No ``iso:``.
    """
    if (
        decide_failure_action(text, normal_retries_used=normal_retries_used)
        != FAILURE_START_RECOVERY
    ):
        return ()
    pids = list(makemkv_pids) if makemkv_pids is not None else makemkvcon_pids()
    if recovery_blocked(makemkv_pids=pids, lock_held=False):
        return ()
    iso_path, mapped = prepare_image_paths(iso, mapfile)
    ddrescue = binary if binary is not None else Path("ddrescue")
    ladder = state or LadderState()
    current = status
    if run is None:
        return (
            next_recovery_step(
                current,
                ladder,
                device=device,
                iso=iso_path,
                mapfile=mapped,
                binary=ddrescue,
                title=title,
                minlength=minlength,
            ),
        )
    steps: list[LadderStep] = []
    for _ in range(MAX_AUTO_COMMAND_STEPS):
        step = next_recovery_step(
            current,
            ladder,
            device=device,
            iso=iso_path,
            mapfile=mapped,
            binary=ddrescue,
            title=title,
            minlength=minlength,
        )
        if step.kind == "stop" or not _command_tuples(step):
            steps.append(step)
            break
        before = 0 if current is None else current.rescued
        log = execute_capped_step(step, run)
        if read_status is not None:
            current = read_status()
        if step.kind == "drive_remux":
            if makemkv_log_succeeded(log):
                ladder = replace(ladder, drive_remux_done=True)
            else:
                ladder = advance_ladder(ladder, step, bytes_gained=0)
        else:
            after = before if current is None else current.rescued
            ladder = advance_ladder(ladder, step, bytes_gained=max(0, after - before))
        steps.append(step)
        if on_state is not None:
            on_state(ladder)
    else:
        raise RuntimeError("recovery command cap spent; not another cycle")
    return tuple(steps)


def makemkv_log_succeeded(text: str) -> bool:
    """Whether the MakeMKV log itself reports success.

    The process exit code is not an argument. Exit 0 is not success.
    ``info`` succeeds when a feature title is listed (``TCOUNT`` greater
    than 0, a ``TINFO`` line, or ``Title #N``). ``mkv`` succeeds on
    ``Copy complete`` or a positive ``titles saved``. ``Failed to open
    disc`` or ``TCOUNT:0`` fails both.
    """
    if _FAILED_OPEN_DISC_RE.search(text):
        return False
    counts = [int(match.group(1)) for match in _TCOUNT_VALUE_RE.finditer(text)]
    if any(count == 0 for count in counts):
        return False
    if _COPY_COMPLETE_RE.search(text) or _TITLES_SAVED_RE.search(text):
        return True
    if any(count > 0 for count in counts):
        return True
    if _TINFO_TITLE_RE.search(text) or _TITLE_LISTED_RE.search(text):
        return True
    return False


def device_lock_held(lock_path: Path) -> bool:
    """True when an existing lock file is flocked. Does not create the file."""
    if not lock_path.exists():
        return False
    if not lock_path.is_file():
        raise RuntimeError(f"device lock path is not a file: {lock_path}")
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError as exc:
        raise RuntimeError(
            f"cannot check device lock {lock_path}; refusing to start"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def recovery_blocked(*, makemkv_pids: Sequence[int], lock_held: bool) -> str | None:
    """Why a recovery read must not start. ``None`` when both gates are clear."""
    if makemkv_pids:
        listed = ", ".join(str(pid) for pid in makemkv_pids)
        return f"makemkvcon is running (pids {listed})"
    if lock_held:
        return "device lock is held"
    return None


def estimate_skip(
    start_byte: int,
    end_byte: int,
    *,
    feature_bytes: int,
    duration_sec: float,
) -> Span:
    """Constant-bitrate skip from a hole, the feature size, and its duration.

    The LLM does not place this range. Odd cases (wrong playlist, several
    large titles, an estimate far from the observed read errors) are the
    LLM's to judge.
    """
    if feature_bytes <= 0 or duration_sec <= 0:
        raise ValueError("feature size and duration must be positive")
    if start_byte < 0 or end_byte <= start_byte:
        raise ValueError("hole end must be after start")
    if end_byte > feature_bytes:
        raise ValueError("hole extends past the feature")
    return Span(
        duration_sec * start_byte / feature_bytes,
        duration_sec * end_byte / feature_bytes,
    )


def estimate_skips(
    holes: Sequence[tuple[int, int]],
    *,
    feature_bytes: int,
    duration_sec: float,
) -> tuple[Span, ...]:
    return tuple(
        estimate_skip(start, end, feature_bytes=feature_bytes, duration_sec=duration_sec)
        for start, end in holes
    )


def split_span_once(span: Span) -> tuple[Span, Span]:
    """Two halves of a 0-byte span. The caller tries each half once."""
    mid = span.start_sec + (span.duration_sec / 2)
    if mid <= span.start_sec or mid >= span.end_sec:
        raise ValueError("span is too short to split")
    return Span(span.start_sec, mid), Span(mid, span.end_sec)


def empty_span_decision(*, nbytes: int, already_split: bool) -> str:
    """``keep``, ``split_once``, or ``skip``.

    A positive chunk is kept. A 0-byte chunk splits once. Both empty halves
    are a skip. Do not retry an empty span.
    """
    if nbytes < 0:
        raise ValueError("byte count must be >= 0")
    if nbytes > 0:
        return "keep"
    if already_split:
        return "skip"
    return "split_once"


def refuse_stitch_overwrite(path: Path) -> None:
    if path.exists():
        raise RuntimeError(f"output already exists: {path}")


def render_recovery_report(
    *,
    tried: Sequence[str],
    rescued_before: int,
    rescued_after: int,
    bad_before: int,
    bad_after: int,
    non_scraped_before: int,
    non_scraped_after: int,
    gaps: Sequence[Span] = (),
    stitch_path: Path | None = None,
    gapped: bool = False,
) -> str:
    """What was tried and what is left. Does not copy to NAS."""
    lines = [BUDGET_SUMMARY, "tried:"]
    lines.extend(f"- {item}" for item in tried)
    lines.append(f"rescued before: {rescued_before}")
    lines.append(f"rescued after: {rescued_after}")
    lines.append(f"bad before: {bad_before}")
    lines.append(f"bad after: {bad_after}")
    lines.append(f"non-scraped before: {non_scraped_before}")
    lines.append(f"non-scraped after: {non_scraped_after}")
    if gaps:
        for gap in gaps:
            lines.append(f"gap: {gap.start_sec:.3f}-{gap.end_sec:.3f}")
    else:
        lines.append("gaps: none")
    if stitch_path is not None:
        lines.append(f"stitch: {stitch_path}")
        if gapped:
            lines.append("gapped stitch stays in the recovery directory")
    lines.append("NAS copy: not a recovery step")
    return "\n".join(lines) + "\n"


def _ffmpeg_base(ffmpeg: str) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
    ]


def build_ffmpeg_remux_cmd(device: str, playlist: int, output: Path, *, ffmpeg: str) -> list[str]:
    if playlist < 0:
        raise ValueError("--playlist must be >= 0")
    return [
        *_ffmpeg_base(ffmpeg),
        "-playlist",
        str(playlist),
        "-i",
        f"bluray:{block_device(device)}",
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-map",
        "0:s?",
        "-c",
        "copy",
        str(output),
    ]


def build_span_cmd(
    device: str,
    playlist: int,
    span: Span,
    segment: Path,
    *,
    ffmpeg: str,
) -> list[str]:
    cmd = _ffmpeg_base(ffmpeg)
    if span.start_sec > 0:
        cmd.extend(["-ss", f"{span.start_sec:.3f}"])
    cmd.extend(
        [
            "-playlist",
            str(playlist),
            "-i",
            f"bluray:{block_device(device)}",
            "-t",
            f"{span.duration_sec:.3f}",
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-map",
            "0:s?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(segment),
        ]
    )
    return cmd


def concat_list_text(paths: Sequence[Path]) -> str:
    lines = []
    for path in paths:
        escaped = str(path).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def build_stitch_plan(
    device: str,
    playlist: int,
    skips: Sequence[Span],
    output: Path,
    *,
    duration_sec: float,
    segment_dir: Path,
    ffmpeg: str,
) -> StitchPlan:
    spans = readable_spans(duration_sec, skips)
    segment_dir = Path(segment_dir)
    segment_paths = tuple(segment_dir / f"part-{index:02d}.mkv" for index in range(len(spans)))
    segment_cmds = tuple(
        tuple(
            build_span_cmd(device, playlist, span, path, ffmpeg=ffmpeg),
        )
        for span, path in zip(spans, segment_paths, strict=True)
    )
    concat_list = segment_dir / "concat.txt"
    concat_cmd = (
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(output),
    )
    return StitchPlan(
        output=output,
        segment_dir=segment_dir,
        spans=spans,
        segment_cmds=segment_cmds,
        segment_paths=segment_paths,
        concat_list=concat_list,
        concat_cmd=concat_cmd,
    )


@dataclass(frozen=True)
class AlignPiece:
    """One stitched chunk. Times are on the feature clock, in seconds."""

    start_sec: float
    end_sec: float
    actual_duration_sec: float
    source: Path

    @property
    def planned_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class AlignAction:
    source: Path
    planned_sec: float
    actual_sec: float
    output_sec: float
    dest: Path | None
    trim_cmd: tuple[str, ...] | None
    gap: Span | None
    kind: str  # trim, keep, or gap

    @property
    def kept(self) -> bool:
        return self.kind == "keep"


@dataclass(frozen=True)
class VerifyPlan:
    """One decode pass. No NAS copy command is in here."""

    source: Path
    feature_duration_sec: float
    tolerance_sec: float
    full_decode_cmd: tuple[str, ...]
    spot_cmds: tuple[tuple[str, ...], ...]
    gaps: tuple[Span, ...]

    def command_lines(self) -> list[str]:
        lines = [shlex.join(self.full_decode_cmd)]
        lines.extend(shlex.join(cmd) for cmd in self.spot_cmds)
        return lines

    def display_lines(self) -> list[str]:
        lines = [
            "# verify once",
            "# duration within 0.5s of the feature; video and audio present",
            "# any error, a duration miss, or a gap: not a finished movie",
            "# NAS copy only after verify passes and the map has no missing bytes",
            "# recover does not copy",
        ]
        for gap in self.gaps:
            lines.append(f"# gap {gap.start_sec:.3f}-{gap.end_sec:.3f}")
        lines.extend(self.command_lines())
        return lines


@dataclass(frozen=True)
class VerifyAssessment:
    ok: bool
    finished_movie: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AlignPlan:
    output: Path
    feature_duration_sec: float
    actions: tuple[AlignAction, ...]
    gaps: tuple[Span, ...]
    concat_paths: tuple[Path, ...]
    concat_list: Path
    concat_cmd: tuple[str, ...]
    raw_duration_sec: float
    verify: VerifyPlan

    @property
    def aligned_end_sec(self) -> float:
        total = sum(action.output_sec for action in self.actions)
        return min(total, self.feature_duration_sec)

    def display_lines(self) -> list[str]:
        lines = [
            "# align: stream-copy trim each overrun to its planned span",
            "# a tail trim of the concat is not alignment",
            (
                f"# feature {self.feature_duration_sec:.3f}s; "
                f"aligned end {self.aligned_end_sec:.3f}s"
            ),
        ]
        for action in self.actions:
            if action.gap is not None:
                lines.append(
                    f"# gap {action.gap.start_sec:.3f}-{action.gap.end_sec:.3f} "
                    "(short by more than 1s; picture is not invented)"
                )
            if action.trim_cmd is not None:
                lines.append(f"# trim to {action.output_sec:.3f}s")
                lines.append(shlex.join(action.trim_cmd))
            elif action.kind == "keep":
                lines.append(
                    f"# keep {action.source} ({action.output_sec:.3f}s, within 1s)"
                )
            else:
                lines.append(
                    f"# keep partial {action.source} ({action.output_sec:.3f}s); gap stays"
                )
        lines.append("# concat, cut at the feature duration")
        lines.append(shlex.join(self.concat_cmd))
        lines.extend(self.verify.display_lines())
        return lines


def parse_align_piece(text: str) -> AlignPiece:
    """``START-END=ACTUAL=PATH``. Clocks match ``--skip``."""
    raw = text.strip()
    parts = raw.split("=", 2)
    if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
        raise ValueError(f"piece must be START-END=ACTUAL=PATH (got {text!r})")
    span = parse_skip(parts[0])
    actual = parse_clock(parts[1])
    return AlignPiece(span.start_sec, span.end_sec, actual, Path(parts[2]))


def _decode_maps() -> tuple[str, ...]:
    return ("-map", "0:v:0", "-map", "0:a", "-f", "null", "-")


def build_align_trim_cmd(
    source: Path,
    dest: Path,
    duration_sec: float,
    *,
    ffmpeg: str,
) -> tuple[str, ...]:
    """Stream-copy trim. No filters and no encoder."""
    if duration_sec <= 0:
        raise ValueError("trim duration must be positive")
    return (
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(source),
        "-t",
        f"{duration_sec:.3f}",
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-map",
        "0:s?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(dest),
    )


def align_boundaries(
    pieces: Sequence[AlignPiece],
    feature_duration_sec: float,
) -> tuple[float, ...]:
    """Planned piece edges that sit inside the feature."""
    points: list[float] = []
    for piece in pieces:
        for value in (piece.start_sec, piece.end_sec):
            if 0 <= value <= feature_duration_sec:
                points.append(value)
    ordered = sorted(points)
    unique: list[float] = []
    for value in ordered:
        if not unique or value - unique[-1] > 0.05:
            unique.append(value)
    return tuple(unique)


def build_full_decode_cmd(source: Path, *, ffmpeg: str) -> tuple[str, ...]:
    """One demux/decode of the aligned file. ``-f null`` writes nothing."""
    return (ffmpeg, "-v", "error", "-nostdin", "-i", str(source), *_decode_maps())


def build_spot_decode_cmd(
    source: Path,
    boundary_sec: float,
    *,
    ffmpeg: str,
    feature_duration_sec: float,
    spot_sec: float = SPOT_DECODE_SEC,
) -> tuple[str, ...]:
    """Short decode at one planned boundary. Seek stays inside the file."""
    seek = boundary_sec
    if feature_duration_sec - boundary_sec < spot_sec:
        seek = max(0.0, feature_duration_sec - spot_sec)
    return (
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-ss",
        f"{seek:.3f}",
        "-i",
        str(source),
        "-t",
        f"{spot_sec:.3f}",
        *_decode_maps(),
    )


def build_verify_plan(
    source: Path,
    *,
    feature_duration_sec: float,
    boundaries: Sequence[float],
    gaps: Sequence[Span] = (),
    ffmpeg: str,
    tolerance_sec: float = VERIFY_TOLERANCE_SEC,
) -> VerifyPlan:
    """Commands for one verify. Does not copy to the NAS and does not retry."""
    if feature_duration_sec <= 0:
        raise ValueError("feature duration must be positive")
    if tolerance_sec < 0:
        raise ValueError("verify tolerance must be >= 0")
    spots: list[tuple[str, ...]] = []
    seen: list[float] = []
    for boundary in boundaries:
        cmd = build_spot_decode_cmd(
            source,
            boundary,
            ffmpeg=ffmpeg,
            feature_duration_sec=feature_duration_sec,
        )
        seek = float(cmd[cmd.index("-ss") + 1])
        if any(abs(seek - previous) <= 0.05 for previous in seen):
            continue
        seen.append(seek)
        spots.append(cmd)
    return VerifyPlan(
        source=source,
        feature_duration_sec=feature_duration_sec,
        tolerance_sec=tolerance_sec,
        full_decode_cmd=build_full_decode_cmd(source, ffmpeg=ffmpeg),
        spot_cmds=tuple(spots),
        gaps=tuple(gaps),
    )


def _is_whole_seconds(value: float) -> bool:
    return abs(value - round(value)) <= 1e-6


def makemkv_clock_matches(measured: float, feature: float) -> bool:
    """True when a container matches a truncated MakeMKV HH:MM:SS.

    TINFO stores whole seconds. 5891.936 still displays as 1:38:11. Accept
    the displayed second, or a fraction under 1s past that integer. A
    feature duration that already has a fraction is not this clock.
    """
    if not _is_whole_seconds(feature):
        return False
    return -1e-6 <= measured - feature < MAKEMKV_CLOCK_FRACTION_SEC


def _duration_within_feature(
    measured: float,
    feature: float,
    tolerance_sec: float,
    *,
    whole_second_clock: bool,
) -> bool:
    if whole_second_clock and makemkv_clock_matches(measured, feature):
        return True
    return abs(measured - feature) - tolerance_sec <= 1e-6


def assess_verify(
    *,
    feature_duration_sec: float,
    measured_duration_sec: float | None,
    video_streams: int,
    audio_streams: int,
    decode_ok: bool = True,
    decode_errors: Sequence[str] = (),
    spot_ok: bool = True,
    spot_errors: Sequence[str] = (),
    gaps: Sequence[Span] = (),
    tolerance_sec: float = VERIFY_TOLERANCE_SEC,
    whole_second_clock: bool = False,
) -> VerifyAssessment:
    """Pass only when duration, streams, decode, spots, and gaps all agree.

    A failure is not a finished movie. The caller stops. This does not start
    another speed cycle and it does not copy to the NAS. ``whole_second_clock``
    is the drive-remux path, where the only known length is MakeMKV's
    truncated HH:MM:SS. A stitch keeps the symmetric tolerance.
    """
    if feature_duration_sec <= 0:
        raise ValueError("feature duration must be positive")
    if tolerance_sec < 0:
        raise ValueError("verify tolerance must be >= 0")
    reasons: list[str] = []
    if gaps:
        reasons.append("recorded gap; not a finished movie")
    if measured_duration_sec is None:
        reasons.append("duration missing")
    elif not _duration_within_feature(
        measured_duration_sec,
        feature_duration_sec,
        tolerance_sec,
        whole_second_clock=whole_second_clock,
    ):
        if whole_second_clock and _is_whole_seconds(feature_duration_sec):
            reasons.append(
                f"duration {measured_duration_sec:.3f}s is outside the truncated "
                f"{feature_duration_sec:.0f}s MakeMKV clock"
            )
        else:
            reasons.append(
                f"duration {measured_duration_sec:.3f}s is outside "
                f"{tolerance_sec:.1f}s of {feature_duration_sec:.3f}s"
            )
    if video_streams < 1 or audio_streams < 1:
        reasons.append("expected video and audio streams missing")
    if not decode_ok or any(err.strip() for err in decode_errors):
        reasons.append("decode error")
    if not spot_ok or any(err.strip() for err in spot_errors):
        reasons.append("spot-decode error")
    ok = not reasons
    return VerifyAssessment(ok=ok, finished_movie=ok, reasons=tuple(reasons))


def assess_drive_remux_verify(
    *,
    feature_duration_sec: float,
    measured_duration_sec: float | None,
    video_streams: int,
    audio_streams: int,
    decode_exit: int,
    decode_stderr: str,
    tolerance_sec: float = VERIFY_TOLERANCE_SEC,
) -> VerifyAssessment:
    """One file check after a drive remux. Not a stitch and not a disc read.

    Any stderr byte fails, including a one-frame non-monotonic DTS line.
    ``verify_stderr_errors`` drops that line for a stitch; this path does not.
    No spot-decodes. A pass is the finished movie. When the feature length
    is MakeMKV's truncated HH:MM:SS, a container whose displayed second is
    that value matches. NAS copy is ``nas_copy_allowed`` after that.
    """
    failed = decode_exit != 0 or decode_stderr != ""
    detail = decode_stderr if decode_stderr else f"exit {decode_exit}"
    decode_errors = (detail,) if failed else ()
    return assess_verify(
        feature_duration_sec=feature_duration_sec,
        measured_duration_sec=measured_duration_sec,
        video_streams=video_streams,
        audio_streams=audio_streams,
        decode_ok=not decode_errors,
        decode_errors=decode_errors,
        spot_ok=True,
        spot_errors=(),
        gaps=(),
        tolerance_sec=tolerance_sec,
        whole_second_clock=True,
    )


def drive_remux_reported_read_errors(text: str) -> bool:
    """True when a drive-remux log reports a read error.

    ``No read errors`` and ``0 read errors`` are a clean read. A log that
    never mentions a read error is a clean read. A positive count or
    ``read error at`` is not.
    """
    for raw in text.splitlines():
        line = _NO_READ_ERRORS_RE.sub(" ", raw)
        line = _ZERO_READ_ERRORS_RE.sub(" ", line)
        if _POSITIVE_READ_ERRORS_RE.search(line) or _READ_ERROR_AT_RE.search(line):
            return True
        if _READ_ERROR_RE.search(line):
            return True
    return False


def nas_copy_allowed(
    *,
    verify_passed: bool,
    status: MapStatus,
    source: str = "stitch",
    remux_read_errors: bool = False,
    gaps: bool = False,
) -> bool:
    """Whether the normal NAS copy is allowed. ``recover`` does not perform it.

    A drive remux that verified (Copy complete or titles saved, no read
    errors, empty decode stderr, duration accepted) is the movie even when
    the image still has bad bytes outside that read. A stitch, a gapped
    file, and a remux that hit read errors still need a clean map. A gapped
    stitch is never a NAS copy.
    """
    if source not in {"stitch", "drive_remux"}:
        raise ValueError("NAS copy source must be stitch or drive_remux")
    if gaps or not verify_passed:
        return False
    if source == "drive_remux" and not remux_read_errors:
        return True
    return _map_clean(status)


def _piece_gap(piece: AlignPiece, *, feature_duration_sec: float, actual: float) -> Span | None:
    missing_from = piece.start_sec + actual
    missing_to = min(piece.end_sec, feature_duration_sec)
    if missing_to - missing_from > 0.05:
        return Span(missing_from, missing_to)
    return None


def build_align_plan(
    pieces: Sequence[AlignPiece],
    output: Path,
    *,
    feature_duration_sec: float,
    ffmpeg: str,
    work_dir: Path | None = None,
) -> AlignPlan:
    """Trim overruns to their planned spans, then cut at the feature duration.

    Stream copy only. A piece longer than its planned span is trimmed to that
    span. A piece within about 1 second under the span is kept as-is. A piece
    short by more than that is a gap: the picture already read is kept and
    nothing is invented to fill the hole. The concat ``-t`` is the feature
    duration. That final cut is not a substitute for the per-piece trims.
    The output path is a new file.
    """
    if feature_duration_sec <= 0:
        raise ValueError("feature duration must be positive")
    if not pieces:
        raise ValueError("at least one piece is required")
    ordered = tuple(sorted(pieces, key=lambda piece: (piece.start_sec, piece.end_sec)))
    prev_end: float | None = None
    for piece in ordered:
        if piece.start_sec < 0:
            raise ValueError("piece start must be >= 0")
        if piece.end_sec <= piece.start_sec:
            raise ValueError("piece end must be after start")
        if piece.actual_duration_sec < 0:
            raise ValueError("actual duration must be >= 0")
        if prev_end is not None and piece.start_sec < prev_end - 0.05:
            raise ValueError(
                "align pieces overlap: "
                f"{prev_end:.3f}s meets {piece.start_sec:.3f}-{piece.end_sec:.3f}s"
            )
        if os.path.abspath(piece.source) == os.path.abspath(output):
            raise ValueError("refusing to overwrite a source piece; write a new aligned file")
        prev_end = piece.end_sec

    work = Path(work_dir) if work_dir is not None else output.parent / f"{output.stem}-align"
    actions: list[AlignAction] = []
    gaps: list[Span] = []
    concat_paths: list[Path] = []
    covered_until = 0.0
    out_total = 0.0

    for index, piece in enumerate(ordered):
        if piece.start_sec >= feature_duration_sec - 0.001:
            break
        if piece.start_sec - covered_until > ALIGN_KEEP_SLACK_SEC:
            gaps.append(Span(covered_until, min(piece.start_sec, feature_duration_sec)))
        planned = min(piece.end_sec, feature_duration_sec) - piece.start_sec
        actual = piece.actual_duration_sec
        gap: Span | None = None
        if actual > planned:
            output_sec = planned
            kind = "trim"
        elif span_copy_complete(actual, planned, slack_sec=ALIGN_KEEP_SLACK_SEC):
            output_sec = actual
            kind = "keep"
        else:
            output_sec = actual
            kind = "gap"
            gap = _piece_gap(
                piece,
                feature_duration_sec=feature_duration_sec,
                actual=actual,
            )
            if gap is not None:
                gaps.append(gap)
        room = feature_duration_sec - out_total
        if output_sec > room + 0.001:
            output_sec = max(0.0, room)
            kind = "trim"
        if output_sec <= 0.001:
            covered_until = min(piece.end_sec, feature_duration_sec)
            continue
        dest: Path | None = None
        trim_cmd: tuple[str, ...] | None = None
        if kind == "trim":
            dest = work / f"part-{index:02d}.mkv"
            trim_cmd = build_align_trim_cmd(piece.source, dest, output_sec, ffmpeg=ffmpeg)
            concat_paths.append(dest)
        else:
            concat_paths.append(piece.source)
        actions.append(
            AlignAction(
                source=piece.source,
                planned_sec=planned,
                actual_sec=actual,
                output_sec=output_sec,
                dest=dest,
                trim_cmd=trim_cmd,
                gap=gap,
                kind=kind,
            )
        )
        out_total += output_sec
        covered_until = min(piece.end_sec, feature_duration_sec)

    if feature_duration_sec - covered_until > ALIGN_KEEP_SLACK_SEC:
        gaps.append(Span(covered_until, feature_duration_sec))
    if not concat_paths:
        raise ValueError("no readable picture to align")

    concat_list = work / "concat.txt"
    concat_cmd = (
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-t",
        f"{feature_duration_sec:.3f}",
        "-c",
        "copy",
        str(output),
    )
    gap_tuple = tuple(gaps)
    verify = build_verify_plan(
        output,
        feature_duration_sec=feature_duration_sec,
        boundaries=align_boundaries(ordered, feature_duration_sec),
        gaps=gap_tuple,
        ffmpeg=ffmpeg,
    )
    return AlignPlan(
        output=output,
        feature_duration_sec=feature_duration_sec,
        actions=tuple(actions),
        gaps=gap_tuple,
        concat_paths=tuple(concat_paths),
        concat_list=concat_list,
        concat_cmd=concat_cmd,
        raw_duration_sec=sum(piece.actual_duration_sec for piece in ordered),
        verify=verify,
    )


def _run(cmd: Sequence[str], *, env: dict[str, str] | None = None) -> None:
    log_debug("$ %s", shlex.join(cmd))
    proc = subprocess.run(list(cmd), env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command exited {proc.returncode}: {shlex.join(cmd)}")


def _capture(cmd: Sequence[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    log_debug("$ %s", shlex.join(cmd))
    proc = subprocess.run(
        list(cmd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stderr or ""


def execute_makemkv(cmd: Sequence[str], *, settings: Path | None = None) -> None:
    from ripknife.makemkv import makemkv_env

    output_dir = Path(cmd[-1])
    with scoped_ignore_read_errors(settings):
        output_dir.mkdir(parents=True, exist_ok=True)
        _run(cmd, env=makemkv_env())


def execute_ddrescue(cmd: Sequence[str]) -> None:
    assert_makemkvcon_idle()
    iso = Path(cmd[-2])
    iso.parent.mkdir(parents=True, exist_ok=True)
    _run(cmd)


def execute_ffmpeg(cmd: Sequence[str]) -> None:
    assert_makemkvcon_idle()
    output = Path(cmd[-1])
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(cmd, env=ffmpeg_env())


def execute_stitch(plan: StitchPlan) -> None:
    assert_makemkvcon_idle()
    refuse_stitch_overwrite(plan.output)
    plan.segment_dir.mkdir(parents=True, exist_ok=True)
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    env = ffmpeg_env()
    for index, cmd in enumerate(plan.segment_cmds, start=1):
        console.print(f"[dim]stitch segment {index}/{len(plan.segment_cmds)}[/dim]")
        _run(cmd, env=env)
    plan.concat_list.write_text(concat_list_text(plan.segment_paths), encoding="utf-8")
    console.print("[dim]stitch concat[/dim]")
    _run(plan.concat_cmd, env=env)


def execute_align(plan: AlignPlan) -> None:
    """Write a new aligned file. Does not read the drive and does not copy to NAS."""
    refuse_stitch_overwrite(plan.output)
    for action in plan.actions:
        if action.dest is not None and action.dest.exists():
            raise RuntimeError(f"output already exists: {action.dest}")
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    plan.concat_list.parent.mkdir(parents=True, exist_ok=True)
    env = ffmpeg_env()
    for action in plan.actions:
        if action.trim_cmd is None:
            continue
        console.print(f"[dim]align trim {action.output_sec:.3f}s[/dim]")
        _run(action.trim_cmd, env=env)
    plan.concat_list.write_text(concat_list_text(plan.concat_paths), encoding="utf-8")
    console.print("[dim]align concat[/dim]")
    _run(plan.concat_cmd, env=env)


def _capture_text(cmd: Sequence[str], *, env: dict[str, str]) -> tuple[int, str, str]:
    log_debug("$ %s", shlex.join(cmd))
    proc = subprocess.run(
        list(cmd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def probe_file_streams(path: Path, *, ffprobe: str) -> tuple[float, int, int]:
    """Duration plus video and audio stream counts for one local file."""
    env = ffmpeg_env()
    duration_cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
    ]
    code, out, err = _capture_text(duration_cmd, env=env)
    if code != 0:
        detail = err.strip() or f"exit {code}"
        raise RuntimeError(f"ffprobe failed: {detail}")
    try:
        duration = float(out.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned no duration: {out!r}") from exc
    stream_cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
    ]
    code, out, err = _capture_text(stream_cmd, env=env)
    if code != 0:
        detail = err.strip() or f"exit {code}"
        raise RuntimeError(f"ffprobe failed: {detail}")
    kinds = [line.strip() for line in out.splitlines() if line.strip()]
    return duration, kinds.count("video"), kinds.count("audio")


_MONOTONIC_DTS = "non monotonically increasing dts"


def verify_stderr_errors(code: int, stderr: str) -> tuple[str, ...]:
    """Drop one-frame join steps. Any other line, or a non-zero exit, remains.

    Stream copy joins log ``non monotonically increasing dts`` at ``-v error``
    when the next packet steps back by about one frame. That is not a broken
    frame. ``Last message repeated`` belongs to the line above it.
    """
    kept: list[str] = []
    ignoring = False
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _MONOTONIC_DTS in line:
            ignoring = True
            continue
        if line.startswith("Last message repeated") and ignoring:
            continue
        ignoring = False
        kept.append(line)
    if code != 0:
        return tuple(kept) or (f"exit {code}",)
    return tuple(kept)


def collect_verify_errors(
    plan: VerifyPlan,
    *,
    env: dict[str, str] | None = None,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Run the one full decode and the spot decodes. No second pass."""
    run_env = env if env is not None else ffmpeg_env()
    code, err = _capture(plan.full_decode_cmd, env=run_env)
    decode_errors = verify_stderr_errors(code, err)
    spot_errors: list[str] = []
    for cmd in plan.spot_cmds:
        scode, serr = _capture(cmd, env=run_env)
        spot_errors.extend(verify_stderr_errors(scode, serr))
    return (not decode_errors, decode_errors, tuple(spot_errors))


def run_align_and_verify(plan: AlignPlan, *, ffprobe: str) -> VerifyAssessment:
    """Align, then one verify. Stops in the caller when the assessment fails.

    Does not copy to the NAS and does not start another speed cycle.
    """
    execute_align(plan)
    duration, video, audio = probe_file_streams(plan.output, ffprobe=ffprobe)
    decode_ok, decode_errors, spot_errors = collect_verify_errors(plan.verify)
    return assess_verify(
        feature_duration_sec=plan.feature_duration_sec,
        measured_duration_sec=duration,
        video_streams=video,
        audio_streams=audio,
        decode_ok=decode_ok,
        decode_errors=decode_errors,
        spot_ok=not spot_errors,
        spot_errors=spot_errors,
        gaps=plan.gaps,
    )


def probe_playlist_duration(device: str, playlist: int, *, ffprobe: str | None = None) -> float:
    """Open the live disc. Callers that must not touch hardware skip this."""
    binary = ffprobe or resolve_ffprobe()
    cmd = [
        binary,
        "-v",
        "error",
        "-playlist",
        str(playlist),
        "-i",
        f"bluray:{block_device(device)}",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=ffmpeg_env(),
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"ffprobe failed: {detail}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned no duration: {proc.stdout!r}") from exc


def _fail(exc: BaseException) -> NoReturn:
    console.print(f"[red]Recovery failed:[/red] {exc}")
    raise typer.Exit(code=1) from exc


def _echo_cmd(cmd: Sequence[str], *, env_prefix: bool = False) -> None:
    line = shlex.join(cmd)
    if env_prefix:
        line = f"MAKEMKVCON={shlex.quote(str(MAKEMKV_BIN))} {line}"
    typer.echo(line)


@recover_app.command("makemkv")
def recover_makemkv_cmd(
    device: Annotated[
        str,
        typer.Option("--device", help="Drive: /dev/srN or dev:/dev/srN. Required."),
    ],
    title: Annotated[int, typer.Option("--title", help="MakeMKV title index from a scan.")],
    output: Annotated[
        Path,
        typer.Option("--output", help="Directory under the output root. Required."),
    ],
    minlength: Annotated[
        int,
        typer.Option("--minlength", help="Same minlength as the scan that produced --title."),
    ] = 300,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the makemkvcon command. Does not edit settings or open the drive."),
    ] = False,
) -> None:
    """Rip one title with ignore-read-errors.

    Opt-in. Watchers never call this. MakeMKV has no per-run switch for
    io_IgnoreReadErrors; a real run sets that key and io_ErrorRetryCount in
    ~/.MakeMKV/settings.conf and restores the previous file. Refuses if any
    makemkvcon is already running.

    Worth a manual try on a small defect. A full-feature ignore-read-errors pass
    can stall and save nothing. An aborted MakeMKV file is unfinalized EBML and
    will not play.
    """
    try:
        dest = under_output(output)
        cmd = build_makemkv_cmd(device, title, dest, minlength=minlength)
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if dry_run:
        typer.echo(
            "# real run sets io_IgnoreReadErrors for this process only, then restores settings.conf"
        )
        _echo_cmd(cmd)
        return
    setup_scan_logger(None, console=True)
    try:
        execute_makemkv(cmd)
    except (RuntimeError, OSError) as exc:
        _fail(exc)


@recover_app.command("ddrescue")
def recover_ddrescue_cmd(
    device: Annotated[
        str,
        typer.Option("--device", help="Drive: /dev/srN or dev:/dev/srN. Required."),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="ISO path under the output root. Reuse the same file to resume.",
        ),
    ],
    mapfile: Annotated[
        Optional[Path],
        typer.Option(
            "--map",
            help="Mapfile. Default: ISO name with .map. Reuse it to fill holes from another disc.",
        ),
    ] = None,
    sector_size: Annotated[
        int,
        typer.Option("--sector-size", help="Input sector size. 2048 for DVD/BD."),
    ] = 2048,
    retrim: Annotated[
        bool,
        typer.Option(
            "--retrim",
            help=(
                "Pass ddrescue -M once (hole edges). Does not pass -r. "
                "Does not change spindle speed."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the ddrescue command. Does not open the drive."),
    ] = False,
) -> None:
    """Image a drive to an ISO plus a ddrescue mapfile.

    Saves readable bytes. Does not decrypt. MakeMKV refuses a damaged ISO
    (Failed to open disc / content hash). Reusing the same ISO and map is
    the resume: plain ddrescue -b 2048 -d reads only never-tried blocks and
    leaves finished holes alone. Another physical copy of the same pressing,
    same map, is what can fill those holes.

    After a stitch, original bytes in this order. This command does not
    change spindle speed and does not pass -r.

    1. Slow the spindle yourself (eject -x 25, or sdparm --command=speed=4500).
       Blu-ray 1x is about those values (CD units of 177 kB/s, or kB/s).
       ddrescue --max-read-rate does not slow the disc. Then run this command
       on the existing map. A plain resume reads never-tried blocks only.
       --retrim passes -M once. -M re-reads the edges of blocks the map
       marked bad; at 1x that can rescue a hole, not only a few boundary
       sectors. The scrape that follows can sit at 0 B/s on the core that
       is still non-scraped. Stop that scrape. Do not pass -r on that core.
       This command never passes -r. After the core is already rescued, one
       manual ddrescue -b 2048 -d -M -r1 at about eject -x 4 can still read
       a ~150 KB finished-bad remainder. Do not raise -r above 1.
    2. A different drive brand (LG, ASUS, Lite-On), same map. Another Pioneer
       slim is not a new attempt.
    3. A second physical copy of the same pressing, same map
       (ddrescue -b 2048 -d). If the same logical blocks fail, the defect is
       in the stamper and only a different pressing supplies those sectors.

    These do not recover original sectors: dvdisaster (retail BD has no ecc
    data from when the disc was good), safecopy, myrescue, readom -noerror
    (writes zeros), redumper, DiscImageCreator, Aaru (CD-level repair; BD
    reads are 2048 bytes), QpxTool/qscan on Pioneer XS (no Blu-ray quality
    scan), IsoBuster retries, DVDFab ExactRead (stops on a hard error) or
    ignore-errors (skips up to 64 sectors and leaves a hole), CloneBD (jumps,
    same idea as stitch), AnyDVD HD (gone since June 2024), MakeMKV 2.0.0
    and libaacs (no ECC), ffmpeg concealment, ddrescue fill mode (invented
    data), freemkv (can slow the spindle and zero-fill — do not point it at
    this ISO).

    The binary is ddrescue on PATH, else .tmp/tools/ddrescue-pkg/usr/bin/ddrescue.
    It is not vendored in git.
    """
    try:
        binary = resolve_ddrescue()
        iso = under_output(output)
        mapped = under_output(mapfile) if mapfile is not None else under_output(default_mapfile(iso))
        cmd = build_ddrescue_cmd(
            device,
            iso,
            mapped,
            binary=binary,
            sector_size=sector_size,
            retrim=retrim,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if dry_run:
        typer.echo(
            "# resume existing map; slow the spindle first (manual eject -x or sdparm); no -r"
        )
        if retrim:
            typer.echo("# --retrim passes -M (edges of blocks marked bad; not -r on the core)")
        else:
            typer.echo("# plain resume leaves finished holes alone; --retrim for one -M")
        _echo_cmd(cmd)
        return
    setup_scan_logger(None, console=True)
    try:
        execute_ddrescue(cmd)
    except (RuntimeError, OSError) as exc:
        _fail(exc)


@recover_app.command("ffmpeg")
def recover_ffmpeg_cmd(
    device: Annotated[
        str,
        typer.Option("--device", help="Live drive: /dev/srN or dev:/dev/srN. Required."),
    ],
    playlist: Annotated[
        int,
        typer.Option(
            "--playlist",
            help="libbluray playlist id (mpls), not a MakeMKV title index.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="MKV path under the output root. Required."),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the ffmpeg command. Does not open the drive."),
    ] = False,
) -> None:
    """Remux one playlist from a live drive via ffmpeg and libmmbd.

    Requires --device and --playlist. Sets MAKEMKVCON and reads bluray:/dev/srN.
    Stream copy. Watchers never call this. A short seek that decodes is not
    this full remux. A short seek does not prove the feature will finish.
    Loop-mounted ISO decrypt and makemkvcon backup are not this command.
    """
    try:
        ffmpeg = resolve_ffmpeg()
        dest = under_output(output)
        cmd = build_ffmpeg_remux_cmd(device, playlist, dest, ffmpeg=ffmpeg)
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if dry_run:
        typer.echo("# full stream-copy remux from the live drive; not a short seek")
        _echo_cmd(cmd, env_prefix=True)
        return
    setup_scan_logger(None, console=True)
    try:
        execute_ffmpeg(cmd)
    except (RuntimeError, OSError) as exc:
        _fail(exc)


@recover_app.command("stitch")
def recover_stitch_cmd(
    device: Annotated[
        str,
        typer.Option("--device", help="Live drive: /dev/srN or dev:/dev/srN. Required."),
    ],
    playlist: Annotated[
        int,
        typer.Option("--playlist", help="libbluray playlist id (mpls), not a MakeMKV title index."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="Final MKV under the output root. Required."),
    ],
    skip: Annotated[
        list[str],
        typer.Option(
            "--skip",
            help=(
                "Hole to cut, START-END (HH:MM:SS or MM:SS). Repeat for each hole. "
                "Example, not a default: 42:31-43:17 and 44:36-45:05."
            ),
        ),
    ],
    duration: Annotated[
        Optional[str],
        typer.Option(
            "--duration",
            help="Feature length (HH:MM:SS). Required with --dry-run. Otherwise ffprobe opens the drive.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print span and concat commands. Requires --duration. Does not open the drive.",
        ),
    ] = False,
) -> None:
    """Copy readable spans and concatenate them into one MKV.

    Each --skip range is a hole. Example arguments from a damaged feature
    (about the ddrescue stretches, not a built-in default): --skip 42:31-43:17
    --skip 44:36-45:05. Stream copy. The joins jump; missing pictures are not
    recreated. Prefer several 2–5 minute spans over one long span: a single
    span of about 42 minutes died around one minute in.     A chunk whose
    out_time is within about 1 second of the requested duration is done
    (see span_copy_complete). That is not a stall. A short seek is not a
    finished remux. When the pieces exist, ``recover align`` trims overruns
    to those spans. That align is part of this stitch, not another disc read.
    Watchers never call this.
    """
    if not skip:
        raise typer.BadParameter("at least one --skip START-END range is required")
    if dry_run and duration is None:
        raise typer.BadParameter("--duration is required with --dry-run (dry-run does not open the drive)")
    try:
        holes = [parse_skip(item) for item in skip]
        dest = under_output(output)
        ffmpeg = resolve_ffmpeg()
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    logging_ready = False
    if duration is not None:
        try:
            duration_sec = parse_clock(duration)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    else:
        setup_scan_logger(None, console=True)
        logging_ready = True
        try:
            assert_makemkvcon_idle()
            duration_sec = probe_playlist_duration(device, playlist)
        except (RuntimeError, OSError, ValueError) as exc:
            _fail(exc)

    segment_dir = TMP_ROOT / "recover-stitch" / f"{dest.stem}-p{playlist}"
    try:
        plan = build_stitch_plan(
            device,
            playlist,
            holes,
            dest,
            duration_sec=duration_sec,
            segment_dir=segment_dir,
            ffmpeg=ffmpeg,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    if dry_run:
        typer.echo("# joins jump; does not recreate missing sectors")
        for line in plan.display_lines():
            typer.echo(line)
        return
    if not logging_ready:
        setup_scan_logger(None, console=True)
    try:
        execute_stitch(plan)
    except (RuntimeError, OSError) as exc:
        _fail(exc)


@recover_app.command("align")
def recover_align_cmd(
    piece: Annotated[
        list[str],
        typer.Option(
            "--piece",
            help=(
                "START-END=ACTUAL=PATH. Planned span, actual seconds, source file. "
                "Repeat for each chunk. Example: 0-240=241.4=part-00.mkv."
            ),
        ),
    ],
    output: Annotated[
        Path,
        typer.Option(
            "--output",
            help="New aligned MKV under the output root. Refuses an existing file.",
        ),
    ],
    duration: Annotated[
        str,
        typer.Option("--duration", help="Feature length (HH:MM:SS or seconds). The output is cut here."),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the trim, concat, and verify commands. Does not run ffmpeg or open the drive.",
        ),
    ] = False,
) -> None:
    """Stream-copy each overrun back to its planned span, then verify once.

    A tail trim of the concatenated file is not this. Extra seconds that sit
    on the chunks (a 240s span landing near 241.4s) repeat picture at the
    joins. Each long piece is trimmed with ``-c copy`` to its planned span,
    and the new file is cut at ``--duration``. A piece within about 1 second
    under the span is kept. A piece short by more than that is a gap; missing
    picture is not invented. Writes a new file and does not overwrite the
    existing stitch.

    Verify is one pass: duration within 0.5s, video and audio present, one
    ``ffmpeg -v error -map 0:v:0 -map 0:a -f null -``, and short spot-decodes
    at the planned boundaries. Any error, a duration miss, or a recorded gap
    means the file is not a finished movie. This command does not copy to
    the NAS. The normal NAS copy is allowed only after verify passes and the
    map has no missing bytes. Watchers never call this.
    """
    if not piece:
        raise typer.BadParameter("at least one --piece START-END=ACTUAL=PATH is required")
    try:
        feature = parse_clock(duration)
        dest = under_output(output)
        parsed = []
        for item in piece:
            one = parse_align_piece(item)
            parsed.append(replace(one, source=under_output(one.source)))
        if dest.exists():
            raise ValueError(f"output already exists: {dest}; write a new aligned file")
        ffmpeg = resolve_ffmpeg()
        plan = build_align_plan(
            parsed,
            dest,
            feature_duration_sec=feature,
            ffmpeg=ffmpeg,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if dry_run:
        typer.echo("# stream-copy align, then one verify; does not read the drive or copy to NAS")
        for line in plan.display_lines():
            typer.echo(line)
        typer.echo("# not executed")
        return
    setup_scan_logger(None, console=True)
    try:
        result = run_align_and_verify(plan, ffprobe=resolve_ffprobe())
    except (RuntimeError, OSError) as exc:
        _fail(exc)
    if not result.finished_movie:
        detail = "; ".join(result.reasons) if result.reasons else "verify failed"
        _fail(
            RuntimeError(
                f"{detail}; stop and report; do not start another speed cycle; no NAS copy"
            )
        )
    typer.echo("verify passed; recover does not copy to NAS")


def _ladder_binary() -> Path:
    """Label for the printed command. Does not start ddrescue."""
    try:
        return resolve_ddrescue()
    except FileNotFoundError:
        return Path("ddrescue")


@recover_app.command("auto")
def recover_auto_cmd(
    device: Annotated[
        str,
        typer.Option("--device", help="Drive: /dev/srN or dev:/dev/srN. Printed only."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", help="ISO under the output root. Every pass reuses it."),
    ],
    mapfile: Annotated[
        Optional[Path],
        typer.Option(
            "--map",
            help="Existing map to read. Omit only when the image has not been made.",
        ),
    ] = None,
    image_done: Annotated[
        bool,
        typer.Option("--image-done", help="The one ddrescue image already exists."),
    ] = False,
    unfinished_passes: Annotated[
        int,
        typer.Option("--unfinished-passes", help="Unfinished resumes already run (0, 1, or 2)."),
    ] = 0,
    large_retrim_done: Annotated[
        bool,
        typer.Option("--large-retrim-done", help="The one -M (no -r) pass already ran."),
    ] = False,
    small_retry_done: Annotated[
        bool,
        typer.Option("--small-retry-done", help="The one -M -r1 pass already ran."),
    ] = False,
    zero_gain_streak: Annotated[
        int,
        typer.Option("--zero-gain-streak", help="Consecutive passes that added 0 bytes."),
    ] = 0,
    stitch_done: Annotated[
        bool,
        typer.Option("--stitch-done", help="The one stitch (including align) already ran."),
    ] = False,
    verify_done: Annotated[
        bool,
        typer.Option("--verify-done", help="The one verify already ran."),
    ] = False,
    gain_speed: Annotated[
        Optional[list[int]],
        typer.Option(
            "--gain-speed",
            help="eject -x of one pass that increased rescued bytes. Repeat in order.",
        ),
    ] = None,
    remux_errors: Annotated[
        int,
        typer.Option(
            "--remux-errors",
            help="Drive remux attempts that already errored (0 or 1).",
        ),
    ] = 0,
    drive_remux_done: Annotated[
        bool,
        typer.Option(
            "--drive-remux-done",
            help=(
                "The drive mkv log says Copy complete or titles saved. "
                "Exit 0 is not that. Verify is next, not a stitch."
            ),
        ),
    ] = False,
    drive_remux_failed: Annotated[
        bool,
        typer.Option(
            "--drive-remux-failed",
            help="The drive remux already failed. Stitch is next on a clean map.",
        ),
    ] = False,
    title: Annotated[
        int,
        typer.Option("--title", help="MakeMKV title index of the feature. Printed only."),
    ] = 0,
    minlength: Annotated[
        int,
        typer.Option("--minlength", help="Same minlength as the scan that numbered --title."),
    ] = 300,
    mkv_output: Annotated[
        Optional[Path],
        typer.Option(
            "--mkv-output",
            help="Directory for the drive mkv. Default: <iso stem>-drive beside the image.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the next step. This command never opens the drive either way.",
        ),
    ] = False,
) -> None:
    """Print the next recovery step. Does not open the drive.

    The first ddrescue image is the scan. There is no separate pre-scan and
    no execute switch. A clean map prints a drive remux, not ``iso:``.
    After that remux reports Copy complete or titles saved, the next print
    is a verify of that file, not a stitch. Watchers never call this.
    """
    try:
        if unfinished_passes < 0 or unfinished_passes > MAX_UNFINISHED_PASSES:
            raise ValueError(f"unfinished resumes are capped at {MAX_UNFINISHED_PASSES}")
        if zero_gain_streak < 0:
            raise ValueError("zero-gain streak must be >= 0")
        iso, mapped = prepare_image_paths(output, mapfile)
        status: MapStatus | None = None
        scanned = image_done
        if mapfile is not None:
            status = parse_ddrescue_map(mapped.read_text(encoding="utf-8"))
            scanned = True
        if scanned and status is None:
            raise ValueError("map status is required after the image; pass --map")
        mkv_dir = under_output(mkv_output) if mkv_output is not None else None
        state = LadderState(
            image_done=scanned,
            unfinished_passes=unfinished_passes,
            large_retrim_done=large_retrim_done,
            small_retry_done=small_retry_done,
            zero_gain_streak=zero_gain_streak,
            gain_speeds=tuple(gain_speed or ()),
            remux_errors=remux_errors,
            drive_remux_done=drive_remux_done,
            drive_remux_failed=drive_remux_failed,
            stitch_done=stitch_done,
            verify_done=verify_done,
        )
        step = next_recovery_step(
            status,
            state,
            device=device,
            iso=iso,
            mapfile=mapped,
            binary=_ladder_binary(),
            title=title,
            mkv_output=mkv_dir,
            minlength=minlength,
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    plan = format_recovery_plan(step)
    if dry_run:
        plan = "# dry-run\n" + plan
    typer.echo(plan, nl=False)
