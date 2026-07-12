.PHONY: build install lint test run clean help watch-disc watch-rip trigger-map

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
	@echo "  watch-disc   Run disc-watcher (Process 1)"
	@echo "  watch-rip    Run rip-watcher (Process 2)"
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
	$(PYTHON) scripts/disc-watcher.py

watch-rip: install
	$(PYTHON) scripts/rip-watcher.py

trigger-map:
	@if [ -z "$(DISC_ID)" ]; then echo "Usage: make trigger-map DISC_ID=foo"; exit 2; fi
	bash scripts/trigger-map.sh "$(DISC_ID)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache
