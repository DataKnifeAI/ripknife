# Archived legacy rip scripts

These one-off per-disc and per-movie bash scripts were used before the config-driven Python CLI existed. They are **kept for reference and resume** only — do not add new scripts here.

## Use instead (preferred)

- **Manifests:** `configs/` (e.g. `configs/spongebob/s8-disc4.yaml`, `configs/movies/*.yaml`)
- **CLI:** `scripts/videosripper` → `validate` | `scan` | `rip` `<manifest.yaml>` (real `rip` ejects when `post.eject: true`; scan/validate/dry-run never eject)
- **Shared bash helpers (still active):** `scripts/videosripper-common.sh`

See:

- [README.md](../../README.md) — project overview and quick start
- [LLM-RIP-WORKFLOW.md](../../LLM-RIP-WORKFLOW.md) — full workflow, YAML schema, NAS paths, disc mappings

Prefer the Python CLI for all new rips. **Season 8 Disc 4** is the first disc intended to be authored entirely as a new YAML manifest (see LLM-RIP-WORKFLOW → Season 8 Disc 4). Use these archived scripts only when resuming a legacy in-flight job that has no manifest yet.

## Resuming an archived script

Run from any cwd — paths are relative to the script file:

```bash
bash scripts/archive/rip-<name>.sh
VIDEOSRIPPER_START_AT=N bash scripts/archive/rip-<name>.sh   # resume from slot N
```

Each script sources `scripts/videosripper-common.sh` via `source "$(dirname "$0")/../videosripper-common.sh"`.

When a YAML manifest exists for the same disc (e.g. `configs/spongebob/s8-disc3.yaml`), use `scripts/videosripper rip configs/…yaml` instead.

## Contents

20 archived scripts: former `scripts/rip-*.sh` and related one-offs (`rip-dvd.sh`, `rip-ghost-rider-*.sh`). Episode mapping examples remain under `scripts/examples/`.
