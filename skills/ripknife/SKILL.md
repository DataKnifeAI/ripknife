---
name: ripknife
description: >-
  Operates the Ripknife disc pipeline in this repo: detect, map, manifest,
  rip, library copy, episode files, multi-disc join, editions, library roles,
  slot drives, and OpenRouter calls. Use when working in the Ripknife checkout,
  editing ripknife code, manifests, watchers, recovery, library mapping, or
  model prompts.
---

# Ripknife

The agent does not identify the title, pick tracks, split episodes, or stitch discs by hand. The pipeline does that. Depth is in the docs. This file is the entry, not a second copy.

| Topic | Read |
|-------|------|
| Automatic path, commands, config, what not to commit, what to leave alone during a rip | [docs/agent.md](../../docs/agent.md) |
| Detect, map, rip, copy, episodes, multi-disc join, editions | [docs/pipeline.md](../../docs/pipeline.md) |
| Animation series, animated movies, live-action TV, live-action movies | [docs/library.md](../../docs/library.md) |
| Slot drives, eject, empty slot | [docs/drives.md](../../docs/drives.md) |
| OpenRouter calls and model env vars | [docs/llm.md](../../docs/llm.md) |
| Damaged-disc ladder and when a copy is allowed | [docs/recovery.md](../../docs/recovery.md) |

Model prompts the script sends live under `skills/`. `ripknife.map_agent` loads `skills/map`. `ripknife.llm` loads `skills/failure`, `skills/gap`, and `skills/verify`. Edit the skill, not a string in Python.

## Generated configs

Generated manifests are gitignored here. The rip reads `staging/ready`. When a manifest is generated, archive a copy in the sibling `ripknife-configs` checkout (`generated/`) for reference. That repo is not the live rip queue and is not committed inside this repo. Do not push it from the watcher. See [docs/agent.md](../../docs/agent.md).

Do not start a rip, open a drive, or image a disc while changing the repo. One drive never runs two `makemkvcon mkv` processes. Do not copy a bad or gapped file to the library.
