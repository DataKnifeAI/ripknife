#!/usr/bin/env bash
# SpongeBob S7 Complete Season Disc 2 — 11 episodes (DVD order 7x15–7x25)
# makemkv titles 1–11; title 11 = The Cent of Money + bonus reel (split at ~661s); skip play-all 0, extra 12
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 7"
SEASON_DIR="Season 7"
TITLES="${SCRIPT_DIR}/examples/s7-disc2-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
LOG="${LOG_ROOT}/rip-s7-disc2.log"
START_AT="${VIDEOSRIPPER_START_AT:-1}"
mkdir -p "$OUT" "$LOG_ROOT"
declare -a TITLE_IDX=(1 2 3 4 5 6 7 8 9 10 11)
declare -a EP_NUMS=(15 16 17 18 19 20 21 22 23 24 25)
CENT_OF_MONEY_SPLIT=661.327333
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

split_episode() {
  local src=$1 dest=$2 split_at=$3
  ffmpeg -y -i "$src" -map 0 -map -0:d -c copy -reset_timestamps 1 -to "$split_at" \
    "$dest" >/dev/null
}

exec > >(tee -a "$LOG") 2>&1
echo "=== S7 Disc 2 rip started $(date) ==="
echo "Output: $OUT"
echo "Staging: $STAGING"

for i in $(seq 0 9); do
  slot=$((i + 1))
  (( slot < START_AT )) && continue
  idx="${TITLE_IDX[$i]}"
  ep="${EP_NUMS[$i]}"
  name="${NAMES[$i]}"
  final="$OUT/SpongeBob SquarePants - 7x${ep} - ${name}.mkv"
  if [[ -f "$final" ]]; then
    echo "=== Skipping title $idx (already exists): $(basename "$final") ==="
    continue
  fi
  echo "=== Ripping title $idx -> 7x${ep} - $name ==="
  rm -f "$STAGING"/*.mkv 2>/dev/null || true
  makemkvcon mkv -r --minlength=300 "$DEVICE" "$idx" "$STAGING"
  ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
  [[ -f "$ripped" ]] || { echo "Rip failed for title $idx" >&2; exit 1; }
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" | awk '{printf "%.0f", $1}')
  mv -f "$ripped" "$final"
  echo "  ${dur}s -> $(basename "$final")"
  copy_episode_to_share "$final" "$SEASON_DIR" || true
done

if (( START_AT <= 11 )); then
  final="$OUT/SpongeBob SquarePants - 7x25 - ${NAMES[10]}.mkv"
  if [[ -f "$final" ]]; then
    echo "=== Skipping title 11 (already exists): $(basename "$final") ==="
  else
    echo "=== Ripping title 11 -> 7x25 ${NAMES[10]} (split bonus at ${CENT_OF_MONEY_SPLIT}s) ==="
    rm -f "$STAGING"/*.mkv 2>/dev/null || true
    makemkvcon mkv -r --minlength=300 "$DEVICE" 11 "$STAGING"
    ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
    [[ -f "$ripped" ]] || { echo "Rip failed for title 11" >&2; exit 1; }
    split_episode "$ripped" "$final" "$CENT_OF_MONEY_SPLIT"
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$final" | awk '{printf "%.0f", $1}')
    echo "  ${dur}s -> $(basename "$final")"
    copy_episode_to_share "$final" "$SEASON_DIR" || true
  fi
fi

file_count=$(find "$OUT" -maxdepth 1 -name '*.mkv' 2>/dev/null | wc -l)
echo "All done: ${file_count} files in ${OUT}"
echo "=== Finished $(date) ==="
eject_disc /dev/sr0
