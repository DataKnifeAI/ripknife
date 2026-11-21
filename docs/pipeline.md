# Pipeline

Two processes. Neither one asks the operator to name the title, pick tracks, split episodes, or join discs.

Process 1, the disc watcher: insert, scan, registry hit or map agent, then `staging/ready/<id>.yaml`.

Process 2, the rip watcher: ready manifest, verify the disc, rip, verify the file, library copy, eject, `staging/done`.

The map call returns JSON. Python writes the manifest. See [llm.md](llm.md). Personal manifests live in `RIPKNIFE_CONFIG_DIR` or a sibling `ripknife-configs`. See [agent.md](agent.md).

Several drives can rip at once. One drive never runs two `makemkvcon mkv` processes.

Preferred always-on start is `make install-service`. `make dev` runs the watchers in this checkout instead. Do not run both. Logs for units: `/var/log/ripknife`. Ad-hoc logs: repo `logs/`.

## Episodes

A disc of episodes becomes one file per episode: `Show - NxMM - Title.mkv` (episode zero-padded). An absolute episode number is fine for a long-running series. A box-set number is not the library season. Set `meta.season` to the library season and check which episode numbers are already present.

MakeMKV title index is not episode order. Skip play-all. Pair titles by playlist name, duration, chapter cards, and a known order, then set `makemkv_title` to that scan index.

One episode failing does not abort the disc. Remaining slots continue. The job lands in `staging/failed/` as partial.

## Multi-disc movies

One manifest per disc. Local name: `Title (Year) - Disc N.mkv`. `meta.part` is 1-based. `post.copy_to_nas: false` on every part. The same `stitch` block on every part. The rip watcher joins the parts after every part file exists. The join does not take the MakeMKV lock.

Copy only the joined file. Keep the per-disc masters until the join is verified. Do not join different cuts, different editions, or a feature plus its extras.

Prefer `mkvmerge`. The output duration should match the sum of the parts. Small muxer skew is normal.

```yaml
stitch:
  enabled: true
  group_id: example-feature-2001
  expected_parts: 2
  output_filename: Example Feature (2001).mkv
  copy_to_nas: true
```

A concat list uses working-storage paths:

```
file '/path/to/ripknife/output/Movies/Title (Year) - Disc 1.mkv'
file '/path/to/ripknife/output/Movies/Title (Year) - Disc 2.mkv'
```

## Editions

Non-theatrical cuts set `output.edition`. That becomes Plex `{edition-…}` on the filename. Keep `meta.title` and the base filename as `Title (Year)`. Do not put the edition words in `meta.title`.

Set `output.edition` on every disc master and on the joined file. Masters look like `Title (Year) {edition-<label>} - Disc N.mkv`. The joined file is `Title (Year) {edition-<label>}.mkv`.

Strip colons from titles and filenames.

## Title research checklist for LLM mapper

1. Parse the scan JSON: `disc_name`, `titles`, obfuscation, identification. Read the pending meta JSON.
2. Identify the work (show, season, and disc, or `Title (Year)`). A generic volume label is not the title.
3. When two well-known films share the runtime and there is no fingerprint, bdmt title, or explicit user id: the JSON action is `ambiguous` and Python does not write YAML.
4. When `identification.fingerprint_title` is set, use it. Else when `identification.bluray_title` is set, use it. A single runtime candidate is a hint.
5. Blu-ray playlist obfuscation (`title_count` > 50, or `duplicate_count` >= 5): movies use `title_selection.mode: auto_feature` (or fixed at `recommended_title`) and skip decoy indexes.
6. DVD and TV: skip play-all. Do not number episodes from sequential title indexes. Pair by playlist name, duration, chapter cards, and a known order, then set `makemkv_title` to that scan index.
7. Cross-reference `registry.yaml` and earlier manifests for the same show. Match episode numbering and whether indexes are TINFO (0-based) or MSG (often 1-based).
8. Set `meta.id` to the identified slug, `meta.disc_name` to the exact scan name, and `meta.minlength` from the scan (default 300).
9. Put the title choice in the manifest fields. The model returns JSON, which has no comments.
10. Python validates the JSON and writes `staging/ready/<id>.yaml`. The model does not write the file and does not get a shell. Do not write straight into the config directory.

Library routing for that manifest is in [library.md](library.md).

`stream_defaults`: Japanese anime uses `audio_default: jpn` and `subtitle_default: eng`. English-language cartoons and live-action use `audio_default: eng` and omit `subtitle_default`. `keep_all_tracks: true` keeps every audio and subtitle track. Do not omit `stream_defaults` on movies.

Theatrical shorts (about 6–8 minutes) need `meta.min_episode_sec: 360`. The default 480 rejects them. Use a unique show name plus `plex.local: true` so the library does not bind a catalog series with a different order. Season N is the collection volume. Episode numbers continue across discs of that volume. Quote titles that contain a comma.

`extract_chapter`: `chapter_index` and `chapter_end` are 0-based and both inclusive. With N chapters the last index is N-1.

`plex.local: true` writes a show-folder `.plexmatch` (`title` and optional `year`, no catalog ids).

Size floors (`title_selection.min_size_bytes`, `validation.min_size_bytes`) default to 1 GiB. That catches an empty rip and still accepts a real DVD. Prefer omitting `min_size_bytes`, or set about 90% of the scan `size_bytes` for the feature. Do not copy a 5 GiB floor onto a DVD unless the scan feature is that large.

## Manifests

`configs/examples/` holds fake titles. Real per-disc YAML lives in the personal config directory.

### TV disc

```yaml
kind: tv_disc
meta:
  id: example-show-s1-disc1
  show: Example Show
  season: 1
  device: dev:/dev/sr0
  minlength: 300
  disc_name: EXAMPLE SHOW S1D1
output:
  season_dir: Example Show/Season 1
nas:
  share: /path/to/library/Shows/Example Show
  subdir: Season 1
stream_defaults:
  keep_all_tracks: true
  audio_default: eng
tracks:
  - { slot: 1, makemkv_title: 0, episode: "01", title: First Episode }
skips:
  - { makemkv_title: 8, reason: play-all }
post:
  eject: true
  copy_to_nas: true
```

### Movie

```yaml
kind: movie
meta:
  id: example-movie-1991
  title: Example Movie (1991)
  device: dev:/dev/sr0
  minlength: 300
output:
  dir: Movies
  filename: Example Movie (1991).mkv
nas:
  movies: true
  share: /path/to/library/Movies
stream_defaults:
  keep_all_tracks: true
  audio_default: eng
title_selection:
  mode: auto_feature
  min_duration_sec: 4800
validation:
  min_duration_sec: 4800
```

`rip` and the rip watcher re-scan and refuse a manifest whose title indexes do not match the disc. Play-all and bonus titles belong in `skips`.

```yaml
post:
  eject: true
  copy_to_nas: true
  delete_local_after_nas: false  # Makefile and systemd set the env var to 1
```

## Commands

```bash
scripts/ripknife scan /path/to/manifest.yaml
scripts/ripknife scan /path/to/manifest.yaml --json
scripts/ripknife validate /path/to/manifest.yaml
scripts/ripknife validate /path/to/manifest.yaml --with-scan
scripts/ripknife rip /path/to/manifest.yaml --dry-run
scripts/ripknife rip /path/to/manifest.yaml
scripts/ripknife rip /path/to/manifest.yaml --start-at 5
scripts/ripknife preview --title 1 --config /path/to/manifest.yaml --offset 60 --seconds 45
```

`preview` does not rip the feature. `--dry-run` writes no files and does not eject. A real rip log ends with `=== RIP COMPLETE ===`. A dry-run log ends with `=== DRY RUN COMPLETE ===`.

## Resume

Slots whose output already exists are skipped. When `copy_to_nas` is on, an existing library copy is skipped too.

Watchers auto-resume: re-insert a failed disc, or `make watch-restart` with the disc still loaded. Cap: `RIPKNIFE_REINSERT_MAX` (default 5). An incomplete mid-title slot restarts from 0%. A bad-sector rip gets one in-rip retry, then the ladder in [recovery.md](recovery.md). One reinsert of that disc does not start another full-speed rip.

An ambiguous map, and a library copy failure after a good local file, are not re-ripped. An empty tray is not imaged. See [drives.md](drives.md).

## YouTube

`scripts/ripknife youtube` pulls one released episode, a `--movie`, or a show `--playlist`. The disc watcher, the rip watcher, and `make watch-*` do not call it.

Show name, animated versus live-action, movie versus series, the library folder, and how a playlist title becomes season, episode, and title live in `youtube.yaml`. The functions stay generic: `load_youtube_map`, `match_youtube_show`, `parse_youtube_title`, and `build_youtube_plan`. A new show is a new entry in that file.

`kind: series` with `animated: true` goes to `Anime/<Show>/Season N/` as `Show - NxMM - Title.mp4`. `animated: false` on a series goes to `Shows/<Show>/`. `kind: movie` with `animated: true` goes to `Anime/Movies/Title (Year).mp4`. A live-action movie goes to `Movies/`.

A single episode needs `--show`, `--season`, `--episode`, and `--title`. Those flags override the map. `--playlist` needs a mapped show (`--show`, or a playlist id on the URL). `--dry-run` prints the destination and the yt-dlp command and does not download.

`yt-dlp` comes from `PATH`, otherwise `python -m yt_dlp`. The download is written under `output/youtube/` and moved into the library folder only after yt-dlp exits 0 and ffprobe sees video and audio. An existing destination file is left in place.

## Monitor

Real rips watch MakeMKV for stalls and disc-open failures.

| Variable | Default | Purpose |
|----------|---------|---------|
| `RIPKNIFE_STALL_TIMEOUT_SEC` | `900` | Kill or warn when there is no PRGV and no staging file growth (movies; TV default 600s) |
| `RIPKNIFE_OPEN_TIMEOUT_SEC` | `600` | Preflight `makemkv info` timeout |
| `RIPKNIFE_ON_STALL` | `exit` | `exit` kills makemkv, `warn` does not |
| `RIPKNIFE_RETRY_COUNT` | `1` | Retries after the first failure (1 means 2 attempts) |
| `RIPKNIFE_RETRY_DELAY_SEC` | `30` | Wait between retries. The disc stays loaded |
| `RIPKNIFE_STALL_RETRY` | `true` | Retry the same title after a stall |
| `RIPKNIFE_NAS_RETRIES` | `1` | Extra rsync attempts, then an alert, and the job continues |

## Logs

Override the directory with `RIPKNIFE_LOGS`. Rotate at 50,000 lines and keep 2 backups. Ad-hoc and systemd runs use the same filenames.

| File | Purpose |
|------|---------|
| `pipeline.log` | Stage and progress |
| `rip.log` | Slot start and done, episodes, library copy, alerts |
| `debug.log` | makemkv stdout, ffprobe, staging, monitor settings |
| `scan.log` | Scan summaries |
| `error.log` | ERROR, CRITICAL, `=== ALERT ===`, tracebacks |
| `map.log` | Map agent |

## Environment

Model variables are in [llm.md](llm.md). Library roots are in [library.md](library.md).

| Variable | Fallback | Purpose |
|----------|----------|---------|
| `RIPKNIFE_ROOT` | directory of the `ripknife` package | Working storage root |
| `RIPKNIFE_CONFIG_DIR` | sibling `ripknife-configs` if present | Personal manifests and maps |
| `RIPKNIFE_OUTPUT` | `$ROOT/output` | Finished MKVs |
| `RIPKNIFE_STAGING` | `$ROOT/staging` | Job queue |
| `RIPKNIFE_TMP` | `$ROOT/.tmp` | Temp |
| `RIPKNIFE_LOGS` | `$ROOT/logs` | Ad-hoc logs. systemd uses `/var/log/ripknife` |
| `RIPKNIFE_DEVICE` | `dev:/dev/sr0` | Default drive |
