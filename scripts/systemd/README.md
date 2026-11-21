# Ripknife systemd user units
#
# Durable alternative to `make watch-all` (survives logout, agent shell exit,
# and accidental process-group SIGTERM). Make targets remain supported.
#
# User units vs system units
# --------------------------
# **User units (shipped, preferred):** `~/.config/systemd/user/*.service`
# managed with `systemctl --user`. Run as your login user (optical drive
# group, NAS mounts). Start at graphical/login session; with
# `loginctl enable-linger` they also start at boot with no GUI session.
#
# **System units (not shipped):** `/etc/systemd/system/*.service` run as
# root or `User=`. Only needed if you want a machine-wide service without
# a user systemd instance. You would add `User=` / `Group=` / `SupplementaryGroups=cdrom`
# and still need the NAS mount available to that user. Prefer user units
# unless you have a reason to go system-wide.
#
# Prerequisites: `make install` so `.venv` exists.
#
# The unit templates use `@RIPKNIFE_ROOT@`. `make install-service` rewrites
# that token to the checkout path when copying into `~/.config/systemd/user/`.
# Do not copy the templates in unchanged.
#
# Install (creates `/var/log/ripknife`, linger, enable --now):
#
#   make -C /path/to/ripknife install-service
#
# Check:
#   systemctl --user status ripknife-disc-watcher ripknife-rip-watcher
#   make -C /path/to/ripknife watch-status
#   journalctl --user -u ripknife-disc-watcher -u ripknife-rip-watcher -f
#   tail -F /var/log/ripknife/pipeline.log
#
# Restart (keep enabled at boot):
#   make -C /path/to/ripknife watch-restart
#
# Stop / disable:
#   make -C /path/to/ripknife uninstall-service
#   # or: systemctl --user disable --now ripknife-disc-watcher ripknife-rip-watcher
#
# Logs:
#   make watch-all → `$ROOT/logs` (repo, default `RIPKNIFE_LOGS`)
#   systemd units → `/var/log/ripknife` (`RIPKNIFE_LOGS` in the unit)
#   Same filenames and line-count rotation as today. stdout/stderr also go
#   to the user journal (`journalctl --user -u …`).
#
# Temp dirs (avoid filling /tmp tmpfs):
#   RIPKNIFE_TMP=/path/to/ripknife/.tmp
#   TMPDIR=/path/to/ripknife/.tmp
# Both are set in the shipped .service files; override in a drop-in if needed.
#
# OpenRouter (map manifests and three disc decisions)
# ---------------------------------------------------
# Disc-watcher calls the map function, which asks OpenRouter for strict JSON.
# Python validates that JSON and writes `staging/ready/*.yaml`. The model
# has no shell. Which call uses which env var, and when vision runs, is in
# [docs/llm.md](../../docs/llm.md). The model id for each call comes from
# `.env.example`.
#
# Set the key in the gitignored repo `.env` (units load it via
# `EnvironmentFile=-…/.env`):
#
#   cp /path/to/ripknife/.env.example /path/to/ripknife/.env
#   # paste OPENROUTER_API_KEY=… from https://openrouter.ai/keys
#   chmod 600 /path/to/ripknife/.env
#
# `map_agent` re-reads that `.env` on each map, so a new map can see the key
# without restarting watchers. After `make install-service` (or copying the
# .service files), `daemon-reload` applies the EnvironmentFile to the *next*
# unit start. **Do not restart mid-rip.**
#
# A missing key, invalid JSON, or an action outside the schema does not write
# a manifest and does not start a rip. Disc-watcher logs
# `=== ALERT === MAP_AGENT_AUTH` or `MAP_FAILED` to pipeline.log, error.log,
# and map.log, and sets watcher-state `last_error`. `make watch-status`
# prints that line. Recovery: put `OPENROUTER_API_KEY` in `.env` (no unit
# restart), or hand-write `staging/ready/<disc-id>.yaml`. A similar-season
# prior manifest is mentioned as a template only — it is not copied. Auth
# gets one retry after `.env` merge; nothing loops silently.
#
# Never commit `.env` or a real key.
#
# Do not also run `make watch-all` while units are active (singleton flock).
# Stop make watch-all before enabling systemd:
#   make watch-stop
#   make install-service
