"""CLI scan --json must emit parseable JSON on stdout only."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from videosripper.cli import app
from videosripper.makemkv import ScanResult, TitleInfo

runner = CliRunner()


def test_scan_device_json_stdout_is_json_only(monkeypatch) -> None:
    def fake_scan(device: str, minlength: int = 300, *, timeout_sec: int | None = None) -> ScanResult:
        return ScanResult(
            raw="",
            disc_name="Test Disc",
            titles={0: TitleInfo(index=0, duration="01:30:00", duration_sec=5400, name="Feature")},
        )

    monkeypatch.setattr("videosripper.cli.scan", fake_scan)

    result = runner.invoke(app, ["scan-device", "--json", "--device", "dev:/dev/sr0"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip(), "expected JSON on stdout"
    assert "Scanning" not in result.stdout
    data = json.loads(result.stdout)
    assert data["disc_name"] == "Test Disc"
    assert len(data["titles"]) == 1


def test_scan_device_json_includes_obfuscation_for_duplicates(monkeypatch) -> None:
    titles = {
        i: TitleInfo(
            index=i,
            duration="2:02:22",
            duration_sec=7342,
            size_bytes=36_000_000_000,
            name="John Wick Chapter 2",
        )
        for i in range(10)
    }
    titles[9] = TitleInfo(
        index=9,
        duration="2:04:08",
        duration_sec=7448,
        size_bytes=36_700_000_000,
        name="John Wick Chapter 2",
    )

    def fake_scan(device: str, minlength: int = 300, *, timeout_sec: int | None = None) -> ScanResult:
        return ScanResult(raw="", disc_name="John Wick Chapter 2", titles=titles)

    monkeypatch.setattr("videosripper.cli.scan", fake_scan)

    result = runner.invoke(app, ["scan-device", "--json", "--device", "dev:/dev/sr0"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["obfuscation"]["detected"] is True
    assert data["obfuscation"]["duplicate_count"] == 9
    assert data["obfuscation"]["recommended_title"] == 0
    assert 9 in data["obfuscation"]["decoy_indices"]
