#!/usr/bin/env bash
# Install Ripknife systemd *user* units (start at login + linger boot).
# Prefer these over system-wide units unless you need a root-owned service.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="${RIPKNIFE_ROOT:-$SOURCE_ROOT}"
LOG_DIR="${RIPKNIFE_SYSTEMD_LOGS:-/var/log/ripknife}"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SRC_DIR="${ROOT}/scripts/systemd"
UNITS=(ripknife-disc-watcher.service ripknife-rip-watcher.service)

ad_hoc_watchers_running() {
  pgrep -f '[p]ython.*scripts/disc-watcher\.py' >/dev/null 2>&1 \
    || pgrep -f '[p]ython.*scripts/rip-watcher\.py' >/dev/null 2>&1
}

systemd_units_active() {
  systemctl --user is-active --quiet ripknife-disc-watcher.service 2>/dev/null \
    || systemctl --user is-active --quiet ripknife-rip-watcher.service 2>/dev/null
}

ensure_venv() {
  if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
    echo "Missing ${ROOT}/.venv — run: make -C ${ROOT} install" >&2
    exit 1
  fi
}

ensure_log_dir() {
  if [[ -d "${LOG_DIR}" && -w "${LOG_DIR}" ]]; then
    echo "Log dir already writable: ${LOG_DIR}"
    return
  fi
  echo "Creating ${LOG_DIR} owned by ${USER} (needs sudo; /var/log is root-owned)"
  if [[ "$(id -u)" -eq 0 ]]; then
    install -d -o "${SUDO_USER:-$USER}" -g "${SUDO_USER:-$USER}" -m 0755 "${LOG_DIR}"
  else
    sudo install -d -o "${USER}" -g "${USER}" -m 0755 "${LOG_DIR}"
  fi
  if command -v setfacl >/dev/null 2>&1; then
    sudo setfacl -m "u:${USER}:rwx" "${LOG_DIR}" 2>/dev/null || true
  fi
  echo "Log dir ready: ${LOG_DIR}"
}

enable_linger() {
  local linger
  linger="$(loginctl show-user "${USER}" --property=Linger --value 2>/dev/null || true)"
  if [[ "${linger}" == "yes" ]]; then
    echo "Linger already enabled for ${USER} (user systemd starts at boot without a GUI session)"
    return
  fi
  echo "Enabling linger for ${USER} so user units start at boot without a GUI login"
  if ! loginctl enable-linger "${USER}" 2>/dev/null; then
    sudo loginctl enable-linger "${USER}"
  fi
}

install_units() {
  mkdir -p "${USER_UNIT_DIR}"
  local unit
  for unit in "${UNITS[@]}"; do
    sed "s|@RIPKNIFE_ROOT@|${ROOT}|g" "${SRC_DIR}/${unit}" > "${USER_UNIT_DIR}/${unit}"
  done
  systemctl --user daemon-reload
  echo "Installed user units to ${USER_UNIT_DIR}"
}

ensure_venv
ensure_log_dir
install_units
enable_linger

if systemd_units_active; then
  systemctl --user enable ripknife-disc-watcher.service ripknife-rip-watcher.service
  echo "Units already running via systemd (not restarted — avoid interrupting a rip)."
  echo "New env (RIPKNIFE_LOGS=${LOG_DIR}) applies after:"
  echo "  systemctl --user restart ripknife-disc-watcher ripknife-rip-watcher"
  echo "Do not restart mid-rip."
elif ad_hoc_watchers_running; then
  systemctl --user enable ripknife-disc-watcher.service ripknife-rip-watcher.service
  echo "Ad-hoc watchers are running (make watch-all). Not starting systemd units (flock conflict)."
  echo "Units are installed and enabled for next login/boot."
  echo "To switch now:"
  echo "  make -C ${ROOT} watch-stop"
  echo "  systemctl --user start ripknife-disc-watcher ripknife-rip-watcher"
else
  systemctl --user enable --now ripknife-disc-watcher.service ripknife-rip-watcher.service
  echo "Enabled and started disc-watcher + rip-watcher (user systemd; linger on)."
fi

echo
echo "Logs: ${LOG_DIR}  (make watch-all still uses ${ROOT}/logs)"
echo "Status: systemctl --user status ripknife-disc-watcher ripknife-rip-watcher"
echo "Journal: journalctl --user -u ripknife-disc-watcher -u ripknife-rip-watcher -f"
echo "Files: tail -F ${LOG_DIR}/pipeline.log"
echo "Do not run make watch-all while these units are active."
