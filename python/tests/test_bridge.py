"""The cached ``/metrics`` endpoint: what it serves, and when it refuses to."""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

import pytest

from cisco_exporter.bridge import METRICS_PATH, Bridge, _Server
from cisco_exporter.metrics import CONTENT_TYPE
from fakes import snapshot


class _FakeExporter:
    """A fetcher that answers with a canned snapshot, or fails."""

    def __init__(self, body: dict[str, object] | None = None,
                 error: Exception | None = None) -> None:
        self.url = "http://fake/api/status"
        self._body = snapshot() if body is None else body
        self._error = error

    def fetch(self) -> dict[str, object]:
        if self._error is not None:
            raise self._error
        return self._body


def test_before_the_first_fetch_there_is_no_switch_data() -> None:
    body = Bridge(_FakeExporter()).body().decode()
    assert "cisco_exporter_scrape_ok 0" in body
    assert "cisco_switch_command_ok" not in body


def test_a_refresh_renders_the_switch_state() -> None:
    bridge = Bridge(_FakeExporter())
    assert bridge.refresh() is True
    body = bridge.body().decode()
    assert "cisco_exporter_scrape_ok 1" in body
    assert 'cisco_interface_up{switch="north",port="Gi1/0/1"} 1' in body


def test_a_failed_refresh_drops_the_switch_series() -> None:
    bridge = Bridge(_FakeExporter(error=OSError("connection refused")))
    assert bridge.refresh() is False
    body = bridge.body().decode()
    assert "cisco_exporter_scrape_ok 0" in body
    assert "cisco_switch_command_ok" not in body


def _scrape_age(body: bytes) -> float:
    """The age sample the served body carries."""
    for line in body.decode().splitlines():
        if line.startswith("cisco_exporter_scrape_age_seconds "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError("the body carries no scrape age")


def test_the_scrape_age_is_computed_when_scraped_not_when_fetched() -> None:
    """A scraper asks how stale the data is now; an age baked in at fetch time
    would always read zero, and a stalled refresher would look healthy."""
    bridge = Bridge(_FakeExporter())
    assert bridge.refresh() is True
    first = _scrape_age(bridge.body())
    time.sleep(0.05)
    assert _scrape_age(bridge.body()) > first


def test_metrics_are_served_and_any_other_path_is_404(
        capsys: pytest.CaptureFixture[str]) -> None:
    bridge = Bridge(_FakeExporter())
    assert bridge.refresh() is True
    server = _Server(("127.0.0.1", 0), bridge)
    port = server.server_address[1]
    assert isinstance(port, int)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # No proxy: this must reach the bridge on loopback, whatever the
    # environment's proxy variables say.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{METRICS_PATH}", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == CONTENT_TYPE
            assert "cisco_exporter_scrape_ok 1" in response.read().decode()
        with pytest.raises(urllib.error.HTTPError) as failure:
            opener.open(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert failure.value.code == 404
        failure.value.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    # Requests are not logged: the cache reports its own state on /metrics.
    assert "GET" not in capsys.readouterr().err
