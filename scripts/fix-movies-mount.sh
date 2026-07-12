#!/usr/bin/env bash
# Remount /mnt/tn-sas/Movies with async + nconnect for faster bulk copies.
# For all NAS shares, use: sudo bash scripts/fix-nas-mounts.sh
set -euo pipefail

FSTAB=/etc/fstab
MARKER='nas.dataknife.net:/mnt/SAS/Movies'
OLD_OPTS='rw,relatime,vers=4.2,sync,_netdev,soft,nofail,x-systemd.automount,x-systemd.idle-timeout=600'
NEW_OPTS='rw,relatime,vers=4.2,async,nconnect=4,rsize=1048576,wsize=1048576,_netdev,soft,nofail,x-systemd.automount,x-systemd.idle-timeout=600'
MOUNT=/mnt/tn-sas/Movies
SRC="/mnt/game1/VideosRipper/output/Movies/Ghost Rider (2007).mkv"
DEST="$MOUNT/.mount-speedtest-$$.mkv.partial"
LAZY_UMOUNT=0

usage() {
  cat <<EOF
Usage: sudo bash $0 [--lazy]

Remount $MOUNT with async,nconnect=4 from fstab.

  --lazy   If normal umount fails, use umount -l (lazy) after warning.
           Prefer stopping blockers first; lazy umount breaks open file handles.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lazy) LAZY_UMOUNT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0 [--lazy]" >&2; exit 1; }

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
    # +D is slow on large trees; enough to identify blockers at mount root.
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
  local opts=$1
  mount | grep -F "$MOUNT" | grep -q "$opts"
}

verify_mount_opts() {
  echo "Active mount options:"
  mount | grep -F "$MOUNT" || true
  echo
  nfsstat -m 2>/dev/null | sed -n '/Movies/,/^$/p' || true
  echo
  if mount_has_target_opts 'async' && mount_has_target_opts 'nconnect=4'; then
    echo "OK: mount has async and nconnect=4"
    return 0
  fi
  echo "WARN: mount missing async and/or nconnect=4 (fstab updated but remount may have failed)" >&2
  return 1
}

try_umount() {
  local attempt lazy=$1
  if ! mountpoint -q "$MOUNT"; then
    echo "$MOUNT is not mounted"
    return 0
  fi
  if [[ "$lazy" -eq 1 ]]; then
    echo "WARN: lazy umount (umount -l) — open handles will fail; in-flight writes may be lost." >&2
    echo "      Stop blockers above when possible; use --lazy only if you accept broken readers." >&2
    umount -l "$MOUNT"
    return 0
  fi
  umount "$MOUNT"
}

if ! grep -q "$MARKER" "$FSTAB"; then
  echo "Movies fstab entry not found" >&2
  exit 1
fi

if grep -q 'SAS/Movies.*async,nconnect=4' "$FSTAB"; then
  echo "fstab already updated"
else
  cp -a "$FSTAB" "${FSTAB}.bak.$(date +%Y%m%d-%H%M%S)"
  sed -i "s|${MARKER}        /mnt/tn-sas/Movies      nfs4    ${OLD_OPTS}|${MARKER}        /mnt/tn-sas/Movies      nfs4    ${NEW_OPTS}|" "$FSTAB"
  echo "Updated $FSTAB"
  grep Movies "$FSTAB"
fi

systemctl daemon-reload

if mountpoint -q "$MOUNT"; then
  if mount_has_target_opts 'async' && mount_has_target_opts 'nconnect=4'; then
    echo "Mount already has async,nconnect=4; no remount needed."
    verify_mount_opts
    exit 0
  fi

  echo "Remount required (active mount lacks async,nconnect=4)."
  show_mount_blockers "$MOUNT"

  if ! try_umount 0; then
    echo
    echo "umount failed: device is busy."
    echo "Common blockers: sha256sum/cp on NAS files, shells with cwd under $MOUNT, file managers, Plex."
    echo "Fix: stop or wait for those jobs, then re-run this script."
    echo "     Example: kill <pid>   or   cd ~ && re-run"
    if [[ "$LAZY_UMOUNT" -eq 1 ]]; then
      try_umount 1
    else
      echo
      echo "To force lazy umount anyway: sudo bash $0 --lazy"
      exit 1
    fi
  fi
fi

AUTOMOUNT_UNIT="$(systemd-escape -p --suffix=automount "$MOUNT")"
systemctl restart "$AUTOMOUNT_UNIT" 2>/dev/null || true

for attempt in 1 2 3; do
  if mount "$MOUNT" 2>/dev/null; then
    break
  fi
  echo "mount attempt $attempt failed; retrying in 2s..." >&2
  sleep 2
done

if ! mountpoint -q "$MOUNT"; then
  echo "Failed to mount $MOUNT after umount" >&2
  exit 1
fi

echo
verify_mount_opts || exit 1

echo
echo "=== Write test: 1 GiB dd ==="
dd if=/dev/zero of="$MOUNT/.dd-speedtest-$$" bs=1M count=1024 conv=fsync 2>&1 | tail -1
rm -f "$MOUNT/.dd-speedtest-$$"

echo
echo "=== Copy test: 2 GiB from local Ghost Rider (cp) ==="
if [[ -f "$SRC" ]]; then
  START=$(date +%s)
  dd if="$SRC" of="$DEST" bs=1M count=2048 conv=fsync status=progress 2>&1 | tail -3
  END=$(date +%s)
  BYTES=$(stat -c%s "$DEST")
  ELAPSED=$((END - START))
  MBPS=$(awk -v b="$BYTES" -v e="$ELAPSED" 'BEGIN{if(e>0) printf "%.1f", (b/e)/1048576; else print "0"}')
  echo "elapsed: ${ELAPSED}s  avg: ${MBPS} MB/s"
  rm -f "$DEST"
else
  echo "Skip file copy test; source missing: $SRC"
fi

echo
echo "Done."
