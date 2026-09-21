"""Render a parsed snapshot in the Prometheus text exposition format.

The exporter is a pipe that returns raw IOS text, so this module is the
standalone consumer: it turns :func:`cisco_exporter.parsers.parse_snapshot`
output into metrics. It decides what is interesting; the exporter does not.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .parsers import PARSERS, field_of, tables_of

#: Content type of the exposition format this module emits (version 0.0.4).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The only `show interfaces status` word that means the link is up.
_LINK_UP = "connected"

SCRAPE_OK = "cisco_exporter_scrape_ok"
SCRAPE_AGE = "cisco_exporter_scrape_age_seconds"
SCRAPE_SECONDS = "cisco_exporter_scrape_duration_seconds"
COMMAND_OK = "cisco_switch_command_ok"
VERSION_INFO = "cisco_switch_version_info"
INTERFACE_UP = "cisco_interface_up"
INTERFACE_INFO = "cisco_interface_info"
IN_OCTETS = "cisco_interface_in_octets_total"
IN_PKTS = "cisco_interface_in_pkts_total"
OUT_OCTETS = "cisco_interface_out_octets_total"
OUT_PKTS = "cisco_interface_out_pkts_total"
POE_WATTS = "cisco_poe_watts"
POE_INFO = "cisco_poe_info"
MAC_TOTAL = "cisco_mac_entries_total"
MAC_BY_PORT = "cisco_mac_entries_by_port"
ARP_TOTAL = "cisco_arp_entries_total"
IP_UP = "cisco_ip_interface_up"
IP_INFO = "cisco_ip_interface_info"
PORT_ADDRESS = "cisco_port_address_info"

#: Every family this module can emit, in the order they are rendered. A
#: family with no samples is omitted from the output entirely.
_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    (SCRAPE_OK, "gauge", "1 when the last fetch of the exporter succeeded."),
    (SCRAPE_AGE, "gauge", "Seconds since the last fetch of the exporter finished."),
    (SCRAPE_SECONDS, "gauge", "Seconds the last fetch of the exporter took."),
    (COMMAND_OK, "gauge", "1 when the switch answered this show command, 0 when it failed."),
    (VERSION_INFO, "gauge", "IOS version, model and serial number of the switch."),
    (INTERFACE_UP, "gauge", "1 when the port's status is connected, 0 otherwise."),
    (INTERFACE_INFO, "gauge", "The port's row from show interfaces status."),
    (IN_OCTETS, "counter", "Bytes received on the port since the switch booted."),
    (IN_PKTS, "counter", "Packets received on the port since the switch booted."),
    (OUT_OCTETS, "counter", "Bytes sent on the port since the switch booted."),
    (OUT_PKTS, "counter", "Packets sent on the port since the switch booted."),
    (POE_WATTS, "gauge", "Power drawn by the device on the port, in watts."),
    (POE_INFO, "gauge", "The port's row from show power inline."),
    (MAC_TOTAL, "gauge", "Dynamic MAC addresses the switch has learned."),
    (MAC_BY_PORT, "gauge", "Dynamic MAC addresses the switch learned on the port."),
    (ARP_TOTAL, "gauge", "ARP entries the switch knows."),
    (IP_UP, "gauge", "1 when the interface's status and protocol are both up."),
    (IP_INFO, "gauge", "The interface's row from show ip interface brief."),
    (PORT_ADDRESS, "gauge", "An IP whose MAC the switch learned on this port."),
)

_COUNTER_KEYS = (
    ("in_octets", IN_OCTETS),
    ("in_pkts", IN_PKTS),
    ("out_octets", OUT_OCTETS),
    ("out_pkts", OUT_PKTS),
)

_INTERFACE_FIELDS = ("name", "status", "vlan", "duplex", "speed", "type")
_POE_FIELDS = ("admin", "oper", "device", "class", "max")


@dataclass(frozen=True)
class Scrape:
    """What the consumer knows about its own last fetch of the exporter.

    The exporter caches nothing, so "did that work, and how stale is it" is
    only knowable here, by whoever did the fetching.
    """

    ok: bool
    age_seconds: float
    duration_seconds: float


class _Family:
    """One metric family: its HELP/TYPE header, then its samples."""

    def __init__(self, name: str, kind: str, help_text: str) -> None:
        self._name = name
        self._kind = kind
        self._help = help_text
        self._samples: list[str] = []

    def add(self, labels: Mapping[str, str], value: str) -> None:
        if not labels:
            self._samples.append(f"{self._name} {value}")
            return
        rendered = ",".join(f'{key}="{_escape(item)}"' for key, item in labels.items())
        self._samples.append(f"{self._name}{{{rendered}}} {value}")

    def lines(self) -> list[str]:
        if not self._samples:
            return []
        return [f"# HELP {self._name} {self._help}",
                f"# TYPE {self._name} {self._kind}",
                *self._samples]


def _escape(value: str) -> str:
    """Escape a label value: backslash, newline and quote."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _seconds(value: float) -> str:
    """Seconds with millisecond precision, so output is byte-stable."""
    return f"{value:.3f}"


def _table(value: object) -> Mapping[str, object]:
    """A parsed table value, or an empty one."""
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> Mapping[str, Mapping[str, object]]:
    """A parser's {key: {field: value}} table, skipping malformed rows."""
    return {str(key): row for key, row in _table(value).items()
            if isinstance(row, Mapping)}


def _info(family: _Family, labels: Mapping[str, str]) -> None:
    """An info metric: the state as labels, with the value 1."""
    family.add(labels, "1")


def _watts(text: str) -> float | None:
    """The watts column, which is a number of text words (``off``, ``n/a``)."""
    try:
        return float(text)
    except ValueError:
        return None


def _render_scrape(families: dict[str, _Family], scrape: Scrape) -> None:
    families[SCRAPE_OK].add({}, "1" if scrape.ok else "0")
    families[SCRAPE_AGE].add({}, _seconds(scrape.age_seconds))
    families[SCRAPE_SECONDS].add({}, _seconds(scrape.duration_seconds))


def _render_commands(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    """Whether each show command produced data, in the order it was run.

    The failure message is not a label: it is unbounded text, and it stays
    available through the exporter's own ``GET /api/status``.
    """
    for command in PARSERS:
        failed = "error" in _table(switch.get(command))
        families[COMMAND_OK].add({"switch": sid, "command": command},
                                 "0" if failed else "1")


def _render_version(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    version = _table(switch.get("version"))
    if not version or "error" in version:
        return
    _info(families[VERSION_INFO], {
        "switch": sid,
        "version": field_of(version, "version"),
        "model": field_of(version, "model"),
        "serial": field_of(version, "serial"),
    })


def _render_interfaces(families: dict[str, _Family], sid: str,
                       switch: Mapping[str, object]) -> None:
    for port, row in _rows(switch.get("interfaces")).items():
        labels = {"switch": sid, "port": port}
        status = field_of(row, "status")
        families[INTERFACE_UP].add(labels, "1" if status == _LINK_UP else "0")
        _info(families[INTERFACE_INFO],
              {**labels, **{name: field_of(row, name) for name in _INTERFACE_FIELDS}})


def _render_counters(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    for port, row in _rows(switch.get("counters")).items():
        labels = {"switch": sid, "port": port}
        for key, family in _COUNTER_KEYS:
            count = row.get(key)
            if isinstance(count, int):
                families[family].add(labels, str(count))


def _render_poe(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    for port, row in _rows(switch.get("poe")).items():
        labels = {"switch": sid, "port": port}
        _info(families[POE_INFO],
              {**labels, **{name: field_of(row, name) for name in _POE_FIELDS}})
        watts = _watts(field_of(row, "watts"))
        if watts is not None:
            families[POE_WATTS].add(labels, _seconds(watts))


def _render_macs(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    macs = tables_of(switch.get("macs"))
    families[MAC_TOTAL].add({"switch": sid}, str(len(macs)))
    per_port: dict[str, int] = {}
    for entry in macs:
        port = field_of(entry, "port")
        if port:
            per_port[port] = per_port.get(port, 0) + 1
    for port in sorted(per_port):
        families[MAC_BY_PORT].add({"switch": sid, "port": port}, str(per_port[port]))


def _render_arp(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    families[ARP_TOTAL].add({"switch": sid}, str(len(tables_of(switch.get("arp")))))


def _render_ip_brief(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    for interface, row in _rows(switch.get("ip_brief")).items():
        labels = {"switch": sid, "interface": interface}
        status, protocol = field_of(row, "status"), field_of(row, "protocol")
        families[IP_UP].add(labels, "1" if status == "up" and protocol == "up" else "0")
        _info(families[IP_INFO], {**labels, "ip": field_of(row, "ip"),
                                  "method": field_of(row, "method"),
                                  "status": status, "protocol": protocol})


def _render_port_addresses(families: dict[str, _Family], sid: str,
                           switch: Mapping[str, object]) -> None:
    """The MAC-only join the parser did, as one series per port and address."""
    for port, row in _rows(switch.get("ports")).items():
        addresses = row.get("ips")
        for address in addresses if isinstance(addresses, list) else []:
            if isinstance(address, str) and address:
                _info(families[PORT_ADDRESS],
                      {"switch": sid, "port": port, "ip": address})


def _render_switch(families: dict[str, _Family], sid: str, switch: Mapping[str, object]) -> None:
    _render_commands(families, sid, switch)
    _render_version(families, sid, switch)
    _render_interfaces(families, sid, switch)
    _render_counters(families, sid, switch)
    _render_poe(families, sid, switch)
    _render_macs(families, sid, switch)
    _render_arp(families, sid, switch)
    _render_ip_brief(families, sid, switch)
    _render_port_addresses(families, sid, switch)


def _families() -> dict[str, _Family]:
    """Every family this module can emit, ready to be filled in."""
    return {name: _Family(name, kind, help_text)
            for name, kind, help_text in _DEFINITIONS}


def _emit(families: dict[str, _Family]) -> str:
    """The filled families, in the order they are declared."""
    lines: list[str] = []
    for name, _, _ in _DEFINITIONS:
        lines.extend(families[name].lines())
    return "\n".join([*lines, ""]) if lines else ""


def render_scrape(scrape: Scrape) -> str:
    """The scrape state alone: whether the last fetch worked, and how old it is.

    A scraper needs this computed when it asks, not when the data was fetched:
    an age frozen at fetch time would always read zero. The bridge renders this
    per request and concatenates it with the cached switch state.
    """
    families = _families()
    _render_scrape(families, scrape)
    return _emit(families)


def render_switches(parsed: Mapping[str, object]) -> str:
    """The per-switch state of a parsed snapshot, and nothing about the scrape.

    A switch whose command failed still gets its ``cisco_switch_command_ok 0``
    sample, so a scrape that reached the exporter always says which part of
    the switch went quiet.
    """
    families = _families()
    switches = parsed.get("switches")
    if isinstance(switches, Mapping):
        for sid, switch in switches.items():
            if isinstance(switch, Mapping):
                _render_switch(families, str(sid), switch)
    return _emit(families)


def render(parsed: Mapping[str, object], scrape: Scrape) -> str:
    """Both halves, in the exposition format: the scrape state, then the switches."""
    return render_scrape(scrape) + render_switches(parsed)
