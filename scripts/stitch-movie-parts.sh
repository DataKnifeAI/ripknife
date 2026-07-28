#!/usr/bin/env bash
# Lossless join of multi-disc / multi-part movie remuxes.
# Prefers mkvmerge append; falls back to ffmpeg concat demuxer (-c copy).
# See LLM-RIP-WORKFLOW.md → "Multi-disc / multi-part movies".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=videosripper-common.sh
source "${SCRIPT_DIR}/videosripper-common.sh"
mkdir -p "$TMP_ROOT"
export TMPDIR="$TMP_ROOT"

usage() {
  cat <<'EOF'
Usage: stitch-movie-parts.sh [options] -o OUTPUT part1.mkv part2.mkv [part3.mkv ...]

  Losslessly append movie parts into one MKV (mkvmerge preferred, else ffmpeg).

Options:
  -o, --output PATH   Output MKV (required)
  -t, --tolerance PCT Duration check tolerance percent (default: 2)
  -n, --dry-run       Print planned command only
  -h, --help          Show this help

Examples:
  scripts/stitch-movie-parts.sh -o "output/Movies/Title (Year).mkv" \
    "output/Movies/Title (Year) - Disc 1.mkv" \
    "output/Movies/Title (Year) - Disc 2.mkv"
EOF
}

OUTPUT=""
TOLERANCE_PCT=2
DRY_RUN=0
PARTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)
      OUTPUT="${2:-}"
      shift 2
      ;;
    -t|--tolerance)
      TOLERANCE_PCT="${2:-}"
      shift 2
      ;;
    -n|--dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      PARTS+=("$@")
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      PARTS+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$OUTPUT" ]]; then
  echo "error: -o/--output is required" >&2
  usage >&2
  exit 2
fi
if [[ ${#PARTS[@]} -lt 2 ]]; then
  echo "error: need at least two input parts" >&2
  usage >&2
  exit 2
fi

for p in "${PARTS[@]}"; do
  if [[ ! -f "$p" ]]; then
    echo "error: missing input: $p" >&2
    exit 1
  fi
done

duration_sec() {
  local f="$1"
  local d
  d="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$f" 2>/dev/null || true)"
  if [[ -z "$d" || "$d" == "N/A" ]]; then
    echo "error: could not read duration: $f" >&2
    return 1
  fi
  # Strip scientific notation edge cases; awk handles float.
  awk -v x="$d" 'BEGIN { printf "%.6f", x+0 }'
}

sum_inputs=0
echo "Inputs:"
for p in "${PARTS[@]}"; do
  d="$(duration_sec "$p")"
  sum_inputs="$(awk -v a="$sum_inputs" -v b="$d" 'BEGIN { printf "%.6f", a+b }')"
  printf '  %s  (%.3fs)\n' "$p" "$d"
done
printf 'Sum of part durations: %.3fs\n' "$sum_inputs"

TOOL=""
if command -v mkvmerge >/dev/null 2>&1; then
  TOOL=mkvmerge
elif command -v ffmpeg >/dev/null 2>&1; then
  TOOL=ffmpeg
else
  echo "error: neither mkvmerge nor ffmpeg found in PATH" >&2
  echo "  install: sudo pacman -S mkvtoolnix-cli   # preferred" >&2
  echo "  (ffmpeg is usually already present on this host)" >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Dry-run: would use $TOOL → $OUTPUT"
  if [[ "$TOOL" == mkvmerge ]]; then
    echo -n "  mkvmerge -o $(printf '%q' "$OUTPUT")"
    echo -n " $(printf '%q' "${PARTS[0]}")"
    for ((i = 1; i < ${#PARTS[@]}; i++)); do
      echo -n " + $(printf '%q' "${PARTS[$i]}")"
    done
    echo
  else
    echo "  ffmpeg -f concat -safe 0 -i <list> -c copy $(printf '%q' "$OUTPUT")"
  fi
  exit 0
fi

mkdir -p "$(dirname "$OUTPUT")"
if [[ -e "$OUTPUT" ]]; then
  echo "error: refusing to overwrite existing file: $OUTPUT" >&2
  exit 1
fi

echo "Stitching with $TOOL → $OUTPUT"
if [[ "$TOOL" == mkvmerge ]]; then
  args=(-o "$OUTPUT" "${PARTS[0]}")
  for ((i = 1; i < ${#PARTS[@]}; i++)); do
    args+=(+ "${PARTS[$i]}")
  done
  mkvmerge "${args[@]}"
else
  list="$(mktemp "${TMP_ROOT}/stitch-XXXXXX.txt")"
  trap 'rm -f "$list"' EXIT
  for p in "${PARTS[@]}"; do
    # concat demuxer: escape single quotes in paths
    esc="${p//\'/\'\\\'\'}"
    printf "file '%s'\n" "$esc" >>"$list"
  done
  ffmpeg -hide_banner -loglevel error -f concat -safe 0 -i "$list" -c copy "$OUTPUT"
fi

out_dur="$(duration_sec "$OUTPUT")"
printf 'Output duration: %.3fs\n' "$out_dur"

awk -v out="$out_dur" -v sum="$sum_inputs" -v tol="$TOLERANCE_PCT" 'BEGIN {
  if (sum <= 0) { print "error: invalid input duration sum" > "/dev/stderr"; exit 1 }
  diff = (out > sum) ? out - sum : sum - out
  pct = 100.0 * diff / sum
  if (pct > tol + 0) {
    printf "error: duration mismatch: out=%.3fs sum=%.3fs (%.3f%% > %.3f%% tolerance)\n", out, sum, pct, tol > "/dev/stderr"
    exit 1
  }
  printf "Duration check OK (Δ=%.3fs, %.3f%% ≤ %.3f%%)\n", diff, pct, tol
}'

echo "Done: $OUTPUT"
