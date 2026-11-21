#!/usr/bin/env bash
# Stop and disable Ripknife systemd user units. Leaves linger and log dir.
set -euo pipefail

USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
LOG_DIR="${RIPKNIFE_SYSTEMD_LOGS:-/var/log/ripknife}"
UNITS=(ripknife-disc-watcher.service ripknife-rip-watcher.service)

systemctl --user disable --now ripknife-disc-watcher.service ripknife-rip-watcher.service 2>/dev/null || true

for unit in "${UNITS[@]}"; do
  rm -f "${USER_UNIT_DIR}/${unit}"
done
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed ripknife-disc-watcher.service ripknife-rip-watcher.service 2>/dev/null || true

echo "Disabled and removed user units from ${USER_UNIT_DIR}"
echo "Left linger on (other user services may need it). To turn off:"
echo "  loginctl disable-linger ${USER}"
echo "Left log files in ${LOG_DIR}"
echo "Ad-hoc watchers: make watch-all"
