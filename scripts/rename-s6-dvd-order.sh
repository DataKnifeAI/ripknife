#!/usr/bin/env bash
# Rename Season 6 rips: aired/mixed numbers -> TheTVDB DVD order (all 6x## in Season 6/)
set -euo pipefail
source "$(dirname "$0")/videosripper-common.sh"

# Episode title -> DVD episode number (TheTVDB DVD order, season 6)
declare -A DVD_EP=(
  ["House Fancy"]=1
  ["Krabby Road"]=2
  ["Penny Foolish"]=3
  ["Nautical Novice"]=4
  ["Spongicus"]=5
  ["Suction Cup Symphony"]=6
  ["Not Normal"]=7
  ["Gone"]=8
  ["The Splinter"]=9
  ["Slide Whistle Stooges"]=10
  ["A Life in a Day"]=11
  ["Sun Bleached"]=12
  ["Giant Squidward"]=13
  ["No Nose Knows"]=14
  ["Patty Caper"]=15
  ["Plankton's Regular"]=16
  ["Boating Buddies"]=17
  ["The Krabby Kronicle"]=18
  ["The Slumber Party"]=19
  ["Grooming Gary"]=20
  ["SpongeBob SquarePants vs. The Big One"]=21
  ["Porous Pockets"]=22
  ["Choir Boys"]=23
  ["Krusty Krushers"]=24
  ["The Card"]=25
  ["Dear Vikings"]=26
  ["Ditchin'"]=27
  ["Grandpappy the Pirate"]=28
  ["Cephalopod Lodge"]=29
  ["Squid's Visit"]=30
  ["To SquarePants or Not to SquarePants"]=31
  ["Shuffleboarding"]=32
  ["Professor Squidward"]=33
  ["Pet or Pests"]=34
  ["Komputer Overload"]=35
  ["Gullible Pants"]=36
  ["Overbooked"]=37
  ["No Hat for Pat"]=38
  ["Toy Store of Doom"]=39
  ["Sand Castles in the Sand"]=40
  ["Shell Shocked"]=41
  ["Chum Bucket Supreme"]=42
  ["Single Cell Anniversary"]=43
  ["Truth or Square"]=44
  ["Pineapple Fever"]=45
  ["Chum Caverns"]=46
  ["The Clash of Triton"]=47
)

rename_dir() {
  local dir=$1
  local tmp="$dir/.dvd-rename-staging"
  [[ -d "$dir" ]] || { echo "Skip missing dir: $dir" >&2; return 0; }

  mkdir -p "$tmp"
  shopt -s nullglob
  local f basename title ep ep_fmt new
  for f in "$dir"/SpongeBob*.mkv; do
    basename=$(basename "$f")
    if [[ "$basename" =~ SpongeBob\ SquarePants\ -\ [0-9]+x[0-9]+\ -\ (.+)\.mkv ]]; then
      title="${BASH_REMATCH[1]}"
    else
      echo "  skip (bad name): $basename" >&2
      continue
    fi
    ep="${DVD_EP[$title]:-}"
    if [[ -z "$ep" ]]; then
      echo "  skip (no DVD map): $basename" >&2
      continue
    fi
    ep_fmt=$(printf '%02d' "$ep")
    new="SpongeBob SquarePants - 6x${ep_fmt} - ${title}.mkv"
    if [[ "$basename" == "$new" ]]; then
      echo "  ok: $basename"
      continue
    fi
    echo "  $basename -> $new"
    mv -f "$f" "$tmp/$new"
  done

  if compgen -G "$tmp"/*.mkv >/dev/null; then
    mv -f "$tmp"/*.mkv "$dir/"
  fi
  rmdir "$tmp" 2>/dev/null || true
  shopt -u nullglob
}

echo "=== Renaming local output ==="
rename_dir "${OUTPUT_ROOT}/Season 6"

echo "=== Renaming NAS share ==="
rename_dir "${NAS_SHARE}/Season 6"

echo "=== Done ==="
ls -1 "${OUTPUT_ROOT}/Season 6"/*.mkv 2>/dev/null | sort
