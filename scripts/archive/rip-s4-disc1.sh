#!/usr/bin/env bash
# SpongeBob S4 Complete Season Disc 1 — 9 episodes (DVD order 4x01–4x09)
# makemkv: titles 1–7 direct; title 8 = Selling Out + Funny Pants (split by chapter); skip title 9 (play-all)
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 4"
SEASON_DIR="Season 4"
TITLES="${SCRIPT_DIR}/examples/s4-disc1-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
LOG="${LOG_ROOT}/rip-s4-disc1.log"
START_AT="${VIDEOSRIPPER_START_AT:-1}"
mkdir -p "$OUT" "$LOG_ROOT"
# titles 4/5 on disc are swapped vs DVD menu labels
declare -a TITLE_IDX=(1 2 3 4 5 6 7 8)
declare -a EP_NUMS=(01 02 03 05 04 06 07 08)
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

split_title8() {
  local src=$1
  local split_dir=$2
  mkdir -p "$split_dir"
  ffmpeg -y -i "$src" -map 0 -map -0:d -c copy -reset_timestamps 1 -to 662.428433 \
    "$split_dir/4x08.mkv" >/dev/null
  ffmpeg -y -i "$src" -ss 662.428433 -map 0 -map -0:d -c copy -reset_timestamps 1 \
    "$split_dir/4x09.mkv" >/dev/null
}

exec > >(tee -a "$LOG") 2>&1
echo "=== S4 Disc 1 rip started $(date) ==="
echo "Output: $OUT"
echo "Staging: $STAGING"

for i in $(seq 0 6); do
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

# Title 8: combined Selling Out + Funny Pants — split into 4x08 and 4x09
if (( START_AT <= 8 )); then
  out08="$OUT/SpongeBob SquarePants - 4x08 - Selling Out.mkv"
  out09="$OUT/SpongeBob SquarePants - 4x09 - Funny Pants.mkv"
  if [[ -f "$out08" && -f "$out09" ]]; then
    echo "=== Skipping title 8 (4x08 and 4x09 already exist) ==="
  else
    echo "=== Ripping title 8 -> 4x08 Selling Out + 4x09 Funny Pants (chapter split) ==="
    rm -f "$STAGING"/*.mkv 2>/dev/null || true
    makemkvcon mkv -r --minlength=300 "$DEVICE" 8 "$STAGING"
    ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
    [[ -f "$ripped" ]] || { echo "Rip failed for title 8" >&2; exit 1; }
    split_title8 "$ripped" "$STAGING/split"
    for ep name in "08 Selling Out" "09 Funny Pants"; do
      final="$OUT/SpongeBob SquarePants - 4x${ep} - ${name}.mkv"
      [[ -f "$final" ]] && continue
      mv -f "$STAGING/split/4x${ep}.mkv" "$final"
      dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$final" | awk '{printf "%.0f", $1}')
      echo "  ${dur}s -> $(basename "$final")"
      copy_episode_to_share "$final" "$SEASON_DIR" || true
    done
  fi
fi

file_count=$(find "$OUT" -maxdepth 1 -name '*.mkv' 2>/dev/null | wc -l)
echo "All done: ${file_count} files in ${OUT}"
echo "=== Finished $(date) ==="
eject_disc /dev/sr0
