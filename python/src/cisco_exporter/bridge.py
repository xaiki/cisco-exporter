"""Serve a Prometheus ``/metrics`` endpoint from a cached exporter snapshot.

A poll costs one SSH login per switch per show command (eight of them) unless
the exporter is configured to share one session, and the exporter caches
nothing. Serving a scrape straight from it would put that
whole session in front of every scrape — longer than a scraper's timeout, and
repeated as often as the scraper polls. So this bridge fetches on its own
schedule and serves the last result: a scraper never waits on a switch, and
the switch is not asked once per scrape.

A fetch that fails is not served with the previous switch data: the body then
carries only ``cisco_exporter_scrape_ok 0`` and the age, so the per-switch
series go stale in Prometheus instead of freezing at their last value.
"""
from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from .metrics import CONTENT_TYPE, Scrape, render_scrape, render_switches
from .parsers import parse_snapshot


class Fetcher(Protocol):
    """Whatever the bridge fetches through: the real client, or a stand-in.

    The bridge only needs a snapshot and a name for its source, so anything
    that can produce one is enough — which is what lets the tests drive it
    without a switch.
    """

    @property
    def url(self) -> str:
        """Named in the startup line, so the log says what is being fetched."""

    def fetch(self) -> dict[str, object]:
        """Return the raw snapshot envelope."""


#: The one path the bridge serves.
METRICS_PATH = "/metrics"

#: How often to fetch the exporter, in seconds: the switches are polled once
#: per interval, however often Prometheus scrapes.
DEFAULT_INTERVAL = 60.0


class _Handler(BaseHTTPRequestHandler):
    """Serve the cached body; :class:`Bridge` does the fetching."""

    def do_GET(self) -> None:  # noqa: N802 — the name is BaseHTTPRequestHandler's
        server = self.server
        assert isinstance(server, _Server)
        if self.path != METRICS_PATH:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        self._send(200, server.bridge.body(), CONTENT_TYPE)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Stay quiet: the cache reports its own state on /metrics."""


class _Server(ThreadingHTTPServer):
    """The listener, holding the bridge its handler reads from."""

    def __init__(self, address: tuple[str, int], bridge: Bridge) -> None:
        super().__init__(address, _Handler)
        self.bridge = bridge


class Bridge:
    """Fetch the exporter on a schedule and remember the rendered result."""

    def __init__(self, exporter: Fetcher, interval: float = DEFAULT_INTERVAL) -> None:
        self._exporter = exporter
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._finished: float | None = None
        self._ok = False
        self._duration = 0.0
        self._switches = b""

    def body(self) -> bytes:
        """The metrics as served: the scrape state, then the cached switches.

        The age is rendered here, per request, because a scraper asks how stale
        the data is *now* — an age baked in at fetch time would always read zero.
        Before the first fetch there is no switch state to serve, and the age is
        the bridge's own uptime.
        """
        with self._lock:
            return render_scrape(self._scrape()).encode() + self._switches

    def refresh(self) -> bool:
        """Fetch, parse and render the switch state once, replacing any previous.

        Returns whether the fetch worked. A failed one leaves no switch state
        behind, so the previous values stop being served rather than appearing
        to still be current.
        """
        started = time.monotonic()
        state = ""
        ok = True
        try:
            state = render_switches(parse_snapshot(self._exporter.fetch()))
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            ok = False
        with self._lock:
            self._finished = time.monotonic()
            self._ok = ok
            self._duration = self._finished - started
            self._switches = state.encode()
        return ok

    def serve(self, host: str, port: int) -> None:
        """Serve ``/metrics`` until the process is signalled to stop.

        Writes one line to stderr once bound, like the exporter itself; the
        requests are not logged (the cache reports its state on every scrape).
        """
        server = _Server((host, port), self)
        thread = threading.Thread(target=self._refresh_forever,
                                  name="cisco-exporter-refresh", daemon=True)
        thread.start()
        print(f"[cisco-exporter-metrics] serving /metrics on {host}:{port}, "
              f"fetching {self._exporter.url} every {self._interval:g}s",
              file=sys.stderr)
        try:
            server.serve_forever()
        finally:
            self._stop.set()
            server.server_close()

    def _refresh_forever(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._interval)

    def _scrape(self) -> Scrape:
        """The scrape state, read under the lock.

        Before the first fetch finishes there is nothing to be stale, so the
        age is the bridge's own uptime.
        """
        finished = self._finished if self._finished is not None else self._started
        return Scrape(ok=self._ok,
                      age_seconds=time.monotonic() - finished,
                      duration_seconds=self._duration)
