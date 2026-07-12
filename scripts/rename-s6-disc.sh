#!/usr/bin/env bash
# Rename Season 6 rips: makemkv disc order -> TheTVDB aired segment numbers + titles
# Source: Season 6 Volume 1 DVD Disc 1 order + TheTVDB official S6 segment map
set -euo pipefail
source "$(dirname "$0")/videosripper-common.sh"

DIR="${OUTPUT_ROOT}/Season 6"
TMP="$DIR/.rename-staging"
mkdir -p "$TMP"

# makemkv index (old 6x01..6x10) -> "EP|Title"
declare -A MAP=(
  [01]="07|Spongicus"
  [02]="04|Suction Cup Symphony"
  [03]="12|House Fancy"
  [04]="01|Krabby Road"
  [05]="05|Penny Foolish"
  [06]="06|Nautical Novice"
  [07]="02|Not Normal"
  [08]="03|Gone"
  [09]="21|SpongeBob SquarePants vs. The Big One"
  [10]="10|A Life in a Day"
)

for old_idx in $(seq -w 1 10); do
  old=$(ls "$DIR"/SpongeBob\ SquarePants\ -\ 6x${old_idx}\ -\ *.mkv 2>/dev/null | head -1)
  [[ -n "$old" ]] || { echo "Missing 6x${old_idx}" >&2; exit 1; }
  IFS='|' read -r ep title <<< "${MAP[$old_idx]}"
  new="$TMP/SpongeBob SquarePants - 6x${ep} - ${title}.mkv"
  mv -v "$old" "$new"
done

mv -v "$TMP"/*.mkv "$DIR/"
rmdir "$TMP"

ls -1 "$DIR"/*.mkv
