# VideosRipper — LLM Workflow Instructions

Instructions for an LLM assisting with DVD ripping on this machine. Read this file before starting any rip task.

## Goal

Rip SpongeBob SquarePants DVDs losslessly with MakeMKV, rename files for Plex, copy each episode to the NAS library, and optionally eject the disc when finished (`post.eject: true` in the manifest).

**Archive principle:** the pipeline is **lossless remux only** (MakeMKV → MKV; ffmpeg disposition remux, no re-encode). Blu-ray / UHD stay native. Optional AI upscale is a **DVD-only derivative** — never replace the remux. Optional multi-disc **stitch** is also a derivative (keep per-disc masters). See [Optional post-processing: DVD upscale (RTX 4090)](#optional-post-processing-dvd-upscale-rtx-4090) and [Multi-disc / multi-part movies](#multi-disc--multi-part-movies).

**Workflow:** Config-driven **Python pipeline** (`videosripper/pipeline.py`) + YAML manifests. Watchers call pipeline step functions directly — not shell scripts. See [Pipeline orchestration](#pipeline-orchestration). Legacy bash rip scripts are archived in `scripts/archive/`.

## Host environment

| Item | Value |
|------|-------|
| OS | CachyOS / Arch Linux |
| MakeMKV | 1.18.4 official install in `~/.local/` (not AUR) |
| CLI | `~/.local/bin/makemkvcon` |
| Key | `~/.MakeMKV/settings.conf` (`app_Key`, mode 600) |
| Library path | Source `~/.config/environment.d/makemkv.conf` before running makemkv |
| Optical drive | Pioneer BDR-XS07U USB (fw 1.04) → `/dev/sr0` |
| Drive group | User must be in `optical` group; kernel modules: `sg`, `sr_mod`, `usb_storage` |
| GPU (optional upscale) | NVIDIA RTX 4090 (Ada, 24 GB VRAM, CUDA / TensorRT) — DVD derivative post-processing only |
| Stitch tools | `ffmpeg` present; install `mkvtoolnix-cli` for preferred `mkvmerge` append (multi-disc) |

## Directory layout (all on game drive)

```
/mnt/game1/VideosRipper/
  README.md                 ← project overview
  LLM-RIP-WORKFLOW.md       ← this file (LLM-assisted ripping)
  pyproject.toml            ← Python package metadata
  .venv/                    ← local venv (create on install; not in git)
  videosripper/
    pipeline.py           ← step functions (serial / parallel / background)
    map_agent.py          ← LLM YAML mapper (replaces trigger-map.sh logic)
    cli.py, runner.py, …
  scripts/
    videosripper            ← thin bash launcher → Python CLI
    disc-watcher.py         ← Process 1 → pipeline.handle_disc_insert()
    rip-watcher.py          ← Process 2 → pipeline.process_ready_manifest()
    trigger-map.sh          ← wrapper → python -m videosripper.map_agent
    fix-*.sh                ← host helpers (drive, NAS mounts) only
    stitch-movie-parts.sh   ← optional lossless multi-disc join (mkvmerge / ffmpeg)
    logs/                     ← running logs (append; rotate at 50k lines, keep .1–.3)
    pipeline.log            ← high-level STAGE / progress (main overview)
    rip.log                 ← high-level rip status
    debug.log               ← makemkv/ffprobe/subprocess detail
    scan.log                ← scan summaries
    error.log               ← ERROR/CRITICAL + exception tracebacks
    disc-watcher.log / rip-watcher.log / map.log
  output/                   ← final MKV files (e.g. output/Season 8/)
  staging/                  ← temp dirs during each title rip (.rip.XXXXXX)
```

**Always use the game drive** for staging and output. Do not rip to `~/VideosRipper/output/` or `/tmp`.

Override paths only via env vars (see [Environment variables](#environment-variables)).

## NAS library

| Path | Purpose |
|------|---------|
| `/mnt/tn-sas/Anime/Spongebob Squarepants/Season N/` | SpongeBob Plex library (Anime share) |
| `/mnt/tn-sas/Anime/Naruto Shippuden/Season N/` | Naruto Shippuden Plex library (Anime share) |
| `/mnt/tn-sas/Shows/<Show>/Season N/` | Live-action / non-anime TV (Shows share) |
| `/mnt/tn-sas/Movies/` | Feature films (Movies share) |
| Copy method | `rsync` via `videosripper/nas.py` (`copy_episode_to_share`, `copy_movie_to_share`, `eject_disc`) |

Ensure the destination folder is writable (`drwxrwxrwx` or equivalent) before copying.

### NAS mounts (`/etc/fstab`)

All **9** `nas.dataknife.net` NFS4 lines must use the **same** options:

```
rw,relatime,vers=4.2,async,nconnect=4,rsize=1048576,wsize=1048576,_netdev,soft,nofail,x-systemd.automount,x-systemd.idle-timeout=600
```

| Share | Mount point | Library use |
|-------|-------------|-------------|
| Anime, FAV, Shows, YouTube, Photos, Timelapse | `/mnt/tn-sas/...` | SpongeBob: `/mnt/tn-sas/Anime/Spongebob Squarepants` |
| Movies | `/mnt/tn-sas/Movies` | Feature films |
| Tools, Backup | `/mnt/tn-cold/...` | — |

**`nconnect=4` on every line:** The Linux NFS client shares one RPC transport per server. The **first automount wins** — if any share still has `sync` without `nconnect=4`, later mounts inherit the slow single-connection path. All 9 lines must match, then **remount all shares** (or reboot).

**`async` in fstab vs reality:** `async` is listed for consistency, but modern Linux NFS **ignores it for data integrity** — client writes are effectively synchronous. The measured speedup (~**200 MB/s** with `rsync`/`cp` vs old ~**12–29 MB/s**) comes mainly from **`nconnect=4`** and **`rsize`/`wsize=1048576`**, not from async.

**Copy files with `rsync` or `cp`** for movie/episode transfers. Do not use `dd` for file copies.

### Feature films (Blu-ray / BD-Java)

- **BD-Java discs need a JRE** — MakeMKV warns `requires Java runtime (JRE), but none was found`. Install `jre-openjdk` (or `jre8-openjdk` if MakeMKV still misses it); verify with `java -version` and re-scan.
- **Use the same `--minlength` for scan and rip** — e.g. `info -r --minlength=300` then `mkv -r --minlength=300`. A mismatched minlength renumbers titles (Ghost Rider: title 0 became a ~294 MB promo at minlength=120 instead of 00800.mpls theatrical ~24 GB).
- **Verify before NAS copy** — `ffprobe` duration (reject feature rips &lt; ~80 min) and file size (reject &lt; ~5 GB for a typical BD feature). Do not rsync a bad rip to `/mnt/tn-sas/Movies/`.
- **Always set `stream_defaults` on movie manifests** — English-language films: `audio_default: eng`, omit `subtitle_default` (clear subtitle default). Without this, MakeMKV preserves the disc’s default audio (often French/Spanish on multi-audio DVDs/BDs). See [stream_defaults](#stream_defaults-anime-language-defaults).
- **UHD / 4K (future):** pipeline is BD-path compatible (MakeMKV `mkv` + keep-all tracks + ffmpeg disposition remux). Expect gaps before first UHD rip: AACS2 / disc drive firmware support in MakeMKV, HDR10 vs Dolby Vision retention (no HDR tone-map step today), TrueHD+Atmos / multi-audio selection (prefer lossless via `prefer_lossless` in ffmpeg remux), and larger size floors in `validation.min_size_bytes`.

**After fstab changes:**

```bash
sudo bash /mnt/game1/VideosRipper/scripts/fix-nas-mounts.sh
```

- `--fstab-only` — update fstab only; reboot or run again without the flag to remount.
- `--lazy` — if umount fails due to open files (breaks lazy-unmounted handles).
- `--stop-verify` — kill `sha256sum` on Movies (common umount blocker) and skip the optional cp speed test.

**Do not** run `sha256sum` verify on large (e.g. 32 GB) movie files over NFS while remounting — it holds the Movies mount busy. Stop verify jobs before remount, or use `--stop-verify`.

Script verifies remount via mount options and `ss` to `nas.dataknife.net:2049` (expect multiple established connections when `nconnect=4` is active).

## File naming

```
SpongeBob SquarePants - SxEE - Episode Title.mkv
```

- **All rips go in `Season N/`** on the game drive and NAS (e.g. `Season 3/`, `Season 6/`).
- Use **TheTVDB DVD order** episode numbers (all `Nx##` within that season). Reference: [TheTVDB DVD order](https://thetvdb.com/series/spongebob-squarepants/seasons/dvd/).
- Do **not** use aired-order numbers or wrong-season prefixes — breaks Plex when everything lives in one season folder.
- **Plex:** set episode ordering to **DVD order** (Show → Edit → Advanced → Episode ordering).
- Sanitize titles: no `?`, `/`, `:`, etc.
- Fix S6 files after aired-order rips: `bash /mnt/game1/VideosRipper/scripts/rename-s6-dvd-order.sh`

### Anime (Naruto Shippuden and similar)

```
Naruto Shippuden - 12x249 - Thank You.mkv
```

- **Show folder on NAS:** `/mnt/tn-sas/Anime/Naruto Shippuden/Season N/` (absolute episode numbers in filenames, e.g. `12x249` not aired-order).
- **Local output:** use a show-specific path so seasons do not collide with other shows, e.g. `output/Naruto Shippuden/Season 12/`.
- **YAML:** set `nas.share` to the show folder and `nas.subdir: Season N` when `output.season_dir` includes the show name (see [stream_defaults](#stream_defaults-anime-language-defaults)).
- **Physical Blu-ray set number ≠ anime season.** Viz Media box sets are numbered independently of Plex/TheTVDB seasons — one physical set can span multiple anime seasons. Always check the NAS library (`ls "/mnt/tn-sas/Anime/Naruto Shippuden/Season N/"`) for the next missing episode numbers and set `meta.season` to that Plex season, not the box-set number. Example: **Blu-ray Set 10 Disc 1** contains episodes **249–255**, which belong in **Season 12** (NAS already has 12x243–12x248).
- Match existing NAS spelling (`Naruto Shippuden` without macron in recent rips).
- Sanitize titles: no `?`, `/`, `:`, etc. (existing library keeps `!` where present).

## Pipeline orchestration

All rip workflow steps are **Python functions** in `videosripper/pipeline.py`. Scheduling depends on step kind:

| Kind | Steps | Scheduling |
|------|-------|------------|
| `MAKEMKV` | scan, rescan, rip | **Serial per drive** — `makemkv[-<slug>]` lock |
| `LOCAL` | validate, registry lookup, write pending JSON | **Parallel** — `run_parallel()` |
| `BACKGROUND` | LLM map agent | **Async** — `step_trigger_map_async()` |

**Process 1** (`disc-watcher.py`): `handle_disc_insert()` → scan (serial) → registry hit OR background map.

**Process 2** (`rip-watcher.py`): `process_ready_manifest()` → validate (local) → rescan + verify + rip (serial under makemkv lock).

### Multiple drives / parallel rips

The unit of concurrency is the **drive**, not the individual job. MakeMKV locks are
**per-drive** (`makemkv-sr0.lock`, `makemkv-sr1.lock`, …), so one disc per drive can
rip simultaneously while a single drive stays mutually exclusive. `videosripper/devices.py`
provides `discover_optical_devices()` (enumerates `/dev/sr*`) and `device_slug()`.

- **Single drive (default):** run the watchers with no device args — behavior is unchanged
  (`dev:/dev/sr0`, global-compatible flow).
- **Specific drives:** `--device dev:/dev/sr0 --device dev:/dev/sr1` (repeatable).
- **Auto-discover all drives:** `--auto`.

```bash
# Preferred: start both detached from the shell/session (setsid + nohup):
make watch-all
make watch-status    # running? last heartbeat? disc present?
make watch-stop      # or: make watch-restart

# Watch every optical drive for inserts, scan + map each in parallel:
scripts/disc-watcher.py --auto

# Run one rip worker per drive (pull-by-disc: each drive rips the ready
# manifest matching the disc it currently holds):
scripts/rip-watcher.py --auto
# or bind specific drives:
scripts/rip-watcher.py --device dev:/dev/sr0 --device dev:/dev/sr1
```

`make watch-all` uses `setsid -f nohup …` so watchers are **session leaders** and survive
the make/agent shell exiting (see [Background watchers](#background-watchers-make-watch-)).
Prefer either make **or** systemd user units under `scripts/systemd/`, not both (flock
singletons reject a second instance).

**Disc-watcher** runs one thread per drive; each has its own scan lock, watcher lock
(`disc-watcher-<slug>`), and state section (`disc_watcher:<slug>`).

**Rip-watcher** has two modes:
- *Default (no device args):* single-drive, file-driven — rips each ready manifest against
  its `meta.device`.
- *Device-bound (`--device`/`--auto`):* one worker per drive using `run_device_worker_once()`.
  Each worker scans the disc in its drive, atomically **claims** the matching ready manifest
  (rename to `*.claimed-<slug>`), rebinds `meta.device` to that drive, and rips. This avoids
  the "which drive holds this disc?" problem — a manifest rips on whichever drive has its disc.

**Manual steps:** `scripts/videosripper scan|validate|rip …` — same Python code paths as the watchers.

Shell remains only for: `scripts/videosripper` launcher, `fix-optical-drive.sh`, NAS mount helpers, and `trigger-map.sh` (one-line wrapper to `python -m videosripper.map_agent`).

### Background watchers (`make watch-*`)

| Target | Purpose |
|--------|---------|
| `make watch-all` | Start both watchers **detached** (`setsid -f nohup …`) → `logs/nohup-watch-*.log` |
| `make watch-stop` | `pkill` the python watcher processes (logs `WATCHER_STOPPED` in `pipeline.log`) |
| `make watch-restart` | Stop → brief pause → start (do **not** while `makemkvcon` is mid-rip) |
| `make watch-status` | Running/not, last heartbeat age, disc present |

`setsid` is required: plain `nohup … &` keeps watchers in the make/shell process group, so when an agent or terminal tears that group down with **SIGTERM**, both die **silently** (`nohup` only ignores SIGHUP). That was the 2026-07-25 ~20:12 failure mode after a live `make watch-restart` from an agent shell.

Crash resilience: each device worker runs inside `run_resilient_loop` (logs `WATCHER_CRASH` to `pipeline.log` + traceback to `error.log`, then restarts). Heartbeats land in `logs/watcher-state.json` under `heartbeat:disc-watcher` / `heartbeat:rip-watcher`. SIGTERM/SIGINT log `WATCHER_STOPPED`.

**Durable option:** systemd user units under [`scripts/systemd/`](scripts/systemd/) (`Restart=always`, survives logout). Do not run units and `make watch-all` at the same time.

### Pipeline stage log (`logs/pipeline.log`)

Single overview log for “where are we?” — watchers and rip call `log_stage` / `log_progress`.
Both watchers log `STAGE WATCHERS_UP` at startup, so the file exists (and `tail -F logs/pipeline.log`
works) immediately after `make watch-all`. Every file line is timestamped, and each STAGE line
carries a `[+…]` marker with the elapsed time since the previous stage in that process —
so per-stage timing is on the line, and `DONE` / `RIP_FAILED` also carry `total=…` for the
whole rip (first-timestamp → DONE-timestamp still works as a cross-check).

**Two numbered pipelines** (never bare `[1/5]` — that was ambiguous):

| Tag | Process | Steps |
|-----|---------|-------|
| `[insert N/5]` | disc-watcher | SCAN → PENDING → REGISTRY → MAP → MAP_LAUNCHED (or REGISTRY_HIT / READY_EXISTS) |
| `[rip N/7]` | rip-watcher | CLAIM/VALIDATE → RESCAN → VERIFY → RIP → COPY → EJECT → DONE |

Post-rip detail inside RIP (unnumbered `[rip]` lines, not part of N/7):
- **REMUX** — ffmpeg `stream_defaults` disposition remux when configured (per title)
- **COPY** / **COPY_OK** / **COPY_FAILED** — per-file NAS rsync for TV (movies use numbered COPY start + result)
- Numbered **COPY** (5/7) always closes the NAS phase: dest path on success, or `skipped (…)`
  (`post.copy_to_nas: false`, no share, output already complete); TV also logs a summary
  (`N ok, M failed → season dir`)
- Numbered **EJECT** (6/7) when `post.eject` runs (or `skipped` when disabled). Eject is now
  logged *after* it actually runs — the old summary claimed "not attempted" before ejecting
  and only logs **ok** when `media_present()` confirms the disc is gone (slot loaders may
  report udisksctl/eject success while media is still readable). Up to **3 attempts** with
  verify polling before `EJECT_FAILED` + `USER INTERVENTION REQUIRED`.
- **DONE** (7/7) runs after copy/eject logging so the rip process clearly closes, and carries
  `total=…` — wall time since `[rip 1/7]`, since `[+…]` alone would only show the eject gap.
  `RIP_FAILED` carries the same `total=…`

Insert-side sub-stage: **IDENTIFY** (unnumbered `[insert]`) reports what bdmt/runtime hints
produced (`generic=`, `bdmt=`, `candidates=`, `feature=`) — previously a generic-label disc
jumped SCAN → PENDING with no trace of why the map agent got (or missed) a real title.

After `[rip …] DONE`, the remux is the archive master. **Optional DVD upscale** would be a later, separate step on those finished MKVs — not part of the numbered rip pipeline today. See [Optional post-processing: DVD upscale (RTX 4090)](#optional-post-processing-dvd-upscale-rtx-4090).

Status-only lines omit `N/M`: `WATCHERS_UP`, `DISC_DETECTED`, `IDLE`, `WAITING_MANIFEST`,
`WATCHER_STOPPED`, `WATCHER_CRASH`.
Failures keep the process tag without a step: `=== STAGE [insert] SCAN_FAILED — … ===`.

Each stage names the object it is working on: device for SCAN/DISC_DETECTED, disc name + slug id
for PENDING/REGISTRY/MAP, manifest label + yaml filename for VALIDATE/VERIFY/RIP/DONE,
file → NAS dest for COPY. `progress [rip] RIP` uses **tracks completed / tracks in this run**
(not slot or makemkv title index), so `0/12 (0%)` means starting the first of 12, `3/12 (25%)`
means three finished. Movies log `0/1` while ripping and `1/1` after the file lands. That
numbered `RIP N/M` only advances *between* titles.

**Within-title live percent:** each `makemkvcon mkv` runs with `--progress=-same` (default robot
mode emits no progress), so `PRGV:current,total,max` lines stream to stdout. `prgv_percent` takes
the `total`/`max` (overall) bar and a `RipDecileTracker` throttles it to one line per rising 10%,
logged as `  progress [rip] RIP title 0 — 40% (MakeMKV, ~14m left)` — so `tail -F logs/pipeline.log`
shows movement inside a long title. ETA uses elapsed×(100−pct)/pct once ≥10% and ≥30s have elapsed
(omitted earlier while the rate is unstable; formats as `~14m` / `~1h05m` / `<1m`). The same percent
and `eta_sec` land in `rip_progress` in `logs/watcher-state.json` and `make watch-status`
(`rip: title 0 — 40% (MakeMKV, ~14m left)`). PRGV still feeds stall detection (alongside staging file growth and
`MSG:5014`), so a rip with no PRGV *and* no file growth still trips `VIDEOSRIPPER_STALL_TIMEOUT_SEC`.
Caveats: `current`/`total` reset per title (we rip one title per `makemkvcon` call, so `total` = the
current title); the disc-open phase precedes any PRGV; on multi-title TV discs each title's percent
is separate from the numbered `RIP N/M` track counter.

```
2026-07-25 16:40:02 === STAGE WATCHERS_UP — disc-watcher waiting (devices=dev:/dev/sr0, dev:/dev/sr1) ===
2026-07-25 16:40:03 === STAGE WATCHERS_UP — rip-watcher waiting (devices=dev:/dev/sr0, dev:/dev/sr1) ===
2026-07-25 16:41:26 === STAGE DISC_DETECTED — device=dev:/dev/sr0; debouncing 45s === [+1m23s]
2026-07-25 16:42:11 === STAGE [insert 1/5] SCAN — device=dev:/dev/sr0 === [+45s]
2026-07-25 16:43:20   progress [insert] SCAN 2/2 (100%) — titles found (DVD_VIDEO)
2026-07-25 16:43:20 === STAGE [insert] IDENTIFY — label='DVD_VIDEO' generic=True bdmt=- candidates=2 feature=10123s ===
2026-07-25 16:43:20 === STAGE [insert 2/5] PENDING — disc=DVD_VIDEO id=dvd-video -> dvd-video.scan.json === [+1m09s]
2026-07-25 16:43:20 === STAGE [insert 3/5] REGISTRY — lookup disc=DVD_VIDEO id=dvd-video ===
2026-07-25 16:43:20 === STAGE [insert 4/5] MAP — unknown disc id=dvd-video (DVD_VIDEO) ===
2026-07-25 16:43:21 === STAGE [insert 5/5] MAP_LAUNCHED — id=dvd-video background agent; waiting for staging/ready/dvd-video.yaml ===
2026-07-25 16:45:01 === STAGE [rip 1/7] CLAIM — scarface-1983.yaml claimed by dev=dev:/dev/sr0 (disc=DVD_VIDEO) === [+1m40s]
2026-07-25 16:45:02 === STAGE [rip 2/7] RESCAN — Scarface (1983) dev=dev:/dev/sr0 ===
2026-07-25 16:47:29 === STAGE [rip 3/7] VERIFY — disc=DVD_VIDEO vs manifest=Scarface (1983) (scarface-1983.yaml) === [+2m27s]
2026-07-25 16:47:30 === STAGE [rip 4/7] RIP — Scarface (1983) (1 track(s)) dev=dev:/dev/sr0 ===
2026-07-25 16:47:30   progress [rip] RIP 0/1 (0%) — ripping 1/1: title 0 → Scarface (1983).mkv
2026-07-25 17:11:30 === STAGE [rip] REMUX — stream_defaults audio=eng subtitle=cleared — title_t00.mkv === [+24m00s]
2026-07-25 17:12:44   progress [rip] RIP 1/1 (100%) — done 1/1: title 0 → Scarface (1983).mkv
2026-07-25 17:12:50 === STAGE [rip 5/7] COPY — start Scarface (1983).mkv → /mnt/tn-sas/Movies/Scarface (1983).mkv === [+1m20s]
2026-07-25 17:13:05 === STAGE [rip] COPY_OK — Scarface (1983).mkv → /mnt/tn-sas/Movies/Scarface (1983).mkv === [+15s]
2026-07-25 17:13:06 === STAGE [rip 6/7] EJECT — ok /dev/sr0 === [+1s]
2026-07-25 17:13:06 === STAGE [rip 7/7] DONE — Scarface (1983) (scarface-1983.yaml -> staging/done; copy/eject above) total=28m05s ===
2026-07-25 17:13:10 === STAGE IDLE — device=dev:/dev/sr0 — no disc; waiting for insert === [+4s]
```

TV discs copy each episode as it finishes, so the NAS phase interleaves with `RIP` progress and
the numbered `COPY` line is the end-of-disc summary:

```
2026-07-25 18:02:11   progress [rip] RIP 1/5 (20%) — ripping 2/5: slot 2 title 4 — 6x02 Realm of Fear
2026-07-25 18:02:11 === STAGE [rip] COPY — start Star Trek TNG - 6x01 - Time's Arrow.mkv → /mnt/tn-sas/Shows/Star Trek TNG/Season 06/Star Trek TNG - 6x01 - Time's Arrow.mkv ===
2026-07-25 18:03:04 === STAGE [rip] COPY_OK — Star Trek TNG - 6x01 - Time's Arrow.mkv → /mnt/tn-sas/Shows/Star Trek TNG/Season 06/Star Trek TNG - 6x01 - Time's Arrow.mkv === [+53s]
2026-07-25 18:44:52 === STAGE [rip 5/7] COPY — 5 ok, 0 failed → /mnt/tn-sas/Shows/Star Trek TNG/Season 06 ===
2026-07-25 18:44:53 === STAGE [rip 6/7] EJECT — skipped (post.eject: false) ===
2026-07-25 18:44:53 === STAGE [rip 7/7] DONE — Star Trek TNG S6D1 (st-tng-s6d1.yaml -> staging/done; copy/eject above) total=1h04m12s ===
```

A failed copy is loud in `pipeline.log` now instead of only `rip.log`:
`=== STAGE [rip] COPY_FAILED — Scarface (1983).mkv → /mnt/tn-sas/Movies/Scarface (1983).mkv ===`
followed by `=== STAGE [rip 5/7] COPY — 0 ok, 1 failed → … ===` (TV) — the local rip is kept.

Per-process detail stays in `rip.log` / `disc-watcher.log` / `rip-watcher.log` / `debug.log`.

## Primary workflow (Python CLI + YAML)

**Use the config-driven Python CLI for all new rips.** Do not create new bash scripts under `scripts/` — author a YAML manifest under `configs/` instead. Legacy per-disc bash scripts were moved to `scripts/archive/` for reference only (see [Completed discs](#completed-discs-historical-reference)).

### 0. One-time install

```bash
cd /mnt/game1/VideosRipper
python -m venv .venv
.venv/bin/pip install -e .
chmod +x scripts/videosripper
```

Launcher: `scripts/videosripper` → `.venv/bin/python -m videosripper.cli`.

### 1. Verify drive

```bash
ls -la /dev/sr0
```

If missing but USB drive is plugged in:

```bash
bash /mnt/game1/VideosRipper/scripts/fix-optical-drive.sh
```

User may need to enter sudo password. If still no `/dev/sr0`, ask user to unplug/replug, then rerun fix script.

### 2. Scan the disc

With a draft manifest (device + `minlength` set):

```bash
scripts/videosripper scan configs/spongebob/s8-disc4.yaml
scripts/videosripper scan configs/spongebob/s8-disc4.yaml --json   # machine-readable
```

**Optional — preview a mapped title before ripping:**

```bash
# After scan, spot-check title 1 (skip ~60s FBI, sample 45s of content)
scripts/videosripper preview --title 1 --config configs/spongebob/s8-disc4.yaml --offset 60 --seconds 45

# Faster on DVDs: rip only chapter 2, then slice
scripts/videosripper preview --title 1 --chapter 2 --offset 0 --seconds 45 --scan-json /path/to/scan.json

# Keep sample MKV for manual viewing
scripts/videosripper preview --title 1 --config configs/shows/breaking-bad-s3-disc1.yaml --keep
```

PASS/FAIL prints to console; details in `logs/debug.log`. Preview rips to `staging/.preview.*` (cleaned unless `--keep`). Full-title preview is slow on Blu-ray — prefer `--chapter` on DVDs or rely on scan validation when under time pressure.

Or manually (same minlength as manifest):

```bash
source ~/.config/environment.d/makemkv.conf
scripts/videosripper scan configs/spongebob/s8-disc4.yaml
# summary → logs/scan.log ; full makemkv output → logs/debug.log
```

**Identify episode titles vs extras:**

- Title 0 is often **Play All** (~2+ hours) — skip on most multi-episode discs.
- ~11 min titles = single episodes.
- ~20+ min with 2 chapters = two episodes merged on one title — use `split` in YAML (see [YAML manifest schema](#yaml-manifest-schema)).
- <5 min after minlength filter = menus/trailers — add to `skips[]`.

Confirm episode count with user (“next disc has N eps”).

**Title validation (avoid wrong / copy-protected titles):**

**Strategy: fast pre-rip / post-rip confirm** — keep the rip path quick; confirm outputs after each title rip instead of cycling through candidate titles.

- **Pre-rip (fast):** One lightweight scan only. Reject obvious bad mappings: suspicious names (COPYRIGHT, FBI, WARNING, …), durations below `minlength`, short standalone episodes, obfuscation cluster/decoy checks, `disc_name` match. No title cycling, no blocking two-pass scan by default.
- **Post-rip (confirm):** After each episode/track rip, `ffprobe` duration and file size are compared to the pre-rip scan for that `makemkv_title`. Tolerance: duration ±5% or ±30s (whichever is larger); size ±10% (proportional for chapter splits). On mismatch: log `ERROR`, delete the bad output, retry the slot (same `retry.count` as makemkv stalls), then stop for manual fix — **do not** auto-cycle to the next title index.
- **Wrong title workflow:** post-rip fails → slot retries exhaust → re-run with `--start-at N` after fixing the manifest mapping (or pick the right title manually from `scan` Notes).
- **Optional/slow:** Two-pass scan and MPLS parsing stay CLI-only flags; they never block the watcher rip path.

- Use the **same `minlength` for scan and rip** — mismatched minlength renumbers titles (Ghost Rider: title 0 became a promo at minlength=120 instead of the ~24 GB feature).
- **Reject suspicious titles** — names containing COPYRIGHT, FBI, WARNING, anti-piracy loops; durations below `minlength`; standalone episodes under ~8 min after the filter.
- **Check later titles** — on some DVDs the first few indices are dummy protection loops or menus; the real episode may be at a **higher** makemkv index. When mapping, compare duration and size against neighbors; if title N looks wrong, inspect N+1, N+2, etc. (pre-rip hints only — post-rip verify catches wrong picks after rip).
- **Title count explosion:** scan/disc-watcher log `WARNING` when >50 titles (obfuscated Blu-ray); no blocking.
- **Validate before rip:** `scripts/videosripper validate configs/…yaml --with-scan` (or `--scan-json staging/pending/….scan.json`). `rip` and `rip-watcher` re-scan and refuse bad mappings.
- **Pre-watch title slices (optional):** `scripts/videosripper preview --title N` rips a short sample, skips FBI/menu loops with `--offset 60`, and checks blackframe/silence heuristics. Use `--chapter 2` on DVDs to rip only chapter 2 (faster than full title). Off by default on `rip`; pass `--sample-verify` to run before each title. Does not replace scan validation or post-rip ffprobe checks.
- **Scan hints:** `scan` / `scan-device` show a **Notes** column flagging suspicious titles and suggesting later alternatives.
- **Movies:** use `title_selection.mode: auto_feature` or verify fixed index duration/size (Scarface: title 0 ~27 min promo, title 1 ~2:50 feature). Post-rip uses scan duration/size with the same tolerances plus manifest `validation` minimum floors.

**Obfuscated Blu-ray playlists (duplicate / decoy titles):**

Some Blu-rays expose hundreds of playlists with the **same name and duration** (playlist obfuscation). One or more **decoy** playlists may be slightly longer or shorter than the real feature.

- **Detection:** scan JSON includes an `obfuscation` block when ≥5 titles share the dominant duration: `dominant_duration_sec`, `duplicate_count`, `decoy_indices`, `recommended_title` (lowest index in the largest cluster).
- **John Wick Chapter 2 example:** 330 titles all named "John Wick Chapter 2"; 321 at 2:02:22 (7342 s); title **169** is a decoy at 2:04:08 (7448 s); short menu snippets at 148, 268, 324–329. Use `title_selection.mode: auto_feature` or `fixed` at title **0**; document decoys in `skips[]`.
- **Mapping:** pick `recommended_title` from scan JSON (or lowest index in the dominant cluster). Add every `decoy_indices` entry to `skips[]` with reason like `decoy playlist (duration outlier)`.
- **Validation:** `validate --scan-json` and `rip-watcher` reject fixed/auto titles outside the dominant cluster or on decoy indices.
- **Scan table:** Notes column marks recommended feature and decoy outliers on obfuscated discs.

### Title research checklist for LLM mapper

Use this when **Process 1.5** (`videosripper.map_agent` / Cursor `agent -p`) authors `staging/ready/<disc-id>.yaml` from `staging/pending/*.scan.json`. Wrong title picks waste hours on copy-protect loops, decoy playlists, or scene-jump dummies — research before writing YAML.

**1. Read all inputs**

- `staging/pending/<disc-id>.scan.json` — full title list (`index`, `name`, `duration`, `duration_sec`, `size_bytes`) plus `obfuscation` and `identification` when present.
- `staging/pending/<disc-id>.meta.json` — `disc_name`, `title_count`, `minlength`, flattened `recommended_title` / `decoy_indices`, plus identification fields:
  - `identification.bluray_title` / `bluray_title` — from `BDMV/META/DL/bdmt_*.xml` (strong signal on Blu-ray)
  - `identification.candidates[]` / `title_candidates` — TMDb/OMDb runtime shortlist for generic DVD/BD labels
  - `preferred_id` / `identified_title` when a confident title was inferred
- Scan **Notes** (console `videosripper scan --json` table, or re-run `scan-device --json` and inspect suspicious names / short durations). Flags: `suspicious:`, `short title; later title N`, `recommended feature`, `decoy:`.
- `configs/registry.yaml` — if a similar `disc_name` already has a manifest, copy its `tracks[]` / `skips[]` pattern and episode numbering; do not invent indices from memory.
- This file — season/disc tables and [Title validation](#title-validation-avoid-wrong--copy-protected-titles) rules.

**1b. Generic volume labels (uninformative heuristic)**

MakeMKV's `disc_name` is only the filesystem volume label. Many commercial discs ship with uninformative labels (e.g. `DVD_VIDEO`, `DVDVolume`, `VIDEO_TS`, `BD_VIDEO`, `untitled`) — **examples, not an exhaustive list**. Process 1 marks `identification.is_generic_label` via heuristic matching (normalized patterns + a small boilerplate-token vocabulary), then attaches an `identification` block:

| Source | When | Strength |
|--------|------|----------|
| Blu-ray `bdmt_*.xml` (`di:name`) | Disc mounts with `BDMV/META/DL/` | Strong — prefer as `meta.title` |
| TMDb discover-by-runtime | `VIDEOSRIPPER_TMDB_API_KEY` set; feature duration known | Candidates only — pick among them |
| OMDb title search | `VIDEOSRIPPER_OMDB_API_KEY` set + non-generic hint | Candidates / runtime filter |

Never invent placeholders like `DVD_VIDEO (unknown)`. If hints are empty, leave pending for human identification.

**2. Scene-jump / dummy early titles (DVD TV)**

- Title **0** is usually **Play All** — add to `skips[]`; never map episode slots to it on multi-episode discs.
- First few indices may be **menus, FBI/COPYRIGHT loops, or copy-protect dummies** while the real episode sits at a **higher** index. If title N is suspicious (short, bad name, Notes flag) inspect N+1, N+2, …
- **Multi-angle DVDs:** consecutive even/odd pairs — rip one angle only; document the skipped angle in `skips[]`.
- **Missing menu numbers** (e.g. menu 15 absent, jumps 14→16): real episode may live in **play-all** — use `extract_chapter` (see S3 Disc 1).
- **Paired episodes:** ~20+ min, 2 chapters → `split` at ffprobe chapter boundary.
- Spot-check uncertain mappings: `scripts/videosripper preview --title N --chapter 2 --offset 0 --seconds 45 --scan-json staging/pending/<disc-id>.scan.json` (DVD chapter 2 skips FBI/menu lead-in).

**3. Obfuscated Blu-ray playlists (movies and some TV)**

When `obfuscation.detected` is true (or `title_count` > 50 with many identical names/durations):

- **Dominant cluster:** `duplicate_count` titles share `dominant_duration_sec` — the real feature is one of these, not a duration outlier.
- **`recommended_title`:** lowest index in the dominant cluster — prefer this for `title_selection.fixed` or trust `auto_feature` (it already prefers the cluster).
- **`decoy_indices`:** add **every** index to `skips[]` with reason `decoy playlist (duration outlier)`.
- Do **not** pick a decoy because it is longer/shorter unless preview proves otherwise.
- Document other duplicate indices in `skips[]` as `duplicate playlist (not selected)` when helpful — only the chosen feature index is ripped.
- Example: John Wick Chapter 2 — 330 titles, 321× 2:02:22, decoy at 169 (2:04:08); use title **0** + `auto_feature`, skip decoys.

**4. RCE / copy-protect decoys**

- Reject titles whose **names** match COPYRIGHT, FBI, WARNING, anti-piracy strings (see scan Notes / `title_verify` suspicious-name list).
- Reject durations **below `minlength`** or standalone TV episodes **under ~8 min** after the filter.
- **Movies:** short promos before the feature (Scarface title 0 ~27 min vs title 1 ~2:50 feature) — use `title_selection.mode: auto_feature` with sensible `min_duration_sec` / `min_size_bytes`, or `fixed` at the verified feature index.
- If still uncertain after scan: `scripts/videosripper preview --title N --offset 60 --seconds 45 --scan-json …` and check PASS/FAIL in `logs/debug.log`.

**5. Same-duration scrambled playlists**

- Hundreds of playlists with the **same name and duration** — any index in the dominant cluster may work; **pick `recommended_title`** (lowest index) for reproducibility.
- List non-selected outliers and short menu snippets in `skips[]`; note alternates in a YAML comment above `title_selection` or `tracks` (e.g. `# title 0 chosen; 321 duplicates at 7342s; decoy 169 skipped`).

**6. Required manifest fields**

- Always set `meta.disc_name` from scan `disc_name` (watcher/registry verification) — **even when generic** (uninformative volume label; e.g. `DVD_VIDEO`, `DVDVolume`, `VIDEO_TS`).
- **`meta.id` / `meta.title` / `output.filename` / ready YAML basename = identified work title**, not the disc label.
  - Good: `title: Scarface (1983)`, `id: scarface-1983`, `filename: Scarface (1983).mkv`, `disc_name: DVD_VIDEO`
  - Bad: using a generic volume label as the movie title or output filename
  - **Multi-disc movies:** one manifest per disc; filename `Title (Year) - Disc N.mkv` (see [Multi-disc / multi-part movies](#multi-disc--multi-part-movies)) — rip discs independently; optional lossless stitch is a **post** step, not part of MakeMKV.
- If disc_name is generic (heuristic: uninformative volume label), **identify the movie/show first** using `identification` hints (bdmt title, runtime candidates), then box art / web search before writing YAML. Do not invent placeholder titles for NAS.
- Set `meta.minlength` to match the scan (default 300).
- For movies: `title_selection.mode: auto_feature` unless a specific index is proven; include `validation` floors.
  - **Size floors:** default 1 GiB (`1073741824`) catches empty rips. Do **not** blanket-set 5368709120 (5 GiB) — many DVDs rip at 2–4 GB. Omit `min_size_bytes` or set ~90% of scan feature `size_bytes`. Reserve 5+ GiB floors for large BD/UHD features only.
- For TV: map `tracks[].makemkv_title` to TINFO indices from the scan; `skips[]` for play-all, extras, decoys, and skipped angles.

**7. Validate before ready**

```bash
# Prefer staging/ready/<identified-slug>.yaml (not the generic disc slug):
scripts/videosripper validate staging/ready/<identified-slug>.yaml --with-scan \
  --scan-json staging/pending/<scan-disc-id>.scan.json
```

Fix all errors (unknown title index, decoy pick, below minlength, suspicious name) before Process 2 moves the manifest to `configs/` or rips.

**Optional preview commands**

```bash
# DVD episode spot-check (chapter 2, no FBI lead-in)
scripts/videosripper preview --title 1 --chapter 2 --seconds 45 \
  --scan-json staging/pending/<disc-id>.scan.json

# Blu-ray / long title — skip menu loops
scripts/videosripper preview --title 0 --offset 120 --seconds 60 \
  --scan-json staging/pending/<disc-id>.scan.json --keep

# After draft YAML exists
scripts/videosripper preview --title 3 --config staging/ready/<disc-id>.yaml --offset 60
```

### 3. Create or edit YAML manifest

1. Map each makemkv title → **TheTVDB DVD order** `Nx##` + episode title ([TheTVDB DVD order](https://thetvdb.com/series/spongebob-squarepants/seasons/dvd/)).
2. Add or update `configs/<show>/…yaml` (copy an existing disc manifest as a template).
3. Optional title lists: `scripts/examples/*-episodes.txt`.
4. Validate:

```bash
scripts/videosripper validate configs/spongebob/s8-disc4.yaml
scripts/videosripper validate configs/spongebob/s8-disc4.yaml --with-scan
```

### 4. Dry-run rip plan

```bash
scripts/videosripper rip configs/spongebob/s8-disc4.yaml --dry-run
```

Review slot → makemkv title → output filename mapping before touching the drive.

### 5. Run the rip

```bash
scripts/videosripper rip configs/spongebob/s8-disc4.yaml
```

Run in background for long jobs. Logs append to fixed files under `logs/` (rotate at 50,000 lines; keep `*.log.1`–`*.log.3`):

| File | Use |
|------|-----|
| `logs/rip.log` | Monitor rip progress (`tail -f logs/rip.log`) |
| `logs/debug.log` | Troubleshoot makemkv/ffprobe stalls (`tail -f logs/debug.log`) |
| `logs/scan.log` | Scan title tables (`tail -f logs/scan.log`) |
| `logs/error.log` | ERROR/ALERT + exception tracebacks only (`tail -f logs/error.log`) |

Legacy per-disc logs (e.g. `logs/rip-s8-disc4.log`) are from older runs; ignore for new sessions.

**Resume after failure** (episode slot N, not makemkv title index):

```bash
VIDEOSRIPPER_START_AT=5 scripts/videosripper rip configs/spongebob/s8-disc4.yaml
# or
scripts/videosripper rip configs/spongebob/s8-disc4.yaml --start-at 5
```

The ripper skips slots whose output file already exists under `output/`.

**Full restart** (user deleted output folder):

```bash
pkill -f 'videosripper|makemkvcon mkv.*sr0' || true
rm -rf /mnt/game1/VideosRipper/output/Season\ 8
mkdir -p /mnt/game1/VideosRipper/output/Season\ 8 /mnt/game1/VideosRipper/logs
scripts/videosripper rip configs/spongebob/s8-disc4.yaml
```

Never restart while makemkvcon is mid-rip without killing it first. **Do not** run two rips against `/dev/sr0` at once.

### 6. Verify and NAS

- Each TV episode ~600–700 MB, duration ~640–720 s (except specials/doubles).
- Count matches expected episodes.
- Files on NAS: `ls "/mnt/tn-sas/Anime/Spongebob Squarepants/Season 8/"`.
- Disc ejects when `post.eject: true` (default) after a successful real rip — never on scan, validate, or `--dry-run`. Exhausted disc-open/stall retries also auto-eject for inspection (then exit non-zero).

Movies: `ffprobe` duration and file size before trusting NAS copy (see [Feature films](#feature-films-blu-ray--bd-java)).

## Optional post-processing: DVD upscale (RTX 4090)

**Not implemented in code yet** — research + design notes for a future optional step. Do not upscale during rip; do not touch BD/UHD masters.

### Scope and archive rules

| Rule | Detail |
|------|--------|
| **DVD only** | Consider AI upscale for DVD (≈480i/480p / 576i) sources. **Blu-ray and UHD stay native** — no upscale pass. |
| **Keep the remux** | MakeMKV → MKV remains the archival master (lossless video/audio). Upscale is always a **derivative copy**. |
| **Never replace originals** | Do not overwrite files under `output/` or the Plex library remuxes. Write a new path/name. |
| **HDR** | N/A for DVD (SDR). Do not invent HDR metadata on upscaled DVD derivatives. |
| **Time / disk** | Expect hours–days per feature or season disc on even a 4090; 1080p derivatives are often **several×** the remux size (re-encoded HEVC/AV1). Budget NAS space separately. |

**Suggested paths / names** (pick one convention and stick to it):

```
# Sibling derivative next to remux (local staging for review)
output/Season 8/SpongeBob SquarePants - S08E01 - Accidents Will Happen.mkv          # remux (master)
output/upscaled/Season 8/SpongeBob SquarePants - S08E01 - Accidents Will Happen.upscaled.1080p.mkv

# Or a separate NAS / Plex library root (recommended for “watching” copies)
/mnt/tn-sas/Anime/Spongebob Squarepants Upscaled/Season 8/...upscaled.1080p.mkv
```

### Recommended target resolution

Prefer **1080p** for DVD → archive derivatives (≈3–4× linear from NTSC 480p; best quality / size / time tradeoff on a 4090).

**4K from DVD** mostly synthesizes detail the disc never had. Use only for intentional big-screen vanity copies after checking artifacts on a short sample. For anime line art, aggressive 4× models can look over-sharpened or temporally unstable — prefer 2×→1080p (or 2× then mild encode) over blind 4×→2160p.

### Tool comparison (2025–2026, practical archival DVD use)

| Tool | License | 4090 support | DVD quality (anime / live-action) | Speed ballpark (4090) | CLI / automation |
|------|---------|--------------|-----------------------------------|------------------------|------------------|
| **Topaz Video AI** | Commercial (paid license) | Excellent — CUDA + TensorRT (incl. TensorRT for RTX) | Best overall live-action; Gaia / anime-oriented models for 2D | Proteus-class ~15–25 fps @ 2× from HD benchmarks; DVD→1080p often real-time–ish; DVD→4K / Starlight much slower (minutes per second of film for heavy models) | Strong — bundled `ffmpeg` + `tvai_up`; GUI “Show Export Command”; [topyaz](https://github.com/twardoch/topyaz) Python wrapper |
| **Video2X** (+ Real-ESRGAN / Real-CUGAN / RIFE) | AGPL-3.0 | Vulkan on NVIDIA (Kepler+); 24 GB VRAM comfortable | Strong anime (`realesr-animevideov3`, Real-CUGAN); live-action OK with `realesrgan-plus` / general models | Compact anime models often ~few–tens fps on mid/high GPUs; heavy `realesrgan-plus` can drop to ≪1–few fps | Excellent CLI (`video2x -i … -o … -p realesrgan …`) |
| **VapourSynth + vs-mlrt** | LGPL (plugin) + model licenses | Best open NVIDIA path — TensorRT / TRT-RTX, CUDA ORT, Vulkan | Top-tier if you build a proper chain (QTGMC deinterlace → DPIR/SCUNet denoise → RealESRGAN/waifu2x) | TensorRT on 4090 can be very fast once engines are built; setup cost is high | Scriptable (`.vpy` + `vspipe` → ffmpeg); steeper than Video2X |
| **chaiNNer** | GPL-3.0 | PyTorch CUDA / ONNX / TensorRT | Flexible model zoo (ESRGAN family, etc.); quality depends on chosen models | Competitive with PyTorch/TRT backends; video via Load Video node | GUI-first; **experimental** CLI (`chainner run chain.chn --override …`) — weaker for production automation |
| **ffmpeg libplacebo / CUDA scale** | LGPL (ffmpeg) | CUDA hwaccel / Vulkan libplacebo | **Not AI** — best traditional scalers (`ewa_lanczossharp`, Anime4K shaders). Sharper than bicubic, **no** reconstructed detail | Real-time or faster | Trivial CLI; fine for quick previews, not archival “remaster” quality |
| **SeedVR2** (ComfyUI / research) | Research / model terms (ByteDance Seed) | CUDA; 3B fits 4090; 7B needs careful VRAM | Strong generative restoration (stylized/anime); live-action often still favors Topaz | Slow vs CNN upscalers; batch/chunk oriented | ComfyUI / scripts — powerful but heavy for a rip pipeline |

**Other notes:** RIFE (in Video2X / vs-mlrt) is **frame interpolation**, not upscale — optional after upscale if you want 48/60 fps; usually skip for archival fidelity. Deinterlace DVD **before or inside** the enhancer (Topaz interlaced / Dione-class models, or QTGMC in VapourSynth). Fix anamorphic PAR (store / display square pixels) as part of the derivative encode.

### Recommendation for this workflow

| Role | Choice | Why |
|------|--------|-----|
| **Primary** (quality + 4090 + automatable) | **Topaz Video AI** | Best practical live-action DVD results; first-class Ada / TensorRT; exportable ffmpeg CLI and Python wrappers fit a future VideosRipper post-step |
| **Free / open alternative** | **Video2X** (Real-ESRGAN / Real-CUGAN) | Solid CLI, Linux-friendly, good anime models; no license fee. Step up to **vs-mlrt + TensorRT** when you want maximum open-source NVIDIA throughput and custom deinterlace/denoise chains |

**Suggested models / presets**

- **Live-action DVD:** Topaz **Proteus** (general) or **Iris** (noisy / face-heavy / older DVD); try interlaced-aware path or deinterlace first. Free path: Video2X `realesrgan-plus` or `realesr-generalv3` @ **2× → 1080p**.
- **Animation / anime DVD:** Topaz **Gaia CG / Gaia Animation**; free: Video2X `realesr-animevideov3` or Real-CUGAN. Avoid live-action face models on hard-edged cel animation.
- **Always:** process a **1–2 minute sample** before committing a full feature or season disc.

### Rough CLI examples

**Topaz Video AI** (use Topaz’s bundled ffmpeg; set model dirs; generate exact filter args via GUI → Process → Show Export Command):

```bash
export TVAI_MODEL_DIR="/path/to/Topaz Video AI/models"
export TVAI_MODEL_DATA_DIR="$TVAI_MODEL_DIR"

# Illustrative only — model name / scale / encoder flags must match your Topaz install
"/path/to/Topaz Video AI/ffmpeg" -y -i \
  "output/Season 8/Show - S08E01 - Title.mkv" \
  -vf "tvai_up=model=prob-4:scale=2:device=0:vram=1:instances=1" \
  -c:v hevc_nvenc -preset p5 -cq 18 -c:a copy \
  "output/upscaled/Season 8/Show - S08E01 - Title.upscaled.1080p.mkv"
```

Or batch via [topyaz](https://pypi.org/project/topyaz/): `topyaz video -i in.mkv -o out.mkv -m <model> --scale 2 --codec hevc_nvenc …`

**Video2X** (free path — anime example, 2×):

```bash
video2x -i "output/Season 8/Show - S08E01 - Title.mkv" \
  -o "output/upscaled/Season 8/Show - S08E01 - Title.upscaled.1080p.mkv" \
  -p realesrgan -s 2 --realesrgan-model realesr-animevideov3 -g 0
```

Live-action: swap model to `realesrgan-plus` (or current general model). List GPUs: `video2x --list-gpus`.

**ffmpeg libplacebo** (fast non-AI preview / comparison only):

```bash
ffmpeg -i "in.mkv" -vf "libplacebo=w=1920:h=1080:upscaler=ewa_lanczossharp:normalize_sar=true" \
  -c:v hevc_nvenc -cq 18 -c:a copy "out.libplacebo.1080p.mkv"
```

**vs-mlrt** — build a `.vpy` (deinterlace → TensorRT RealESRGAN) and pipe: `vspipe script.vpy -c y4m - | ffmpeg -i - -i in.mkv -map 0:v -map 1:a -c:v hevc_nvenc …` (engine build is one-time per model/resolution).

### Where it would plug into VideosRipper later (design only)

Natural hook: **after** remux + verify + optional `post.copy_to_nas` — i.e. once `[rip …] DONE` has produced the master MKV. Do **not** fold upscale into MakeMKV stages or the `[rip N/7]` numbered pipeline.

Possible future shapes (none implemented):

1. **Manifest flag** — e.g. `post.upscale: { enabled: true, tool: topaz|video2x, target: 1080p, only_if: dvd }` skipped for BD/UHD manifests.
2. **Separate queue / CLI** — `scripts/videosripper upscale configs/….yaml` or a watcher that consumes finished remuxes from `output/` into `output/upscaled/` (or a dedicated NAS share) so GPU work does not block optical rips.
3. **Library policy** — Plex continues to point at remuxes; upscaled copies live in a second library or `… Upscaled/` tree.

Until then: rip lossless as today; run Topaz or Video2X **manually** on selected DVD remuxes when you want a watchable HD derivative.

## YAML manifest schema

Manifests live under `configs/`. See `configs/defaults.yaml` for shared path defaults.

### TV disc (`kind: tv_disc`)

```yaml
kind: tv_disc
meta:
  id: spongebob-s8-disc1          # unique id
  show: SpongeBob SquarePants
  season: 8
  device: dev:/dev/sr0
  minlength: 300                  # seconds; use same value for scan and rip
output:
  season_dir: Season 8            # under output/
  # log_file: deprecated — rips append to logs/rip.log
nas:
  share: /mnt/tn-sas/Anime/Spongebob Squarepants  # omit or null → paths.NAS_SHARE (same default)
post:
  eject: true                     # default; eject after successful real rip only
  copy_to_nas: true               # rsync to NAS after rip
  # Future (not implemented): optional DVD-only upscale derivative — see
  # "Optional post-processing: DVD upscale (RTX 4090)". Never replace remux.
# optional — defaults match configs/defaults.yaml / env
# retry:
#   count: 3                      # 3 retries = 4 total attempts; then eject + user intervention
#   delay_sec: 30                 # wait between retries (no mid-retry eject)
#   stall_retry: true
#   nas_retries: 1
tracks:
  - { slot: 1, makemkv_title: 1, episode: "01", title: Accidents Will Happen }
  - { slot: 2, makemkv_title: 2, episode: "02", title: The Other Patty }
  # non-contiguous makemkv indices OK (e.g. slot 12 → makemkv_title: 13)
skips:
  - { makemkv_title: 0, reason: play-all (~2:39) }
  - { makemkv_title: 12, reason: bonus (~5 min) }
```

**`tracks[]`:** ordered episode slots (1-based `slot`). Each track rips one makemkv title unless `split` or `extract_chapter` is set. Optional `season:` overrides `meta.season` for output path and NAS subdir (cross-season discs).

**`skips[]`:** documentation only — titles not ripped (play-all, extras).

**Split** — two episodes from one title (S4 Disc 1 title 8 pattern):

```yaml
  - slot: 8
    makemkv_title: 8
    split:
      at_seconds: 662.428433
      outputs:
        - { episode: "08", title: Selling Out }
        - { episode: "09", title: Funny Pants }
```

**Extract chapter** — one episode from play-all (S3 Disc 1 title 30 ch15 pattern):

```yaml
  - slot: 15
    makemkv_title: 30
    episode: "15"
    title: No Weenies Allowed
    extract_chapter:
      chapter_index: 14           # 0-based ffprobe chapter index
```

### stream_defaults (language defaults)

Keep all audio and subtitle tracks in the MKV but set Plex/player defaults:

```yaml
# Anime (Japanese audio + English subs)
stream_defaults:
  keep_all_tracks: true          # MakeMKV app_DefaultSelectionString +sel:all
  audio_default: jpn             # ffmpeg remux sets default audio disposition
  subtitle_default: eng          # ffmpeg remux sets default subtitle disposition

# Live-action TV / English-language movies (English audio, no default subtitle)
stream_defaults:
  keep_all_tracks: true
  audio_default: eng
  # omit subtitle_default (or null) → clear/disable subtitle default disposition

# Foreign-language film (example: French original)
stream_defaults:
  keep_all_tracks: true
  audio_default: fre
  subtitle_default: eng
```

- **`keep_all_tracks`:** temporarily sets `app_DefaultSelectionString` in `~/.MakeMKV/settings.conf` during each title rip (restored after). Applies to **TV and movie** manifests (`runner.py` / `movie.py`).
- **`audio_default` / `subtitle_default`:** post-rip ffmpeg copy remux adjusts MKV default flags only; no re-encode.
- **Omit `subtitle_default`:** when `audio_default` is set and `subtitle_default` is omitted/null, remux clears all subtitle default dispositions (tracks kept, none marked default).
- Anime discs may ship with English audio default — post-process flips default to Japanese while keeping English dubs/subs.
- **Movies:** required for English-language titles. Multi-audio DVDs/BDs often flag a non-English track as disc default (Hudson Hawk DVD: French `SINFO` default; John Wick BD: Spanish). Without `stream_defaults`, that flag wins in the MKV.
- Examples: `configs/anime/naruto-shippuden-set10-disc1.yaml`, `configs/shows/breaking-bad-s1-disc1.yaml`, `configs/movies/hudson-hawk-1991.yaml`.

When local output includes the show name but NAS uses `Season N` only:

```yaml
output:
  season_dir: Naruto Shippuden/Season 12
nas:
  share: /mnt/tn-sas/Anime/Naruto Shippuden
  subdir: Season 12              # NAS dest = share/subdir (not output.season_dir)
```

### Movie (`kind: movie`)

```yaml
kind: movie
meta:
  id: ghost-rider-spirit-of-vengeance
  title: Ghost Rider Spirit of Vengeance (2011)
  device: dev:/dev/sr0
  minlength: 300
  disc_pattern: GHOST_RIDER       # optional scan hint
output:
  dir: Movies
  filename: Ghost Rider Spirit of Vengeance (2011).mkv
  # log_file: deprecated — rips append to logs/rip.log
nas:
  movies: true
  share: /mnt/tn-sas/Movies       # omit or null → paths.MOVIES_NAS (same default)
post:
  eject: true
  copy_to_nas: true
stream_defaults:
  keep_all_tracks: true
  audio_default: eng              # English films; use original lang for foreign films
  # omit subtitle_default → no forced default subtitles
title_selection:
  mode: auto_feature              # pick longest feature title
  min_duration_sec: 4800
  # omit min_size_bytes → 1 GiB default; or ~90% of scan feature size_bytes
validation:
  min_duration_sec: 4800
  # omit min_size_bytes → 1 GiB default
```

Existing manifests: `configs/spongebob/s8-disc{1,2,3}.yaml`, `configs/movies/ghost-rider-spirit-of-vengeance.yaml`, `configs/movies/scarface-1983.yaml`.

### Multi-disc / multi-part movies

Long theatrical features sometimes ship split across physical discs (e.g. **Pearl Harbor (2001)** 60th Anniversary DVD: Disc 1 ≈ first ~129 min, Disc 2 ≈ remainder + extras). The pipeline still models **one disc → one movie manifest → one MKV**. Joining parts into a single file is an **optional post step** after both remuxes exist — same archive philosophy as [DVD upscale](#optional-post-processing-dvd-upscale-rtx-4090): keep per-disc remux masters; treat a stitch as a derivative (or Plex-facing copy), not a replacement of the masters.

#### 1. Per-disc rip first (required)

| Rule | Convention |
|------|------------|
| Manifests | **One YAML per disc** — never combine physical discs in a single MakeMKV rip |
| `meta.id` | `title-year-discN` (e.g. `pearl-harbor-2001-disc1`) |
| `meta.title` / `output.filename` | `Title (Year) - Disc N.mkv` (Plex multi-part naming; see below) |
| `meta.disc_name` | Exact MakeMKV scan label (e.g. `PEARL_HARBOR_DSC1`) |
| Independence | Rip Disc 1 and Disc 2 separately under the normal makemkv lock |
| Consistency | If Disc 2+ arrives after Disc 1, match the existing `Title (Year)` stem and the same `- Disc N` suffix style |
| Validation | Floors are **per-disc** runtime/size (Disc 1 of a split film is not the full theatrical length) |
| `stream_defaults` | Same on every part (e.g. `audio_default: eng`) so track order/defaults match before any stitch |
| `post.copy_to_nas` | **`false`** when you plan to stitch (default for multi-part) — keep per-disc masters on game drive only; copy the stitched file to NAS after join (see [NAS copy policy](#6-nas-copy-policy-default-when-stitching)) |

Example ready basename: `staging/ready/pearl-harbor-2001-disc1.yaml` → `Pearl Harbor (2001) - Disc 1.mkv`.

Per-disc manifest snippet (stitch planned):

```yaml
meta:
  part: 1                        # 1-based; required when stitch.enabled
post:
  copy_to_nas: false   # multi-disc part — masters stay on output/Movies/; NAS gets stitched file only
stitch:
  enabled: true
  group_id: pearl-harbor-2001
  expected_parts: 2
  output_filename: Pearl Harbor (2001).mkv
  copy_to_nas: true
```

**MakeMKV / DVD specifics before you decide to stitch:**

- **Feature split across discs** — Disc 1 ends mid-story; Disc 2 continues the feature (sometimes plus extras). Only stitch the **feature** titles, in play order.
- **Disc 2 = extras only** — common on anniversary sets. Do **not** stitch extras onto the feature; rip extras as separate files (or skip). Lower `--minlength` only if you intentionally want short bonus titles.
- **Different title counts / playlists** — each disc has its own title list; `auto_feature` / validation floors are per disc. Do not assume Disc 2’s title 0 is “part 2 of title 0 on Disc 1.”
- **Seamless branching (mostly BD)** — alternate angles / branching playlists are a **single-disc** playlist problem (prefer the correct `.mpls`), not a multi-disc stitch problem. Do not invent a join to “fix” branching.
- **DVD multi-VOB** — MakeMKV already remuxes a title’s VOB chain into one MKV; you do not need `mkvmerge` “additional parts” for `VTS_01_*.VOB` after a normal title rip.

#### 2. Plex: multi-part files vs one file

[Plex movie naming](https://support.plex.tv/articles/naming-and-organizing-your-movie-media-files/) supports stacked parts in a dedicated movie folder:

```
/Movies/Title (Year)/Title (Year) - disc1.mkv
/Movies/Title (Year)/Title (Year) - disc2.mkv
```

Allowed split tokens (replace `X` with the part number): `cdX`, `discX`, `diskX`, `dvdX`, `partX`, `ptX`. VideosRipper’s `Title (Year) - Disc N.mkv` matches `discX` after normalizing spaces (Plex expects `disc1` / `Disc 1` style suffixes on an otherwise identical stem).

| Approach | When it’s fine | Trade-offs |
|----------|----------------|------------|
| **Separate multi-part files** | Quick path after per-disc rip; keep masters as-is on NAS | Plex support is **limited**: not all apps stack; preview/chapter images, stream selection across parts, and some features degrade; max **8** parts; same container + matching audio/subtitle order preferred |
| **Single combined file** | **Preferred for Plex** when the feature is truly continuous across discs | Best client compatibility; one library item; stitch once after both discs finish |

**Recommendation:** rip and archive per-disc remuxes; for the Movies library, **prefer one stitched file** when Disc 2 continues the feature. Keep multi-part naming only as a temporary/fallback (or if you deliberately refuse to stitch).

#### 3. Lossless stitch (recommended tool + commands)

**Host status (4090 / CachyOS):** `ffmpeg` / `ffprobe` are installed (`/usr/bin/ffmpeg`). **`mkvmerge` is not installed** — install when needed: `sudo pacman -S mkvtoolnix-cli` (package `extra/mkvtoolnix-cli`). Do not install or run heavy package ops mid-rip if the optical pipeline is busy; stitch after both discs are done.

| Tool | Role | Notes |
|------|------|-------|
| **`mkvmerge` (preferred)** | Append Matroska files losslessly | Designed for MKV; preserves chapters better than ffmpeg concat; use `+` to **append** (sequential). Bare `mkvmerge -o out a.mkv b.mkv` **adds in parallel** (wrong). |
| **`ffmpeg` concat demuxer** | Fallback `-c copy` join | Requires **identical** codecs, time base, resolution, frame rate, and matching stream layouts; strips/weakens chapter metadata; timestamp gaps if streams differ in length |
| **Re-encode** | Last resort | Only when codecs/layouts/resolution mismatch; prefer matching remux settings / same `stream_defaults` so this is unnecessary |

**mkvmerge (recommended):**

```bash
# After: sudo pacman -S mkvtoolnix-cli
mkvmerge -o "output/Movies/Title (Year).mkv" \
  "output/Movies/Title (Year) - Disc 1.mkv" \
  + "output/Movies/Title (Year) - Disc 2.mkv"
```

**ffmpeg concat demuxer (fallback when mkvmerge missing):**

```bash
cat > /tmp/stitch-list.txt <<'EOF'
file '/mnt/game1/VideosRipper/output/Movies/Title (Year) - Disc 1.mkv'
file '/mnt/game1/VideosRipper/output/Movies/Title (Year) - Disc 2.mkv'
EOF
ffmpeg -f concat -safe 0 -i /tmp/stitch-list.txt -c copy \
  "output/Movies/Title (Year).mkv"
```

**Helper:** `scripts/stitch-movie-parts.sh` prefers `mkvmerge`, falls back to ffmpeg, and checks that output duration ≈ sum of inputs (default ±2%).

```bash
scripts/stitch-movie-parts.sh \
  -o "output/Movies/Title (Year).mkv" \
  "output/Movies/Title (Year) - Disc 1.mkv" \
  "output/Movies/Title (Year) - Disc 2.mkv"
```

**Validate after stitch:**

```bash
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "…/Disc 1.mkv"
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "…/Disc 2.mkv"
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "…/Title (Year).mkv"
# Expect: duration(out) ≈ duration(d1) + duration(d2)  (small muxer skew OK)
```

**When re-encode is required:** mismatched video codec/profile, resolution/PAR, frame rate, or incompatible audio layouts (e.g. Disc 1 stereo vs Disc 2 5.1 with different codec). Fix by re-ripping with consistent track selection, or encode a derivative (not the archive master).

#### 4. When NOT to stitch

- Disc 2 (or later) is **extras / behind-the-scenes only**
- Parts have **different aspect ratio**, resolution, or video codec
- Audio/subtitle **layouts differ** and cannot be aligned without re-encode
- You only need temporary Plex playback and prefer leaving masters as `… - Disc N.mkv`
- Seamless-branching / alternate-cut selection belongs in the **rip title/playlist choice**, not a join

#### 5. Archival best practice

| Artifact | Keep? | Role |
|----------|-------|------|
| Per-disc remux (`… - Disc N.mkv`) | **Yes — master** | Lossless MakeMKV (+ optional disposition remux); re-stitch anytime |
| Stitched single file (`Title (Year).mkv`) | Optional derivative / Plex copy | Convenience for clients; may live on NAS Movies while masters stay under `output/Movies/` (or a `…/parts/` sibling) |
| Re-encoded join | Avoid unless forced | Not archival |

Mirror the upscale policy: **never delete the remux masters** just because a stitched file exists. If NAS space is tight, prefer masters on game drive + stitched file on `/mnt/tn-sas/Movies/`, or vice versa — but keep at least one lossless per-disc set until you are sure.

#### 6. NAS copy policy (default when stitching)

**Yes — skip NAS for per-disc parts when you plan to stitch.** The rip pipeline honors `post.copy_to_nas` per manifest (`movie.py` / `runner.py`). After all parts exist, the rip-watcher post-rip hook (`videosripper/stitch.py` → `maybe_stitch_after_part`) stitches and copies the combined file when `stitch.copy_to_nas: true`.

| Stage | `post.copy_to_nas` | Where files live |
|-------|-------------------|------------------|
| Per-disc rip (`Title (Year) - Disc N.mkv`) | **`false`** (default for multi-part) | Game drive: `output/Movies/` — archival masters |
| After stitch (`Title (Year).mkv`) | N/A — use `stitch.copy_to_nas: true` | NAS Movies: `/mnt/tn-sas/Movies/` — Plex-facing copy |

**Automatic (watcher):** declare `stitch.copy_to_nas: true` on part manifests; the hook rsyncs after stitch (same as `copy_movie_to_share` / `rsync -ah`).

**Manual fallback** when stitching outside the watcher:

```bash
cd /mnt/game1/VideosRipper

scripts/stitch-movie-parts.sh \
  -o "output/Movies/Pearl Harbor (2001).mkv" \
  "output/Movies/Pearl Harbor (2001) - Disc 1.mkv" \
  "output/Movies/Pearl Harbor (2001) - Disc 2.mkv"

rsync -ah --info=progress2 \
  "output/Movies/Pearl Harbor (2001).mkv" \
  "/mnt/tn-sas/Movies/"
```

**Optional redundancy (not default):** set `post.copy_to_nas: true` on part manifests if you want Plex multi-part files on NAS *without* stitching, or if you want both per-disc copies and a stitched file on NAS. That doubles NAS usage for the feature.

#### 7. Pipeline hook (implemented)

After each part rip completes (`[rip 7/7] DONE`), the rip-watcher calls `maybe_stitch_after_part()` **outside the makemkv lock**. It does **not** run inside `rip_movie()`.

```yaml
kind: movie
meta:
  id: pearl-harbor-2001-disc1
  title: Pearl Harbor (2001)
  disc_name: PEARL_HARBOR_DSC1
  part: 1                        # 1-based disc number
output:
  dir: Movies
  filename: Pearl Harbor (2001) - Disc 1.mkv
post:
  copy_to_nas: false             # per-disc parts: no NAS
  eject: true
stitch:
  enabled: true
  group_id: pearl-harbor-2001    # links parts (defaults: meta.id minus -discN)
  expected_parts: 2              # wait until N part MKVs exist on disk
  output_filename: Pearl Harbor (2001).mkv
  copy_to_nas: true              # copy stitched file to NAS after join
nas:
  movies: true
  share: /mnt/tn-sas/Movies
```

**Every part manifest** declares the same `stitch` block (consistent `group_id`, `expected_parts`, `output_filename`). Only `meta.part` and per-disc `output.filename` / `meta.id` differ.

Pipeline stages (post-rip, unnumbered):

| Stage | When |
|-------|------|
| `STITCH_WAITING` | Part rip done; fewer than N parts on disk (`1/2`, etc.) |
| `STITCH` | All N parts present — stitch starting |
| `STITCH_OK` | Combined MKV written |
| `STITCH_FAILED` | mkvmerge/ffmpeg or duration check failed |
| `COPY` / `COPY_OK` | Stitched file rsync to NAS (`stitch.copy_to_nas: true`) |

Design notes:

- **Ordering / locking:** stitch only when all part files exist; never hold the makemkv lock; run after `[rip …] DONE`.
- **Validation:** `duration(out) ≈ Σ duration(inputs)` within `stitch.tolerance_pct` (default 2%); codec/layout warnings via ffprobe.
- **Tool:** `stitch_movie_parts()` prefers `mkvmerge`, falls back to ffmpeg concat demuxer (`-c copy`).
- **State:** `staging/stitch/{group_id}.json` tracks registered parts; discovery also globs `Title (Year) - Disc N.mkv` on disk.
- **Manual fallback:** `scripts/stitch-movie-parts.sh` for ad-hoc joins outside the watcher.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `VIDEOSRIPPER_ROOT` | `/mnt/game1/VideosRipper` | Base path |
| `VIDEOSRIPPER_OUTPUT` | `$ROOT/output` | Final MKV output |
| `VIDEOSRIPPER_STAGING` | `$ROOT/staging` | Temp rip dirs |
| `VIDEOSRIPPER_TMP` | `$ROOT/.tmp` | Ephemeral work (identify mounts); keep off `/tmp` tmpfs |
| `VIDEOSRIPPER_LOGS` | `$ROOT/logs` | Log files |
| `VIDEOSRIPPER_NAS` | `/mnt/tn-sas/Anime/Spongebob Squarepants` | SpongeBob NAS base (Anime share) |
| `VIDEOSRIPPER_SHOWS_NAS` | `/mnt/tn-sas/Shows` | Live-action TV Shows share root |
| `VIDEOSRIPPER_MOVIES_NAS` | `/mnt/tn-sas/Movies` | Feature-film NAS destination |
| `VIDEOSRIPPER_DEVICE` | `dev:/dev/sr0` | MakeMKV source (overrides manifest when set) |
| `VIDEOSRIPPER_START_AT` | `1` | Resume TV rip from episode **slot** N |
| `VIDEOSRIPPER_STALL_TIMEOUT_SEC` | `900` | No PRGV/file growth during title rip before stall alert (15 min) |
| `VIDEOSRIPPER_OPEN_TIMEOUT_SEC` | `600` | Preflight disc-open timeout; stall limit before first PRGV (10 min) |
| `VIDEOSRIPPER_ON_STALL` | `exit` | On stall: `exit` (kill makemkv) or `warn` (log only) |
| `VIDEOSRIPPER_RETRY_COUNT` | `3` | Retries after first failure (**3 retries = 4 total attempts**) before user intervention |
| `VIDEOSRIPPER_RETRY_DELAY_SEC` | `30` | Seconds to wait before retry (preflight / title rip / NAS); disc stays loaded |
| `VIDEOSRIPPER_STALL_RETRY` | `true` | Retry same title after stall (uses retry count) |
| `VIDEOSRIPPER_NAS_RETRIES` | `1` | Extra rsync attempts; on final fail log ALERT and continue rip |

CLI: `--retry N`, `--retry-delay SEC`. Optional per-manifest `monitor:` / `retry:` blocks (see `configs/defaults.yaml`) override defaults when env/CLI unset.

**Retry semantics:** `count` / `VIDEOSRIPPER_RETRY_COUNT` is the number of *retries after the first failure*, not total attempts. Default `3` → initial attempt + 3 retries = **4 tries** before giving up. After exhaustion on disc open / Failed to open disc / stall, VideosRipper logs `=== ALERT === USER INTERVENTION REQUIRED`, **auto-ejects** the disc (so you can clean/re-seat it), and exits non-zero. Re-insert and re-run (TV: `--start-at N`). The disc is **not** ejected between retries.

## Completed discs (historical reference)

These tables record episode↔makemkv title mappings from finished rips. **Legacy bash scripts** referenced by filename (e.g. `rip-s6-disc.sh`) live in [`scripts/archive/`](scripts/archive/) — see [`scripts/archive/README.md`](scripts/archive/README.md). **YAML manifests** exist for Season 8 discs 1–3 and Ghost Rider; resume those with `scripts/videosripper rip configs/…yaml`. For seasons without manifests, resume archived bash with `VIDEOSRIPPER_START_AT` (examples below).

### Season 6

### Vol 1 Disc 1 — `scripts/archive/rip-s6-disc.sh` (14 titles, makemkv 0–13)

| Idx | File | Episode |
|-----|------|---------|
| 0 | 6x05 | Spongicus |
| 1 | 6x06 | Suction Cup Symphony |
| 2 | 6x01 | House Fancy |
| 3 | 6x02 | Krabby Road |
| 4 | 6x03 | Penny Foolish |
| 5 | 6x04 | Nautical Novice |
| 6 | 6x07 | Not Normal |
| 7 | 6x08 | Gone |
| 8 | 6x21 | SpongeBob SquarePants vs. The Big One (~22 min) |
| 9 | 6x11 | A Life in a Day |
| 10 | 6x12 | Sun Bleached |
| 11 | 6x13 | Giant Squidward |
| 12 | 6x14 | No Nose Knows |
| 13 | 6x15 | Patty Caper |

### Vol 1 Disc 2 — `scripts/archive/rip-s6-disc2.sh` (10 titles, makemkv 0–9)

| Idx | File | Episode |
|-----|------|---------|
| 0 | 6x16 | Plankton's Regular |
| 1 | 6x31 | To SquarePants or Not to SquarePants |
| 2 | 6x30 | Squid's Visit |
| 3 | 6x09 | The Splinter |
| 4 | 6x10 | Slide Whistle Stooges |
| 5 | 6x17 | Boating Buddies |
| 6 | 6x18 | The Krabby Kronicle |
| 7 | 6x19 | The Slumber Party |
| 8 | 6x20 | Grooming Gary |
| 9 | 6x44 | Truth or Square (~59 min) |

### Vol 2 Disc 1 — `scripts/archive/rip-s6-disc3.sh` (12 titles, makemkv 1–12; skip 0 play-all) ✅ **Completed 2026-07-03**

All 12 episodes ripped to `output/Season 6/` and copied to NAS. Log: `logs/rip-disc3.log`. Renamed to DVD order 2026-07-03.

| Idx | File | Episode |
|-----|------|---------|
| 1 | 6x22 | Porous Pockets |
| 2 | 6x23 | Choir Boys |
| 3 | 6x24 | Krusty Krushers |
| 4 | 6x25 | The Card |
| 5 | 6x26 | Dear Vikings |
| 6 | 6x27 | Ditchin' |
| 7 | 6x45 | Pineapple Fever |
| 8 | 6x46 | Chum Caverns |
| 9 | 6x28 | Grandpappy the Pirate |
| 10 | 6x29 | Cephalopod Lodge |
| 11 | 6x32 | Shuffleboarding |
| 12 | 6x33 | Professor Squidward |

**Disc scan notes for Vol 2 Disc 1:**

- Title 0 = Play All (2:20:15) — do not rip.
- Title 13 = ~8 min extra — skip.
- Title 12 ripped as **1188 s (~20 min)** single MKV — longer than a normal ep; verify content if needed.

### Vol 2 Disc 2 — `scripts/archive/rip-s6-disc4.sh` (11 titles, makemkv 1–11; skip 0 play-all, 12–13 extras) ✅ **Completed 2026-07-03**

All 11 episodes ripped to `output/Season 6/` and copied to NAS. Log: `logs/rip-disc4.log`.

| Idx | File | Episode |
|-----|------|---------|
| 1 | 6x34 | Pet or Pests |
| 2 | 6x35 | Komputer Overload |
| 3 | 6x36 | Gullible Pants |
| 4 | 6x37 | Overbooked |
| 5 | 6x38 | No Hat for Pat |
| 6 | 6x39 | Toy Store of Doom |
| 7 | 6x47 | The Clash of Triton (~22 min) |
| 8 | 6x40 | Sand Castles in the Sand |
| 9 | 6x41 | Shell Shocked |
| 10 | 6x42 | Chum Bucket Supreme |
| 11 | 6x43 | Single Cell Anniversary (~19 min) |

**Disc scan notes for Vol 2 Disc 2:**

- Title 0 = Play All (2:20:01) — do not rip.
- Title 7 = ~22 min — Clash of Triton TV movie.
- Title 12 (~8 min) and title 13 (~5 min) — DVD shorts/extras — skip.

**Big One vs Krusty Krushers:** both were briefly `6x21` under aired order; DVD order fixes this — Big One **6x21**, Krusty Krushers **6x24**.

## Season 3 — Complete Third Season ✅ **All 3 discs ripped (37 files, 2026-07-04)**

### Disc 1 — `scripts/archive/rip-s3-disc1.sh` (16 episodes; makemkv 1–26 angle 1, play-all ch15, title 28) ✅ **Fixed 2026-07-04**

Multi-angle DVD: each episode has two makemkv titles (language variants) — script rips angle 1 only. **Menu 15 has no standalone title** (jumps 14→16); **3x15 No Weenies Allowed** is extracted from play-all title 30 chapter 15. Title 29 is angle 2 of Squilliam Returns — skip. Skip titles 31 (play-all angle 2), 32–33 (extras).

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 3x01 | The Algae's Always Greener |
| 2 | 2 | 3x02 | SpongeGuard on Duty |
| 3 | 4 | 3x03 | Club SpongeBob |
| 4 | 6 | 3x04 | My Pretty Seahorse |
| 5 | 8 | 3x05 | Just One Bite |
| 6 | 10 | 3x06 | The Bully |
| 7 | 12 | 3x07 | Nasty Patty |
| 8 | 14 | 3x08 | Idiot Box |
| 9 | 16 | 3x09 | Mermaid Man and Barnacle Boy IV |
| 10 | 18 | 3x10 | Doing Time |
| 11 | 20 | 3x11 | Snowball Effect |
| 12 | 22 | 3x12 | One Krabs Trash |
| 13 | 24 | 3x13 | As Seen on TV |
| 14 | 26 | 3x14 | Can You Spare a Dime? |
| 15 | 30 ch15 | 3x15 | No Weenies Allowed (play-all extract) |
| 16 | 28 | 3x16 | Squilliam Returns |

Resume: `VIDEOSRIPPER_START_AT=5 bash /mnt/game1/VideosRipper/scripts/archive/rip-s3-disc1.sh` (episode slot 1–16).

### Disc 2 — `scripts/archive/rip-s3-disc2.sh` (14 episodes, DVD 3x17–3x30; makemkv 1,2,4,6,8,10,12,14,16,18,20,22,24,26) ✅ **Completed 2026-07-04**

All 14 episodes ripped to `output/Season 3/` and copied to NAS (30 S3 files total with Disc 1). Log: `logs/rip-s3-disc2.log`.

Disc menu lists 16 segments; **Party Pooper Pants** and **Ugh** are ~21 min specials (one file each). Skip play-all (28–29) and extras (30–31).

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 3x17 | Krab Borg |
| 2 | 2 | 3x18 | Rock-a-Bye Bivalve |
| 3 | 4 | 3x19 | Wet Painters |
| 4 | 6 | 3x20 | Krusty Krab Training Video |
| 5 | 8 | 3x21 | Party Pooper Pants (~22 min) |
| 6 | 10 | 3x22 | Chocolate with Nuts |
| 7 | 12 | 3x23 | Mermaid Man and Barnacle Boy V |
| 8 | 14 | 3x24 | New Student Starfish |
| 9 | 16 | 3x25 | Clams |
| 10 | 18 | 3x26 | Ugh (~21 min) |
| 11 | 20 | 3x27 | The Great Snail Race |
| 12 | 22 | 3x28 | Mid-Life Crustacean |
| 13 | 24 | 3x29 | Born Again Krabs |
| 14 | 26 | 3x30 | I Had an Accident |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s3-disc2.sh` (slot 1–14).

### Disc 3 — `scripts/archive/rip-s3-disc3.sh` (7 episodes, DVD 3x31–3x37; makemkv 0,2,4,6,8,10,12)

Multi-angle DVD: each menu has two makemkv titles — script rips **angle 1 only** (even indices 0,2,4…). Old mapping 1–7 only covered menus 01–04 and missed 3x35–3x37. Title 8 is ~22 min special **The Sponge Who Could Fly**. Skip play-all (14–15), bonus Help Wanted (16), extras (17+).

| Slot | MkIdx | Menu | File | Episode |
|------|-------|------|------|---------|
| 1 | 0 | 01 | 3x31 | Krabby Land |
| 2 | 2 | 02 | 3x32 | The Camping Episode |
| 3 | 4 | 03 | 3x33 | Missing Identity |
| 4 | 6 | 04 | 3x34 | Plankton's Army |
| 5 | 8 | 05 | 3x35 | The Sponge Who Could Fly (~22 min) |
| 6 | 10 | 06 | 3x36 | SpongeBob Meets the Strangler |
| 7 | 12 | 07 | 3x37 | Pranks a Lot |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s3-disc3.sh` (slot 1–7). Completing this disc finishes Season 3 (37 files total).

## Season 4 — Complete Fourth Season (38 episodes DVD order)

### Disc 1 — `scripts/archive/rip-s4-disc1.sh` (9 episodes, DVD 4x01–4x09; makemkv 1–8) ✅ **Completed 2026-07-04**

All 9 episodes on local output and NAS. Log: `logs/rip-s4-disc1.log`.

**Disc quirks:** Makemkv titles 4 and 5 are swapped vs menu labels (Krabs/Snail). **Title 8** is one file with two chapters — Selling Out then Funny Pants; script splits at ~662s into `4x08` and `4x09`. Skip makemkv title 9 (resolves to play-all).

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 4x01 | Fear of a Krabby Patty |
| 2 | 2 | 4x02 | Shell of a Man |
| 3 | 3 | 4x03 | The Lost Mattress |
| 4 | 4 | 4x05 | Have You Seen This Snail? (~22 min) |
| 5 | 5 | 4x04 | Krabs vs. Plankton |
| 6 | 6 | 4x06 | Skill Crane |
| 7 | 7 | 4x07 | Good Neighbors |
| 8 | 8 ch1 | 4x08 | Selling Out |
| 9 | 8 ch2 | 4x09 | Funny Pants |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s4-disc1.sh` (slot 1–9).

### Disc 2 — `scripts/archive/rip-s4-disc2.sh` (9 episodes; makemkv 0–8)

Scanned as `SBSP_S4_V1_D2`. No multi-angle dupes. **TheTVDB DVD numbers skip** (4x17, 4x19–4x21 are on other discs). Title 0 is ~23 min **Dunces and Dragons** (one file, 4x10). Title 8 is Wishing You Well + credits — split at ~663s for 4x22. Skip play-all (9), animatic/extras (10–11).

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 0 | 4x10 | Dunces and Dragons (~23 min) |
| 2 | 1 | 4x12 | Mermaid Man and Barnacle Boy VI: The Motion Picture |
| 3 | 2 | 4x11 | Enemy-in-Law |
| 4 | 3 | 4x13 | Patrick SmartPants |
| 5 | 4 | 4x14 | SquidBob TentaclePants |
| 6 | 5 | 4x15 | Krusty Towers |
| 7 | 6 | 4x16 | Mrs. Puff, You're Fired |
| 8 | 7 | 4x18 | Ghost Host |
| 9 | 8 ch1 | 4x22 | Wishing You Well |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s4-disc2.sh` (slot 1–9).

### Disc 3 — `scripts/archive/rip-s4-disc3.sh` (10 episodes; makemkv 1–10)

Scanned as `SBSP_S4_V2_D1`. Skip play-all title 0. Title 10 is That's No Lady + credits — split at ~643s for 4x28.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 4x17 | Chimps Ahoy |
| 2 | 2 | 4x19 | Whale of a Birthday |
| 3 | 3 | 4x20 | Karate Island |
| 4 | 4 | 4x21 | All That Glitters |
| 5 | 5 | 4x23 | New Leaf |
| 6 | 6 | 4x24 | Once Bitten |
| 7 | 7 | 4x25 | Bummer Vacation |
| 8 | 8 | 4x26 | Wigstruck |
| 9 | 9 | 4x27 | Squidtastic Voyage |
| 10 | 10 ch1 | 4x28 | That's No Lady |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s4-disc3.sh` (slot 1–10).

### Disc 4 — `scripts/archive/rip-s4-disc4.sh` (10 episodes; makemkv 1–10) ✅ **Completed 2026-07-04**

Scanned as `SBSP_S4_V2_D2`. **Season 4 complete — 38 files** on NAS. Log: `logs/rip-s4-disc4.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 4x30 | Hocus Pocus |
| 2 | 2 | 4x29 | The Thing |
| 3 | 3 | 4x31 | Driven to Tears |
| 4 | 4 | 4x32 | Rule of Dumb |
| 5 | 5 | 4x34 | Best Frenemies |
| 6 | 6 | 4x33 | Born to Be Wild |
| 7 | 7 | 4x36 | Squid Wood |
| 8 | 8 | 4x35 | The Pink Purloiner |
| 9 | 9 | 4x37 | Best Day Ever |
| 10 | 10 ch1 | 4x38 | The Gift of Gum |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s4-disc4.sh` (slot 1–10).

## Season 7 — Complete Seventh Season (50 episodes DVD order)

### Disc 1 — `scripts/archive/rip-s7-disc1.sh` (14 episodes, DVD 7x01–7x14; makemkv 1–14) ✅ **Completed 2026-07-05**

All 14 episodes on NAS. Log: `logs/rip-s7-disc1.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1–13 | 1–13 | 7x01–7x13 | Tentacle-Vision → The Curse of Bikini Bottom |
| 14 | 14 ch1 | 7x14 | Squidward in Clarinetland |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s7-disc1.sh` (slot 1–14).

### Disc 2 — `scripts/archive/rip-s7-disc2.sh` (11 episodes, DVD 7x15–7x25; makemkv 1–11) ✅ **Completed 2026-07-05**

All 11 episodes on NAS (25 S7 files with Disc 1). Log: `logs/rip-s7-disc2.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 7x15 | SpongeBob's Last Stand (~23 min) |
| 2 | 2 | 7x16 | Back to the Past (~15 min) |
| 3 | 3 | 7x17 | The Bad Guy Club for Villains (~8 min) |
| 4–10 | 4–10 | 7x18–7x24 | A Day Without Tears → Gramma's Secret Recipe |
| 11 | 11 ch1 | 7x25 | The Cent of Money |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s7-disc2.sh` (slot 1–11).

### Disc 3 — `scripts/archive/rip-s7-disc3.sh` (13 episodes, DVD 7x26–7x38; makemkv 1–13) ✅ **Completed 2026-07-05**

All 13 episodes on NAS (38 S7 files with Discs 1–2). Log: `logs/rip-s7-disc3.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1–6 | 1–6 | 7x26–7x31 | Monster Who Came to Bikini Bottom → Sponge-Cano! |
| 7 | 7 | 7x32 | The Great Patty Caper (~22 min) |
| 8–12 | 8–12 | 7x33–7x37 | That Sinking Feeling → The Abrasive Side |
| 13 | 13 ch1 | 7x38 | Earworm |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s7-disc3.sh` (slot 1–13).

### Disc 4 — `scripts/archive/rip-s7-disc4.sh` (12 episodes, DVD 7x39–7x50; makemkv 1–12) ✅ **Completed 2026-07-05**

**Season 7 complete — 50 files** on NAS. Log: `logs/rip-s7-disc4.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1–11 | 1–11 | 7x39–7x49 | Hide and Then What Happens? → Big Sister Sam |
| 12 | 12 ch1 | 7x50 | Perfect Chemistry |

Resume: `VIDEOSRIPPER_START_AT=N bash /mnt/game1/VideosRipper/scripts/archive/rip-s7-disc4.sh` (slot 1–12).

## Season 8 — Eighth Season (47 episodes DVD order)

Manifests: `configs/spongebob/s8-disc{1,2,3,4}.yaml`. Legacy bash equivalents in `scripts/archive/rip-s8-disc*.sh`.

### Disc 1 — `configs/spongebob/s8-disc1.yaml` (11 episodes, DVD 8x01–8x11; makemkv 1–11) ✅ **Completed 2026-07-05**

All 11 episodes on NAS. Log: `logs/rip-s8-disc1.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1–6 | 1–6 | 8x01–8x06 | Accidents Will Happen → Sentimental Sponge |
| 7 | 7 | 8x07 | Frozen Face-Off (~22 min) |
| 8–11 | 8–11 | 8x08–8x11 | Squidward's School for Grown-Ups → The Googly Artiste |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s8-disc1.yaml`

### Disc 2 — `configs/spongebob/s8-disc2.yaml` (12 episodes, DVD 8x12–8x23; makemkv 1–11, 13) ✅ **Completed 2026-07-05**

All 12 episodes on NAS (23 S8 files with Disc 1). Log: `logs/rip-s8-disc2.log`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 8x12 | A SquarePants Family Vacation (~23 min) |
| 2–5 | 2–5 | 8x13–8x16 | Patrick's Staycation → Mr. Krabs Takes a Vacation |
| 6 | 6 | 8x17 | Ghoul Fools (~22 min) |
| 7–11 | 7–11 | 8x18–8x22 | Mermaid Man Begins → House Sittin' for Sandy |
| 12 | 13 | 8x23 | Smoothe Jazz at Bikini Bottom |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s8-disc2.yaml`

### Disc 3 — `configs/spongebob/s8-disc3.yaml` (14 episodes, DVD 8x24–8x37; makemkv 1–11, 13–15) ✅ **Completed 2026-07-05**

Scanned as `SPONGEBOB_SQUAREPANTS_S8`. Skip play-all title 0 (2:39:40), bonus title 12 (~5 min). Log: `logs/rip-s8-disc3.log`. Archived bash: `scripts/archive/rip-s8-disc3.sh`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1–11 | 1–11 | 8x24–8x34 | Bubble Troubles → Karen 2.0 |
| 12–14 | 13–15 | 8x35–8x37 | InSPONGEiac → Glove World R.I.P. |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s8-disc3.yaml`

### Disc 4 — `configs/spongebob/s8-disc4.yaml` (10 episodes, DVD 8x38–8x47; makemkv 1–10) 🔄 **In progress — Python CLI**

Scanned as `SPONGEBOB_SQUAREPANTS_S8`. Skip play-all title 0 (2:19:06), bonus title 11 (~5 min). Log: `logs/rip-s8-disc4.log`. Completing this disc finishes Season 8 (47 files total on NAS).

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 1 | 8x38 | Squiditis |
| 2 | 2 | 8x39 | Demolition Doofus |
| 3 | 3 | 8x40 | Treats! |
| 4 | 4 | 8x41 | For Here or to Go |
| 5 | 5 | 8x42 | It's a SpongeBob Christmas! (~24 min) |
| 6 | 6 | 8x43 | Super Evil Aquatic Villain Team Up is Go! |
| 7 | 7 | 8x44 | Chum Fricassee |
| 8 | 8 | 8x45 | The Good Krabby Name |
| 9 | 9 | 8x46 | Move It or Lose It |
| 10 | 10 | 8x47 | Hello Bikini Bottom! (~22 min) |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s8-disc4.yaml`

## Season 9 — Ninth Season (49 episodes DVD order)

Manifests: `configs/spongebob/s9-disc{1,2,3,4}.yaml`.

### Disc 1 — `configs/spongebob/s9-disc1.yaml` (13 episodes, DVD 9x01–9x13; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S9 D1`. Episodes are **paired on 2-chapter titles** (split at ffprobe chapter boundary); title 6 is single ~22 min special. Skip play-all TINFO 7 (2:34:17), bonus MSG titles 14+16 (~5 min, filtered by `--minlength=300`). Log: `logs/rip-s9-disc1.log`. Output: `output/Season 9/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 9/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 710.94s | 9x01–9x02 | Extreme Spots / Squirrel Record |
| 2 | 1 | split 655.82s | 9x03–9x04 | Patrick-Man! / Gary's New Toy |
| 3 | 2 | split 659.69s | 9x05–9x06 | License to Milkshake / Squid Baby |
| 4 | 3 | split 660.03s | 9x07–9x08 | Little Yellow Book / Bumper to Bumper |
| 5 | 4 | split 655.82s | 9x09–9x10 | Eek, an Urchin! / Squid Defense |
| 6 | 5 | split 659.69s | 9x11–9x12 | Jailbreak! / Evil Spatula |
| 7 | 6 | — | 9x13 | It Came from Goo Lagoon (~22 min) |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s9-disc1.yaml`

### Disc 2 — `configs/spongebob/s9-disc2.yaml` (13 episodes, DVD 9x14–9x26; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S9 D2`. Episodes are **paired on 2-chapter titles** (split at ffprobe chapter boundary); title 3 is single ~22 min special (9x20). Skip play-all TINFO 7 (2:33:16), bonus MSG titles 14+16 (~5 min, filtered by `--minlength=300`). Log: `logs/rip-s9-disc2.log`. Output: `output/Season 9/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 9/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 710.61s | 9x14–9x15 | Safe Deposit Krabs / Plankton's Pet |
| 2 | 1 | split 658.32s | 9x16–9x17 | Don't Look Now / Séance Shméance |
| 3 | 2 | split 660.09s | 9x18–9x19 | Kenny the Cat / Yeti Krabs |
| 4 | 3 | — | 9x20 | SpongeBob You're Fired (~22 min) |
| 5 | 4 | split 646.61s | 9x21–9x22 | Lost in Bikini Bottom / Tutor Sauce |
| 6 | 5 | split 651.32s | 9x23–9x24 | Squid Plus One / The Executive Treatment |
| 7 | 6 | split 655.59s | 9x25–9x26 | Company Picnic / Pull Up a Barrel |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s9-disc2.yaml`

### Disc 3 — `configs/spongebob/s9-disc3.yaml` (14 episodes, DVD 9x27–9x40; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S9 D3`. All 7 slots are **paired on 2-chapter titles** (split at ffprobe chapter boundary). Skip play-all TINFO 7 (2:33:31), bonus MSG titles 14+16 (~5 min, 298s; filtered by `--minlength=300`). Log: `logs/rip-s9-disc3.log`. Output: `output/Season 9/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 9/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 706.41s | 9x27–9x28 | Sanctuary! / What's Eating Patrick? |
| 2 | 1 | split 652.62s | 9x29–9x30 | Patrick! The Game / The Sewers of Bikini Bottom |
| 3 | 2 | split 657.62s | 9x31–9x32 | SpongeBob LongPants / Larry's Gym |
| 4 | 3 | split 652.65s | 9x33–9x34 | The Fish Bowl / Married to Money |
| 5 | 4 | split 660.63s | 9x35–9x36 | Mall Girl Pearl / Two Thumbs Down |
| 6 | 5 | split 653.85s | 9x37–9x38 | Sharks vs. Pods / CopyBob DittoPants |
| 7 | 6 | split 651.62s | 9x39–9x40 | Sold! / Lame and Fortune |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s9-disc3.yaml`

### Disc 4 — `configs/spongebob/s9-disc4.yaml` (9 episodes, DVD 9x41–9x49; makemkv TINFO 0–4) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S9 D4`. Title 0 is single ~22 min special (9x41); titles 1–4 are **paired on 2-chapter titles** (split at ffprobe chapter boundary). Skip play-all TINFO 5 (1:50:21), bonus Krabby Patty shorts TINFO 6 (~6:37, 5 chapters), MSG titles 14+16 (~3:35, 215s; filtered by `--minlength=300`). Log: `logs/rip-s9-disc4.log`. Output: `output/Season 9/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 9/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | — | 9x41 | Goodbye, Krabby Patty? (~22 min) |
| 2 | 1 | split 658.69s | 9x42–9x43 | Sandy's Nutmare / Bulletin Board |
| 3 | 2 | split 659.19s | 9x44–9x45 | Food Con Castaways / Snail Mail |
| 4 | 3 | split 658.99s | 9x46–9x47 | Pineapple Invasion / Salsa Imbecilicus |
| 5 | 4 | split 658.44s | 9x48–9x49 | Mutiny on the Krusty / The Whole Tooth |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s9-disc4.yaml`

**Season 9 status:** All 4 discs complete — **49** DVD-order episodes in `output/Season 9/` and NAS.

## Season 10 — Tenth Season (22 episodes DVD order)

Manifests: `configs/spongebob/s10-disc{1,2}.yaml`.

### Disc 1 — `configs/spongebob/s10-disc1.yaml` (14 episodes, DVD 10x01–10x14; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S10 D1`. All 7 slots are **paired on 2-chapter titles** (split at ffprobe chapter boundary). Skip play-all TINFO 7 (2:34:13), bonus TINFO 8 (~5:05), MSG titles 14+16 (~5:05 duplicate). Log: `logs/rip-s10-disc1.log`. Output: `output/Season 10/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 10/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 712.28s | 10x01–10x02 | Whirly Brains / Mermaid Pants |
| 2 | 1 | split 661.33s | 10x03–10x04 | Unreal Estate / Code Yellow |
| 3 | 2 | split 661.49s | 10x05–10x06 | Mimic Madness / House Worming |
| 4 | 3 | split 656.41s | 10x07–10x08 | Snooze You Lose / Krusty Katering |
| 5 | 4 | split 661.33s | 10x09–10x10 | SpongeBob's Place / Plankton Gets the Boot |
| 6 | 5 | split 655.32s | 10x11–10x12 | Life Insurance / Burst Your Bubble |
| 7 | 6 | split 654.32s | 10x13–10x14 | Plankton Retires / Trident Trouble |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s10-disc1.yaml`

### Disc 2 — `configs/spongebob/s10-disc2.yaml` (8 episodes, DVD 10x15–10x22; makemkv TINFO 0–3) 🔄 **In progress — Python CLI**

Scanned as `SPONGEBOB SQUAREPANTS S10 D2`. All 4 slots are **paired on 2-chapter titles** (split at ffprobe chapter boundary). Skip play-all TINFO 4 (1:28:27), bonus MSG titles 14+16 (~3 min, 178s; filtered by `--minlength=300`). Log: `logs/rip-s10-disc2.log`. Output: `output/Season 10/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 10/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 705.94s | 10x15–10x16 | The Incredible Shrinking Sponge / Sportz? |
| 2 | 1 | split 651.32s | 10x17–10x18 | The Getaway / Lost and Found |
| 3 | 2 | split 659.33s | 10x19–10x20 | Patrick's Coupon / Out of the Picture |
| 4 | 3 | split 946.40s | 10x21–10x22 | Feral Friends / Don't Wake Patrick |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s10-disc2.yaml`

**Season 10 status:** Disc 1 complete (14 episodes). Disc 2 in progress (8 episodes, 10x15–10x22). Completing Disc 2 finishes Season 10 (22 files total on NAS).

## Season 11 — Eleventh Season (50 episodes DVD order)

Manifests: `configs/spongebob/s11-disc{1,2,3}.yaml` (as authored).

### Disc 1 — `configs/spongebob/s11-disc1.yaml` (17 episodes, DVD 11x01–11x17; makemkv TINFO 0–8) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S11 D1`. TINFO 0–3 and 5–8 are **paired on 2-chapter titles** (split at ffprobe chapter boundary); TINFO 4 is single ~22 min special **The Legend of Boo-Kini Bottom** (no chapter metadata in rip). Skip play-all TINFO 9 (3:18:03), bonus TINFO 10 (~6:49, 409s). Log: `logs/rip-s11-disc1.log`. Output: `output/Season 11/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 11/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 712.63s | 11x01–11x02 | Cave Dwelling Sponge / The Clam Whisperer |
| 2 | 1 | split 658.49s | 11x03–11x04 | Spot Returns / The Check-Up |
| 3 | 2 | split 657.36s | 11x05–11x06 | Spin the Bottle / There's a Sponge in My Soup |
| 4 | 3 | split 659.41s | 11x07–11x08 | Man Ray Returns / Larry the Floor Manager |
| 5 | 4 | — | 11x09 | The Legend of Boo-Kini Bottom (~22 min) |
| 6 | 5 | split 661.36s | 11x10–11x11 | No Pictures Please / Stuck on the Roof |
| 7 | 6 | split 656.52s | 11x12–11x13 | Krabby Patty Creature Feature / Teacher's Pests |
| 8 | 7 | split 661.36s | 11x14–11x15 | Sanitation Insanity / Bunny Hunt |
| 9 | 8 | split 658.41s | 11x16–11x17 | Squid Noir / Scavenger Pants |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s11-disc1.yaml`

### Disc 2 — `configs/spongebob/s11-disc2.yaml` (18 episodes, DVD 11x18–11x35; makemkv TINFO 0–8) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S11 D2`. All 9 slots are **paired on 2-chapter titles** (split at ffprobe chapter boundary). Skip play-all TINFO 9 (3:18:07), bonus TINFO 10 (~6:53, 413s). Log: `logs/rip-s11-disc2.log`. Output: `output/Season 11/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 11/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 710.74s | 11x18–11x19 | Cuddle E. Hugs / Pat the Horse |
| 2 | 1 | split 652.40s | 11x20–11x21 | Chatterbox Gary / Don't Feed the Clowns |
| 3 | 2 | split 656.41s | 11x22–11x23 | Drive Happy / Old Man Patrick |
| 4 | 3 | split 659.41s | 11x24–11x25 | Fun-Sized Friends / Grandmum's the Word |
| 5 | 4 | split 660.26s | 11x26–11x27 | Doodle Dimension / Moving Bubble Bass |
| 6 | 5 | split 660.41s | 11x28–11x29 | High Sea Diving / Bottle Burglars |
| 7 | 6 | split 661.36s | 11x30–11x31 | My Leg! / Ink Lemonade |
| 8 | 7 | split 658.32s | 11x32–11x33 | Mustard O' Mine / Shopping List |
| 9 | 8 | split 661.36s | 11x34–11x35 | Whale Watching / Krusty Kleaners |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s11-disc2.yaml`

### Disc 3 — `configs/spongebob/s11-disc3.yaml` (15 episodes, DVD 11x36–11x50; makemkv TINFO 0–7) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S11 D3`. TINFO 0–2 and 4–7 are **paired on 2-chapter titles** (split at ffprobe chapter boundary); TINFO 3 is single ~22 min special **Goons on the Moon**; TINFO 7 uses extended first chapter (~970 s) for Squirrel Jelly / The String. Skip play-all TINFO 8 (2:55:24), bonus TINFO 9 (~6:05, 365s). Log: `logs/rip-s11-disc3.log`. Output: `output/Season 11/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 11/`. **Completing this disc finishes Season 11 (50 files total on NAS).**

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 710.83s | 11x36–11x37 | Patnocchio / ChefBob |
| 2 | 1 | split 653.44s | 11x38–11x39 | Plankton Paranoia / Library Cards |
| 3 | 2 | split 658.44s | 11x40–11x41 | Call the Cops / Surf N' Turf |
| 4 | 3 | — | 11x42 | Goons on the Moon (~22 min) |
| 5 | 4 | split 660.44s | 11x43–11x44 | Appointment TV / Karen's Virus |
| 6 | 5 | split 654.12s | 11x45–11x46 | The Grill is Gone / The Night Patty |
| 7 | 6 | split 651.43s | 11x47–11x48 | Bubbletown / Girls' Night Out |
| 8 | 7 | split 970.00s | 11x49–11x50 | Squirrel Jelly / The String |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s11-disc3.yaml`

**Season 11 status:** Disc 1 complete (17 episodes, 11x01–11x17). Disc 2 complete (18 episodes, 11x18–11x35). Disc 3 complete (15 episodes, 11x36–11x50). **Season 11 complete — 50 files** on NAS.

## Season 13 — Thirteenth Season (52 episodes DVD order)

Manifests: `configs/spongebob/s13-disc{1,2,3,4}.yaml` (as authored).

### Disc 1 — `configs/spongebob/s13-disc1.yaml` (13 episodes, DVD 13x01–13x13; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S13 D1`. **No play-all or bonus titles** exposed by MakeMKV (DVD MSG titles 1–3 absent; only MSG 4–10 / TINFO 0–6). TINFO 0–3 and 5–6 are **paired on 3-chapter titles** (split at ffprobe chapter-1 boundary; chapter 3 is end credits); TINFO 4 is single ~22 min special **SpongeBob's Road to Christmas**. Log: `logs/rip-s13-disc1.log`. Scan log: `logs/scan-s13-disc1-2026-07-06.log`. Output: `output/Season 13/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 13/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #4).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 686.35s | 13x01–13x02 | A Place for Pets / Lockdown for Love |
| 2 | 1 | split 645.51s | 13x03–13x04 | Under the Small Top / Squidward's Sick Daze |
| 3 | 2 | split 658.32s | 13x05–13x06 | Goofy Scoopers / Pat the Dog |
| 4 | 3 | split 646.98s | 13x07–13x08 | Something Narwhal This Way Comes / C.H.U.M.S |
| 5 | 4 | — | 13x09 | SpongeBob's Road to Christmas (~22 min) |
| 6 | 5 | split 647.81s | 13x10–13x11 | Potato Puff / There Will Be Grease |
| 7 | 6 | split 652.52s | 13x12–13x13 | The Big Bad Bubble Bass / Sea-Man Sponge Haters Club |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s13-disc1.yaml`

### Disc 2 — `configs/spongebob/s13-disc2.yaml` (14 episodes, DVD 13x14–13x27; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S13 D2`. **No play-all or bonus titles** exposed by MakeMKV (7 TINFO titles only). All 7 slots are **paired on 3-chapter titles** (split at ffprobe chapter-1 boundary; chapter 3 is end credits); TINFO 3 uses extended first chapter (~849 s) for Squidferatu / Slappy Daze. Log: `logs/rip-s13-disc2.log`. Scan log: `logs/scan-s13-disc2-2026-07-06.log`. Output: `output/Season 13/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 13/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD.

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 690.36s | 13x14–13x15 | Food PBFFT! Truck / Upturn Girls |
| 2 | 1 | split 662.16s | 13x16–13x17 | Say Awww! / Patrick the Mailman |
| 3 | 2 | split 655.32s | 13x18–13x19 | Captain Pipsqueak / Plane to Sea |
| 4 | 3 | split 849.35s | 13x20–13x21 | Squidferatu / Slappy Daze |
| 5 | 4 | split 645.34s | 13x22–13x23 | Welcome to Binary Bottom / You're Going to Pay...Phone |
| 6 | 5 | split 660.03s | 13x24–13x25 | A Skin Wrinkle in Time / Abandon Twits |
| 7 | 6 | split 646.31s | 13x26–13x27 | Wallhalla / Salty Sponge |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s13-disc2.yaml`

**Karen for Spot (13x28):** Listed on DVD packaging after Salty Sponge on Disc 2, but **not exposed as a MakeMKV title** on D2 or D3 scans — found on **D4 TINFO 0** (chapter extract); see Disc 4 section.

### Disc 3 — `configs/spongebob/s13-disc3.yaml` (14 episodes, DVD 13x29–13x42; makemkv TINFO 0–6) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S13 D3`. **No play-all or bonus titles** exposed by MakeMKV (7 TINFO titles only). All 7 slots are **paired on 3-chapter titles** (split at ffprobe chapter-1 boundary; chapter 3 is end credits); TINFO 6 uses extended first chapter (~885 s) for Dopey Dick / Plankton and the Beanstalk. Log: `logs/rip.log` (legacy: `logs/rip-s13-disc3.log`). Output: `output/Season 13/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 13/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD. Physical menu starts with Arbor Day Disarray (TheTVDB **13x29**); **13x28 Karen for Spot** is not on this disc scan (see D2/D4 notes).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 690.52s | 13x29–13x30 | Arbor Day Disarray / Ain't That the Tooth |
| 2 | 1 | split 656.32s | 13x31–13x32 | Ma and Pa's Big Hurrah / Yellow Pavement |
| 3 | 2 | split 657.66s | 13x33–13x34 | The Flower Plot / SpongeBob on Parade |
| 4 | 3 | split 661.33s | 13x35–13x36 | Delivery to Monster Island / Ride Patrick Ride |
| 5 | 4 | split 661.16s | 13x37–13x38 | Hot Crossed Nuts / Sir Urchin and Snail Fail |
| 6 | 5 | split 657.32s | 13x39–13x40 | Friendiversary / Mandatory Music |
| 7 | 6 | split 885.08s | 13x41–13x42 | Dopey Dick / Plankton and the Beanstalk |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s13-disc3.yaml`

### Disc 4 — `configs/spongebob/s13-disc4.yaml` (11 episodes, DVD 13x28 + 13x43–13x52; makemkv TINFO 0–5) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S13 D4`. **No play-all or bonus titles** exposed by MakeMKV (6 TINFO titles). TINFO 0 is a **6-chapter combined title** (~44 min): **13x28 Karen for Spot** is extracted from chapter 1 (ffprobe index 1, ~652 s); remaining cells duplicate content also on TINFO 1–2 — rip 13x43+ from TINFO 1–5. TINFO 1–5 are **paired on 3-chapter titles** (split at ffprobe chapter-1 boundary). Log: `logs/rip.log`, `logs/debug.log`. Scan: `logs/scan.log`. Output: `output/Season 13/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 13/`. **Completing this disc finishes Season 13 (52 DVD-order files on NAS).**

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD.

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | extract ch1 | 13x28 | Karen for Spot |
| 2 | 1 | split 698.70s | 13x43–13x44 | My Friend Patty / FUN-Believable |
| 3 | 2 | split 653.49s | 13x45–13x46 | Spatula of the Heavens / Gary's Playhouse |
| 4 | 3 | split 645.14s | 13x47–13x48 | Swimming Fools / The Goobfather |
| 5 | 4 | split 662.03s | 13x49–13x50 | SquidBird / Allergy Attack! |
| 6 | 5 | split 661.69s | 13x51–13x52 | Big Top Flop / Sandy, Help Us! |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s13-disc4.yaml`

**Karen for Spot (13x28):** Missing from D2/D3 MakeMKV scans; **found on D4 TINFO 0** as chapter 1 (~652 s) within the 6-chapter combined title.

**Season 13 status:** All 4 discs complete — **52** DVD-order episodes in `output/Season 13/` and NAS. **Season 13 complete.**

## Season 12 — Twelfth Season (48 episodes DVD order)

Manifests: `configs/spongebob/s12-disc{1,2,3}.yaml` (as authored).

### Disc 1 — `configs/spongebob/s12-disc1.yaml` (18 episodes, DVD 12x01–12x18; makemkv TINFO 0–8) ✅ **Completed 2026-07-06**

Scanned as `SPONGEBOB SQUAREPANTS S12 D1`. All 9 slots are **paired on 2-chapter titles** (split at ffprobe chapter-0 boundary); TINFO 0 uses extended first chapter (~708 s) for FarmerBob / Gary & Spot; TINFO 5 uses extended first chapter (~945 s) for Swamp Mates / One Trick Sponge. TINFO 4 reports 3 DVD chapters in MakeMKV scan but ffprobe exposes 2 chapters — split at chapter-0 boundary. Skip play-all TINFO 9 (3:24:32). No bonus titles filtered by `--minlength=300`. Log: `logs/rip.log`. Output: `output/Season 12/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 12/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 707.51s | 12x01–12x02 | FarmerBob / Gary & Spot |
| 2 | 1 | split 660.16s | 12x03–12x04 | The Nitwitting / The Ballad of Filthy Muck |
| 3 | 2 | split 651.08s | 12x05–12x06 | The Krusty Slammer / Pineapple RV |
| 4 | 3 | split 656.09s | 12x07–12x08 | Gary's Got Legs / King Plankton |
| 5 | 4 | split 661.03s | 12x09–12x10 | Plankton's Old Chum / Stormy Weather |
| 6 | 5 | split 944.71s | 12x11–12x12 | Swamp Mates / One Trick Sponge |
| 7 | 6 | split 659.09s | 12x13–12x14 | The Krusty Bucket / Squid's on a Bus |
| 8 | 7 | split 653.09s | 12x15–12x16 | Sandy's Nutty Nieces / Insecurity Guards |
| 9 | 8 | split 652.40s | 12x17–12x18 | Broken Alarm / Karen's Baby |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s12-disc1.yaml`

### Disc 2 — `configs/spongebob/s12-disc2.yaml` (15 episodes, DVD 12x19–12x33; makemkv TINFO 0–7) ✅ **Completed 2026-07-07**

Scanned as `SPONGEBOB SQUAREPANTS S12 D2`. TINFO 0–2 and 4–7 are **paired on 2-chapter titles** (split at ffprobe chapter-0 boundary); TINFO 0 and 5 use extended first chapter (~710 s / ~940 s); TINFO 3 is single ~43 min special **SpongeBob's Big Birthday Blowout**; TINFO 4 reports 3 DVD chapters in MakeMKV scan but ffprobe exposes 2 chapters — split at chapter-0 boundary. Skip play-all TINFO 8 (3:22:17). No bonus titles filtered by `--minlength=300`. Log: `logs/rip.log`. Output: `output/Season 12/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 12/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 709.51s | 12x19–12x20 | Shell Games / Senior Discount |
| 2 | 1 | split 661.16s | 12x21–12x22 | Mind the Gap / Dirty Bubble Returns |
| 3 | 2 | split 660.09s | 12x23–12x24 | Jolly Lodgers / Biddy Sitting |
| 4 | 3 | — | 12x25 | SpongeBob's Big Birthday Blowout (~43 min) |
| 5 | 4 | split 661.03s | 12x26–12x27 | SpongeBob in RandomLand / SpongeBob's Bad Habit |
| 6 | 5 | split 940.04s | 12x28–12x29 | Handemonium / Breakin' |
| 7 | 6 | split 654.09s | 12x30–12x31 | Boss for a Day / The Goofy Newbie |
| 8 | 7 | split 652.08s | 12x32–12x33 | The Ghost of Plankton / My Two Krabses |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s12-disc2.yaml`

### Disc 3 — `configs/spongebob/s12-disc3.yaml` (15 episodes, DVD 12x34–12x48; makemkv TINFO 0–7) ✅ **Completed 2026-07-07**

Scanned as `SPONGEBOB SQUAREPANTS S12 D3`. TINFO 0–2, 4–7 are **paired on 2-chapter titles** (split at ffprobe chapter-0 boundary); TINFO 0 and 7 use extended first chapter (~705 s / ~847 s); TINFO 3 is single ~11 min **Who R Zoo?**; TINFO 4 reports 3 DVD chapters in MakeMKV scan but ffprobe exposes 2 chapters — split at chapter-0 boundary; TINFO 6 has no ffprobe chapter markers — split at ~655 s (same boundary as TINFO 5). Skip play-all TINFO 8 (2:50:28), bonus TINFO 9 (~22 min, MSG 15). Log: `logs/rip.log`. Output: `output/Season 12/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 12/`. **Completing this disc finishes Season 12 (48 DVD-order files on NAS).**

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #3).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 704.50s | 12x34–12x35 | Knock Knock, Who's There? / Pat Hearts Squid |
| 2 | 1 | split 654.09s | 12x36–12x37 | Lighthouse Louie / Hiccup Plague |
| 3 | 2 | split 651.08s | 12x38–12x39 | A Cabin in the Kelp / The Hankering |
| 4 | 3 | — | 12x40 | Who R Zoo? (~12 min) |
| 5 | 4 | split 660.09s | 12x41–12x42 | Kwarantined Krab / Plankton's Intern |
| 6 | 5 | split 655.09s | 12x43–12x44 | Patrick's Tantrum / Bubble Bass's Tab |
| 7 | 6 | split 655.09s | 12x45–12x46 | Kooky Cooks / Escape from Beneath Glove World |
| 8 | 7 | split 846.51s | 12x47–12x48 | Krusty Koncessionaires / Dream Hoppers |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s12-disc3.yaml`

**Season 12 status:** All 3 discs complete — **48** DVD-order episodes in `output/Season 12/` and NAS. **Season 12 complete.**

## Season 14 — Fourteenth Season (21 episodes DVD order)

Manifests: `configs/spongebob/s14-disc{1,2}.yaml` (as authored).

### Disc 1 — `configs/spongebob/s14-disc1.yaml` (12 episodes, DVD 14x01–14x12; makemkv TINFO 0–5) ✅ **Completed 2026-07-07**

Scanned as `SPONGEBOB SQUAREPANTS S14 D1`. All 6 slots are **paired on 3-chapter titles** (split at ffprobe chapter-1 boundary; chapter 3 is end credits). **No play-all or bonus titles** exposed by MakeMKV at `--minlength=300` (TCOUNT 6 only; DVD MSG titles 1–3 absent). Log: `logs/rip.log`. Output: `output/Season 14/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 14/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #4).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 695.56s | 14x01–14x02 | Single Celled Defense / Buff for Puff |
| 2 | 1 | split 661.86s | 14x03–14x04 | We Heart Hoops / SpongeChovy |
| 3 | 2 | split 661.69s | 14x05–14x06 | BassWard / Squidiot Box |
| 4 | 3 | split 661.99s | 14x07–14x08 | Blood is Thicker Than Grease / Don't Make Me Laugh |
| 5 | 4 | split 661.66s | 14x09–14x10 | Momageddon / Pet the Rock |
| 6 | 5 | split 657.32s | 14x11–14x12 | Tango Tangle / Necro-Nom-Nom-Nom-I-Con |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s14-disc1.yaml`

### Disc 2 — `configs/spongebob/s14-disc2.yaml` (9 episodes, DVD 14x13–14x21; makemkv TINFO 0–5) ✅ **Completed 2026-07-07**

Scanned as `SPONGEBOB SQUAREPANTS S14 D2`. TINFO 0, 1, 4 are **paired on 3-chapter titles** (split at ffprobe chapter-1 boundary; chapter 3 is end credits); TINFO 2–3 and 5 are **single-title specials** (Kreepaway Kamp ~44 min, Snow Yellow ~22 min, Sandy's Country Christmas ~23 min). **No play-all or bonus titles** exposed by MakeMKV at `--minlength=300` (TCOUNT 6 only). Log: `logs/rip.log`. Output: `output/Season 14/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 14/`. **Completing this disc finishes Season 14 (21 DVD-order files on NAS).**

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #4).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 706.57s | 14x13–14x14 | PL-1413 / In the Mood to Feud |
| 2 | 1 | split 661.99s | 14x15–14x16 | Mooned! / Hysterical History |
| 3 | 2 | — | 14x17 | Kreepaway Kamp (~44 min) |
| 4 | 3 | — | 14x18 | Snow Yellow (~22 min) |
| 5 | 4 | split 654.02s | 14x19–14x20 | The Dirty Bubble Bass / Sheldon SquarePants |
| 6 | 5 | — | 14x21 | Sandy's Country Christmas (~23 min) |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s14-disc2.yaml`

**Season 14 status:** All 2 discs complete — **21** DVD-order episodes in `output/Season 14/` and NAS. **Season 14 complete.**

## Season 15 — Fifteenth Season (26 episodes DVD order)

Manifests: `configs/spongebob/s15-disc{1,2}.yaml` (as authored).

### Disc 1 — `configs/spongebob/s15-disc1.yaml` (14 episodes, DVD 15x01–15x14; makemkv TINFO 0–6) ✅ **Completed 2026-07-07**

Scanned as `SPONGEBOB SQUAREPANTS S15 D1`. All 7 slots are **paired on 3-chapter titles** (split at ffprobe chapter-0 end; chapter 2 is end credits). **No play-all or bonus titles** exposed by MakeMKV at `--minlength=300` (TCOUNT 7 only; DVD MSG titles 1–3 absent). Log: `logs/rip.log`. Output: `output/Season 15/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 15/`.

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #4).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 699.23s | 15x01–15x02 | Sammy Suckerfish / Big League Bob |
| 2 | 1 | split 660.33s | 15x03–15x04 | UpWard / Unidentified Flailing Octopus |
| 3 | 2 | split 660.19s | 15x05–15x06 | Bad Luck Bob / The Sandman Cometh |
| 4 | 3 | split 656.16s | 15x07–15x08 | Biscuit Ballyhoo / Student Driver Survivor |
| 5 | 4 | split 647.35s | 15x09–15x10 | Wiener Takes All / Stuck in an Elevator |
| 6 | 5 | split 647.65s | 15x11–15x12 | Squidness Protection / Dome Alone |
| 7 | 6 | split 660.03s | 15x13–15x14 | Wary Gary / Pinned |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s15-disc1.yaml`

### Disc 2 — `configs/spongebob/s15-disc2.yaml` (12 episodes, DVD 15x15–15x26; makemkv TINFO 0–5) ✅ **Completed 2026-07-07**

Scanned as `SPONGEBOB SQUAREPANTS S15 D2`. All 6 slots are **paired on 3-chapter titles** (split at ffprobe chapter-0 end; chapter 2 is end credits). **No play-all or bonus titles** exposed by MakeMKV at `--minlength=300` (TCOUNT 6 only; DVD MSG titles 1–3 absent). Log: `logs/rip.log`. Output: `output/Season 15/`. NAS: `/mnt/tn-sas/Anime/Spongebob Squarepants/Season 15/`. **Completing this disc finishes Season 15 (26 DVD-order files on NAS).**

**Note:** On this disc, `makemkv_title` in YAML is the **TINFO index** from scan (0-based filtered list), not the MSG title number on the DVD (episodes start at MSG #4).

| Slot | MkIdx | Ch | File | Episode |
|------|-------|----|------|---------|
| 1 | 0 | split 706.24s | 15x15–15x16 | Jeffy T's Prankwell Emporium / A Taste of Plankton |
| 2 | 1 | split 659.36s | 15x17–15x18 | Smartificial Intelligence / Firehouse Bob |
| 3 | 2 | split 659.69s | 15x19–15x20 | Pablum Plankton / MuseBob ModelPants |
| 4 | 3 | split 654.52s | 15x21–15x22 | Delivery of Doom / My Father the Boat |
| 5 | 4 | split 649.98s | 15x23–15x24 | Who's Afraid of Mr. Snippers? / A Fish Called Sandy |
| 6 | 5 | split 661.86s | 15x25–15x26 | Making Waves / Captain Quasar: The Next Iteration |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/spongebob/s15-disc2.yaml`

**Season 15 status:** All 2 discs complete — **26** DVD-order episodes in `output/Season 15/` and NAS. **Season 15 complete.**

## Naruto Shippuden — Blu-ray (Anime share)

Manifests: `configs/anime/naruto-shippuden-set*.yaml`. Output: `output/Naruto Shippuden/Season N/`. NAS: `/mnt/tn-sas/Anime/Naruto Shippuden/Season N/`.

**Important:** Viz Blu-ray **box-set number ≠ Plex season**. Before authoring YAML, list the NAS season folder and match the next missing `Nx##` episode numbers — do not assume Set N → Season N.

**Viz Blu-ray set → TVDB/Plex season mapping (2023–2026 NA releases):**

| Viz set | Abs. eps | TVDB seasons | Disc layout (7 eps/disc) |
|---------|----------|--------------|--------------------------|
| Set 8 | 193–220 | 9–10 | D1 193–199 … D4 214–220 |
| Set 9 | 221–248 | 10–12 | D1 221–227 … D4 242–248 |
| **Set 10** | **249–276** | **12–13** | **D1 249–255 · D2 256–262 · D3 263–269 · D4 270–276** |
| Set 11 | 277–304 | 13–14 | D1 277–283 … D4 298–304 |

Sources: [Viz Part 10](https://www.viz.com/anime/tv-show/naruto-shippuden-video/video-unit/18), [Blu-ray.com Set 10](https://www.blu-ray.com/movies/Naruto-Shippuden-Set-10-Blu-ray/408670/), [ANN Set 10](https://www.animenewsnetwork.com/encyclopedia/releases.php?id=57466), [TheTVDB S12](https://thetvdb.com/series/naruto-shippuden/seasons/official/12), [TheTVDB S13](https://thetvdb.com/series/naruto-shippuden/seasons/official/13). **Note:** legacy DVD “Set 10” (2012) is episodes 113–126 — different product from the Blu-ray line.

**Set 10 full episode list (28 eps, 249–276):**

249 Thank You · 250 Battle in Paradise! Odd Beast vs. The Monster! · 251 The Man Named Kisame · 252 The Angelic Herald of Death · 253 The Bridge to Peace · 254 The Super Secret S-Rank Mission · 255 The Artist Returns · 256 Assemble! Allied Shinobi Forces! · 257 Meeting · 258 Rivals · 259 Rift · 260 Parting · 261 For My Friend · 262 War Begins! · 263 Sai and Shin · 264 Secrets of the Reanimation Jutsu · 265 An Old Nemesis Returns · 266 The First and Last Opponent · 267 The Brilliant Military Advisor of the Hidden Leaf · 268 Battleground! · 269 Forbidden Words · 270 Golden Bonds · 271 Road to Sakura · 272 Mifune vs. Hanzo · 273 True Kindness · 274 The Complete Ino-Shika-Cho Formation! · 275 A Message from the Heart · 276 Attack of the Gedo Statue

Season boundary: **249–275 = Season 12** (TVDB S12E07–S12E33); **276 = Season 13** (TVDB S13E01). Set 9 ends at 248 (S12E06); Set 11 starts at 277 (S13E02).

### Set 10 Disc 1 — `configs/anime/naruto-shippuden-set10-disc1.yaml` (7 episodes, 12x249–12x255; makemkv titles 0–6) ✅ **Completed 2026-07-08**

Scanned as 7 titles (~23 min each) at `--minlength=300`. **Plex Season 12** (NAS already had 12x243–12x248). Skip menus/extras filtered by minlength. Log: `logs/rip.log`. `stream_defaults`: Japanese audio default, English subs default, keep all tracks. Output: `output/Naruto Shippuden/Season 12/`. NAS: `/mnt/tn-sas/Anime/Naruto Shippuden/Season 12/`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 0 | 12x249 | Thank You |
| 2 | 1 | 12x250 | Battle in Paradise! Odd Beast vs. The Monster! |
| 3 | 2 | 12x251 | The Man Named Kisame |
| 4 | 3 | 12x252 | The Angelic Herald of Death |
| 5 | 4 | 12x253 | The Bridge to Peace |
| 6 | 5 | 12x254 | The Super Secret S-Rank Mission |
| 7 | 6 | 12x255 | The Artist Returns |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/anime/naruto-shippuden-set10-disc1.yaml`

### Set 10 Disc 2 — `configs/anime/naruto-shippuden-set10-disc2.yaml` (7 episodes, 12x256–12x262; makemkv titles 0–6) ✅ **Completed 2026-07-08**

Scanned as `Naruto Shippuden 256 - 262` — 7 titles (~23:36 each) at `--minlength=300`. **Plex Season 12** (S12 E14–E20). No play-all or bonus titles exposed by minlength filter. Log: `logs/rip.log`. `stream_defaults`: Japanese audio default, English subs default, keep all tracks. Output: `output/Naruto Shippuden/Season 12/`. NAS: `/mnt/tn-sas/Anime/Naruto Shippuden/Season 12/`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 0 | 12x256 | Assemble! Allied Shinobi Forces! |
| 2 | 1 | 12x257 | Meeting |
| 3 | 2 | 12x258 | Rivals |
| 4 | 3 | 12x259 | Rift |
| 5 | 4 | 12x260 | Parting |
| 6 | 5 | 12x261 | For My Friend |
| 7 | 6 | 12x262 | War Begins! |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/anime/naruto-shippuden-set10-disc2.yaml`

### Set 10 Disc 3 — `configs/anime/naruto-shippuden-set10-disc3.yaml` (7 episodes, 12x263–12x269; makemkv titles 0–6) ✅ **Completed 2026-07-08**

Scanned as `Naruto Shippuden 263 - 269` — 7 titles (~23:36 each) at `--minlength=300`. **Plex Season 12** (S12 E21–E27). No play-all or bonus titles exposed by minlength filter. Log: `logs/rip.log`. `stream_defaults`: Japanese audio default, English subs default, keep all tracks. Output: `output/Naruto Shippuden/Season 12/`. NAS: `/mnt/tn-sas/Anime/Naruto Shippuden/Season 12/`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 0 | 12x263 | Sai and Shin |
| 2 | 1 | 12x264 | Secrets of the Reanimation Jutsu |
| 3 | 2 | 12x265 | An Old Nemesis Returns |
| 4 | 3 | 12x266 | The First and Last Opponent |
| 5 | 4 | 12x267 | The Brilliant Military Advisor of the Hidden Leaf |
| 6 | 5 | 12x268 | Battleground! |
| 7 | 6 | 12x269 | Forbidden Words |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/anime/naruto-shippuden-set10-disc3.yaml`

### Set 10 Disc 4 — `configs/anime/naruto-shippuden-set10-disc4.yaml` (7 episodes, 12x270–12x275 + 13x276; makemkv titles 0–6) ✅ **Completed 2026-07-08**

Scanned as `Naruto Shippuden 270 - 276` — 7 titles (~23:36 each) at `--minlength=300`. **Cross-season disc:** episodes 270–275 → **Plex Season 12** (S12 E28–E33); episode 276 → **Plex Season 13** (S13 E01) via `tracks[].season: 13` override. No play-all or bonus titles exposed by minlength filter. Log: `logs/rip.log`. `stream_defaults`: Japanese audio default, English subs default, keep all tracks. Output: `output/Naruto Shippuden/Season 12/` + `Season 13/` (ep 276). NAS: `/mnt/tn-sas/Anime/Naruto Shippuden/Season 12/` + `Season 13/`.

| Slot | MkIdx | File | Episode |
|------|-------|------|---------|
| 1 | 0 | 12x270 | Golden Bonds |
| 2 | 1 | 12x271 | Road to Sakura |
| 3 | 2 | 12x272 | Mifune vs. Hanzo |
| 4 | 3 | 12x273 | True Kindness |
| 5 | 4 | 12x274 | The Complete Ino-Shika-Cho Formation! |
| 6 | 5 | 12x275 | A Message from the Heart |
| 7 | 6 | 13x276 | Attack of the Gedo Statue |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/anime/naruto-shippuden-set10-disc4.yaml`

**Set 10 status:** All 4 discs complete — **28** episodes (12x249–12x275 + 13x276) across Plex Seasons 12 and 13. **Blu-ray Set 10 complete.**

## Breaking Bad — Blu-ray (Shows share)

Manifests: `configs/shows/breaking-bad-*.yaml`. Output: `output/Breaking Bad/Season N/`. NAS: `/mnt/tn-sas/Shows/Breaking Bad/Season N/`.

Naming matches other Shows library titles (e.g. House, Game of Thrones): `Breaking Bad - 1x01 - Pilot.mkv` ([TheTVDB official order](https://thetvdb.com/series/breaking-bad/seasons/official/1)).

**Season 1 Blu-ray (2 discs, 7 episodes):** Disc 1 = 1x01–1x04; Disc 2 = 1x05–1x07. **Disc 2 completes Season 1.**

### Season 1 Disc 1 — `configs/shows/breaking-bad-s1-disc1.yaml` (4 episodes, 1x01–1x04; makemkv titles 0–3) ✅ **Completed**

Scanned as `Breaking Bad: Season 1: Disc 1` / volume `BREAKINGBADS1` — 4 titles at `--minlength=300` (durations match Blu-ray.com: Pilot 58:06, others ~48 min). No play-all or extras exposed by minlength. Log: `logs/rip.log`. `stream_defaults`: English audio default, **no** subtitle default (tracks kept), keep all tracks. Output: `output/Breaking Bad/Season 1/`. NAS: `/mnt/tn-sas/Shows/Breaking Bad/Season 1/`.

| Slot | MkIdx | Duration | File | Episode |
|------|-------|----------|------|---------|
| 1 | 0 | 0:58:06 | 1x01 | Pilot |
| 2 | 1 | 0:48:12 | 1x02 | Cat's in the Bag... |
| 3 | 2 | 0:48:11 | 1x03 | ...And the Bag's in the River |
| 4 | 3 | 0:48:15 | 1x04 | Cancer Man |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/shows/breaking-bad-s1-disc1.yaml`

### Season 1 Disc 2 — `configs/shows/breaking-bad-s1-disc2.yaml` (3 episodes, 1x05–1x07; makemkv titles 0–2) ✅ **Completed**

Scanned as `Breaking Bad: Season 1: Disc 2` / volume `BREAKINGBADS1` — 3 episode titles (~48 min, ~11.7–11.9 GB) at `--minlength=300` plus shorter extras (titles 3–6; not ripped). Completes Season 1 (7/7). Same `stream_defaults` / post as Disc 1. Log: `logs/rip.log`.

| Slot | MkIdx | Duration | File | Episode |
|------|-------|----------|------|---------|
| 1 | 0 | 0:48:11 | 1x05 | Gray Matter |
| 2 | 1 | 0:48:04 | 1x06 | Crazy Handful of Nothin' |
| 3 | 2 | 0:47:42 | 1x07 | A No-Rough-Stuff-Type Deal |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/shows/breaking-bad-s1-disc2.yaml`

**Season 1 status:** Complete (7/7 on NAS).

**Season 2 Blu-ray (3 discs, 13 episodes):** Disc 1 = 2x01–2x05; Disc 2 = 2x06–2x09; Disc 3 = 2x10–2x13. Volume label `BREAKINGBADS2`. Physical “Disc 3” in the set is **not** Season 2 Disc 1 — always trust MakeMKV disc name + durations.

### Season 2 Disc 1 — `configs/shows/breaking-bad-s2-disc1.yaml` (5 episodes, 2x01–2x05; makemkv titles 0–4) ✅ **Completed 2026-07-09**

Scanned as `Breaking Bad: Season 2: Disc 1` / volume `BREAKINGBADS2` — 5 episode titles (~47 min, ~8.4–8.7 GB) at `--minlength=300` matching Blu-ray.com Disc 1 (Seven Thirty-Seven / Grilled / Bit by a Dead Bee / Down / Breakage). Title 5 is extra (~15:48; not ripped). Same `stream_defaults` / post as S1. Log: `logs/rip.log`. Output: `output/Breaking Bad/Season 2/`. NAS: `/mnt/tn-sas/Shows/Breaking Bad/Season 2/`.

| Slot | MkIdx | Duration | File | Episode |
|------|-------|----------|------|---------|
| 1 | 0 | 0:47:15 | 2x01 | Seven Thirty-Seven |
| 2 | 1 | 0:47:57 | 2x02 | Grilled |
| 3 | 2 | 0:46:59 | 2x03 | Bit by a Dead Bee |
| 4 | 3 | 0:47:24 | 2x04 | Down |
| 5 | 4 | 0:47:21 | 2x05 | Breakage |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/shows/breaking-bad-s2-disc1.yaml`

### Season 2 Disc 2 — `configs/shows/breaking-bad-s2-disc2.yaml` (4 episodes, 2x06–2x09; makemkv titles 0–3) ✅ **Completed 2026-07-09**

Scanned as `Breaking Bad: Season 2: Disc 2` / volume `BREAKINGBADS2` — 4 episode titles (~47 min, ~10.4 GB) at `--minlength=300` matching Blu-ray.com Disc 2 (Peekaboo / Negro y Azul / Better Call Saul / 4 Days Out). Title 4 is extra (~13:05; not ripped). Same `stream_defaults` / post as S1. Log: `logs/rip.log`. Output: `output/Breaking Bad/Season 2/`. NAS: `/mnt/tn-sas/Shows/Breaking Bad/Season 2/`.

| Slot | MkIdx | Duration | File | Episode |
|------|-------|----------|------|---------|
| 1 | 0 | 0:47:22 | 2x06 | Peekaboo |
| 2 | 1 | 0:47:21 | 2x07 | Negro y Azul |
| 3 | 2 | 0:47:21 | 2x08 | Better Call Saul |
| 4 | 3 | 0:47:21 | 2x09 | 4 Days Out |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/shows/breaking-bad-s2-disc2.yaml`

### Season 2 Disc 3 — `configs/shows/breaking-bad-s2-disc3.yaml` (4 episodes, 2x10–2x13; makemkv titles 0–3) ⚠️ **Partial (3/4 on NAS — 2x13 ABQ missing)**

Scanned as `Breaking Bad: Season 2: Disc 3` / volume `BREAKINGBADS2` — 4 episode titles (~47 min, ~8.6–8.7 GB) at `--minlength=300` matching Blu-ray.com Disc 3 (Over / Mandala / Phoenix / ABQ). Titles 4–6 are extras (Cop Talk ~10 min, Webisodes ~23 min, short featurette; not ripped). Same `stream_defaults` / post as S1. Log: `logs/rip.log`. Output: `output/Breaking Bad/Season 2/`. NAS: `/mnt/tn-sas/Shows/Breaking Bad/Season 2/`. **2x10–2x12 on NAS; 2x13 ABQ failed (title 3, no MKV after retries) — re-insert Disc 3 and resume with `--start-at 4`.**

| Slot | MkIdx | Duration | File | Episode |
|------|-------|----------|------|---------|
| 1 | 0 | 0:47:22 | 2x10 | Over |
| 2 | 1 | 0:47:21 | 2x11 | Mandala |
| 3 | 2 | 0:47:24 | 2x12 | Phoenix |
| 4 | 3 | 0:47:34 | 2x13 | ABQ |

Resume: `VIDEOSRIPPER_START_AT=4 scripts/videosripper rip configs/shows/breaking-bad-s2-disc3.yaml`

**Season 2 status:** 12/13 on NAS — **2x13 ABQ missing** (Disc 3 slot 4 failed; re-insert S2 Disc 3 and resume with `--start-at 4`).

**Season 3 Blu-ray (3 discs, 13 episodes):** Disc 1 = 3x01–3x05; Disc 2 = 3x06–3x09; Disc 3 = 3x10–3x13. Volume label expected `BREAKINGBADS3` (verify on scan).

### Season 3 Disc 1 — `configs/shows/breaking-bad-s3-disc1.yaml` (5 episodes, 3x01–3x05; makemkv titles 0–4) 🔄 **In progress — Python CLI**

Scanned as `Breaking Bad: Season 3: Disc 1` — 5 episode titles (~47 min, ~8.0–8.6 GB) at `--minlength=300` matching Blu-ray.com Disc 1 (No Más / Caballo Sin Nombre / I.F.T. / Green Light / Más). Title 5 is extra (~7:24; not ripped). Same `stream_defaults` / post as S1/S2. Log: `logs/rip.log`. Output: `output/Breaking Bad/Season 3/`. NAS: `/mnt/tn-sas/Shows/Breaking Bad/Season 3/`.

| Slot | MkIdx | Duration | File | Episode |
|------|-------|----------|------|---------|
| 1 | 0 | 0:47:12 | 3x01 | No Más |
| 2 | 1 | 0:47:12 | 3x02 | Caballo Sin Nombre |
| 3 | 2 | 0:47:13 | 3x03 | I.F.T. |
| 4 | 3 | 0:47:13 | 3x04 | Green Light |
| 5 | 4 | 0:47:13 | 3x05 | Más |

Resume: `VIDEOSRIPPER_START_AT=N scripts/videosripper rip configs/shows/breaking-bad-s3-disc1.yaml`

## Feature films (Movies share)

Manifests: `configs/movies/*.yaml`. Output: `output/Movies/`. NAS: `/mnt/tn-sas/Movies/`.

### Ghost Rider: Spirit of Vengeance — `configs/movies/ghost-rider-spirit-of-vengeance.yaml` ✅ **Completed**

Scanned as `GHOST_RIDER`. Theatrical feature at makemkv title 0 (~24 GB, ~95 min at `--minlength=300`). Use matching minlength for scan and rip.

Resume: `scripts/videosripper rip configs/movies/ghost-rider-spirit-of-vengeance.yaml`

### Scarface (1983) — `configs/movies/scarface-1983.yaml` ✅ **Completed 2026-07-07**

Scanned as `Scarface`. Feature at makemkv **title 1** (2:49:53, 33.97 GB); title 0 (~27 min intro/promo) skipped by `auto_feature` (`min_duration_sec: 4800`). Extras titles 2–11 (~10–38 min each). Log: `logs/rip.log`.

| Idx | Duration | Size | Notes |
|-----|----------|------|-------|
| 0 | 0:27:06 | 2.47 GB | intro/promo — skip |
| 1 | 2:49:53 | 33.97 GB | **feature** (auto_feature) |
| 2–11 | 0:10–0:38 | 0.3–3.7 GB | extras — skip |

Resume: `scripts/videosripper rip configs/movies/scarface-1983.yaml`

## Common failures

| Symptom | Fix |
|---------|-----|
| Watchers die silently after `make watch-all` / agent shell exits | Fixed by `setsid -f` in `watch-all`. Confirm session leaders: `ps -o pid,pgid,sid,stat,cmd -C python` should show `S<s` and PID=SID. Restart with `make watch-restart`. Check `pipeline.log` for `WATCHER_STOPPED` (clean signal) vs sudden gap (old nohup-only bug). |
| Watcher process up but idle forever | Device thread may have crashed pre-hardening; now logs `WATCHER_CRASH` + restarts. `make watch-status` — stale heartbeat → restart. |
| `VideosRipper venv missing` | `cd /mnt/game1/VideosRipper && python -m venv .venv && .venv/bin/pip install -e .` |
| No `/dev/sr0` | Run `scripts/fix-optical-drive.sh`; replug USB drive |
| Manifest validation error | Fix YAML per [schema](#yaml-manifest-schema); run `scripts/videosripper validate` |
| `mv: No such file or directory` | Ensure `output/Season N/` exists (`mkdir -p`) before rip |
| NAS copy permission denied | Fix permissions on NAS season folder |
| Rip killed (exit 143) | Intentional stop; resume with `VIDEOSRIPPER_START_AT` or `--start-at` |
| Wrong episode names | Re-scan disc; fix manifest `tracks[]`; do not guess from prior disc |
| Home disk full | Ensure output/staging stay on `/mnt/game1` (1.5 TB free) |
| Plex wrong episode metadata | Use DVD-order `Nx##` filenames + set Plex episode ordering to **DVD** |
| Two rips on same drive | Never run parallel `videosripper rip` / `makemkvcon mkv` on the **same** `/dev/srN`; per-drive `makemkv-<slug>` lock serializes it, CLI warns `=== ALERT === DUPLICATE MAKEKV`. Parallel rips across **different** drives are supported (`--auto` / `--device`). |
| Disc ejected during scan | Scan/dry-run/validate never eject; successful real `rip` with `post.eject: true` ejects on success; exhausted disc-open/stall retries also eject for inspection — re-insert before re-running |
| Post-rip eject reports OK but disc still in drive (Pioneer slot loader) | `eject_disc` verifies with `media_present()` after each attempt (poll up to ~5s); retries up to **3 times**; then `=== STAGE EJECT_FAILED` + `=== ALERT === USER INTERVENTION REQUIRED — eject failed after 3 attempts`. Rip **DONE** is unchanged — eject manually (`eject /dev/srN`) |
| Disc ejects **before OS reads it** (no scan/rip yet) | **Usually not a documented firmware “auto-eject” policy.** Pioneer BDR-XS07U slot-loader: common causes are **USB power** (use USB 3 direct port or optional DC adapter), **dirty/disc-fit** (clean; re-seat several times), **mechanical wear** (tray-position sensor / eject motor — forum reports on XS07 after hundreds of rips). Region mismatch, bad TOC, and copy-protection failures normally return SCSI errors to software **without** ejecting. Linux shows `Medium not present - tray open` on *any* eject (software or drive button); correlate with `pipeline.log` / `EJECT` stage. Try: `echo on > /sys/block/sr0/device/power/control` (disable USB autosuspend), different USB port, insert with watchers stopped (`make watch-stop`), watch `journalctl -kf` + `udevadm monitor` while inserting. Firmware settings (region, Quiet Drive, PureRead) need **Pioneer BD Drive Utility on Windows** — no Linux tool. fw 1.04 is current; do not downgrade (UHD-capable units lose libre fw). |
| `Failed to open disc` / makemkv exit 11 / TCOUNT:0 | Auto-retries **3 times** (30s delay) by default = 4 total attempts; on exhaustion: `USER INTERVENTION REQUIRED`, auto-eject, exit non-zero — clean/re-seat, re-insert, re-run |
| `Disc open timed out after 600s` on **one** disc; same drive rips others same day | **Not firmware** — drive + MakeMKV path are fine. Insert scan can succeed while rip preflight (`makemkv info` under `VIDEOSRIPPER_OPEN_TIMEOUT_SEC`) hangs on a second open. Check disc surface / re-seat; try another drive; if insert scan took **>10 min**, bump `VIDEOSRIPPER_OPEN_TIMEOUT_SEC` (insert scan has no timeout). No studio pattern unless multiple titles from same pressing fail. |
| Rip hangs (no PRGV, drive LED stuck) | Stall kills makemkv (`--on-stall exit`); default retries same title up to `retry.count` (3); on exhaustion ejects + requires intervention; increase `VIDEOSRIPPER_STALL_TIMEOUT_SEC` for slow BD; resume with `--start-at N` |
| makemkv subprocess failure | Log shows `=== ALERT === MAKEKV FAILURE — exit N` plus MSG lines in rip.log (also `error.log`); full traceback in `debug.log` and `error.log` |
| NAS rsync fails | Retries once; then `=== ALERT === NAS COPY FAILED` and **continues** the disc rip (local file kept) |
| ffmpeg stream_defaults remux fails | Logs WARNING and **keeps original ripped MKV** (episode not aborted) |

### Alert log examples (grep `=== ALERT ===`)

```
=== ALERT === DUPLICATE MAKEKV — another makemkvcon is using dev:/dev/sr0
=== ALERT === Failed to open disc (Failed to open disc); Action: after retries exhaust the disc is ejected — ...
=== ALERT === RETRY 1/3 — disc preflight failed; waiting 30s before retry: ...
=== ALERT === RETRY 2/3 — disc preflight failed; waiting 30s before retry: ...
=== ALERT === RETRY 3/3 — disc preflight failed; waiting 30s before retry: ...
=== ALERT === USER INTERVENTION REQUIRED — disc preflight failed after all retries (3 retries / 4 total attempts)
=== ALERT === Disc ejected for inspection — re-insert when ready
=== ALERT === RIP STALL — no progress for 900s during title rip (limit 900s)
=== ALERT === MAKEKV FAILURE — exit 11
=== ALERT === MSG: Failed to open disc
=== ALERT === NAS COPY FAILED — kept local rip; check permissions for ...
```

Stall behavior: default `--on-stall exit` kills makemkv; with default `retry.count: 3` + `stall_retry: true` the same title is retried up to 3 times after the delay (4 total attempts). Use `--on-stall warn` / `VIDEOSRIPPER_ON_STALL=warn` to log only without killing.

## LLM checklist (copy when starting a session)

- [ ] Read this file; confirm `.venv` installed (`pip install -e .`)
- [ ] Confirm disc inserted and `/dev/sr0` exists
- [ ] Scan disc (`scripts/videosripper scan <manifest>` or manual makemkvcon)
- [ ] Map episodes to TheTVDB DVD numbers; create/update YAML under `configs/`
- [ ] `scripts/videosripper validate` then `--dry-run` before rip
- [ ] Ensure `output/Season N/` and `logs/` exist on game drive
- [ ] `scripts/videosripper rip configs/…yaml` — monitor `logs/rip.log` (`tail -f logs/rip.log`); errors also in `logs/error.log` (`tail -f logs/error.log`)
- [ ] Verify NAS copies; eject manually or set `post.eject: true` before rip if desired
- [ ] Update this file’s Season/disc section when a new disc is done

## Do not

- Create new bash rip scripts (use YAML manifests + Python CLI)
- Install MakeMKV from AUR (use existing `~/.local` install)
- Rip to home directory or `/tmp` for staging/output
- Commit secrets (MakeMKV key) to git
- Force-push or amend git commits unless user asks
- Skip NAS copy unless user explicitly wants local-only
- Guess title indices — always scan the inserted disc first
