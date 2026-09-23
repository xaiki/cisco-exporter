"""The command line: exit codes, settings resolution, and ``--once`` output."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from cisco_exporter.cli import main
from cisco_exporter.version import RELEASE
from fakes import FakeResponse, snapshot, urlopen_failing, urlopen_returning

_TARGET = ["--url", "http://x/api/status", "--ha-token", "ha", "--exporter-token", "ex-tok"]


def test_once_prints_the_metrics_and_exits_zero(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(urllib.request, "urlopen",
                        urlopen_returning(FakeResponse(json.dumps(snapshot()))))
    assert main(["--once", *_TARGET]) == 0
    out = capsys.readouterr().out
    assert "cisco_exporter_scrape_ok 1" in out
    assert 'cisco_interface_up{switch="north",port="Gi1/0/1"} 1' in out


def test_once_reports_a_failed_fetch_with_exit_one(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(urllib.request, "urlopen",
                        urlopen_failing(OSError("connection refused")))
    assert main(["--once", *_TARGET]) == 1
    out = capsys.readouterr().out
    assert "cisco_exporter_scrape_ok 0" in out
    assert "cisco_switch_command_ok" not in out


def test_missing_settings_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--once", "--url", "http://x"]) == 2
    assert "--ha-token" in capsys.readouterr().err


def test_a_settings_file_supplies_the_target_and_flags_win(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    settings = tmp_path / "cisco_poller.json"
    settings.write_text(json.dumps({
        "url": "http://file/api/status", "ha_token": "file-ha",
        "exporter_token": "file-ex"}))
    seen: list[object] = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        urlopen_returning(FakeResponse(exporter_token="file-ex"), seen))
    assert main(["--once", "--settings", str(settings), "--ha-token", "flag-ha"]) == 0
    request = seen[0]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == "http://file/api/status"
    assert request.headers == {"Authorization": "Bearer flag-ha"}


def test_a_settings_file_that_cannot_be_read_exits_one(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["--once", "--settings", str(tmp_path / "nope.json")]) == 1
    assert "nope.json" in capsys.readouterr().err


def test_a_settings_file_that_is_not_an_object_exits_one(
        capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("[1, 2]")
    assert main(["--once", "--settings", str(settings)]) == 1
    assert "not a JSON object" in capsys.readouterr().err


@pytest.mark.parametrize("listen", ["nonsense", "127.0.0.1:abc", "127.0.0.1:0", ":9101"])
def test_a_bad_listen_is_a_usage_error(capsys: pytest.CaptureFixture[str],
                                       listen: str) -> None:
    assert main(["--listen", listen, *_TARGET]) == 2
    assert "--listen" in capsys.readouterr().err


def test_interval_and_timeout_must_be_positive(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--once", "--interval", "0", *_TARGET]) == 2
    assert "--interval" in capsys.readouterr().err
    assert main(["--once", "--timeout", "0", *_TARGET]) == 2
    assert "--timeout" in capsys.readouterr().err


def test_version_prints_the_build_without_settings(
        capsys: pytest.CaptureFixture[str]) -> None:
    """Like the exporter binary, it answers with no settings: it describes the
    build rather than a run, and whoever asks may be asking about a host whose
    settings are the thing in question."""
    assert main(["--version"]) == 0
    release, digest, tags = capsys.readouterr().out.strip().split("-")
    assert release == RELEASE
    assert len(digest) == 16
    assert tags == "core"
