# Agent guide

Ripknife detects the disc, maps it, writes the manifest, rips it, and copies it to the library. Episode files, multi-disc joins, and edition names are part of that path. An agent does not identify the title, pick tracks, split episodes, or stitch discs by hand.

Do not start a rip, open a drive, or image a disc while changing the repo. One drive never runs two `makemkvcon mkv` processes.

## Topic index

| File | Topic |
|------|--------|
| [pipeline.md](pipeline.md) | Detect, map, manifest, rip, copy, episodes, multi-disc join, editions |
| [library.md](library.md) | Animation series, animated movies, live-action TV, live-action movies |
| [drives.md](drives.md) | Slot drives, eject, empty slot |
| [llm.md](llm.md) | OpenRouter calls, model env vars, vision, missing key |
| [recovery.md](recovery.md) | Damaged-disc ladder, verify, when a copy is allowed |

Unit install stays in [scripts/systemd/README.md](../scripts/systemd/README.md).

## While a rip is running

Leave the drive alone. Do not start a second rip on it, run `recover`, or image the disc.

Do not `daemon-reload`, restart the user units, or run `make watch-restart` until the rip has finished. `daemon-reload` applies unit changes to the next start only.

`scripts/install.sh` installs the latest GitHub release under `~/.local/share/ripknife` and starts the user units. `make dev` is the cloned checkout: it installs the dev extras if needed and runs the watchers there. Do not run both. Stop the ad-hoc watchers before enabling the units.

## Config

Personal manifests are not in this repo. Search order:

1. `RIPKNIFE_CONFIG_DIR` when it is set
2. A sibling directory named `ripknife-configs`, when that directory exists
3. `configs/` in this repo (examples and `defaults.yaml` only)

Put real manifests, `youtube.yaml`, `registry.yaml`, `fingerprints.yaml`, and `site.env` in that personal directory. One manifest per disc. `configs/defaults.yaml` is documentation. It is not loaded or merged at runtime.

## Generated configs

Generated manifests are gitignored in this repo. The rip reads `staging/ready`. `configs/` keeps examples, `defaults.yaml`, `youtube.yaml`, and `registry.yaml` only.

When a manifest is generated, Python copies it to `generated/` in the sibling `ripknife-configs` checkout (or `RIPKNIFE_CONFIG_ARCHIVE`) for reference. That checkout is an archive. It is not a second copy committed here, and the watcher does not push it. Set `RIPKNIFE_CONFIG_ARCHIVE` empty to skip the copy.

`site.env` sets `RIPKNIFE_NAS`, `RIPKNIFE_ANIME_NAS`, `RIPKNIFE_SHOWS_NAS`, and `RIPKNIFE_MOVIES_NAS`.

## Working storage

The checkout holds working files. The library is a separate tree.

| Path | Role |
|------|------|
| `output/` | Finished MKVs |
| `staging/` | Pending, ready, done, and failed jobs |
| `.tmp/` | Process temp (`TMPDIR`, `RIPKNIFE_TMP`). Not `/tmp` |
| `logs/` | Ad-hoc watcher logs (`make watch-all`) |
| `/var/log/ripknife` | systemd file logs |

`RIPKNIFE_ROOT` defaults to the directory that contains the `ripknife` package. Do not rip into a home directory.

## Do not commit

- `.env`, `.env.*`, credentials, API keys
- `.venv/`
- `output/`, `logs/`, `.tmp/`, ripped `*.mkv`
- `staging/` job files (pending, ready, done, failed)
- The personal config checkout (`ripknife-configs` or `RIPKNIFE_CONFIG_DIR`)

## Layout

| Path | Role |
|------|------|
| `ripknife/` | Python package |
| `configs/` | Tracked examples and `defaults.yaml` |
| `scripts/ripknife` | CLI launcher (`.venv/bin/python -m ripknife.cli`) |
| `scripts/systemd/` | User unit templates (`@RIPKNIFE_ROOT@`) |

`make install-service` rewrites `@RIPKNIFE_ROOT@` to the checkout path. Do not copy the templates in unchanged. Makefile and the units set `RIPKNIFE_DELETE_LOCAL_AFTER_NAS=1`.

## Commands

```bash
make install
make check-drive
make install-service
make watch-status
make uninstall-service
```

| Target | Purpose |
|--------|---------|
| `make install` / `make build` | `.venv` and `pip install -e ".[dev]"` |
| `make lint` / `make test` | ruff / pytest |
| `make run ARGS="..."` | CLI passthrough |
| `make check-drive` | Optical drive and LibreDrive |
| `scripts/install.sh` | Latest release package. Not a clone |
| `make package` | `dist/ripknife-<version>.tar.gz` from `HEAD` |
| `make dev` | Cloned checkout: install if needed, then both watchers. Logs in `logs/` |
| `make watch-all` | Same watchers as `make dev`, without the developer name |
| `make watch-stop` / `make watch-restart` | Stop or restart ad-hoc watchers, or `systemctl --user` when units are active |
| `make watch-status` | Running or not, heartbeat, disc present |
| `make install-service` | User units at login. Logs in `/var/log/ripknife` |
| `make uninstall-service` | Stop and disable those units |
| `make clean` | `__pycache__`, pytest temp |

## Model instructions

Script-sent prompts are skills under `skills/`. `ripknife/map_agent.py` sets `WORKFLOW` to `skills/map/SKILL.md` and raises `FileNotFoundError` when that file is missing. `build_map_prompt` reads it and appends the disc context. `ripknife/llm.py` loads `skills/failure`, `skills/gap`, and `skills/verify` the same way. An ambiguous map is JSON `action` `ambiguous`, not a `MAP_AMBIGUOUS:` line.

How an agent works in this checkout is `skills/ripknife/SKILL.md`. `pyproject.toml` sets `readme` to `README.md`.

## Do not

- Add a bash rip script
- Copy a bad, short, or gapped file to the library
- Copy per-disc masters when a movie is joined. Copy the joined file
- Call `recover` from the watchers, or loop a model on a failed rip
- Call `youtube` from the disc watcher, the rip watcher, or `make watch-*`
- Guess a title index without a scan of the inserted disc
- Guess a generic-label movie from runtime alone when two films share that runtime
- Pre-scan a damaged disc before the one image
- Fill missing sectors from anything other than another physical disc of the same pressing that you own
- Tell the operator to close a tray. See [drives.md](drives.md)
