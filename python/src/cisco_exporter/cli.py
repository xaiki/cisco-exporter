"""Command line: serve the metrics, or print them once."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .bridge import DEFAULT_INTERVAL, Bridge
from .client import Exporter

#: Loopback by default: the metrics need no credential of their own, so they
#: are not exposed beyond the host unless it is asked for.
DEFAULT_LISTEN = "127.0.0.1:9101"

#: The settings this command needs, as the flag that supplies each one.
_REQUIRED = (("--url", "url"), ("--ha-token", "ha_token"),
             ("--exporter-token", "exporter_token"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cisco-exporter-metrics",
        description="Prometheus metrics for Cisco IOS switches, from a "
                    "cisco-exporter's raw snapshot.")
    parser.add_argument("--settings", metavar="PATH",
                        help="JSON file holding url, ha_token and exporter_token, "
                             "the keys the Home Assistant deployment writes")
    parser.add_argument("--url", help="the exporter's URL, e.g. "
                                      "http://cisco-exporter:8788/api/status")
    parser.add_argument("--ha-token", help="the bearer token the exporter checks")
    parser.add_argument("--exporter-token", help="the token the exporter stamps on its responses")
    parser.add_argument("--timeout", type=float,
                        help="seconds to wait for the exporter (default: 25)")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, metavar="HOST:PORT",
                        help="serve /metrics here (default: %(default)s)")
    parser.add_argument("--interval", type=float,
                        help="seconds between fetches of the exporter (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="print the metrics once and exit, instead of serving")
    return parser


def _text_option(args: argparse.Namespace, name: str) -> str | None:
    """A string flag, narrowed (``argparse`` hands back untyped values)."""
    value = getattr(args, name, None)
    return value if isinstance(value, str) and value else None


def _number_option(args: argparse.Namespace, name: str) -> float | None:
    """A numeric flag, narrowed."""
    value = getattr(args, name, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _flag(args: argparse.Namespace, name: str) -> bool:
    """A boolean flag, narrowed."""
    return getattr(args, name, None) is True


def _settings(path: str) -> dict[str, object]:
    """Read a settings file, narrowed at the read (``json.loads`` is untyped)."""
    raw: object = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise RuntimeError(f"settings {path}: not a JSON object")
    return {str(key): item for key, item in raw.items()}


def _target(args: argparse.Namespace) -> Exporter:
    """Resolve the exporter from the settings file and the flags, flags winning.

    The attribute names here are argparse's destinations, which drop the
    hyphens from the flag names.
    """
    path = _text_option(args, "settings")
    settings = Exporter.from_settings(_settings(path) if path else {})
    timeout = _number_option(args, "timeout")
    return Exporter(
        url=_text_option(args, "url") or settings.url,
        ha_token=_text_option(args, "ha_token") or settings.ha_token,
        exporter_token=_text_option(args, "exporter_token") or settings.exporter_token,
        timeout=settings.timeout if timeout is None else timeout,
    )


def _listen(value: str) -> tuple[str, int]:
    """``host:port``, split on the last colon."""
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise ValueError(f"--listen {value}: expected host:port")
    try:
        number = int(port)
    except ValueError:
        raise ValueError(f"--listen {value}: port is not a number") from None
    if not 1 <= number <= 65535:
        raise ValueError(f"--listen {value}: port is out of range")
    return host, number


def main(argv: Sequence[str] | None = None) -> int:
    """The console script. 2 is a usage error, 1 a settings or runtime failure."""
    args = _parser().parse_args(argv)
    try:
        target = _target(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    provided = {"url": target.url, "ha_token": target.ha_token,
                "exporter_token": target.exporter_token}
    gaps = [flag for flag, attribute in _REQUIRED if not provided[attribute]]
    if gaps:
        print(f"{' '.join(gaps)} required, as a flag or in --settings", file=sys.stderr)
        return 2
    if target.timeout <= 0:
        print("--timeout must be greater than 0", file=sys.stderr)
        return 2
    interval = _number_option(args, "interval")
    if interval is None:
        interval = DEFAULT_INTERVAL
    elif interval <= 0:
        print("--interval must be greater than 0", file=sys.stderr)
        return 2
    bridge = Bridge(target, interval)
    if _flag(args, "once"):
        ok = bridge.refresh()
        sys.stdout.write(bridge.body().decode())
        return 0 if ok else 1
    listen = _text_option(args, "listen") or DEFAULT_LISTEN
    try:
        host, port = _listen(listen)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        bridge.serve(host, port)
    except OSError as exc:
        print(f"listen {listen}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
