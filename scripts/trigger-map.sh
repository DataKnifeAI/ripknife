#!/usr/bin/env bash
# Trigger LLM manifest mapping for an unknown disc (Process 1.5).
set -euo pipefail

ROOT="/mnt/game1/VideosRipper"
AGENT="${HOME}/.local/bin/agent"
LOG="${ROOT}/logs/map.log"
TIMEOUT_SEC=600
WORKSPACE="${ROOT}"

usage() {
  echo "Usage: $0 DISC_ID [SCAN_JSON_PATH]" >&2
  echo "  DISC_ID        Staging id (e.g. breaking-bad-season-1-disc-1)" >&2
  echo "  SCAN_JSON_PATH Optional scan JSON (default: staging/pending/\${DISC_ID}.scan.json)" >&2
  exit 2
}

log() {
  mkdir -p "$(dirname "$LOG")"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"
}

[[ $# -ge 1 ]] || usage

DISC_ID="$1"
SCAN_JSON="${2:-${ROOT}/staging/pending/${DISC_ID}.scan.json}"
META_JSON="${SCAN_JSON%.scan.json}.meta.json"
READY_YAML="${ROOT}/staging/ready/${DISC_ID}.yaml"
WORKFLOW="${ROOT}/LLM-RIP-WORKFLOW.md"
REGISTRY="${ROOT}/configs/registry.yaml"

if [[ ! -x "$AGENT" ]]; then
  if command -v agent >/dev/null 2>&1; then
    AGENT="$(command -v agent)"
  else
    log "ERROR: agent not found (expected ${HOME}/.local/bin/agent on PATH)"
    exit 1
  fi
fi

if [[ ! -f "$SCAN_JSON" ]]; then
  log "ERROR: scan JSON not found: $SCAN_JSON"
  exit 1
fi

if [[ ! -f "$WORKFLOW" ]]; then
  log "ERROR: workflow doc not found: $WORKFLOW"
  exit 1
fi

mkdir -p "${ROOT}/staging/ready" "$(dirname "$LOG")"

PROMPT=$(cat <<EOF
Read these inputs before authoring YAML:
  - Scan JSON: ${SCAN_JSON}
  - Pending meta: ${META_JSON} (title_count, obfuscation, recommended_title, decoy_indices)
  - Workflow: ${WORKFLOW} — section "Title research checklist for LLM mapper"
  - Registry: ${REGISTRY} — cross-reference similar disc_name matches for track patterns

Write the manifest to: ${READY_YAML}
Do NOT write directly to configs/ — Process 2 rips from staging/ready.

Title research steps (do these in order):
1. Parse scan JSON: disc_name, titles[], obfuscation block (if any). Read meta.json title_count and flattened obfuscation hints.
2. If title_count > 50: treat as likely Blu-ray playlist obfuscation — read duplicate_count, dominant_duration_sec, recommended_title, decoy_indices.
3. If obfuscation.detected or duplicate_count >= 5: for movies use title_selection.mode auto_feature (or fixed at recommended_title); add every decoy_indices entry to skips[] with reason "decoy playlist (duration outlier)".
4. DVD / TV scene-jump risk: skip title 0 play-all; if early indices are suspicious (COPYRIGHT/FBI/WARNING, short duration, Notes-style flags), check later titles (N+1, N+2). Use split for 2-chapter ~20min titles; extract_chapter for play-all-only episodes.
5. Cross-reference ${REGISTRY} and completed manifests under configs/ for the same show — match episode numbering and makemkv index style (TINFO vs MSG).
6. Set meta.id="${DISC_ID}", meta.disc_name from scan disc_name, meta.minlength from meta.json (default 300).
7. Document title choice rationale in a YAML comment above title_selection or tracks (e.g. why recommended_title, which decoys skipped, later-title overrides).
8. Run validation before finishing:
     scripts/videosripper validate ${READY_YAML} --with-scan --scan-json ${SCAN_JSON}
   Fix all errors; re-run until pass.

Manifest requirements:
- kind tv_disc or movie as appropriate
- Map episodes using TheTVDB DVD order (TV) or official order (live-action); movies use title_selection + validation floors
- skips[] for play-all, extras, decoy playlists, skipped angles, suspicious loops
- Each tracks[].makemkv_title must exist in scan, meet minlength, and match expected duration (~11 min cartoons, ~47 min drama, ~23 min anime)
- Match existing manifest style (stream_defaults, nas, post blocks)
- Optional spot-check: scripts/videosripper preview --title N --chapter 2 --scan-json ${SCAN_JSON}
EOF
)

log "Starting map for DISC_ID=${DISC_ID} scan=${SCAN_JSON}"

set +e
timeout "${TIMEOUT_SEC}" "$AGENT" -p --trust --force --approve-mcps \
  --workspace "$WORKSPACE" \
  "$PROMPT" >>"$LOG" 2>&1
status=$?
set -e

if [[ $status -eq 0 && -f "$READY_YAML" ]]; then
  log "SUCCESS: wrote ${READY_YAML}"
  exit 0
fi

if [[ $status -eq 124 ]]; then
  log "ERROR: agent timed out after ${TIMEOUT_SEC}s for DISC_ID=${DISC_ID}"
else
  log "ERROR: agent exited ${status} for DISC_ID=${DISC_ID}"
fi

if [[ ! -f "$READY_YAML" ]]; then
  log "ERROR: expected output missing: ${READY_YAML}"
fi
exit 1
