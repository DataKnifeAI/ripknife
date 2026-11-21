# Damaged-disc recovery

Opt-in. Watchers do not call `recover`, and they do not loop a model. The ladder, speeds, retries, and filenames stay in code. The three decision calls are in [llm.md](llm.md). Slot-drive rules are in [drives.md](drives.md).

One bad-sector retry runs inside the normal rip. That covers a read error, L-EC, a hash-check failure, `Failed to save title`, or no MKV from those. The first error does not start the ladder. When the retry fails, the manifest is in `staging/failed` and the pipeline runs only the capped steps below.

`scripts/ripknife recover auto` prints the next step. It does not open the drive, run `eject`, or run `ddrescue`.

These do not enter the ladder: `MAP_AMBIGUOUS`, a wrong title, an identification failure, an empty tray, and a library-copy failure after a good local file.

Same ISO and same map on every pass. A relative `output/...` path must stay a single `output/` prefix. Refuse to start while `makemkvcon` is running or the device lock is held.

`recover ddrescue` passes `-b 2048 -d` and the mapfile. It never passes `-r`, and it does not run `eject`. The image is the scan. There is no separate pre-scan.

```bash
scripts/ripknife recover auto --help
scripts/ripknife recover makemkv --help
scripts/ripknife recover ddrescue --help
scripts/ripknife recover ffmpeg --help
scripts/ripknife recover stitch --help
scripts/ripknife recover align --help
```

## Ladder

1. Unfinished bytes (non-tried, non-trimmed, or non-scraped) greater than 0: plain resume, no `-M`, no `-r`. The first unfinished pass is `eject -x 25`. The second is `eject -x 12`. Interrupt a pass that stays at 0 B/s for 4 minutes.
2. Bad bytes remain and the large-hole retrim has not been used: one `ddrescue -b 2048 -d -M` (no `-r`) at `eject -x 12`.
3. Bad bytes remain and are at most 4 MiB: one `ddrescue -b 2048 -d -M -r1` at `eject -x 4`. `-r` stays 1. No `-R`.
4. Stop the drive loop when the hole is still large after step 2, when two passes add zero bytes, or when both unfinished resumes are spent and unfinished bytes remain. Restore spindle speed with `eject -x 0`.
5. A clean map (0 bad, 0 unfinished) on a Blu-ray is a drive remux of the feature (`dev:/dev/srN`), at the last `eject -x` speed that increased rescued bytes. The source is `dev:`, not `iso:`. When that remux fails, one retry at `eject -x 4`.

The map read chooses the speed. `-r` stays at 1. Two zero-gain passes, or a pass at 0 B/s for 4 minutes, stops that attempt.

Worst case: 1 normal rip, 1 ddrescue image, 2 unfinished resumes, 1 large-hole `-M` with no `-r`, 1 `-M -r1` only when bad bytes are at most 4 MiB, 1 drive remux, 1 stitch (including align), 1 verify, 3 model calls.

## Verify

MakeMKV's `HH:MM:SS` is truncated whole seconds. The same displayed second, or a fraction under 1 second past that integer, matches. A fractional feature duration stays within 0.5 seconds. A miss of more than about 1 second fails. A short file fails.

Verify video and audio, and one `ffmpeg -v error` null decode with empty stderr.

Exit 0 is not success when the log says `Failed to open disc` and `TCOUNT 0`. Info succeeds when the feature title is listed. A drive `mkv` succeeds on `Copy complete` or titles saved.

A finished ddrescue image of an AACS Blu-ray can still fail `makemkvcon iso:`. After a clean map, `recover auto` prints a drive remux. `--title` is the operator's index.

The longest playlist can be a stereo alternate of a slightly shorter feature. Prefer the near-longest lossless or surround title. A second DTS track beside DTS-HD MA is the core. Do not copy the stereo-only alternate.

A finished drive remux with no read errors may be copied even when the image still has bad bytes outside that read. Do not stitch a remux that verifies. A drive-remux verify fails on any stderr byte.

Stitch only when the drive remux failed, or when the map still has missing bytes and the drive loop has stopped. Align is stream copy: trim each piece that overran its planned span, then cut at the feature duration. A piece short by more than about 1 second is a gap. Do not invent picture. Verify once. A one-frame non-monotonic DTS line is ignored on the stitch verify only.

A gapped stitch stays in the recovery directory. It is never a library copy.

## Model decisions

Three calls at most, none inside the byte loop:

1. After the normal rip fails: bad sectors versus a wrong title, an empty slot, an ambiguous map, or a tooling problem. When it is not bad sectors, do not start ddrescue.
2. After the drive loop stops with bytes still missing: another physical copy you own, a different-brand drive, or accept the gap. Another speed is not a choice.
3. After verify, only when verify failed or gaps remain: keep the file for review, or discard it. A gapped file is never copied.

Anything outside the allowed actions, or a missing `OPENROUTER_API_KEY`, is stop.

## What does not restore sectors

dvdisaster without ECC written when the disc was good, zero-fill, ffmpeg concealment, ddrescue fill mode, a different edition, or a different cut. The legitimate fill is another physical disc of the same pressing that you own, using the existing mapfile so only the missing ranges are read.
