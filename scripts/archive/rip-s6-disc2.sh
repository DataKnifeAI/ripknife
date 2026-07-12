#!/usr/bin/env bash
# SpongeBob S6 Vol.1 Disc 2 — 10 titles (makemkv 0-9)
set -euo pipefail
source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

OUT="${OUTPUT_ROOT}/Season 6"
mkdir -p "$OUT"
SEASON_DIR="Season 6"
TITLES="${SCRIPT_DIR}/examples/s6-disc2-episodes.txt"
DEVICE="${VIDEOSRIPPER_DEVICE:-dev:/dev/sr0}"
# makemkv index -> TheTVDB DVD order S6 episode number (all 6x##)
EP_NUMS=(16 31 30 09 10 17 18 19 20 44)
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

mapfile -t NAMES < "$TITLES"

for idx in $(seq 0 9); do
  ep="${EP_NUMS[$idx]}"
  name="${NAMES[$idx]}"
  final="$OUT/SpongeBob SquarePants - 6x${ep} - ${name}.mkv"
  echo "=== Ripping title $idx -> 6x${ep} - $name ==="
  rm -f "$STAGING"/*.mkv 2>/dev/null || true
  makemkvcon mkv -r --minlength=300 "$DEVICE" "$idx" "$STAGING"
  ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
  [[ -f "$ripped" ]] || { echo "Rip failed for title $idx" >&2; exit 1; }
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" | awk '{printf "%.0f", $1}')
  mv -f "$ripped" "$final"
  echo "  ${dur}s -> $(basename "$final")"
  copy_episode_to_share "$final" "$SEASON_DIR" || true
done

echo "All done: $(ls -1 "$OUT"/*.mkv 2>/dev/null | wc -l) files in \"${OUT}\""
eject_disc /dev/sr0
