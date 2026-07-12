#!/usr/bin/env bash
# SpongeBob S4 Complete Season Disc 4 (SBSP_S4_V2_D2) — 10 episodes, DVD order 4x29–4x38
# makemkv titles 1–10; title 10 = The Gift of Gum + credits (split at ~649s); skip play-all 0, extra 11
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 4"
SEASON_DIR="Season 4"
TITLES="${SCRIPT_DIR}/examples/s4-disc4-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
LOG="${LOG_ROOT}/rip-s4-disc4.log"
START_AT="${VIDEOSRIPPER_START_AT:-1}"
mkdir -p "$OUT" "$LOG_ROOT"
declare -a TITLE_IDX=(1 2 3 4 5 6 7 8 9 10)
# TheTVDB DVD order (disc menu order ≠ episode numbers)
declare -a EP_NUMS=(30 29 31 32 34 33 36 35 37 38)
GIFT_OF_GUM_SPLIT=649.148500
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

split_episode() {
  local src=$1 dest=$2 split_at=$3
  ffmpeg -y -i "$src" -map 0 -map -0:d -c copy -reset_timestamps 1 -to "$split_at" \
    "$dest" >/dev/null
}

exec > >(tee -a "$LOG") 2>&1
echo "=== S4 Disc 4 rip started $(date) ==="
echo "Output: $OUT"
echo "Staging: $STAGING"

for i in $(seq 0 8); do
  slot=$((i + 1))
  (( slot < START_AT )) && continue
  idx="${TITLE_IDX[$i]}"
  ep="${EP_NUMS[$i]}"
  name="${NAMES[$i]}"
  final="$OUT/SpongeBob SquarePants - 4x${ep} - ${name}.mkv"
  if [[ -f "$final" ]]; then
    echo "=== Skipping title $idx (already exists): $(basename "$final") ==="
    continue
  fi
  echo "=== Ripping title $idx -> 4x${ep} - $name ==="
  rm -f "$STAGING"/*.mkv 2>/dev/null || true
  makemkvcon mkv -r --minlength=300 "$DEVICE" "$idx" "$STAGING"
  ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
  [[ -f "$ripped" ]] || { echo "Rip failed for title $idx" >&2; exit 1; }
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" | awk '{printf "%.0f", $1}')
  mv -f "$ripped" "$final"
  echo "  ${dur}s -> $(basename "$final")"
  copy_episode_to_share "$final" "$SEASON_DIR" || true
done

if (( START_AT <= 10 )); then
  final="$OUT/SpongeBob SquarePants - 4x38 - ${NAMES[9]}.mkv"
  if [[ -f "$final" ]]; then
    echo "=== Skipping title 10 (already exists): $(basename "$final") ==="
  else
    echo "=== Ripping title 10 -> 4x38 ${NAMES[9]} (split credits at ${GIFT_OF_GUM_SPLIT}s) ==="
    rm -f "$STAGING"/*.mkv 2>/dev/null || true
    makemkvcon mkv -r --minlength=300 "$DEVICE" 10 "$STAGING"
    ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
    [[ -f "$ripped" ]] || { echo "Rip failed for title 10" >&2; exit 1; }
    split_episode "$ripped" "$final" "$GIFT_OF_GUM_SPLIT"
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$final" | awk '{printf "%.0f", $1}')
    echo "  ${dur}s -> $(basename "$final")"
    copy_episode_to_share "$final" "$SEASON_DIR" || true
  fi
fi

file_count=$(find "$OUT" -maxdepth 1 -name '*.mkv' 2>/dev/null | wc -l)
echo "All done: ${file_count} files in ${OUT}"
echo "=== Finished $(date) ==="
eject_disc /dev/sr0
