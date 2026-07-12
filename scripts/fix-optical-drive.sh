#!/usr/bin/env bash
# Recover Pioneer / USB optical drives when lsusb sees them but /dev/sr0 is missing.
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

modprobe sg
modprobe sr_mod
modprobe usb_storage

for iface in /sys/bus/usb/devices/*:*; do
  [[ -f "$iface/bInterfaceClass" ]] || continue
  class=$(<"$iface/bInterfaceClass")
  [[ "$class" == "08" ]] || continue
  name=$(basename "$iface")
  if [[ ! -e "/sys/bus/usb/drivers/usb-storage/$name" ]]; then
    echo "Binding usb-storage to $name"
    echo "$name" > /sys/bus/usb/drivers/usb-storage/bind 2>/dev/null || true
  fi
done

for host in /sys/class/scsi_host/host*/scan; do
  echo "- - -" > "$host" 2>/dev/null || true
done

sleep 2
echo
echo "Block devices:"
ls -la /dev/sr* /dev/sg* 2>/dev/null || echo "  still no /dev/sr* — try unplug/replug the drive, then run again"
echo
if [[ -e /dev/sr0 ]]; then
  echo "Drive OK. Test with: makemkvcon info -r --minlength=300 dev:/dev/sr0"
fi
