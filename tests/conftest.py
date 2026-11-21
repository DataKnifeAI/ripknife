"""Test defaults. Generated-config archive stays off unless a test opts in."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_config_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIPKNIFE_CONFIG_ARCHIVE", "")
