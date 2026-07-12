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
READY_YAML="${ROOT}/staging/ready/${DISC_ID}.yaml"
WORKFLOW="${ROOT}/LLM-RIP-WORKFLOW.md"

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
Read ${SCAN_JSON} and ${WORKFLOW}.

Author a complete VideosRipper YAML manifest for this disc and write it to:
  ${READY_YAML}

Requirements:
- Use kind tv_disc or movie as appropriate from the scan
- Set meta.id to "${DISC_ID}"
- Set meta.disc_name from the scan disc_name field
- Map makemkv titles to episodes using TheTVDB DVD order (or official order for live-action)
- Include skips[] for play-all and extras
- Avoid copy-protection / dummy titles: skip names like COPYRIGHT/FBI/WARNING loops; prefer later real episode titles when early indices look like menus or protection loops
- Obfuscated Blu-rays (many duplicate playlists with identical names/durations): read obfuscation in scan JSON if present — pick title_selection from the dominant cluster (recommended_title), use auto_feature or fixed at that index; add decoy_indices to skips[] with reason "decoy playlist (duration outlier)"
- Verify each mapped makemkv_title exists in the scan, meets minlength, and duration matches expected episode length (~11 min cartoons, ~47 min live-action); check later titles if an early index looks wrong
- Do NOT write directly to configs/ — Process 2 rips from staging/ready
- Match existing manifest style under configs/ (stream_defaults, nas, post blocks)
- Validate mentally against the scan title list before finishing
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
