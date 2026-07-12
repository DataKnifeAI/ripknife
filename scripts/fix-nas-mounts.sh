#!/usr/bin/env bash
# Update all nas.dataknife.net NFS4 fstab entries to async + nconnect, then remount.
# nconnect=4 applies to the whole NFS client session — first automount wins; ALL lines must match.
set -euo pipefail

FSTAB=/etc/fstab
NAS_HOST=nas.dataknife.net
MARKER="${NAS_HOST}:"
OLD_OPTS='rw,relatime,vers=4.2,sync,_netdev,soft,nofail,x-systemd.automount,x-systemd.idle-timeout=600'
NEW_OPTS='rw,relatime,vers=4.2,async,nconnect=4,rsize=1048576,wsize=1048576,_netdev,soft,nofail,x-systemd.automount,x-systemd.idle-timeout=600'
LAZY_UMOUNT=0
STOP_VERIFY=0
FSTAB_ONLY=0
REMOUNT_ONLY=0

MOUNTS=(
  /mnt/tn-cold/Tools
  /mnt/tn-cold/Backup
  /mnt/tn-sas/Anime
  /mnt/tn-sas/FAV
  /mnt/tn-sas/Movies
  /mnt/tn-sas/Shows
  /mnt/tn-sas/YouTube
  /mnt/tn-sas/Photos
  /mnt/tn-sas/Timelapse
)

usage() {
  cat <<EOF
Usage: sudo bash $0 [--fstab-only] [--remount-only] [--lazy] [--stop-verify]

Update all 9 ${NAS_HOST} NFS4 lines in $FSTAB to:
  async,nconnect=4,rsize=1048576,wsize=1048576
(replaces sync where present), then remount all shares.

  --fstab-only    Update fstab only; do not unmount/remount (reboot to apply).
  --remount-only  Skip fstab update; unmount and remount only (re-run after partial failure).
  --lazy          If normal umount fails, use umount -l after warning.
  --stop-verify   Kill sha256sum on Movies; skip post-remount cp speed test.

Remount strategy: unmount ALL NAS shares, then mount ALL (or reboot after --fstab-only).
If fstab is already updated, a normal re-run skips the sed step and continues to remount.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fstab-only) FSTAB_ONLY=1; shift ;;
    --remount-only) REMOUNT_ONLY=1; shift ;;
    --lazy) LAZY_UMOUNT=1; shift ;;
    --stop-verify) STOP_VERIFY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ "$FSTAB_ONLY" -eq 1 && "$REMOUNT_ONLY" -eq 1 ]]; then
  echo "Cannot combine --fstab-only and --remount-only" >&2
  exit 1
fi

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0 [options]" >&2; exit 1; }

stop_sha256_on_movies() {
  local movies=/mnt/tn-sas/Movies
  local pids
  pids=$(pgrep -f "sha256sum.*${movies}" 2>/dev/null || true)
  if [[ -z "$pids" ]]; then
    pids=$(pgrep -f "sha256sum" 2>/dev/null | while read -r pid; do
      if ls -l "/proc/$pid/fd" 2>/dev/null | grep -qF "$movies"; then
        echo "$pid"
      fi
    done || true)
  fi
  if [[ -n "$pids" ]]; then
    echo "Stopping sha256sum on Movies (blocks umount): $pids"
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}

show_mount_blockers() {
  local target=$1
  echo "=== Processes using $target ==="
  if command -v fuser >/dev/null 2>&1; then
    fuser -vm "$target" 2>&1 || true
  else
    echo "(fuser not installed)"
  fi
  echo
  echo "=== Open files under $target (lsof +D) ==="
  if command -v lsof >/dev/null 2>&1; then
    lsof +D "$target" 2>&1 | head -40 || true
  else
    echo "(lsof not installed)"
  fi
  echo
  echo "=== Mount tree (findmnt) ==="
  findmnt -T "$target" 2>/dev/null || findmnt "$target" 2>/dev/null || true
  echo
}

mount_has_target_opts() {
  local mount=$1
  local opts=$2
  mount | grep -F "$mount" | grep -q "$opts"
}

verify_mount_opts() {
  local mount=$1
  echo "Active mount options for $mount:"
  mount | grep -F "$mount" || true
  echo
  if mount_has_target_opts "$mount" 'nconnect=4'; then
    echo "OK: $mount has nconnect=4"
    return 0
  fi
  echo "WARN: $mount missing nconnect=4" >&2
  return 1
}

verify_nconnect_ss() {
  echo "=== NFS TCP connections to ${NAS_HOST}:2049 (ss) ==="
  if ! command -v ss >/dev/null 2>&1; then
    echo "(ss not installed; skip nconnect check)"
    return 0
  fi
  ss -tn state established 2>/dev/null | grep -F "${NAS_HOST}:2049" || true
  local count
  count=$(ss -tn state established 2>/dev/null | grep -cF "${NAS_HOST}:2049" || true)
  echo "Established connections: ${count}"
  if [[ "$count" -ge 2 ]]; then
    echo "OK: multiple connections — nconnect likely active"
    return 0
  fi
  if [[ "$count" -eq 1 ]]; then
    echo "WARN: only 1 connection — remount all shares or reboot so nconnect=4 applies" >&2
    return 1
  fi
  echo "WARN: no connections (shares may be idle/unmounted)" >&2
  return 0
}

try_umount() {
  local mount=$1
  local lazy=$2
  if ! mountpoint -q "$mount"; then
    echo "$mount is not mounted"
    return 0
  fi
  if [[ "$lazy" -eq 1 ]]; then
    echo "WARN: lazy umount (umount -l) on $mount — open handles will fail." >&2
    umount -l "$mount"
    return 0
  fi
  umount "$mount"
}

umount_all_shares() {
  local mount failed=0
  echo "=== Phase 1: unmount all NAS shares ==="
  # Reverse order: SAS before Cold (arbitrary; all must drop for nconnect reset).
  for (( i=${#MOUNTS[@]}-1; i>=0; i-- )); do
    mount="${MOUNTS[$i]}"
    echo "--- umount $mount ---"
    if ! mountpoint -q "$mount"; then
      echo "$mount is not mounted"
      continue
    fi
    if mount_has_target_opts "$mount" 'nconnect=4' && [[ "$FSTAB_ONLY" -eq 0 ]]; then
      : # still need umount to pick up fstab after partial update
    fi
    if ! try_umount "$mount" 0; then
      echo
      echo "umount failed for $mount: device is busy."
      show_mount_blockers "$mount"
      echo "Common blockers: sha256sum/cp/rsync on NAS files, shells with cwd under mount, Plex."
      if [[ "$LAZY_UMOUNT" -eq 1 ]]; then
        try_umount "$mount" 1
      else
        echo "Stop blockers and re-run, or: sudo bash $0 --lazy [--stop-verify]"
        failed=1
      fi
    fi
  done
  return "$failed"
}

mount_all_shares() {
  local mount automount_unit attempt failed=0
  echo "=== Phase 2: mount all NAS shares ==="
  systemctl daemon-reload
  for mount in "${MOUNTS[@]}"; do
    echo "--- mount $mount ---"
    automount_unit="$(systemd-escape -p --suffix=automount "$mount")"
    systemctl restart "$automount_unit" 2>/dev/null || true
    for attempt in 1 2 3; do
      if mount "$mount" 2>/dev/null; then
        break
      fi
      echo "mount attempt $attempt failed for $mount; retrying in 2s..." >&2
      sleep 2
    done
    if ! mountpoint -q "$mount"; then
      echo "Failed to mount $mount" >&2
      failed=1
      continue
    fi
    verify_mount_opts "$mount" || failed=1
    echo
  done
  return "$failed"
}

update_fstab() {
  if ! grep -q "$MARKER" "$FSTAB"; then
    echo "No ${NAS_HOST} entries in $FSTAB" >&2
    exit 1
  fi

  local nas_lines count_sync count_nconnect
  nas_lines=$(grep -c "$MARKER" "$FSTAB" || true)
  count_sync=$(grep "$MARKER" "$FSTAB" | grep -c 'vers=4.2,sync,' || true)
  count_nconnect=$(grep "$MARKER" "$FSTAB" | grep -c 'nconnect=4' || true)

  if [[ "$count_sync" -gt 0 ]]; then
    cp -a "$FSTAB" "${FSTAB}.bak.$(date +%Y%m%d-%H%M%S)"
    sed -i "s|${OLD_OPTS}|${NEW_OPTS}|g" "$FSTAB"
    echo "Updated $FSTAB: replaced sync with async,nconnect=4,rsize/wsize=1M on ${count_sync} line(s)"
  elif [[ "$count_nconnect" -eq "$nas_lines" ]]; then
    echo "fstab: all ${nas_lines} NAS lines already have nconnect=4 — continuing to remount"
  else
    echo "WARN: fstab has mixed NAS options (${count_nconnect}/${nas_lines} with nconnect=4)" >&2
    echo "      Edit manually or restore from ${FSTAB}.bak.* and re-run." >&2
    grep "$MARKER" "$FSTAB"
    exit 1
  fi

  echo
  grep "$MARKER" "$FSTAB"
  echo
  if [[ "$count_nconnect" -ne "$nas_lines" ]] && [[ "$count_sync" -gt 0 ]]; then
    count_nconnect=$(grep "$MARKER" "$FSTAB" | grep -c 'nconnect=4' || true)
    if [[ "$count_nconnect" -ne "$nas_lines" ]]; then
      echo "ERROR: expected nconnect=4 on all ${nas_lines} lines, got ${count_nconnect}" >&2
      exit 1
    fi
  fi
}

[[ "$STOP_VERIFY" -eq 1 ]] && stop_sha256_on_movies

if [[ "$REMOUNT_ONLY" -eq 0 ]]; then
  update_fstab
else
  echo "Skipping fstab update (--remount-only)"
  echo
fi

if [[ "$FSTAB_ONLY" -eq 1 ]]; then
  echo
  echo "fstab updated (--fstab-only). Remount options:"
  echo "  1. Reboot (cleanest — resets NFS client and nconnect session)"
  echo "  2. sudo bash $0   (unmount all, then mount all)"
  exit 0
fi

failed=0
if ! umount_all_shares; then
  failed=1
fi

echo
if [[ "$failed" -eq 0 ]]; then
  if ! mount_all_shares; then
    failed=1
  fi
fi

echo
verify_nconnect_ss || failed=1

if [[ "$STOP_VERIFY" -eq 0 ]]; then
  echo
  echo "=== Optional speed spot-check (Movies mount) ==="
  MOVIES=/mnt/tn-sas/Movies
  SRC="/mnt/game1/VideosRipper/output/Movies/Ghost Rider (2007).mkv"
  DEST="$MOVIES/.mount-speedtest-$$.mkv.partial"
  if mountpoint -q "$MOVIES" && [[ -f "$SRC" ]]; then
    START=$(date +%s)
    cp "$SRC" "$DEST"
    END=$(date +%s)
    BYTES=$(stat -c%s "$DEST")
    ELAPSED=$((END - START))
    MBPS=$(awk -v b="$BYTES" -v e="$ELAPSED" 'BEGIN{if(e>0) printf "%.1f", (b/e)/1048576; else print "0"}')
    echo "cp test: ${ELAPSED}s avg ${MBPS} MB/s (use cp/rsync for bulk copies, not dd)"
    rm -f "$DEST"
  else
    echo "Skip cp test (Movies not mounted or source missing)"
  fi
else
  echo "Skipping cp speed test (--stop-verify)"
fi

if [[ "$failed" -ne 0 ]]; then
  echo "One or more shares failed. Fix blockers and re-run, or reboot after fstab fix." >&2
  exit 1
fi

echo "Done. All NAS shares updated and remounted."
