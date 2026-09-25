"""Dependency-free IOS ``show`` parsers and the raw-snapshot parser.

This module is deliberately flat: it imports nothing outside the standard
library, so the deployment can copy this one file next to the Home Assistant
poller script and have it work without installing the package.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping


def parse_show_version(output: str) -> dict[str, str]:
    """Parse ``show version`` into ``{version, model, serial}``.

    A 2960X reports e.g.::

        Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M),
        Version 15.2(7)E11, RELEASE SOFTWARE (fc1)
        ...
        Model number : WS-C2960X-48LPS-L
        System serial number : FOC1234ABCD

    ``version`` is kept in IOS form (``15.2(7)E11``) — compare with
    :func:`ios_version_key`.
    """
    out: dict[str, str] = {}
    m = re.search(r"Version\s+([\d.]+(?:\(\d+\))?[A-Z]*\d*)", output)
    if m:
        out["version"] = m.group(1)
    m = re.search(r"Model number\s*:\s*(\S+)", output)
    if m:
        out["model"] = m.group(1)
    m = re.search(r"System serial number\s*:\s*(\S+)", output)
    if m:
        out["serial"] = m.group(1)
    return out


#: IOS interface-name abbreviations, longest source name first (a
#: ``TenGigabitEthernet`` prefix contains ``GigabitEthernet``).
_INTERFACE_ABBREV = (
    ("TenGigabitEthernet", "Te"),
    ("FiftyGigabitEthernet", "Fi"),
    ("FortyGigabitEthernet", "Fo"),
    ("TwentyFiveGigE", "Tw"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Port-channel", "Po"),
    ("Vlan-interface", "Vl"),
)


def interface_abbr(name: str) -> str:
    """IOS interface name in its ``show`` abbreviation (``GigabitEthernet1/0/5``
    → ``Gi1/0/5``).

    Every port-keyed ``net status`` parser returns its keys in this form,
    because the ``show`` tables disagree: ``show interfaces status`` and
    ``show mac address-table`` print the short form while the DHCP snooping
    binding table (and ``show power inline`` on some builds) print the long
    one. Joining them unnormalized splits one physical port into two rows —
    a live port listed twice, once with its MACs and once without.
    Already-abbreviated names pass through, and ``Vlan<id>`` is left alone
    (an L3 interface, not a port).
    """
    text = (name or "").strip()
    for full, short in _INTERFACE_ABBREV:
        if text.lower().startswith(full.lower()):
            return short + text[len(full):]
    return text


#: ``show interfaces status`` — the Name field can contain spaces, so a
#: regex (non-greedy name up to 2+ spaces before the status keyword) beats
#: column slicing. The other show outputs have single-token fields and are
#: whitespace-split.
_STATUS_RE = re.compile(
    r"^(?P<port>\S+)\s+(?P<name>.*?)\s{2,}"
    r"(?P<status>connected|notconnect|disabled|err-disabled|monitor)\s+"
    r"(?P<vlan>\S+)\s+(?P<duplex>\S+)\s+(?P<speed>\S+)\s+(?P<type>\S.*)$"
)
_STATUS_WORDS = ("connected", "notconnect", "disabled", "err-disabled", "monitor")


def parse_interface_status(output: str) -> dict[str, dict[str, str]]:
    """``show interfaces status`` → {port: {name, status, vlan, duplex,
    speed, type}}. Only ports with a status are kept.

    The regex handles the standard fixed-width layout (Name may contain
    spaces); a line it cannot match (e.g. a wrapped/empty type column)
    falls back to locating the status word and taking the trailing
    fields — a port must never vanish from the snapshot just because one
    column wrapped.
    """
    out: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        if not line.strip() or line.startswith(("Port", "----")):
            continue
        m = _STATUS_RE.match(line)
        if m:
            out[interface_abbr(m.group("port"))] = {
                "name": m.group("name"),
                "status": m.group("status"),
                "vlan": m.group("vlan"),
                "duplex": m.group("duplex"),
                "speed": m.group("speed"),
                "type": m.group("type"),
            }
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        port = parts[0]
        for i, tok in enumerate(parts[1:], 1):
            if tok in _STATUS_WORDS:
                rest = parts[i + 1:]
                out[interface_abbr(port)] = {
                    "name": " ".join(parts[1:i]),
                    "status": tok,
                    "vlan": rest[0] if rest else "",
                    "duplex": rest[1] if len(rest) > 1 else "",
                    "speed": rest[2] if len(rest) > 2 else "",
                    "type": " ".join(rest[3:]) if len(rest) > 3 else "",
                }
                break
    return out


def parse_mac_table(output: str) -> list[dict[str, str]]:
    """``show mac address-table`` → [{vlan, mac, type, port}]. Dynamic
    entries only (static/self entries are the switch's own MACs)."""
    out: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip() or line.startswith(("Vlan", "----")):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        vlan, mac, mtype, port = parts[0], parts[1], parts[2], parts[3]
        if mtype.upper() != "DYNAMIC":
            continue
        out.append({"vlan": vlan, "mac": mac, "type": mtype,
                    "port": interface_abbr(port)})
    return out


def parse_ip_arp(output: str) -> list[dict[str, str]]:
    """``show ip arp`` → [{ip, mac, age, interface}]. ARPA entries only."""
    out: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip() or line.startswith(("Protocol", "----")):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        proto, ip, age, mac, mtype, interface = parts[:6]
        if mtype.upper() != "ARPA":
            continue
        out.append({"ip": ip, "mac": mac, "age": age, "interface": interface})
    return out


#: Shapes that make a binding row a *row*: an IPv4 literal and a MAC in either
#: the dotted or the colon form.
_IPV4_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_MAC_RE = re.compile(r"(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2}",
                     re.IGNORECASE)


def parse_dhcp_snooping_binding(output: str) -> list[dict[str, str]]:
    """``show ip dhcp snooping binding`` → [{ip, mac, vlan, port, lease, type}].

    The one address source that is both **per-port** and switch-local: every
    DHCP exchange a snooping-enabled port relays is recorded here, so the
    binding table names the address of the device hanging off each port even
    when the switch is pure L2 for that vlan and its own ARP table never sees
    the address.

    The table is **mac-first**: a 2960X prints

        MacAddress          IpAddress        Lease(sec)  Type           VLAN  Interface
        ------------------  ---------------  ----------  -------------  ----  --------------------
        00:11:22:33:44:55   10.0.30.7        86400        dhcp-snooping   30    GigabitEthernet1/0/5
        Total number of bindings: 1

    so a row is identified by shape — a MAC, then an IPv4 literal — rather than
    by position. Reading it as ``ip mac vlan …`` (which this parser did in
    ``machines.net_ops`` before it moved here) dropped *every* row a real
    switch prints, and dropped them silently: the source simply named no
    addresses. Ports are abbreviated like every other port-keyed part, and the
    header, the ``----`` rule and the trailer fail the shape check.
    """
    out: list[dict[str, str]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        mac, ip, lease, kind, vlan, port = parts[:6]
        if not _MAC_RE.fullmatch(mac) or not _IPV4_RE.fullmatch(ip):
            continue
        out.append({
            "ip": ip,
            "mac": mac,
            "vlan": vlan,
            "port": interface_abbr(port),
            "lease": lease,
            "type": kind,
        })
    return out


def parse_interface_counters(output: str) -> dict[str, dict[str, int]]:
    """``show interfaces counters`` → {port: {in_octets, in_pkts,
    out_octets, out_pkts}}. Values are 64-bit counters (may exceed 2^32)."""
    out: dict[str, dict[str, int]] = {}
    columns: list[str] = []
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Port":
            columns = parts[1:]
            continue
        # Retain support for legacy headerless combined counter snapshots.
        keys = columns or ["InOctets", "InUcastPkts", "OutOctets", "OutUcastPkts"]
        if len(parts) < len(keys) + 1:
            continue
        try:
            # The guard above is what makes the lengths equal.
            values = dict(zip(keys, map(int, parts[1:len(keys) + 1]), strict=True))
        except ValueError:
            continue
        port = out.setdefault(interface_abbr(parts[0]), {})
        for direction in ("In", "Out"):
            prefix = direction.lower()
            if direction + "Octets" in values:
                port[prefix + "_octets"] = values[direction + "Octets"]
            packet_columns = [direction + kind + "Pkts" for kind in ("Ucast", "Mcast", "Bcast")]
            if direction + "UcastPkts" in values:
                port[prefix + "_pkts"] = sum(values.get(key, 0) for key in packet_columns)
    return out


def parse_power_inline(output: str) -> dict[str, dict[str, str]]:
    """``show power inline`` → {port: {admin, oper, watts, device, class,
    max}}. The Device field can contain spaces, so it is the joined middle
    tokens; class/max are the last two."""
    out: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        if not line.strip() or line.startswith(("Interface", "----")):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        out[interface_abbr(parts[0])] = {
            "admin": parts[1],
            "oper": parts[2],
            "watts": parts[3],
            "device": " ".join(parts[4:-2]),
            "class": parts[-2],
            "max": parts[-1],
        }
    return out


def parse_ip_interface_brief(output: str) -> dict[str, dict[str, str]]:
    """``show ip interface brief`` → {interface: {ip, ok, method, status,
    protocol}}. The switch's own SVIs — including a DHCP'd WAN leg (the
    IP the upstream modem handed out)."""
    out: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        if not line.strip() or line.startswith(("Interface", "----")):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        out[parts[0]] = {
            "ip": parts[1],
            "ok": parts[2],
            "method": parts[3],
            "status": parts[4],
            "protocol": parts[5],
        }
    return out


#: Show key → the parser for its output, in the order the commands run.
PARSERS: dict[str, Callable[[str], object]] = {
    "version": parse_show_version,
    "interfaces": parse_interface_status,
    "macs": parse_mac_table,
    "binding": parse_dhcp_snooping_binding,
    "arp": parse_ip_arp,
    "counters": parse_interface_counters,
    "poe": parse_power_inline,
    "ip_brief": parse_ip_interface_brief,
}

#: Commands whose output the snapshot is useless without: an empty parse is
#: reported as an error rather than as an empty table.
_REQUIRED = ("version", "interfaces")

_NO_OUTPUT = ("No recognizable IOS status output; check exporter privilege "
              "and CLI session")


def tables_of(value: object) -> list[Mapping[str, object]]:
    """The list-of-tables a parser returned (``macs``, ``arp``), or nothing.

    Consumers of :func:`parse_snapshot` read rows through this and
    :func:`field_of`, so a snapshot stays readable without repeating the
    narrowing at every use.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def field_of(row: Mapping[str, object], key: str) -> str:
    """A string field of a parsed row, or ``""`` when it is absent."""
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _merge_arp_into_ports(macs: list[Mapping[str, object]],
                          arp: list[Mapping[str, object]],
                          ) -> dict[str, dict[str, list[str]]]:
    """Bind each ARP address to the port its MAC was learned on.

    Joined only within a VLAN: the same MAC can occur in several, and joining
    across them would put one port's addresses on another's.
    """
    ports: dict[str, dict[str, list[str]]] = {}
    for entry in macs:
        port = field_of(entry, "port")
        vlan = field_of(entry, "vlan")
        mac = _normalize_mac(field_of(entry, "mac"))
        if not port or not mac:
            continue
        ips = ports.setdefault(port, {"ips": []})["ips"]
        for address in arp:
            ip = field_of(address, "ip")
            if (ip and ip not in ips
                    and _normalize_mac(field_of(address, "mac")) == mac
                    and field_of(address, "interface").lower() == "vlan" + vlan):
                ips.append(ip)
    return ports


def _normalize_mac(mac: str) -> str:
    """``aabb.ccdd.eeff`` and ``AA:BB:CC:DD:EE:FF`` are the same MAC."""
    return "".join(character for character in mac.lower() if character.isalnum())


def parse_snapshot(raw: Mapping[str, object]) -> dict[str, object]:
    """Feed the raw show output through the show parsers.

    The exporter returns ``{switches: {sid: {key: <raw>, ...}}}``, where each
    raw field is either the show output or ``__error__: <message>``. Each
    command is parsed independently, so one that failed does not hide the
    others: its key becomes ``{"error": <message>}`` and the rest still carry
    data. A whole-snapshot ``error`` produces ``{"error": ..., "switches": {}}``.
    """
    error = raw.get("error")
    if isinstance(error, str) and error:
        return {"error": error, "switches": {}}
    switches = raw.get("switches")
    if not isinstance(switches, Mapping):
        switches = {}
    out: dict[str, object] = {}
    for sid, switch in switches.items():
        if not isinstance(switch, Mapping):
            continue
        parsed: dict[str, object] = {}
        for key, parser in PARSERS.items():
            text = field_of(switch, key)
            if text.startswith("__error__:"):
                parsed[key] = {"error": text[len("__error__:"):].strip()}
                continue
            try:
                value = parser(text)
            except Exception as exc:  # noqa: BLE001 — one bad command must not hide the rest
                parsed[key] = {"error": str(exc)}
                continue
            parsed[key] = {"error": _NO_OUTPUT} if key in _REQUIRED and not value else value
        parsed["ports"] = _merge_arp_into_ports(
            tables_of(parsed.get("macs")), tables_of(parsed.get("arp")))
        out[str(sid)] = parsed
    return {"switches": out}
