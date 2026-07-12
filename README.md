# VideosRipper

Config-driven DVD/Blu-ray ripping with MakeMKV on `/mnt/game1/VideosRipper`. Rips to local storage, copies to NAS (Plex), and ejects the disc when done.

## Quick start

```bash
cd /mnt/game1/VideosRipper
make install          # .venv + pip install -e ".[dev]"
chmod +x scripts/videosripper

# Validate manifest
make run ARGS="validate configs/spongebob/s8-disc1.yaml"
# or: scripts/videosripper validate configs/spongebob/s8-disc1.yaml

# Scan inserted disc
scripts/videosripper scan configs/spongebob/s8-disc1.yaml

# Rip (dry-run first)
scripts/videosripper rip configs/spongebob/s8-disc1.yaml --dry-run
scripts/videosripper rip configs/spongebob/s8-disc1.yaml
```

Launcher: `scripts/videosripper` → `.venv/bin/python -m videosripper.cli`

## Make targets

| Target | Command |
|--------|---------|
| `make install` / `make build` | Create `.venv` and `pip install -e ".[dev]"` |
| `make lint` | `ruff check videosripper/ tests/` |
| `make test` | `pytest tests/` |
| `make run ARGS="..."` | `scripts/videosripper` with passthrough args |
| `make clean` | Remove `__pycache__` and `.pytest_cache` |

Examples:

```bash
make run                          # CLI help
make run ARGS="validate configs/spongebob/s8-disc1.yaml"
make run ARGS="rip configs/spongebob/s8-disc1.yaml --dry-run"
```

## Layout

| Path | Purpose |
|------|---------|
| `videosripper/` | Python package (`validate`, `scan`, `rip`) |
| `configs/` | YAML rip manifests (TV discs, movies) |
| `scripts/videosripper` | CLI launcher |
| `scripts/videosripper-common.sh` | NAS rsync, staging, eject helpers |
| `scripts/archive/` | Legacy per-disc bash scripts (reference only) |
| `scripts/examples/` | Episode title lists for YAML authoring |
| `output/` | Final MKV files |
| `staging/` | Temp rip directories |
| `logs/` | Running logs: `rip.log` (status), `debug.log` (verbose), `scan.log` (scans), `error.log` (ERROR+) |

## Manifests

- `configs/defaults.yaml` — shared defaults
- `configs/spongebob/s8-disc{1,2,3}.yaml` — SpongeBob Season 8
- `configs/movies/ghost-rider-spirit-of-vengeance.yaml` — feature film example

Every manifest should include a `post` block (defaults match `configs/defaults.yaml`):

```yaml
post:
  eject: true       # eject disc after a successful real rip only
  copy_to_nas: true # rsync finished files to NAS
```

`eject` runs on a successful `videosripper rip` (no `--dry-run`), and also after retries are exhausted on disc-open/stall failures (so you can inspect the disc). Scan, validate, and dry-run never eject. Mid-retry attempts never eject.

## Resume

```bash
VIDEOSRIPPER_START_AT=5 scripts/videosripper rip configs/spongebob/s8-disc3.yaml
# or: scripts/videosripper rip configs/...yaml --start-at 5
```

Skips episode slots whose output file already exists under `output/`.

## Rip monitoring

Real rips (`videosripper rip`, not `--dry-run`) watch makemkv for stalls and disc-open failures. Alerts are logged with a grep-friendly prefix:

```
=== ALERT === RIP STALL — no progress for 900s during title rip (limit 900s)
=== ALERT === MAKEKV FAILURE — exit 11
=== ALERT === DUPLICATE MAKEKV — another makemkvcon is using dev:/dev/sr0
=== ALERT === USER INTERVENTION REQUIRED — disc preflight failed after all retries (3 retries / 4 total attempts)
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `VIDEOSRIPPER_STALL_TIMEOUT_SEC` | `900` | Kill/warn if no PRGV or staging file growth during a title rip |
| `VIDEOSRIPPER_OPEN_TIMEOUT_SEC` | `600` | Preflight `makemkv info` timeout; also stall limit before first PRGV |
| `VIDEOSRIPPER_ON_STALL` | `exit` | `exit` (kill makemkv, non-zero) or `warn` (log only) |
| `VIDEOSRIPPER_RETRY_COUNT` | `3` | Retries after first failure (**3 retries = 4 total attempts**) before user intervention |
| `VIDEOSRIPPER_RETRY_DELAY_SEC` | `30` | Wait between retries (disc stays loaded — no mid-retry eject) |
| `VIDEOSRIPPER_STALL_RETRY` | `true` | Retry same title after stall (uses retry count) |
| `VIDEOSRIPPER_NAS_RETRIES` | `1` | Extra rsync attempts; then ALERT and continue rip |

CLI: `--retry N`, `--retry-delay SEC`, `--on-stall exit|warn`. Manifest override:

```yaml
monitor:
  stall_timeout_sec: 1800   # 30 min for long BD titles
  open_timeout_sec: 600
  on_stall: exit
retry:
  count: 3                  # 3 retries = 4 total attempts; then eject + exit
  delay_sec: 30
  stall_retry: true
```

After all retries are exhausted (disc open / Failed to open disc / stall), VideosRipper logs `=== ALERT === USER INTERVENTION REQUIRED`, **auto-ejects** the disc so you can clean/re-seat it, and exits non-zero. Re-insert the disc and re-run the rip (TV: `--start-at N` to resume). The disc is **not** ejected between retries.

On success, logs end with `=== RIP COMPLETE ===` (episodes, NAS copy, eject). Dry-run ends with `=== DRY RUN COMPLETE ===` — no files written, disc not ejected.

## Logging

All sessions append to fixed files under `logs/` (override root with `VIDEOSRIPPER_LOGS`):

| File | Purpose | Tail |
|------|---------|------|
| `rip.log` | High-level rip status (slot start/done, episodes, NAS, alerts, complete) | `tail -f logs/rip.log` |
| `debug.log` | Verbose output (makemkv stdout, ffprobe, staging paths, monitor settings) | `tail -f logs/debug.log` |
| `scan.log` | Scan summaries (title table + pointer to debug) | `tail -f logs/scan.log` |
| `error.log` | ERROR/CRITICAL only (`=== ALERT ===`, exception tracebacks) | `tail -f logs/error.log` |

Each file rotates by **line count** (max 50,000 lines; keep 3 backups): e.g. `rip.log` → `rip.log.1`, `rip.log.2`, `rip.log.3` (`.1` is newest backup).

ERROR/ALERT lines still appear in `rip.log` (or `scan.log` for scans) and are also mirrored to `error.log`. Exception tracebacks go to both `debug.log` and `error.log`.

Per-disc files like `logs/rip-s13-disc3.log` are **legacy** — new runs use the four files above. Manifest `output.log_file` is deprecated (optional, ignored).

## LLM-assisted ripping

See **[LLM-RIP-WORKFLOW.md](LLM-RIP-WORKFLOW.md)** for full host setup, NAS paths, YAML schema, historical disc mappings, and step-by-step instructions (including Season 8 Disc 4 as the next fresh Python CLI test).

## Legacy bash scripts

Per-disc `rip-*.sh` scripts were moved to `scripts/archive/`. See [scripts/archive/README.md](scripts/archive/README.md). Do not add new bash rip scripts — use YAML manifests instead.
