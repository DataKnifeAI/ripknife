#!/usr/bin/env bash
# SpongeBob S3 Complete Season Disc 1 — 16 episodes (skip multi-angle dupes, play-all, extras)
# Menu 15 (No Weenies Allowed) has no standalone title — extract from play-all title 30 ch15.
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 3"
SEASON_DIR="Season 3"
TITLES="${SCRIPT_DIR}/examples/s3-disc1-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
LOG="${LOG_ROOT}/rip-s3-disc1.log"
START_AT="${VIDEOSRIPPER_START_AT:-1}"
PLAYALL_IDX=30
NO_WEENIES_CH=14  # 0-based ffprobe chapter index (menu segment 15)
mkdir -p "$OUT" "$LOG_ROOT"
# makemkv title index (multi-angle disc — angle 1 only; slots 1–14)
declare -a TITLE_IDX=(1 2 4 6 8 10 12 14 16 18 20 22 24 26)
declare -a EP_NUMS=(01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16)
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

chapter_bounds() {
  local file=$1 ch=$2
  ffprobe -v error -show_entries chapter=start_time,end_time -of csv=p=0 "$file" \
    | sed -n "$((ch + 1))p" | tr ',' ' '
}

extract_playall_chapter() {
  local playall=$1 ch=$2 dest=$3
  local start end
  read -r start end < <(chapter_bounds "$playall" "$ch")
  ffmpeg -y -i "$playall" -ss "$start" -to "$end" -map 0 -map -0:d -c copy -reset_timestamps 1 \
    "$dest" >/dev/null
}

exec > >(tee -a "$LOG") 2>&1
echo "=== S3 Disc 1 rip started $(date) ==="
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

# Slot 15: menu 15 missing — No Weenies Allowed from play-all title 30
if (( START_AT <= 15 )); then
  final15="$OUT/SpongeBob SquarePants - 3x15 - ${NAMES[14]}.mkv"
  if [[ -f "$final15" ]]; then
    echo "=== Skipping 3x15 (already exists): $(basename "$final15") ==="
  else
    echo "=== Ripping play-all title $PLAYALL_IDX -> 3x15 ${NAMES[14]} (chapter extract) ==="
    rm -f "$STAGING"/*.mkv 2>/dev/null || true
    makemkvcon mkv -r --minlength=300 "$DEVICE" "$PLAYALL_IDX" "$STAGING"
    playall=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
    [[ -f "$playall" ]] || { echo "Play-all rip failed" >&2; exit 1; }
    extract_playall_chapter "$playall" "$NO_WEENIES_CH" "$final15"
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$final15" | awk '{printf "%.0f", $1}')
    echo "  ${dur}s -> $(basename "$final15")"
    copy_episode_to_share "$final15" "$SEASON_DIR" || true
  fi
fi

# Slot 16: Squilliam Returns — title 28 angle 1 (title 29 is angle 2 dupe)
if (( START_AT <= 16 )); then
  final16="$OUT/SpongeBob SquarePants - 3x16 - ${NAMES[15]}.mkv"
  if [[ -f "$final16" ]]; then
    echo "=== Skipping title 28 (already exists): $(basename "$final16") ==="
  else
    echo "=== Ripping title 28 -> 3x16 - ${NAMES[15]} ==="
    rm -f "$STAGING"/*.mkv 2>/dev/null || true
    makemkvcon mkv -r --minlength=300 "$DEVICE" 28 "$STAGING"
    ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
    [[ -f "$ripped" ]] || { echo "Rip failed for title 28" >&2; exit 1; }
    dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" | awk '{printf "%.0f", $1}')
    mv -f "$ripped" "$final16"
    echo "  ${dur}s -> $(basename "$final16")"
    copy_episode_to_share "$final16" "$SEASON_DIR" || true
  fi
fi

file_count=$(find "$OUT" -maxdepth 1 -name '*.mkv' 2>/dev/null | wc -l)
echo "All done: ${file_count} files in ${OUT}"
echo "=== Finished $(date) ==="
eject_disc /dev/sr0
