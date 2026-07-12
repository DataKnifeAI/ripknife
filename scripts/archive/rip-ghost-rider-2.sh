#!/usr/bin/env bash
# Rip Ghost Rider: Spirit of Vengeance (2011) Blu-ray — theatrical feature only.
set -euo pipefail

source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"

MAKEMKV="${HOME}/.local/bin/makemkvcon"
DEVICE="${DEVICE:-dev:/dev/sr0}"
DISC_DEV="${DISC_DEV:-/dev/sr0}"
MINLENGTH=300
MIN_DURATION_SEC=$((80 * 60))
MIN_SIZE_BYTES=$((5 * 1024 * 1024 * 1024))

LOG="${LOG_ROOT}/rip-ghost-rider-2.log"
OUT_DIR="${OUTPUT_ROOT}/Movies"
OUT_NAME="Ghost Rider Spirit of Vengeance (2011).mkv"
OUT_PATH="${OUT_DIR}/${OUT_NAME}"

mkdir -p "$OUT_DIR" "$LOG_ROOT"
exec > >(tee -a "$LOG") 2>&1

duration_to_seconds() {
  local d=$1 a b c
  IFS=: read -r a b c <<< "$d"
  if [[ -n "$c" ]]; then
    echo $((10#$a * 3600 + 10#$b * 60 + 10#$c))
  else
    echo $((10#$a * 60 + 10#$b))
  fi
}

warn_java_missing() {
  echo "WARNING: Java JRE not found. BD-Java discs need Java for full MakeMKV support."
  echo "  Install: sudo pacman -S jre-openjdk"
  echo "  See https://www.makemkv.com/bdjava/"
}

check_java() {
  if java -version >/dev/null 2>&1; then
    echo "Java JRE found: $(java -version 2>&1 | head -1)"
  else
    warn_java_missing
  fi
}

pick_feature_title() {
  local scan=$1
  local -A dur_sec size_bytes
  local line idx dur size best_idx=-1 best_size=0 t_dur t_size

  while IFS= read -r line; do
    if [[ "$line" =~ ^TINFO:([0-9]+),9,0,\"([^\"]+)\" ]]; then
      idx="${BASH_REMATCH[1]}"
      dur="${BASH_REMATCH[2]}"
      dur_sec[$idx]=$(duration_to_seconds "$dur")
    elif [[ "$line" =~ ^TINFO:([0-9]+),11,0,\"([0-9]+)\" ]]; then
      idx="${BASH_REMATCH[1]}"
      size_bytes[$idx]="${BASH_REMATCH[2]}"
    fi
  done <<< "$scan"

  if [[ -n "${dur_sec[0]:-}" && "${dur_sec[0]}" -ge "$MIN_DURATION_SEC" ]]; then
    echo 0
    return
  fi

  for idx in "${!dur_sec[@]}"; do
    t_dur="${dur_sec[$idx]}"
    t_size="${size_bytes[$idx]:-0}"
    if (( t_dur >= MIN_DURATION_SEC && t_size > best_size )); then
      best_idx=$idx
      best_size=$t_size
    fi
  done

  if (( best_idx < 0 )); then
    echo "ERROR: No title >= 80 min found with --minlength=${MINLENGTH}." >&2
    echo "  Re-scan disc; theatrical should be ~1:35:31 (~24 GB)." >&2
    exit 1
  fi
  echo "$best_idx"
}

verify_rip() {
  local file=$1
  local dur size

  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" | awk '{printf "%.0f", $1}')
  size=$(stat -c '%s' "$file")

  echo "Verify: ${dur}s duration, $(numfmt --to=iec-i --suffix=B "$size" 2>/dev/null || echo "${size} bytes")"

  if (( dur < MIN_DURATION_SEC )); then
    echo "ERROR: Rip too short (${dur}s < ${MIN_DURATION_SEC}s / 80 min) — likely wrong title (promo/extra)." >&2
    echo "  Expected theatrical ~1:35:31 on title 0 (00800.mpls) with --minlength=${MINLENGTH}." >&2
    return 1
  fi
  if (( size < MIN_SIZE_BYTES )); then
    echo "ERROR: Rip too small ($(numfmt --to=iec-i --suffix=B "$size" 2>/dev/null || echo "$size") < 5 GiB) — not copying to NAS." >&2
    return 1
  fi
  return 0
}

echo "=== Ghost Rider: Spirit of Vengeance rip started $(date) ==="
echo "Output: ${OUT_PATH}"
echo "NAS: ${MOVIES_NAS}/${OUT_NAME}"

if [[ ! -b "$DISC_DEV" ]]; then
  echo "ERROR: ${DISC_DEV} not found — insert disc and re-run." >&2
  exit 1
fi

check_java

echo "=== Scanning disc (--minlength=${MINLENGTH}) $(date) ==="
SCAN=$("$MAKEMKV" info -r --minlength="${MINLENGTH}" "$DEVICE" 2>&1) || {
  echo "ERROR: makemkv info scan failed" >&2
  exit 1
}

if ! grep -q 'GHOST_RIDER' <<< "$SCAN"; then
  echo "ERROR: Expected GHOST_RIDER disc in ${DISC_DEV}; insert correct disc." >&2
  grep '^DRV:0,' <<< "$SCAN" || true
  exit 1
fi

if grep -q 'requires Java runtime (JRE), but none was found' <<< "$SCAN"; then
  echo "WARNING: MakeMKV reports no Java JRE (BD-Java disc). Rip may fail or pick wrong title."
  warn_java_missing
fi

TITLE="${TITLE:-$(pick_feature_title "$SCAN")}"
TITLE_DUR=$(grep -m1 "^TINFO:${TITLE},9,0," <<< "$SCAN" | sed -n 's/.*,"\([^"]*\)".*/\1/p')
TITLE_SIZE=$(grep -m1 "^TINFO:${TITLE},11,0," <<< "$SCAN" | sed -n 's/.*,"\([^"]*\)".*/\1/p')
TITLE_MPLS=$(grep -m1 "^TINFO:${TITLE},16,0," <<< "$SCAN" | sed -n 's/.*,"\([^"]*\)".*/\1/p' || true)

echo "Selected title ${TITLE} (${TITLE_MPLS:-unknown}) duration=${TITLE_DUR:-?} size=${TITLE_SIZE:-?} bytes"
echo "Rip flags: mkv -r --minlength=${MINLENGTH} (must match scan)"

STAGING=$(videosripper_staging_dir)
echo "Staging: ${STAGING}"
trap 'rm -rf "$STAGING"' EXIT

if [[ -f "$OUT_PATH" ]]; then
  existing_size=$(stat -c '%s' "$OUT_PATH")
  if (( existing_size < MIN_SIZE_BYTES )); then
    echo "Removing prior bad rip ($(du -h "$OUT_PATH" | cut -f1))"
    rm -f "$OUT_PATH"
  else
    echo "Output already exists and looks valid — skipping rip."
    exit 0
  fi
fi

"$MAKEMKV" mkv -r --minlength="${MINLENGTH}" "$DEVICE" "$TITLE" "$STAGING"

RIPPED=$(find "$STAGING" -name '*.mkv' -type f | head -1)
[[ -n "$RIPPED" ]] || { echo "ERROR: no MKV produced" >&2; exit 1; }

if ! verify_rip "$RIPPED"; then
  rm -f "$RIPPED"
  exit 1
fi

mv "$RIPPED" "$OUT_PATH"
echo "  -> ${OUT_PATH} ($(du -h "$OUT_PATH" | cut -f1))"

copy_movie_to_share "$OUT_PATH"
eject_disc "$DISC_DEV"

echo "=== Done $(date) ==="
