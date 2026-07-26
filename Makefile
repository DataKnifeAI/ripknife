.PHONY: build install lint test run clean help watch-disc watch-rip watch-all watch-stop trigger-map

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
	@echo "  watch-all    Start both watchers in background via nohup"
	@echo "  watch-stop   Stop background disc/rip python watchers (safe if none)"
	@echo "  trigger-map  Run trigger-map.sh (DISC_ID=foo required)"
	@echo "  clean        Remove __pycache__ and .pytest_cache"

$(VENV)/bin/python:
	python -m venv $(VENV)

install: $(VENV)/bin/python
	$(PIP) install -e ".[dev]"

build: install

lint:
	$(VENV)/bin/ruff check videosripper/ tests/

test:
	$(PYTHON) -m pytest tests/ -v

run:
	$(SCRIPTS) $(ARGS)

watch-disc: install
	$(PYTHON) scripts/disc-watcher.py --auto

watch-rip: install
	$(PYTHON) scripts/rip-watcher.py --auto

watch-all: install
	mkdir -p logs
	nohup $(PYTHON) scripts/disc-watcher.py --auto >> logs/nohup-watch-disc.log 2>&1 &
	nohup $(PYTHON) scripts/rip-watcher.py --auto >> logs/nohup-watch-rip.log 2>&1 &
	@echo "Started disc-watcher and rip-watcher (logs/nohup-watch-*.log)"

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

trigger-map:
	@if [ -z "$(DISC_ID)" ]; then echo "Usage: make trigger-map DISC_ID=foo"; exit 2; fi
	$(PYTHON) -m videosripper.map_agent "$(DISC_ID)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
