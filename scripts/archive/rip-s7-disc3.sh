#!/usr/bin/env bash
# SpongeBob S7 Complete Season Disc 3 — 13 episodes (DVD order 7x26–7x38)
# makemkv titles 1–13; title 13 = Earworm + bonus reel (split at ~662s); skip play-all 0, extra 14
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 7"
SEASON_DIR="Season 7"
TITLES="${SCRIPT_DIR}/examples/s7-disc3-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
LOG="${LOG_ROOT}/rip-s7-disc3.log"
START_AT="${VIDEOSRIPPER_START_AT:-1}"
mkdir -p "$OUT" "$LOG_ROOT"
declare -a TITLE_IDX=(1 2 3 4 5 6 7 8 9 10 11 12 13)
declare -a EP_NUMS=(26 27 28 29 30 31 32 33 34 35 36 37 38)
EARWORM_SPLIT=661.961300
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

split_episode() {
  local src=$1 dest=$2 split_at=$3
  ffmpeg -y -i "$src" -map 0 -map -0:d -c copy -reset_timestamps 1 -to "$split_at" \
    "$dest" >/dev/null
}

exec > >(tee -a "$LOG") 2>&1
echo "=== S7 Disc 3 rip started $(date) ==="
echo "Output: $OUT"
echo "Staging: $STAGING"

for i in $(seq 0 11); do
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
  echo "=== Ripping title $idx -> 7x${ep} - ${name} ==="
  rm -f "$STAGING"/*.mkv 2>/dev/null || true
  makemkvcon mkv -r --minlength=300 "$DEVICE" "$idx" "$STAGING"
  ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
  [[ -f "$ripped" ]] || { echo "Rip failed for title $idx" >&2; exit 1; }
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" | awk '{printf "%.0f", $1}')
  mv -f "$ripped" "$final"
  echo "  ${dur}s -> $(basename "$final")"
  copy_episode_to_share "$final" "$SEASON_DIR" || true
done

if (( START_AT <= 13 )); then
  final="$OUT/SpongeBob SquarePants - 7x38 - ${NAMES[12]}.mkv"
  if [[ -f "$final" ]]; then
    echo "=== Skipping title 13 (already exists): $(basename "$final") ==="
  else
    echo "=== Ripping title 13 -> 7x38 ${NAMES[12]} (split bonus at ${EARWORM_SPLIT}s) ==="
    rm -f "$STAGING"/*.mkv 2>/dev/null || true
    makemkvcon mkv -r --minlength=300 "$DEVICE" 13 "$STAGING"
    ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
    [[ -f "$ripped" ]] || { echo "Rip failed for title 13" >&2; exit 1; }
    split_episode "$ripped" "$final" "$EARWORM_SPLIT"
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$final" | awk '{printf "%.0f", $1}')
    echo "  ${dur}s -> $(basename "$final")"
    copy_episode_to_share "$final" "$SEASON_DIR" || true
  fi
fi

file_count=$(find "$OUT" -maxdepth 1 -name '*.mkv' 2>/dev/null | wc -l)
echo "All done: ${file_count} files in ${OUT}"
echo "=== Finished $(date) ==="
eject_disc /dev/sr0
