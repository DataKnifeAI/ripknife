#!/usr/bin/env bash
# Legacy wrapper — map agent runs in Python (videosripper.map_agent).
set -euo pipefail
ROOT="/mnt/game1/VideosRipper"
exec "${ROOT}/.venv/bin/python" -m videosripper.map_agent "$@"
