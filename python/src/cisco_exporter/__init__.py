"""The consumer side of the cisco-exporter: parse it, or export it as metrics.

The Rust exporter in this repository serves the raw text of a switch's
``show`` commands over HTTP. This package is what makes that output useful on
its own: fetch a snapshot from the exporter, parse it with the IOS parsers,
and render it in the Prometheus exposition format.

* :mod:`cisco_exporter.parsers` — the parsers, dependency-free
* :mod:`cisco_exporter.client` — the exporter's HTTP client
* :mod:`cisco_exporter.metrics` — the Prometheus renderer
* :mod:`cisco_exporter.bridge` — a cached ``/metrics`` endpoint
"""
from .client import Exporter
from .metrics import CONTENT_TYPE, Scrape, render, render_scrape, render_switches
from .parsers import parse_snapshot
from .version import RELEASE as __version__
from .version import TAGS, source_digest, version_id

__all__ = ["CONTENT_TYPE", "Exporter", "Scrape", "__version__", "parse_snapshot",
           "render", "render_scrape", "render_switches", "source_digest", "TAGS",
           "version_id"]
