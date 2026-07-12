#!/usr/bin/env bash
# Lossless DVD rip + Plex-style rename via makemkvcon.
set -euo pipefail

source "${HOME}/.config/environment.d/makemkv.conf" 2>/dev/null || true
export LD_LIBRARY_PATH="${HOME}/.local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
source "$(dirname "$0")/../videosripper-common.sh"

SHOW=""
SEASON=""
START_EP=1
TITLES_FILE=""
OUTPUT_DIR="${VIDEOSRIPPER_OUTPUT:-/mnt/game1/VideosRipper/output}"
DEVICE="dev:/dev/sr0"
MINLENGTH=300
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: rip-dvd.sh --show NAME --season N --titles-file FILE [options]

Required:
  --show NAME           e.g. "SpongeBob SquarePants"
  --season N            season number (e.g. 2)
  --titles-file FILE    one episode title per line, in DVD episode order

Options:
  --start-episode N     first episode number on this disc (default: 1)
  --output DIR          rip destination (default: /mnt/game1/VideosRipper/output)
  --device PATH         makemkv source (default: dev:/dev/sr0)
  --minlength SEC       skip titles shorter than SEC (default: 300)
  --dry-run             print plan only, do not rip
  -h, --help

Example:
  ./rip-dvd.sh \
    --show "SpongeBob SquarePants" \
    --season 2 \
    --start-episode 16 \
    --titles-file scripts/examples/s2e16-18.txt
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --show) SHOW=$2; shift 2 ;;
    --season) SEASON=$2; shift 2 ;;
    --start-episode) START_EP=$2; shift 2 ;;
    --titles-file) TITLES_FILE=$2; shift 2 ;;
    --output) OUTPUT_DIR=$2; shift 2 ;;
    --device) DEVICE=$2; shift 2 ;;
    --minlength) MINLENGTH=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

[[ -n "$SHOW" && -n "$SEASON" && -n "$TITLES_FILE" ]] || { usage; exit 1; }
[[ -f "$TITLES_FILE" ]] || { echo "Titles file not found: $TITLES_FILE" >&2; exit 1; }
command -v makemkvcon >/dev/null || { echo "makemkvcon not found" >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found" >&2; exit 1; }

if [[ "$DEVICE" == dev:/dev/sr0 && ! -e /dev/sr0 ]]; then
  echo "No /dev/sr0. Run: scripts/fix-optical-drive.sh" >&2
  exit 1
fi

mapfile -t EPISODE_NAMES < <(grep -v '^[[:space:]]*$' "$TITLES_FILE" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
(( ${#EPISODE_NAMES[@]} > 0 )) || { echo "Titles file is empty: $TITLES_FILE" >&2; exit 1; }

sanitize() {
  local s=$1
  s=${s//\?/}
  s=${s//\//-}
  s=${s//:/ -}
  s=${s//\"/}
  s=${s//\\/}
  s=${s//\*/}
  s=${s//\</}
  s=${s//\>/}
  s=${s//|/-}
  printf '%s' "$s" | sed 's/[[:space:]]\+/ /g;s/^[[:space:]]*//;s/[[:space:]]*$//'
}

format_ep() {
  printf '%dx%02d' "$SEASON" "$1"
}

mkdir -p "$OUTPUT_DIR"
STAGING=$(videosripper_staging_dir)
trap 'rm -rf "$STAGING"' EXIT

echo "Scanning $DEVICE (min ${MINLENGTH}s)..."
INFO=$(makemkvcon info -r --minlength="$MINLENGTH" "$DEVICE" 2>&1) || true

if ! grep -q '^CINFO:2,0,' <<<"$INFO"; then
  echo "$INFO"
  echo "Failed to read disc. Is it inserted?" >&2
  exit 1
fi

disc_name=$(grep '^CINFO:2,0,' <<<"$INFO" | head -1 | sed 's/^CINFO:2,0,"\(.*\)"$/\1/')
echo "Disc: ${disc_name:-unknown}"

declare -a TITLE_IDX=()
declare -a TITLE_DUR=()
while IFS= read -r line; do
  [[ "$line" =~ ^TINFO:([0-9]+),9,0,([0-9.]+) ]] || continue
  idx="${BASH_REMATCH[1]}"
  dur="${BASH_REMATCH[2]%%.*}"
  TITLE_IDX+=("$idx")
  TITLE_DUR+=("$dur")
done <<< "$INFO"

count=${#TITLE_IDX[@]}
need=${#EPISODE_NAMES[@]}
echo "Found $count title(s) >= ${MINLENGTH}s; titles file has $need episode name(s)."

if (( count < need )); then
  echo "Warning: fewer rip candidates than episode names. Will rip $count title(s)." >&2
fi

rip_n=$(( count < need ? count : need ))
ep=$START_EP

for (( i=0; i<rip_n; i++ )); do
  idx=${TITLE_IDX[$i]}
  dur=${TITLE_DUR[$i]}
  name=$(sanitize "${EPISODE_NAMES[$i]}")
  ep_tag=$(format_ep "$ep")
  final="${OUTPUT_DIR}/${SHOW} - ${ep_tag} - ${name}.mkv"
  tmp="${STAGING}/title_${idx}.mkv"

  echo
  echo "[$((i+1))/$rip_n] Title index $idx (${dur}s) -> $(basename "$final")"

  if (( DRY_RUN )); then
    echo "  DRY RUN: makemkvcon mkv -r --minlength=$MINLENGTH $DEVICE $idx $STAGING"
    ep=$((ep+1))
    continue
  fi

  rm -f "$tmp"
  if ! makemkvcon mkv -r --minlength="$MINLENGTH" "$DEVICE" "$idx" "$STAGING" 2>&1; then
    echo "Rip failed for title $idx" >&2
    exit 1
  fi

  ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' -newer "$STAGING" 2>/dev/null | head -1)
  [[ -z "$ripped" ]] && ripped=$(find "$STAGING" -maxdepth 1 -name '*.mkv' | head -1)
  [[ -f "$ripped" ]] || { echo "No MKV produced for title $idx" >&2; exit 1; }

  actual_dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$ripped" 2>/dev/null | awk '{printf "%.0f", $1}')
  echo "  Ripped ${actual_dur}s"
  mv -f "$ripped" "$final"
  echo "  -> $final"
  copy_episode_to_share "$final" "Season ${SEASON}" || true
  ep=$((ep+1))
done

echo
echo "Done. ${rip_n} file(s) in $OUTPUT_DIR"
eject_disc /dev/sr0
