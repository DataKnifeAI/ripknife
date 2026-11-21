.PHONY: build install lint test run clean help dev watch-disc watch-rip watch-all watch-stop watch-restart watch-status trigger-map check-drive install-service uninstall-service

_THIS_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
RIPKNIFE_ROOT ?= $(_THIS_DIR)
# Keep Python/Java/mktemp off the 32G /tmp tmpfs (same as RIPKNIFE_TMP / paths.TMP_ROOT).
export TMPDIR := $(RIPKNIFE_ROOT)/.tmp
export RIPKNIFE_TMP := $(TMPDIR)
export RIPKNIFE_DELETE_LOCAL_AFTER_NAS := 1
# systemd user units write here (make watch-all still uses $(RIPKNIFE_ROOT)/logs).
SYSTEMD_LOG_DIR ?= /var/log/ripknife

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
SCRIPTS := scripts/ripknife
ARGS ?= --help
DISC_ID ?=

help:
	@echo "Ripknife Makefile targets:"
	@echo "  install      Create/refresh .venv and install package with dev extras"
	@echo "  build        Alias for install"
	@echo "  lint         Run ruff on ripknife/ and tests/"
	@echo "  test         Run pytest unit tests"
	@echo "  run          Run ripknife CLI (pass ARGS='...')"
	@echo "  watch-disc   Run disc-watcher --auto (Process 1, all /dev/sr*)"
	@echo "  watch-rip    Run rip-watcher --auto (Process 2, one worker per drive)"
	@echo "  dev          Developer: install if needed, then watch-all (logs/)"
	@echo "  watch-all    Start both watchers detached (setsid + nohup → logs/)"
	@echo "  watch-stop   Stop ad-hoc watchers, or systemd units if those are active"
	@echo "  watch-restart Restart systemd units, or stop+start ad-hoc watchers"
	@echo "  watch-status Show running/not + last heartbeat + disc present + last_error"
	@echo "  check-drive  Verify optical drive + LibreDrive (insert disc to test)"
	@echo "  trigger-map  Run the map agent (DISC_ID=foo required)"
	@echo "  install-service   systemd user units at login/boot; logs → $(SYSTEMD_LOG_DIR)"
	@echo "  uninstall-service Stop and disable those user units (linger + logs kept)"
	@echo "  clean        Remove __pycache__, .pytest_cache, .pytest-tmp, /tmp/pytest-of-\$$USER"
	@echo "  TMPDIR=$(TMPDIR)  (game drive; not /tmp tmpfs)"

$(VENV)/bin/python:
	python -m venv $(VENV)

install: $(VENV)/bin/python
	$(PIP) install -e ".[dev]"

build: install

lint:
	$(VENV)/bin/ruff check ripknife/ tests/

# basetemp defaults in pyproject.toml (.pytest-tmp on game drive). pytest keeps the
# last 3 basetemp runs; test_post_rip_verify writes multi-GB fixtures, so always
# wipe .pytest-tmp after the run while preserving pytest's exit code.
test:
	mkdir -p .tmp .pytest-tmp
	$(PYTHON) -m pytest tests/ -v; \
		rc=$$?; \
		rm -rf .pytest-tmp; \
		exit $$rc

run:
	$(SCRIPTS) $(ARGS)

watch-disc: install
	$(PYTHON) scripts/disc-watcher.py --auto

watch-rip: install
	$(PYTHON) scripts/rip-watcher.py --auto

# setsid -f: new session + fork so the make/recipe process group is NOT the
# watchers' process group. plain `nohup ... &` stays in the recipe PGID —
# when an agent/shell tears down that group with SIGTERM, watchers die silently
# (nohup only ignores SIGHUP). Prefer systemd user units for always-on
# (`make install-service`; see scripts/systemd/). Do not run both at once.
dev: watch-all

watch-all: install
	@if systemctl --user is-active --quiet ripknife-disc-watcher.service 2>/dev/null \
	    || systemctl --user is-active --quiet ripknife-rip-watcher.service 2>/dev/null; then \
		echo "systemd user units are already running. Stop them before make watch-all:"; \
		echo "  systemctl --user stop ripknife-disc-watcher ripknife-rip-watcher"; \
		echo "  # or: make uninstall-service   (also disables start-at-boot)"; \
		exit 1; \
	fi
	mkdir -p logs
	setsid -f nohup $(PYTHON) scripts/disc-watcher.py --auto >> logs/nohup-watch-disc.log 2>&1
	setsid -f nohup $(PYTHON) scripts/rip-watcher.py --auto >> logs/nohup-watch-rip.log 2>&1
	@echo "Started disc-watcher and rip-watcher (detached; logs/nohup-watch-*.log)"

watch-stop:
	@# systemd Restart=always would respawn after pkill — stop via systemctl instead.
	@if systemctl --user is-active --quiet ripknife-disc-watcher.service 2>/dev/null \
	    || systemctl --user is-active --quiet ripknife-rip-watcher.service 2>/dev/null; then \
		systemctl --user stop ripknife-disc-watcher.service ripknife-rip-watcher.service; \
		echo "Stopped systemd user units (still enabled at boot; make uninstall-service to disable)"; \
		exit 0; \
	fi
	@# Match only python interpreters (not make/sh). [p]ython avoids self-match.
	@if pgrep -f '[p]ython.*scripts/disc-watcher\.py' >/dev/null 2>&1; then \
		pkill -f '[p]ython.*scripts/disc-watcher\.py' || true; \
		echo "Stopped disc-watcher"; \
	else \
		echo "disc-watcher was not running"; \
	fi
	@if pgrep -f '[p]ython.*scripts/rip-watcher\.py' >/dev/null 2>&1; then \
		pkill -f '[p]ython.*scripts/rip-watcher\.py' || true; \
		echo "Stopped rip-watcher"; \
	else \
		echo "rip-watcher was not running"; \
	fi

watch-restart:
	@if systemctl --user is-active --quiet ripknife-disc-watcher.service 2>/dev/null \
	    || systemctl --user is-active --quiet ripknife-rip-watcher.service 2>/dev/null; then \
		echo "Restarting systemd user units..."; \
		systemctl --user restart ripknife-disc-watcher.service ripknife-rip-watcher.service; \
		echo "Restart complete."; \
	else \
		echo "Restarting watchers: stopping then starting..."; \
		$(MAKE) watch-stop; \
		sleep 1; \
		$(MAKE) watch-all; \
		echo "Restart complete."; \
	fi

watch-status:
	@if systemctl --user is-active --quiet ripknife-disc-watcher.service 2>/dev/null \
	    || systemctl --user is-active --quiet ripknife-rip-watcher.service 2>/dev/null; then \
		RIPKNIFE_LOGS="$(SYSTEMD_LOG_DIR)" $(PYTHON) -c "from ripknife.watcher_status import print_watch_status; print_watch_status()"; \
	else \
		$(PYTHON) -c "from ripknife.watcher_status import print_watch_status; print_watch_status()"; \
	fi

install-service: install
	@chmod +x scripts/systemd/install-user-units.sh scripts/systemd/uninstall-user-units.sh
	RIPKNIFE_ROOT="$(RIPKNIFE_ROOT)" RIPKNIFE_SYSTEMD_LOGS="$(SYSTEMD_LOG_DIR)" \
		scripts/systemd/install-user-units.sh

uninstall-service:
	@chmod +x scripts/systemd/uninstall-user-units.sh
	RIPKNIFE_SYSTEMD_LOGS="$(SYSTEMD_LOG_DIR)" scripts/systemd/uninstall-user-units.sh

check-drive: install
	$(PYTHON) -m ripknife.cli check-drive

trigger-map:
	@if [ -z "$(DISC_ID)" ]; then echo "Usage: make trigger-map DISC_ID=foo"; exit 2; fi
	$(PYTHON) -m ripknife.map_agent "$(DISC_ID)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .pytest-tmp
	@# Leftover default pytest basetemp on /tmp (from older make test runs).
	rm -rf /tmp/pytest-of-$$USER 2>/dev/null || true
