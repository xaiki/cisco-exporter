"""The IOS parsers and the raw-snapshot parser.

These are the parser unit tests, so they live with the parsers rather than with
a consumer of them. They came here from the Home Assistant repository's
``tests/test_net_status.py``, along with the code: the cases that exercise
functions still defined in ``machines.net_ops`` (the dnsmasq leases and the TDR
pairing) stayed there, and the DHCP-snooping binding followed its parser here.

The fixtures are captured ``show`` output, quirks included — a wrapped Type
column, a static MAC entry, a 64-bit counter, a port named in long form. Keep
them that way: a parser test that needs a switch cannot run.
"""
from __future__ import annotations

from collections.abc import Mapping

from cisco_exporter.parsers import (
    field_of,
    interface_abbr,
    parse_dhcp_snooping_binding,
    parse_interface_counters,
    parse_interface_status,
    parse_ip_arp,
    parse_ip_interface_brief,
    parse_mac_table,
    parse_power_inline,
    parse_snapshot,
    tables_of,
)

IF_STATUS = """\
Port      Name               Status       Vlan     Duplex  Speed Type
Gi0/1     uplink             connected    1        a-full  a-100 10/100/1000BaseTX
Gi0/2                        notconnect   1        auto    auto  10/100/1000BaseTX
Gi0/3     camera-1           connected    30       a-full  a-1000 10/100/1000BaseTX
Gi0/10    printer            connected    70       a-full  a-100 10/100/1000BaseTX
"""

MAC_TABLE = """\
          Mac Address Table
-------------------------------------------
Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
  30    0011.2233.4455    DYNAMIC     Gi0/3
  70    aabb.ccdd.eeff    DYNAMIC     Gi0/10
  70    1122.3344.5566    STATIC      Gi0/10
   1    ccdd.eeff.0011    DYNAMIC     Gi0/1
"""

IP_ARP = """\
Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  192.0.2.5               -   0011.2233.4455  ARPA   Vlan30
Internet  198.51.100.10           2   aabb.ccdd.eeff  ARPA   Vlan70
Internet  198.51.100.11           0   1122.3344.5566  ARPA   Vlan70
"""

#: The shape a 2960X prints — mac first, one lease column, the type, then vlan
#: and a long interface name, with a trailer. Captured from the fleet; the
#: addresses are placeholders, and dropping the trailer is the parser's job.
SNOOPING_BINDING = """\
MacAddress          IpAddress        Lease(sec)  Type           VLAN  Interface
------------------  ---------------  ----------  -------------  ----  --------------------
00:11:22:33:44:55   10.0.30.7        86400        dhcp-snooping   30    GigabitEthernet0/3
aa:bb:cc:dd:ee:ff   10.0.70.9        85462        dhcp-snooping   70    GigabitEthernet0/10
Total number of bindings: 2
"""

COUNTERS = """\
Port        InOctets    InUcastPkts    OutOctets   OutUcastPkts
Gi0/1       1234567890      1234567    9876543210      7654321
Gi0/3        456789012       234567     8765432109      6543210
Gi0/10      1000000000000    9999999    2000000000000    8888888
"""

POWER = """\
Interface Admin  Oper       Power   Device              Class Max
                          (Watts)
--------- ------ ---------- ------- ------------------- ----- ----
Gi0/1     auto   on         15.4    IP Phone 7960       4     30.0
Gi0/2     auto   off        0.0     n/a                 n/a   30.0
Gi0/3     auto   on         4.5     Camera              3     30.0
"""

IP_BRIEF = """\
Interface              IP-Address      OK? Method Status                Protocol
Vlan10                 192.0.2.2       YES NVRAM  up                    up
Vlan70                 192.0.2.1       YES NVRAM  up                    up
Vlan99                 203.0.113.10    YES DHCP   up                    up
GigabitEthernet0/1     unassigned      YES unset  up                    up
"""

_VERSION_AND_MODEL = (
    "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), "
    "Version 15.2(7)E11\n"
    "Model number : WS-C2960X-48LPS-L\n"
)

_INTERFACE_ROW = (
    "Port      Name   Status       Vlan   Duplex  Speed Type\n"
    "Gi0/1            connected   10     a-full a-100 10/100/1000BaseTX\n"
)


# -- one port, one name: every port-keyed parser canonicalizes -------------

def test_interface_abbr() -> None:
    assert interface_abbr("GigabitEthernet1/0/5") == "Gi1/0/5"
    # TenGigabitEthernet contains GigabitEthernet — the longer name wins
    assert interface_abbr("TenGigabitEthernet1/1/1") == "Te1/1/1"
    assert interface_abbr("FastEthernet0/12") == "Fa0/12"
    assert interface_abbr("Port-channel1") == "Po1"
    assert interface_abbr("Gi0/3") == "Gi0/3"  # already short
    assert interface_abbr("Vlan99") == "Vlan99"  # not a port
    assert interface_abbr("") == ""


def test_port_keyed_parsers_all_agree_on_the_port_name() -> None:
    """IOS abbreviates in some tables and not others (``show power inline``
    and the snooping binding table print the long form on some builds).
    Unnormalized, that splits one physical port into two snapshot rows — a
    live port printed twice, once with its MACs and once without.

    ``parse_tdr`` canonicalizes too, and its case stayed with `net_ops`.
    """
    assert parse_interface_status(
        "GigabitEthernet1/0/5  cam  connected  30  a-full  a-1000 1000BaseTX\n"
    )["Gi1/0/5"]["status"] == "connected"
    assert parse_mac_table(
        "  30    0011.2233.4455    DYNAMIC     GigabitEthernet1/0/5\n"
    )[0]["port"] == "Gi1/0/5"
    assert parse_power_inline(
        "GigabitEthernet1/0/5 auto on 4.5 Camera 3 30.0\n")["Gi1/0/5"]["watts"] == "4.5"
    assert parse_interface_counters(
        "GigabitEthernet1/0/5   11   1   22   2     0    0    0    0    0    0\n"
    )["Gi1/0/5"]["in_octets"] == 11


def test_port_names_are_left_alone_when_already_canonical() -> None:
    """The common case must not change shape: these fixtures are all
    short-form, and the parsers keep their keys verbatim."""
    assert set(parse_interface_status(IF_STATUS)) == {"Gi0/1", "Gi0/2", "Gi0/3", "Gi0/10"}
    assert set(parse_power_inline(POWER)) == {"Gi0/1", "Gi0/2", "Gi0/3"}
    assert set(parse_interface_counters(COUNTERS)) == {"Gi0/1", "Gi0/3", "Gi0/10"}


# -- the parsers themselves -------------------------------------------------

def test_parse_interface_status() -> None:
    out = parse_interface_status(IF_STATUS)
    assert out["Gi0/1"]["status"] == "connected"
    assert out["Gi0/1"]["name"] == "uplink"
    assert out["Gi0/1"]["vlan"] == "1"
    assert out["Gi0/1"]["speed"] == "a-100"
    assert out["Gi0/2"]["status"] == "notconnect"
    assert out["Gi0/10"]["vlan"] == "70"
    assert out["Gi0/10"]["name"] == "printer"


def test_parse_interface_status_fallback_wrapped_type() -> None:
    """A line the fixed-width regex cannot match (empty/wrapped type column)
    still parses via the status-word fallback — a port must never vanish from
    the snapshot."""
    out = parse_interface_status(
        "Port      Name               Status       Vlan     Duplex  Speed Type\n"
        "Gi2/0/25  camera-2           notconnect   30       auto    auto\n"
        "Gi2/0/26                      disabled     999      auto    auto\n")
    assert out["Gi2/0/25"]["status"] == "notconnect"
    assert out["Gi2/0/25"]["name"] == "camera-2"
    assert out["Gi2/0/25"]["vlan"] == "30"
    assert out["Gi2/0/26"]["status"] == "disabled"
    assert out["Gi2/0/26"]["vlan"] == "999"


def test_parse_mac_table_filters_static() -> None:
    out = parse_mac_table(MAC_TABLE)
    # STATIC (the switch's own MAC) is dropped
    assert all(entry["type"] == "DYNAMIC" for entry in out)
    macs = {(entry["mac"], entry["port"]) for entry in out}
    assert ("0011.2233.4455", "Gi0/3") in macs
    assert ("aabb.ccdd.eeff", "Gi0/10") in macs
    assert ("ccdd.eeff.0011", "Gi0/1") in macs
    assert all(entry["vlan"] for entry in out)


def test_parse_ip_arp() -> None:
    out = parse_ip_arp(IP_ARP)
    by_ip = {entry["ip"]: entry for entry in out}
    assert by_ip["192.0.2.5"]["mac"] == "0011.2233.4455"
    assert by_ip["198.51.100.10"]["mac"] == "aabb.ccdd.eeff"
    assert by_ip["198.51.100.10"]["interface"] == "Vlan70"


def test_parse_dhcp_snooping_binding_is_per_port() -> None:
    out = parse_dhcp_snooping_binding(SNOOPING_BINDING)
    by_ip = {entry["ip"]: entry for entry in out}
    assert by_ip["10.0.30.7"]["mac"] == "00:11:22:33:44:55"
    assert by_ip["10.0.30.7"]["port"] == "Gi0/3"
    assert by_ip["10.0.30.7"]["vlan"] == "30"
    assert by_ip["10.0.30.7"]["lease"] == "86400"
    assert by_ip["10.0.30.7"]["type"] == "dhcp-snooping"
    # the long interface form is abbreviated, like every other port-keyed part
    assert by_ip["10.0.70.9"]["port"] == "Gi0/10"
    assert len(out) == 2


def test_parse_dhcp_snooping_binding_reads_the_mac_first_table() -> None:
    # The captured column order is mac-then-ip. A parser that assumed the
    # reverse — as this one did while it lived in machines.net_ops — dropped
    # every row a real switch prints, and dropped them silently, so the whole
    # source simply named no addresses. Both MAC spellings are accepted,
    # because the dotted form turns up on some builds.
    dotted = ("0011.2233.4455   10.0.30.8   86400   dhcp-snooping   30   "
              "Gi0/6\n")
    assert [e["ip"] for e in parse_dhcp_snooping_binding(SNOOPING_BINDING)] == [
        "10.0.30.7", "10.0.70.9"]
    assert [e["ip"] for e in parse_dhcp_snooping_binding(dotted)] == ["10.0.30.8"]


def test_binding_rows_that_are_not_rows_are_dropped() -> None:
    # the header, the rule, the trailer, and the notice a switch without
    # snooping prints instead of rows
    assert parse_dhcp_snooping_binding("") == []
    assert parse_dhcp_snooping_binding(
        "MacAddress  IpAddress  Lease(sec)\n----  ----  ----\n"
        "Total number of bindings: 0\n"
    ) == []
    assert parse_dhcp_snooping_binding("% DHCP snooping is not enabled\n") == []


def test_parse_interface_counters_64bit() -> None:
    out = parse_interface_counters(COUNTERS)
    assert out["Gi0/1"]["in_octets"] == 1234567890
    assert out["Gi0/1"]["out_octets"] == 9876543210
    # a 64-bit counter exceeds 2**32
    assert out["Gi0/10"]["in_octets"] == 1000000000000
    assert out["Gi0/10"]["out_pkts"] == 8888888


def test_parse_power_inline() -> None:
    out = parse_power_inline(POWER)
    assert out["Gi0/1"]["oper"] == "on"
    assert out["Gi0/1"]["watts"] == "15.4"
    assert out["Gi0/1"]["device"] == "IP Phone 7960"
    assert out["Gi0/2"]["oper"] == "off"
    assert out["Gi0/3"]["watts"] == "4.5"


def test_parse_ip_interface_brief() -> None:
    out = parse_ip_interface_brief(IP_BRIEF)
    assert out["Vlan10"]["ip"] == "192.0.2.2"
    assert out["Vlan10"]["method"] == "NVRAM"
    assert out["Vlan99"]["ip"] == "203.0.113.10"  # the modem-given WAN leg
    assert out["Vlan99"]["method"] == "DHCP"
    assert out["GigabitEthernet0/1"]["ip"] == "unassigned"


def test_counters_merge_ios_rx_and_tx_tables() -> None:
    """A build that prints the receive and transmit tables separately still
    yields one row per port."""
    assert parse_interface_counters("""Port InOctets InUcastPkts InMcastPkts InBcastPkts
Gi1/0/1 1000 10 2 3
Port OutOctets OutUcastPkts OutMcastPkts OutBcastPkts
Gi1/0/1 2000 20 3 4
""") == {"Gi1/0/1": {"in_octets": 1000, "in_pkts": 15,
                     "out_octets": 2000, "out_pkts": 27}}


# -- the raw-snapshot parser ------------------------------------------------

def _switches(raw: Mapping[str, object]) -> Mapping[str, object]:
    switches = parse_snapshot(raw)["switches"]
    assert isinstance(switches, Mapping)
    return switches


def _table(raw: Mapping[str, object], sid: str, key: str) -> Mapping[str, object]:
    switch = _switches(raw)[sid]
    assert isinstance(switch, Mapping)
    value = switch[key]
    assert isinstance(value, Mapping)
    return value


def _port_ips(raw: Mapping[str, object]) -> list[str]:
    """The IPs the parser bound to ``Gi1/0/1``."""
    ports = _table(raw, "edge", "ports")
    port = ports["Gi1/0/1"]
    assert isinstance(port, Mapping)
    addresses = port["ips"]
    assert isinstance(addresses, list)
    return [address for address in addresses if isinstance(address, str)]


def _joined_raw(arp: str) -> dict[str, object]:
    return {"switches": {"edge": {
        "macs": "20 0011.2233.4455 DYNAMIC Gi1/0/1",
        "arp": arp,
    }}}


def test_parse_snapshot_parses_every_key_and_keeps_a_failed_one() -> None:
    raw: dict[str, object] = {"switches": {
        "north": {"version": _VERSION_AND_MODEL, "interfaces": _INTERFACE_ROW,
                  "macs": "", "arp": "", "counters": "", "poe": "", "ip_brief": ""},
        "south": {"version": "__error__: ssh south: Connection refused",
                  "interfaces": "", "macs": "", "arp": "", "counters": "",
                  "poe": "", "ip_brief": ""},
    }}
    version = _table(raw, "north", "version")
    assert version["version"] == "15.2(7)E11"
    assert version["model"] == "WS-C2960X-48LPS-L"
    assert _table(raw, "north", "interfaces")["Gi0/1"] == {
        "name": "", "status": "connected", "vlan": "10",
        "duplex": "a-full", "speed": "a-100", "type": "10/100/1000BaseTX"}
    # a failed switch carries the exporter's error, not a crash
    assert "error" in _table(raw, "south", "version")


def test_parse_snapshot_reports_a_whole_snapshot_error() -> None:
    assert parse_snapshot({"error": "exporter unreachable"}) == {
        "error": "exporter unreachable", "switches": {}}


def test_parse_snapshot_survives_a_body_that_is_not_a_snapshot() -> None:
    assert parse_snapshot({}) == {"switches": {}}
    assert parse_snapshot({"switches": "not a mapping"}) == {"switches": {}}
    assert parse_snapshot({"switches": {"edge": "not a mapping"}}) == {"switches": {}}


def test_empty_ios_output_is_reported_as_collection_failure() -> None:
    raw: dict[str, object] = {"switches": {"edge": {
        "version": "switch>enable\nPassword:\n", "interfaces": "", "macs": ""}}}
    assert "error" in _table(raw, "edge", "version")
    assert "error" in _table(raw, "edge", "interfaces")
    # An empty learned MAC table can be valid.
    switch = _switches(raw)["edge"]
    assert isinstance(switch, Mapping)
    assert switch["macs"] == []


def test_learned_ips_match_mac_and_vlan() -> None:
    assert _port_ips(_joined_raw(
        "Internet 192.0.2.4 1 0011.2233.4455 ARPA Vlan20\n"
        "Internet 192.0.2.5 1 0011.2233.4455 ARPA Vlan30")) == ["192.0.2.4"]


def test_a_failed_arp_command_leaves_the_ports_empty() -> None:
    assert _port_ips(_joined_raw("__error__: unavailable")) == []


# -- the narrowing helpers consumers read a snapshot through ----------------

def test_field_of_and_tables_of_narrow_what_a_consumer_reads() -> None:
    assert field_of({"ip": "192.0.2.4"}, "ip") == "192.0.2.4"
    assert field_of({"ip": None}, "ip") == ""
    assert field_of({}, "ip") == ""
    assert tables_of([{"a": "b"}, "not a row", 3]) == [{"a": "b"}]
    assert tables_of("not a list") == []
    assert tables_of(None) == []
