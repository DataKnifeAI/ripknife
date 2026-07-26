.PHONY: build install lint test run clean help watch-disc watch-rip watch-all watch-stop watch-restart watch-status trigger-map

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
SCRIPTS := scripts/videosripper
ARGS ?= --help
DISC_ID ?=

help:
	@echo "VideosRipper Makefile targets:"
	@echo "  install      Create/refresh .venv and install package with dev extras"
	@echo "  build        Alias for install"
	@echo "  lint         Run ruff on videosripper/ and tests/"
	@echo "  test         Run pytest unit tests"
	@echo "  run          Run videosripper CLI (pass ARGS='...')"
	@echo "  watch-disc   Run disc-watcher --auto (Process 1, all /dev/sr*)"
	@echo "  watch-rip    Run rip-watcher --auto (Process 2, one worker per drive)"
	@echo "  watch-all    Start both watchers detached (setsid + nohup)"
	@echo "  watch-stop   Stop background disc/rip python watchers (safe if none)"
	@echo "  watch-restart Stop then restart both background watchers"
	@echo "  watch-status Show running/not + last heartbeat + disc present"
	@echo "  trigger-map  Run trigger-map.sh (DISC_ID=foo required)"
	@echo "  clean        Remove __pycache__, .pytest_cache, .pytest-tmp, /tmp/pytest-of-\$$USER"

$(VENV)/bin/python:
	python -m venv $(VENV)

install: $(VENV)/bin/python
	$(PIP) install -e ".[dev]"

build: install

lint:
	$(VENV)/bin/ruff check videosripper/ tests/

# --basetemp on the game drive avoids filling /tmp (tmpfs). pytest keeps the
# last 3 basetemp runs; test_post_rip_verify writes multi-GB fixtures, so always
# wipe .pytest-tmp after the run while preserving pytest's exit code.
test:
	$(PYTHON) -m pytest tests/ -v --basetemp=.pytest-tmp; \
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
# (nohup only ignores SIGHUP). Prefer systemd user units for always-on (see
# scripts/systemd/).
watch-all: install
	mkdir -p logs
	setsid -f nohup $(PYTHON) scripts/disc-watcher.py --auto >> logs/nohup-watch-disc.log 2>&1
	setsid -f nohup $(PYTHON) scripts/rip-watcher.py --auto >> logs/nohup-watch-rip.log 2>&1
	@echo "Started disc-watcher and rip-watcher (detached; logs/nohup-watch-*.log)"

watch-stop:
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
	@echo "Restarting watchers: stopping then starting..."
	$(MAKE) watch-stop
	@# Brief pause so flock locks release before new watchers start.
	sleep 1
	$(MAKE) watch-all
	@echo "Restart complete."

watch-status:
	@$(PYTHON) -c "from videosripper.watcher_status import print_watch_status; print_watch_status()"

trigger-map:
	@if [ -z "$(DISC_ID)" ]; then echo "Usage: make trigger-map DISC_ID=foo"; exit 2; fi
	$(PYTHON) -m videosripper.map_agent "$(DISC_ID)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .pytest-tmp
	@# Leftover default pytest basetemp on /tmp (from older make test runs).
	rm -rf /tmp/pytest-of-$$USER 2>/dev/null || true
