#!/usr/bin/env bash
# SpongeBob S3 Complete Season Disc 2 — 14 episodes (DVD order 3x17–3x30; 16 menu segments, 2 specials)
# makemkv: skip multi-angle dupes (odd titles 3+), play-all 28–29, extras 30–31
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 3"
SEASON_DIR="Season 3"
TITLES="${SCRIPT_DIR}/examples/s3-disc2-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
LOG="${LOG_ROOT}/rip-s3-disc2.log"
START_AT="${VIDEOSRIPPER_START_AT:-1}"
mkdir -p "$OUT" "$LOG_ROOT"
declare -a TITLE_IDX=(0 2 4 6 8 10 12 14 16 18 20 22 24 26)
declare -a EP_NUMS=(17 18 19 20 21 22 23 24 25 26 27 28 29 30)
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

exec > >(tee -a "$LOG") 2>&1
echo "=== S3 Disc 2 rip started $(date) ==="
echo "Output: $OUT"
echo "Staging: $STAGING"

for i in $(seq 0 13); do
  slot=$((i + 1))
  (( slot < START_AT )) && continue
  idx="${TITLE_IDX[$i]}"
  ep="${EP_NUMS[$i]}"
  name="${NAMES[$i]}"
  final="$OUT/SpongeBob SquarePants - 3x${ep} - ${name}.mkv"
  if [[ -f "$final" ]]; then
    echo "=== Skipping title $idx (already exists): $(basename "$final") ==="
    continue
  fi
  echo "=== Ripping title $idx -> 3x${ep} - $name ==="
  rm -f "$STAGING"/*.mkv 2>/dev/null || true
  makemkvcon mkv -r --minlength=300 "$DEVICE" "$idx" "$STAGING"
  ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
  [[ -f "$ripped" ]] || { echo "Rip failed for title $idx" >&2; exit 1; }
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" | awk '{printf "%.0f", $1}')
  mv -f "$ripped" "$final"
  echo "  ${dur}s -> $(basename "$final")"
  copy_episode_to_share "$final" "$SEASON_DIR" || true
done

file_count=$(find "$OUT" -maxdepth 1 -name '*.mkv' 2>/dev/null | wc -l)
echo "All done: ${file_count} files in ${OUT}"
echo "=== Finished $(date) ==="
eject_disc /dev/sr0
