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

echo "Fetching the latest ${REPO} release"
if ! curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" -o "${tmp}/release.json"; then
  echo "No GitHub release found for ${REPO}" >&2
  exit 1
fi

python3 - "${tmp}/release.json" "${tmp}/asset.txt" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
for asset in data.get("assets") or []:
    name = asset.get("name") or ""
    if name.startswith("ripknife-") and name.endswith(".tar.gz"):
        open(sys.argv[2], "w", encoding="utf-8").write(
            asset["browser_download_url"] + "\n" + name + "\n"
        )
        raise SystemExit(0)
raise SystemExit("Latest release has no ripknife-*.tar.gz package")
PY

url="$(sed -n '1p' "${tmp}/asset.txt")"
name="$(sed -n '2p' "${tmp}/asset.txt")"
echo "Downloading ${name}"
curl -fsSL -L "${url}" -o "${tmp}/${name}"

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
