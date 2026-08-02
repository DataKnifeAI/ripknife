"""Map unknown disc scans to YAML manifests via Cursor agent (Process 1.5)."""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from videosripper.paths import LOG_ROOT, ROOT

MAP_LOG = LOG_ROOT / "map.log"
READY_DIR = ROOT / "staging" / "ready"
WORKFLOW = ROOT / "LLM-RIP-WORKFLOW.md"
REGISTRY = ROOT / "configs" / "registry.yaml"
DEFAULT_AGENT = Path.home() / ".local/bin/agent"
DEFAULT_TIMEOUT_SEC = int(os.environ.get("VIDEOSRIPPER_MAP_TIMEOUT", "1200"))


def _utc_log_line(message: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{ts} {message}"


def append_map_log(message: str) -> None:
    MAP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MAP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(_utc_log_line(message) + "\n")


def agent_output_log_path(disc_id: str) -> Path:
    """Per-disc Cursor agent CLI stdout/stderr (safe for concurrent background maps)."""
    return LOG_ROOT / f"map-{disc_id}.log"


def _open_agent_output_log(disc_id: str, *, background: bool) -> tuple[Path, TextIO]:
    path = agent_output_log_path(disc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    mode = "background" if background else "foreground"
    handle.write(_utc_log_line(f"AGENT_OUTPUT_BEGIN DISC_ID={disc_id} ({mode})") + "\n")
    handle.flush()
    return path, handle


def resolve_agent() -> Path:
    agent = Path(os.environ.get("VIDEOSRIPPER_AGENT", DEFAULT_AGENT))
    if agent.is_file() and os.access(agent, os.X_OK):
        return agent
    from shutil import which

    found = which("agent")
    if found:
        return Path(found)
    raise FileNotFoundError(f"agent not found (expected {DEFAULT_AGENT})")


def build_map_prompt(
    disc_id: str,
    scan_json: Path,
    *,
    ready_yaml: Path | None = None,
    meta_json: Path | None = None,
) -> str:
    ready_yaml = ready_yaml or READY_DIR / f"{disc_id}.yaml"
    meta_json = meta_json or scan_json.parent / f"{disc_id}.meta.json"

    return f"""Read these inputs before authoring YAML:
  - Scan JSON: {scan_json}
  - Pending meta: {meta_json} (title_count, obfuscation, recommended_title, decoy_indices, identification)
  - Workflow: {WORKFLOW} — section "Title research checklist for LLM mapper"
  - Registry: {REGISTRY} — cross-reference similar disc_name matches for track patterns

Naming (critical):
- meta.disc_name = exact scan disc_name (may be a generic/uninformative volume label) — used only for disc matching
- meta.id / meta.title / output.filename / ready YAML basename = the IDENTIFIED work title, NOT the disc label
  Examples: meta.title "Scarface (1983)", id scarface-1983, filename "Scarface (1983).mkv"
  Bad: using uninformative volume labels (e.g. DVD_VIDEO, DVDVolume, VIDEO_TS, untitled) as the movie/show name
- Generic means an uninformative volume label detected by heuristic (patterns + boilerplate tokens), not an exhaustive string list.
- If disc_name is generic, research the real title before writing YAML. Use identification hints when present:
  - identification.bluray_title (from BDMV/META/DL/bdmt_*.xml) — strong signal; prefer as meta.title
  - identification.candidates[] (TMDb/OMDb runtime match) — shortlist; pick best fit, do not invent placeholders
  - meta.title_candidates / meta.preferred_id / meta.identified_title when set
- If still unknown after hints + research, leave pending and do NOT invent a placeholder title.

Write the manifest to: {ready_yaml}
(If you identified a better slug than "{disc_id}", write to staging/ready/<identified-slug>.yaml instead and note that in the log.)
Do NOT write directly to configs/ — Process 2 rips from staging/ready.

Title research steps (do these in order):
1. Parse scan JSON: disc_name, titles[], obfuscation block (if any), identification block (if any). Read meta.json.
2. Identify the work: TV show+season+disc OR movie Title (Year). Do not treat disc_name as the title when it is generic.
3. If identification.bluray_title is set, use it (with year from candidates/web if needed). If candidates[] present, choose among them using duration + box art / web search.
4. If title_count > 50: treat as likely Blu-ray playlist obfuscation — read duplicate_count, dominant_duration_sec, recommended_title, decoy_indices.
5. If obfuscation.detected or duplicate_count >= 5: for movies use title_selection.mode auto_feature (or fixed at recommended_title); add every decoy_indices entry to skips[] with reason "decoy playlist (duration outlier)".
6. DVD / TV scene-jump risk: skip title 0 play-all; if early indices are suspicious (COPYRIGHT/FBI/WARNING, short duration, Notes-style flags), check later titles (N+1, N+2). Use split for 2-chapter ~20min titles; extract_chapter for play-all-only episodes.
7. Cross-reference {REGISTRY} and completed manifests under configs/ for the same show — match episode numbering and makemkv index style (TINFO vs MSG).
8. Set meta.id to identified slug, meta.disc_name from scan (exact), meta.minlength from meta.json (default 300).
9. Document title choice rationale in a YAML comment above title_selection or tracks (e.g. why recommended_title, which decoys skipped, how the movie was identified).
10. Run validation before finishing:
     scripts/videosripper validate <ready-yaml> --with-scan --scan-json {scan_json}
   Fix all errors; re-run until pass.

Manifest requirements:
- kind tv_disc or movie as appropriate
- Map episodes using TheTVDB DVD order (TV) or official order (live-action); movies use title_selection + validation floors
- skips[] for play-all, extras, decoy playlists, skipped angles, suspicious loops
- Each tracks[].makemkv_title must exist in scan, meet minlength, and match expected duration (~11 min cartoons, ~47 min drama, ~23 min anime)
- Always set stream_defaults (required for correct player audio/subs):
  - English-language movies / live-action TV: keep_all_tracks true, audio_default eng, omit subtitle_default
  - Anime: keep_all_tracks true, audio_default jpn, subtitle_default eng
  - Foreign-language film: audio_default = original language; add subtitle_default eng when English subs should default on
  - Do NOT omit stream_defaults on movies — discs often mark non-English audio as default
- Match existing manifest style (stream_defaults, nas, post blocks)
- **Size floors (`title_selection.min_size_bytes`, `validation.min_size_bytes`):**
  - Defaults are 1 GiB — enough to catch empty rips, not reject real DVDs (~2–5 GB).
  - Do NOT copy 5368709120 (5 GiB) unless the scan feature is genuinely large (BD/UHD).
  - Prefer omitting min_size_bytes (use defaults) or set ~90% of scan `size_bytes` for the feature title.
  - DVDs and SD features are often 2–4 GB; many successful rips in this library are under 5 GB.
- **Multi-disc movies** (DSC1/DSC2, feature split across discs, user hint):
  - One manifest per disc; ``meta.part: N`` (1-based); ``output.filename: Title (Year) - Disc N.mkv``
  - ``post.copy_to_nas: false`` on every part (masters stay on game drive)
  - ``stitch`` block on every part (same values): ``enabled: true``, ``group_id: title-year``, ``expected_parts: N``, ``output_filename: Title (Year).mkv``, ``copy_to_nas: true``
  - Rip watcher stitches automatically after all N part MKVs exist (post-rip, no makemkv lock)
- Optional spot-check: scripts/videosripper preview --title N --chapter 2 --scan-json {scan_json}
"""


def run_map_agent(
    disc_id: str,
    scan_json: Path,
    *,
    background: bool = False,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    logger: logging.Logger | None = None,
) -> subprocess.Popen[str] | int:
    """Run map agent. Returns Popen when background=True, else exit code."""
    scan_json = Path(scan_json)
    if not scan_json.is_file():
        raise FileNotFoundError(f"scan JSON not found: {scan_json}")
    if not WORKFLOW.is_file():
        raise FileNotFoundError(f"workflow doc not found: {WORKFLOW}")

    ready_yaml = READY_DIR / f"{disc_id}.yaml"
    if ready_yaml.is_file():
        append_map_log(f"SKIP: ready manifest already exists: {ready_yaml}")
        if logger:
            logger.info("Ready manifest already exists for %s; skipping map agent", disc_id)
        return 0

    READY_DIR.mkdir(parents=True, exist_ok=True)
    prompt = build_map_prompt(disc_id, scan_json, ready_yaml=ready_yaml)
    agent = resolve_agent()

    append_map_log(f"Starting map for DISC_ID={disc_id} scan={scan_json}")
    append_map_log(f"PROMPT_BEGIN DISC_ID={disc_id}")
    append_map_log(prompt)
    append_map_log(f"PROMPT_END DISC_ID={disc_id}")
    append_map_log(f"Launching agent: {agent} -p --trust --force --approve-mcps --workspace {ROOT}")

    cmd = [
        str(agent),
        "-p",
        "--trust",
        "--force",
        "--approve-mcps",
        "--workspace",
        str(ROOT),
        prompt,
    ]

    if background:
        log_path, log_handle = _open_agent_output_log(disc_id, background=True)
        append_map_log(f"Agent output → {log_path}")
        if logger:
            logger.info(
                "Launching background map agent for %s (output=%s)",
                disc_id,
                log_path,
            )
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        finally:
            log_handle.close()
        return proc

    append_map_log(f"AGENT_OUTPUT_BEGIN DISC_ID={disc_id}")
    if logger:
        logger.info("Running map agent for %s (timeout=%ss)", disc_id, timeout_sec)
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.stdout:
            append_map_log(proc.stdout.rstrip())
        if proc.stderr:
            append_map_log(proc.stderr.rstrip())
        status = proc.returncode
    except subprocess.TimeoutExpired:
        append_map_log(f"ERROR: agent timed out after {timeout_sec}s for DISC_ID={disc_id}")
        return 124

    append_map_log(f"AGENT_OUTPUT_END DISC_ID={disc_id} exit={status}")
    if status == 0 and ready_yaml.is_file():
        append_map_log(f"SUCCESS: wrote {ready_yaml}")
        return 0
    if status != 124:
        append_map_log(f"ERROR: agent exited {status} for DISC_ID={disc_id}")
    if not ready_yaml.is_file():
        append_map_log(f"ERROR: expected output missing: {ready_yaml}")
    return status if status != 0 else 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Map disc scan to YAML via Cursor agent")
    parser.add_argument("disc_id", help="Staging disc id")
    parser.add_argument(
        "scan_json",
        nargs="?",
        help="Scan JSON path (default: staging/pending/<disc_id>.scan.json)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"Agent timeout seconds (default {DEFAULT_TIMEOUT_SEC})",
    )
    args = parser.parse_args(argv)
    scan_path = (
        Path(args.scan_json)
        if args.scan_json
        else ROOT / "staging" / "pending" / f"{args.disc_id}.scan.json"
    )
    return int(run_map_agent(args.disc_id, scan_path, background=False, timeout_sec=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
