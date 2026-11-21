# Drives

The drives are slot-loading.

`eject -t` fails. Do not tell the operator to close a tray.

`eject -x` sets spindle speed in CD units (about 177 kB/s). It does not close the slot. `eject -x 0` restores spindle speed. `ddrescue --max-read-rate` does not slow the spindle.

Speeds the recovery ladder uses: `eject -x 25` is about 1x Blu-ray, `eject -x 12` is about half speed, `eject -x 4` is the slow pass. The order of those passes is in [recovery.md](recovery.md).

Do not image an empty slot. An empty tray gets one reinsert and is not imaged. An empty tray does not enter the recovery ladder.

`eject` in a manifest runs after a successful real rip, and after retries are exhausted on a disc-open failure or a stall. A scan, a validate, a dry-run, and a mid-retry do not eject.

One drive never runs two `makemkvcon mkv` processes. The per-drive lock serializes them. Several drives can rip in parallel (`--auto` or a repeated `--device`).

Do not start `recover` while `makemkvcon` is running or the device lock is held.

`RIPKNIFE_DEVICE` defaults to `dev:/dev/sr0`.
