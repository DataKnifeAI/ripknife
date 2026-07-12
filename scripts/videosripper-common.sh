#!/usr/bin/env bash
# Shared helpers for VideosRipper scripts
VIDEOSRIPPER_ROOT="${VIDEOSRIPPER_ROOT:-/mnt/game1/VideosRipper}"
# SpongeBob episodes (Anime share); override with VIDEOSRIPPER_NAS
NAS_SHARE="${VIDEOSRIPPER_NAS:-/mnt/tn-sas/Anime/Spongebob Squarepants}"
# TV shows (Shows share); override with VIDEOSRIPPER_SHOWS_NAS
SHOWS_NAS="${VIDEOSRIPPER_SHOWS_NAS:-/mnt/tn-sas/Shows}"
# Feature films; override with VIDEOSRIPPER_MOVIES_NAS
MOVIES_NAS="${VIDEOSRIPPER_MOVIES_NAS:-/mnt/tn-sas/Movies}"
OUTPUT_ROOT="${VIDEOSRIPPER_OUTPUT:-${VIDEOSRIPPER_ROOT}/output}"
STAGING_ROOT="${VIDEOSRIPPER_STAGING:-${VIDEOSRIPPER_ROOT}/staging}"
LOG_ROOT="${VIDEOSRIPPER_LOGS:-${VIDEOSRIPPER_ROOT}/logs}"
SCRIPTS_ROOT="${VIDEOSRIPPER_SCRIPTS:-${VIDEOSRIPPER_ROOT}/scripts}"

videosripper_staging_dir() {
  mkdir -p "$STAGING_ROOT"
  mktemp -d "${STAGING_ROOT}/.rip.XXXXXX"
}

copy_episode_to_share() {
  local src=$1
  local season_dir=$2
  local dest="${NAS_SHARE}/${season_dir}/$(basename "$src")"
  mkdir -p "${NAS_SHARE}/${season_dir}" 2>/dev/null || true
  echo "  -> copy to share: $dest"
  if ! rsync -ah --info=progress2 "$src" "$dest"; then
    echo "  WARNING: copy failed (check NAS permissions for ${NAS_SHARE}/${season_dir})" >&2
    return 1
  fi
}

copy_movie_to_share() {
  local src=$1
  local subdir=${2:-}
  local dest="${MOVIES_NAS}"
  [[ -n "$subdir" ]] && dest="${MOVIES_NAS}/${subdir}"
  dest="${dest}/$(basename "$src")"
  mkdir -p "$(dirname "$dest")" 2>/dev/null || true
  echo "  -> copy to Movies: $dest"
  if ! rsync -ah --info=progress2 "$src" "$dest"; then
    echo "  WARNING: copy failed (check NAS permissions for ${MOVIES_NAS})" >&2
    return 1
  fi
}

eject_disc() {
  local dev=${1:-/dev/sr0}
  echo "Ejecting $dev..."
  if command -v udisksctl >/dev/null; then
    udisksctl eject -b "$dev" 2>/dev/null || udisksctl unmount -b "$dev" 2>/dev/null || true
  fi
  eject "$dev" 2>/dev/null || echo "  (eject failed — run manually: eject $dev)" >&2
}
