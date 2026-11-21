"""Map unknown disc scans to YAML manifests via OpenRouter (Process 1.5).

The model returns strict JSON. Python validates it and writes the manifest.
The model does not get a shell or filesystem write access.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import ValidationError

from ripknife import llm
from ripknife.models import MovieManifest, TvDiscManifest
from ripknife.paths import (
    LOG_ROOT,
    ROOT,
    archive_generated_manifest,
    config_search_dirs,
    resolve_config_file,
)
from ripknife.skills import read_skill, skill_path

MAP_LOG = LOG_ROOT / "map.log"
READY_DIR = ROOT / "staging" / "ready"
WORKFLOW = skill_path("map")


def registry_path() -> Path:
    """Personal registry first, then the tracked example."""
    return resolve_config_file("registry.yaml")
DEFAULT_TIMEOUT_SEC = llm.HTTP_TIMEOUT_SEC
QUICK_FAIL_SEC = float(os.environ.get("RIPKNIFE_MAP_QUICK_FAIL_SEC", "8"))

MAP_FAIL_AUTH = "auth"
MAP_FAIL_RESOURCE = "resource_exhausted"
MAP_FAIL_NOT_FOUND = "agent_not_found"
MAP_FAIL_OTHER = "failed"
MAP_FAIL_AMBIGUOUS = "ambiguous"
MAP_EXIT_AMBIGUOUS = 2
MAP_EXIT_AUTH = 3
MAP_EXIT_RESOURCE = 4

_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# HTTP / provider strings. A missing key never reaches the network.
_AUTH_RE = re.compile(
    r"authentication required"
    r"|unauthenticated"
    r"|unauthorized"
    r"|invalid api key",
    re.IGNORECASE,
)
_RESOURCE_RE = re.compile(
    r"resource[_ ]exhausted"
    r"|RESOURCE_EXHAUSTED"
    r"|rate.?limit(?:ed)?"
    r"|quota.?exceeded",
    re.IGNORECASE,
)
# Line-start only, so the prompt's mention of the token is not a failure.
_AMBIGUOUS_RE = re.compile(r"(?m)^MAP_AMBIGUOUS:")
_SERIES_SUFFIX_RE = re.compile(
    r"-(?:s\d+|season-\d+)(?:-d\d+|-disc-\d+)?(?:-.*)?$",
    re.IGNORECASE,
)
# Prompt allows staging/ready/<identified-slug>.yaml instead of {disc_id}.yaml
_READY_SLUG_RE = re.compile(r"staging/ready/([A-Za-z0-9][A-Za-z0-9._-]*)\.yaml")


def merge_repo_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy ``base`` (default: ``os.environ``) and fill missing keys from ``.env``.

    Existing keys win so systemd ``EnvironmentFile`` / the shell stay authoritative.
    Used so a map can see ``OPENROUTER_API_KEY`` from ``.env`` when the parent
    watcher was started before that file existed (do not restart mid-rip).
    """
    env = dict(os.environ if base is None else base)
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return env
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in env:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        env[key] = value
    return env


def _utc_log_line(message: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{ts} {message}"


def append_map_log(message: str) -> None:
    MAP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MAP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(_utc_log_line(message) + "\n")


def agent_output_log_path(disc_id: str) -> Path:
    """Per-disc map response log (safe for concurrent maps)."""
    return LOG_ROOT / f"map-{disc_id}.log"


def classify_map_failure(text: str) -> str | None:
    """Classify a map response as auth, quota, or ambiguous, or None."""
    if not text or not text.strip():
        return None
    if _AMBIGUOUS_RE.search(text):
        return MAP_FAIL_AMBIGUOUS
    if _AUTH_RE.search(text):
        return MAP_FAIL_AUTH
    if _RESOURCE_RE.search(text):
        return MAP_FAIL_RESOURCE
    return None


def map_alert_code(kind: str) -> str:
    if kind == MAP_FAIL_AUTH:
        return "MAP_AGENT_AUTH"
    if kind == MAP_FAIL_AMBIGUOUS:
        return "MAP_AMBIGUOUS"
    return "MAP_FAILED"


def series_prefix(disc_id: str) -> str:
    """Strip season/disc suffix: ``example-show-s4-d1`` → ``example-show``."""
    slug = disc_id.strip().lower()
    stripped = _SERIES_SUFFIX_RE.sub("", slug)
    return stripped or slug


def find_similar_series_manifests(
    disc_id: str,
    *,
    search_roots: list[Path] | None = None,
) -> list[Path]:
    """Prior-season YAML for the same show. Never copy these — alert only."""
    prefix = series_prefix(disc_id)
    if not prefix or prefix == disc_id.strip().lower():
        return []
    roots = search_roots or [
        *config_search_dirs(),
        READY_DIR,
        ROOT / "staging" / "done",
    ]
    hits: list[Path] = []
    needle = disc_id.strip().lower()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.yaml"):
            stem = path.stem.lower()
            if stem == needle:
                continue
            if series_prefix(stem) == prefix:
                hits.append(path)
    hits.sort(key=lambda p: str(p))
    return hits


def _json_disc_name(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("disc_name")
    return str(name).strip() if name else None


def _yaml_meta_disc_name(path: Path) -> str | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        return None
    name = meta.get("disc_name")
    return str(name).strip() if name else None


def _is_ready_or_claimed_manifest(path: Path) -> bool:
    name = path.name
    return name.endswith(".yaml") or ".yaml.claimed-" in name


def _iter_ready_manifests() -> list[Path]:
    if not READY_DIR.is_dir():
        return []
    return sorted(
        path
        for path in READY_DIR.iterdir()
        if path.is_file() and _is_ready_or_claimed_manifest(path)
    )


def _claimed_ready_yaml(disc_id: str) -> Path | None:
    """Exact slug already claimed by a rip worker (``*.yaml.claimed-<drive>``)."""
    if not READY_DIR.is_dir():
        return None
    prefix = f"{disc_id}.yaml.claimed-"
    hits = sorted(
        path
        for path in READY_DIR.iterdir()
        if path.is_file() and path.name.startswith(prefix)
    )
    return hits[0] if hits else None


def find_ready_yaml(disc_id: str, *, scan_json: Path | None = None) -> Path | None:
    """Ready YAML for this disc — exact slug or the identified-slug filename.

    Python writes ``staging/ready/<identified-slug>.yaml`` when the volume
    label is not the work title (e.g. ``example-force-ww`` →
    ``example-force-1973.yaml``). Success checks must accept that file.

    Rip-watcher may claim the YAML (rename to ``*.yaml.claimed-<drive>``) before
    the map monitor thread exits. That is still a successful map.
    """
    exact = READY_DIR / f"{disc_id}.yaml"
    if exact.is_file():
        return exact
    claimed_exact = _claimed_ready_yaml(disc_id)
    if claimed_exact is not None:
        return claimed_exact
    disc_name = _json_disc_name(scan_json)
    if not disc_name:
        pending_meta = ROOT / "staging" / "pending" / f"{disc_id}.meta.json"
        if scan_json is not None:
            sibling = Path(scan_json).parent / f"{disc_id}.meta.json"
            disc_name = _json_disc_name(sibling)
        if not disc_name:
            disc_name = _json_disc_name(pending_meta)
    if disc_name:
        needle = disc_name.lower()
        for path in _iter_ready_manifests():
            yaml_name = _yaml_meta_disc_name(path)
            if yaml_name and yaml_name.lower() == needle:
                return path
    output = read_agent_output(disc_id)
    for match in _READY_SLUG_RE.finditer(output):
        slug = match.group(1)
        candidate = READY_DIR / f"{slug}.yaml"
        if candidate.is_file():
            return candidate
        claimed = _claimed_ready_yaml(slug)
        if claimed is not None:
            return claimed
    return None


def ambiguous_reason_from_scan(scan_json: Path | None) -> str | None:
    """Reason to refuse a map, or None when this scan may be mapped."""
    if scan_json is None or not Path(scan_json).is_file():
        return None
    try:
        data = json.loads(Path(scan_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    ident = data.get("identification")
    if not isinstance(ident, dict) or not ident.get("ambiguous"):
        return None
    reason = ident.get("ambiguous_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "generic volume label; runtime collision"


def _quarantine_ambiguous_yaml(
    disc_id: str,
    *,
    scan_json: Path | None = None,
    output: str | None = None,
) -> list[str]:
    """Move an unclaimed guessed manifest out of ``staging/ready``.

    Rip-watcher only consumes ``ready/*.yaml``. ``staging/ambiguous/`` is not
    retried. A ``*.yaml.claimed-*`` file is a rip already in flight — leave it.
    """
    text = output if output is not None else read_agent_output(disc_id)
    paths: list[Path] = []
    exact = READY_DIR / f"{disc_id}.yaml"
    if exact.is_file():
        paths.append(exact)
    for match in _READY_SLUG_RE.finditer(text or ""):
        candidate = READY_DIR / f"{match.group(1)}.yaml"
        if candidate.is_file():
            paths.append(candidate)
    found = find_ready_yaml(disc_id, scan_json=scan_json)
    if found is not None and found.is_file() and ".yaml.claimed-" not in found.name:
        paths.append(found)
    moved: list[str] = []
    seen: set[Path] = set()
    dest_dir = ROOT / "staging" / "ambiguous"
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen or not path.is_file() or ".yaml.claimed-" in path.name:
            continue
        seen.add(key)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        if dest.exists():
            dest = dest_dir / f"{path.stem}.ambiguous.yaml"
        path.rename(dest)
        append_map_log(f"MAP_AMBIGUOUS: moved {path.name} -> {dest}")
        moved.append(path.name)
    if found is not None and ".yaml.claimed-" in found.name:
        append_map_log(
            f"MAP_AMBIGUOUS: leave claimed manifest {found.name} (rip in flight)"
        )
    return moved


def read_agent_output(disc_id: str) -> str:
    """Latest agent-output section (after last AGENT_OUTPUT_BEGIN), or the whole file."""
    path = agent_output_log_path(disc_id)
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    marker = "AGENT_OUTPUT_BEGIN"
    idx = text.rfind(marker)
    return text[idx:] if idx >= 0 else text


def format_map_alert_lines(
    disc_id: str,
    kind: str,
    *,
    similar: list[Path] | None = None,
    detail: str | None = None,
) -> list[str]:
    """User-facing ALERT bodies (no ``=== ALERT ===`` prefix)."""
    code = map_alert_code(kind)
    if kind == MAP_FAIL_AUTH:
        lines = [
            f"{code} — OpenRouter key missing or rejected for disc {disc_id}",
            (
                "Action: set OPENROUTER_API_KEY in .env "
                "(https://openrouter.ai/keys), or hand-write "
                f"staging/ready/{disc_id}.yaml"
            ),
        ]
    elif kind == MAP_FAIL_RESOURCE:
        lines = [
            f"{code} — OpenRouter rate limit for disc {disc_id}",
            f"Action: wait and retry, or hand-write staging/ready/{disc_id}.yaml",
        ]
    elif kind == MAP_FAIL_NOT_FOUND:
        lines = [
            f"{code} — map request could not start for disc {disc_id}",
            f"Action: hand-write staging/ready/{disc_id}.yaml",
        ]
    elif kind == MAP_FAIL_AMBIGUOUS:
        lines = [
            (
                f"{code} — generic volume label; runtime matches more than one "
                f"film for disc {disc_id}"
            ),
            (
                "Action: do not rip a guessed title. Need a fingerprint "
                "(duration + size or AACS contentID), a bdmt title, or hand-write "
                "staging/ready/<identified-slug>.yaml"
            ),
        ]
        if detail:
            lines.append(detail)
    else:
        lines = [
            f"{code} — map agent failed for disc {disc_id} (no ready YAML)",
            (
                f"Action: check {agent_output_log_path(disc_id)} and map.log; "
                f"hand-write staging/ready/{disc_id}.yaml"
            ),
        ]
    if similar:
        shown = ", ".join(str(p) for p in similar[:3])
        extra = f" (+{len(similar) - 3} more)" if len(similar) > 3 else ""
        lines.append(f"Similar prior manifest (template only — not copied): {shown}{extra}")
    return lines


def emit_map_alerts(
    disc_id: str,
    kind: str,
    *,
    logger: logging.Logger | None = None,
    device: str | None = None,
    detail: str | None = None,
) -> list[str]:
    """Log ``=== ALERT === MAP_AGENT_AUTH`` / ``MAP_FAILED`` / ``MAP_AMBIGUOUS``."""
    from ripknife.logging_setup import setup_pipeline_logger
    from ripknife.monitor import ALERT_PREFIX

    similar = find_similar_series_manifests(disc_id)
    lines = format_map_alert_lines(disc_id, kind, similar=similar, detail=detail)
    prefixed = [f"{ALERT_PREFIX} {line}" for line in lines]
    pipe = setup_pipeline_logger()
    for msg in prefixed:
        append_map_log(msg)
        if logger:
            logger.error("%s", msg)
        pipe.error("%s", msg)
    record_map_failure_state(device, disc_id, kind, lines[0])
    return prefixed


def record_map_failure_state(
    device: str | None,
    disc_id: str,
    kind: str,
    last_error: str,
) -> None:
    """Persist ``last_error`` on the disc-watcher section for ``make watch-status``."""
    if not device:
        return
    try:
        from ripknife.devices import device_slug
        from ripknife.watcher_util import update_state

        if kind == MAP_FAIL_AUTH:
            status = "map_agent_auth"
        elif kind == MAP_FAIL_AMBIGUOUS:
            status = "map_ambiguous"
        else:
            status = "map_failed"
        update_state(
            f"disc_watcher:{device_slug(device)}",
            {
                "status": status,
                "last_error": last_error,
                "disc_id": disc_id,
                "map_failure": kind,
            },
        )
    except Exception:  # noqa: BLE001 — alerting must not raise
        pass


def _wait_exited(proc: subprocess.Popen[str], timeout_sec: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        if proc.poll() is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return proc.poll() is not None
        time.sleep(min(0.2, remaining))


def _monitor_background_map(
    proc: subprocess.Popen[str],
    disc_id: str,
    *,
    logger: logging.Logger | None,
    device: str | None,
    scan_json: Path | None = None,
) -> None:
    """Wait for a still-running map; alert once if it dies with no YAML. No retry."""
    try:
        proc.wait()
    except Exception:  # noqa: BLE001
        return
    output = read_agent_output(disc_id)
    if classify_map_failure(output) == MAP_FAIL_AMBIGUOUS:
        moved = _quarantine_ambiguous_yaml(disc_id, scan_json=scan_json, output=output)
        append_map_log(
            f"ERROR: MAP_AMBIGUOUS for DISC_ID={disc_id} quarantined={moved or 'none'}"
        )
        emit_map_alerts(disc_id, MAP_FAIL_AMBIGUOUS, logger=logger, device=device)
        return
    found = find_ready_yaml(disc_id, scan_json=scan_json)
    if found is not None:
        append_map_log(
            f"SUCCESS: background map wrote ready YAML for {disc_id} -> {found}"
        )
        return
    output = read_agent_output(disc_id)
    kind = classify_map_failure(output) or MAP_FAIL_OTHER
    append_map_log(
        f"ERROR: background map exited {proc.returncode} for DISC_ID={disc_id} kind={kind}"
    )
    emit_map_alerts(disc_id, kind, logger=logger, device=device)


def supervise_background_map(
    disc_id: str,
    scan_json: Path,
    *,
    logger: logging.Logger | None = None,
    device: str | None = None,
    quick_fail_sec: float | None = None,
) -> tuple[bool, str, str | None]:
    """Map a disc; catch a missing key; at most one .env retry.

    Returns ``(ok, message, kind)``. ``ok`` means a ready YAML exists (written
    on this call, or already present). Failures emit ``=== ALERT ===`` and do
    not loop. A still-running legacy process is monitored the same way.
    """
    wait_sec = QUICK_FAIL_SEC if quick_fail_sec is None else quick_fail_sec
    last_kind = MAP_FAIL_OTHER
    last_message = "map agent failed"
    max_attempts = 2  # initial + one retry after .env merge (auth only)

    for attempt in range(max_attempts):
        if find_ready_yaml(disc_id, scan_json=scan_json) is not None:
            return True, "ready manifest already exists", None
        try:
            result = run_map_agent(
                disc_id,
                scan_json,
                background=True,
                logger=logger,
                device=device,
            )
        except FileNotFoundError as exc:
            last_kind = MAP_FAIL_OTHER
            last_message = str(exc)
            append_map_log(f"ERROR: {exc}")
            break
        except OSError as exc:
            last_kind = MAP_FAIL_OTHER
            last_message = str(exc)
            append_map_log(f"ERROR: map launch failed: {exc}")
            break

        if isinstance(result, int):
            if result == 0:
                return True, "manifest written", None
            if result == MAP_EXIT_AMBIGUOUS:
                return False, "MAP_AMBIGUOUS", MAP_FAIL_AMBIGUOUS
            if result == MAP_EXIT_AUTH and attempt == 0:
                append_map_log(f"MAP retry 1/1 after .env merge for DISC_ID={disc_id}")
                if logger:
                    logger.warning(
                        "Map auth failed for %s; retrying once after .env merge",
                        disc_id,
                    )
                continue
            if result == MAP_EXIT_AUTH:
                last_kind = MAP_FAIL_AUTH
            elif result == MAP_EXIT_RESOURCE:
                last_kind = MAP_FAIL_RESOURCE
            else:
                last_kind = MAP_FAIL_OTHER
            last_message = format_map_alert_lines(disc_id, last_kind)[0]
            break

        if not _wait_exited(result, wait_sec):
            thread = threading.Thread(
                target=_monitor_background_map,
                args=(result, disc_id),
                kwargs={
                    "logger": logger,
                    "device": device,
                    "scan_json": scan_json,
                },
                name=f"map-monitor-{disc_id}",
                daemon=True,
            )
            thread.start()
            return True, "background map still running", None

        output = read_agent_output(disc_id)
        last_kind = classify_map_failure(output) or MAP_FAIL_OTHER
        last_message = format_map_alert_lines(disc_id, last_kind)[0]
        append_map_log(
            f"ERROR: map agent died quickly for DISC_ID={disc_id} "
            f"exit={result.returncode} kind={last_kind} attempt={attempt + 1}"
        )
        if last_kind == MAP_FAIL_AUTH and attempt == 0:
            append_map_log(f"MAP retry 1/1 after .env merge for DISC_ID={disc_id}")
            if logger:
                logger.warning(
                    "Map agent auth failed for %s; retrying once after .env merge",
                    disc_id,
                )
            continue
        break

    if last_kind == MAP_FAIL_AMBIGUOUS:
        _quarantine_ambiguous_yaml(disc_id, scan_json=scan_json)
    emit_map_alerts(disc_id, last_kind, logger=logger, device=device)
    return False, last_message, last_kind


def _open_agent_output_log(disc_id: str, *, background: bool) -> tuple[Path, TextIO]:
    path = agent_output_log_path(disc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    mode = "background" if background else "foreground"
    handle.write(_utc_log_line(f"AGENT_OUTPUT_BEGIN DISC_ID={disc_id} ({mode})") + "\n")
    handle.flush()
    return path, handle


def _local_mapper_notes() -> str:
    """Operator notes from the personal config dir. Empty when that file is absent."""
    for directory in config_search_dirs():
        path = directory / "mapper-notes.md"
        if path.is_file():
            body = path.read_text(encoding="utf-8").strip()
            if body:
                return "\n\nLocal mapper notes:\n" + body + "\n"
    return ""


def build_map_prompt(
    disc_id: str,
    scan_json: Path,
    *,
    ready_yaml: Path | None = None,
    meta_json: Path | None = None,
) -> str:
    ready_yaml = ready_yaml or READY_DIR / f"{disc_id}.yaml"
    meta_json = meta_json or scan_json.parent / f"{disc_id}.meta.json"
    instruction = read_skill(WORKFLOW).rstrip()
    context = (
        "\n\nDisc context:\n"
        f"- Scan JSON: {scan_json}\n"
        f"- Pending meta: {meta_json} "
        "(title_count, obfuscation, recommended_title, decoy_indices, identification)\n"
        f"- Registry: {registry_path()} — cross-reference similar disc_name matches "
        "for track patterns\n"
        f"- Disc id: {disc_id}\n"
        f"- Python writes the manifest to: {ready_yaml}\n"
    )
    return instruction + context + _local_mapper_notes()


def _prompt_with_disc(disc_id: str, scan_json: Path, ready_yaml: Path) -> str:
    prompt = build_map_prompt(disc_id, scan_json, ready_yaml=ready_yaml)
    meta_json = scan_json.parent / f"{disc_id}.meta.json"
    meta_body = (
        meta_json.read_text(encoding="utf-8") if meta_json.is_file() else "(none)"
    )
    scan_body = scan_json.read_text(encoding="utf-8")
    return (
        prompt
        + "\n\nScan JSON:\n"
        + scan_body
        + "\n\nPending meta JSON:\n"
        + meta_body
        + "\n"
    )


def _validated_manifest(
    raw: dict,
    scan_name: str | None,
) -> MovieManifest | TvDiscManifest:
    kind = raw.get("kind")
    if kind == "movie":
        model: MovieManifest | TvDiscManifest = MovieManifest.model_validate(raw)
    elif kind == "tv_disc":
        model = TvDiscManifest.model_validate(raw)
    else:
        raise ValueError(f"unknown kind: {kind!r}")
    if not scan_name or (model.meta.disc_name or "") != scan_name:
        raise ValueError("manifest disc_name does not match the scan")
    if _SLUG_RE.fullmatch(model.meta.id.strip()) is None:
        raise ValueError("manifest id is not a safe slug")
    return model


def _write_ready_manifest(model: MovieManifest | TvDiscManifest) -> Path:
    READY_DIR.mkdir(parents=True, exist_ok=True)
    dest = READY_DIR / f"{model.meta.id.strip()}.yaml"
    payload = yaml.safe_dump(
        model.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(dest)
    archive_generated_manifest(dest)
    return dest


def _failure_code(reply: llm.MapReply) -> tuple[int, str]:
    if reply.failure == "auth":
        return MAP_EXIT_AUTH, MAP_FAIL_AUTH
    if reply.failure == "resource":
        return MAP_EXIT_RESOURCE, MAP_FAIL_RESOURCE
    return 1, MAP_FAIL_OTHER


def run_map_agent(
    disc_id: str,
    scan_json: Path,
    *,
    background: bool = False,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    logger: logging.Logger | None = None,
    device: str | None = None,
    env: dict[str, str] | None = None,
    opener: llm.Opener | None = None,
) -> subprocess.Popen[str] | int:
    """Map one disc. Returns an exit code. The model does not get a shell.

    ``background`` no longer detaches a process. The watcher still calls this
    function. ``env`` replaces the process environment when set, so tests can
    omit a key without reading the real one.
    """
    scan_json = Path(scan_json)
    if not scan_json.is_file():
        raise FileNotFoundError(f"scan JSON not found: {scan_json}")
    if not WORKFLOW.is_file():
        raise FileNotFoundError(f"workflow doc not found: {WORKFLOW}")

    ready_yaml = READY_DIR / f"{disc_id}.yaml"
    existing = find_ready_yaml(disc_id, scan_json=scan_json)
    if existing is not None:
        append_map_log(f"SKIP: ready manifest already exists: {existing}")
        if logger:
            logger.info("Ready manifest already exists for %s; skipping map", disc_id)
        return 0

    reason = ambiguous_reason_from_scan(scan_json)
    if reason:
        append_map_log(f"MAP_AMBIGUOUS DISC_ID={disc_id}: {reason}")
        emit_map_alerts(
            disc_id,
            MAP_FAIL_AMBIGUOUS,
            logger=logger,
            device=device,
            detail=reason,
        )
        return MAP_EXIT_AMBIGUOUS

    merged = merge_repo_env(None if env is None else dict(env))
    prompt = _prompt_with_disc(disc_id, scan_json, ready_yaml)
    append_map_log(f"Starting map for DISC_ID={disc_id} scan={scan_json}")
    append_map_log(f"PROMPT_BEGIN DISC_ID={disc_id}")
    append_map_log(prompt)
    append_map_log(f"PROMPT_END DISC_ID={disc_id}")
    key_state = "set" if str(merged.get(llm.ENV_API_KEY) or "").strip() else "unset"
    append_map_log(
        f"OPENROUTER_API_KEY={key_state} model={llm.resolve_model(merged, call='map')}"
    )
    timeout = llm.HTTP_TIMEOUT_SEC if timeout_sec <= 0 else min(timeout_sec, llm.HTTP_TIMEOUT_SEC)
    reply = llm.request_map(prompt, env=merged, opener=opener, timeout=timeout)
    log_path, log_handle = _open_agent_output_log(disc_id, background=background)
    try:
        log_handle.write(
            f"action={reply.action} failure={reply.failure or '-'}\n"
        )
    finally:
        log_handle.close()
    append_map_log(f"Map output → {log_path}")

    if reply.action == "ambiguous":
        append_map_log(f"MAP_AMBIGUOUS DISC_ID={disc_id}: model declined to guess")
        emit_map_alerts(
            disc_id,
            MAP_FAIL_AMBIGUOUS,
            logger=logger,
            device=device,
        )
        return MAP_EXIT_AMBIGUOUS

    if reply.action != "write" or not isinstance(reply.manifest, dict):
        code, kind = _failure_code(reply)
        append_map_log(
            f"ERROR: map stopped for DISC_ID={disc_id} kind={kind} failure={reply.failure or '-'}"
        )
        if not background:
            emit_map_alerts(disc_id, kind, logger=logger, device=device)
        return code

    try:
        model = _validated_manifest(reply.manifest, _json_disc_name(scan_json))
        dest = _write_ready_manifest(model)
    except (ValidationError, ValueError, OSError) as exc:
        append_map_log(f"ERROR: manifest rejected for DISC_ID={disc_id}: {exc}")
        if not background:
            emit_map_alerts(disc_id, MAP_FAIL_OTHER, logger=logger, device=device)
        return 1

    append_map_log(f"SUCCESS: wrote {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Map disc scan to YAML via OpenRouter")
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
        help=f"HTTP timeout seconds, capped at {llm.HTTP_TIMEOUT_SEC}",
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
