#!/usr/bin/env bash
# Install the latest Ripknife release and start the user units.
# A git clone uses `make dev` instead. This script does not clone the repo.
set -euo pipefail

PREFIX="${RIPKNIFE_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/ripknife}"
REPO="${RIPKNIFE_REPO:-DataKnifeAI/ripknife}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing $1" >&2
    exit 1
  }
}

need curl
need tar
need python3

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

name="ripknife.tar.gz"
url="https://github.com/${REPO}/releases/latest/download/${name}"
echo "Downloading the latest ${REPO} release"
if ! curl -fsSL -L "${url}" -o "${tmp}/${name}"; then
  echo "No release package at ${url}" >&2
  exit 1
fi

python3 - "${tmp}/${name}" "${tmp}/top.txt" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    first = archive.getmembers()[0].name
open(sys.argv[2], "w", encoding="utf-8").write(first.split("/", 1)[0] + "\n")
PY
top="$(sed -n '1p' "${tmp}/top.txt")"
tar -xzf "${tmp}/${name}" -C "${tmp}"
src="${tmp}/${top}"

mkdir -p "$(dirname "${PREFIX}")"
if [[ -d "${PREFIX}" ]]; then
  mkdir -p "${tmp}/keep"
  for keep in .env output staging logs .tmp; do
    if [[ -e "${PREFIX}/${keep}" ]]; then
      mv "${PREFIX}/${keep}" "${tmp}/keep/${keep}"
    fi
  done
  rm -rf "${PREFIX}"
fi
mv "${src}" "${PREFIX}"
if [[ -d "${tmp}/keep" ]]; then
  shopt -s dotglob
  mv "${tmp}/keep/"* "${PREFIX}/" 2>/dev/null || true
  shopt -u dotglob
fi

python3 -m venv "${PREFIX}/.venv"
"${PREFIX}/.venv/bin/pip" install -e "${PREFIX}"
if [[ ! -f "${PREFIX}/.env" ]]; then
  cp "${PREFIX}/.env.example" "${PREFIX}/.env"
  chmod 600 "${PREFIX}/.env"
fi

if [[ "${RIPKNIFE_SKIP_SERVICE:-}" == "1" ]]; then
  echo "Skipped the service install."
else
  RIPKNIFE_ROOT="${PREFIX}" "${PREFIX}/scripts/systemd/install-user-units.sh"
fi

echo "Installed ${name} to ${PREFIX}"
echo "Put an OpenRouter key in ${PREFIX}/.env"
