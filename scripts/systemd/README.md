# VideosRipper systemd user units
#
# Durable alternative to `make watch-all` (survives logout, agent shell exit,
# and accidental process-group SIGTERM). Make targets remain supported.
#
# Prerequisites: `make install` so `.venv` exists.
#
#   mkdir -p ~/.config/systemd/user
#   cp /mnt/game1/VideosRipper/scripts/systemd/*.service ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   loginctl enable-linger "$USER"
#   systemctl --user enable --now videosripper-disc-watcher.service
#   systemctl --user enable --now videosripper-rip-watcher.service
#
# Check:
#   systemctl --user status videosripper-disc-watcher videosripper-rip-watcher
#   make -C /mnt/game1/VideosRipper watch-status
#
# Stop / disable:
#   systemctl --user disable --now videosripper-disc-watcher videosripper-rip-watcher
#
# Temp dirs (avoid filling /tmp tmpfs):
#   VIDEOSRIPPER_TMP=/mnt/game1/VideosRipper/.tmp
#   TMPDIR=/mnt/game1/VideosRipper/.tmp
# Both are set in the shipped .service files; override in a drop-in if needed.
#
# Do not also run `make watch-all` while units are active (singleton flock).
