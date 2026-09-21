"""The Prometheus renderer: what it exports, and what it refuses to."""
from __future__ import annotations

from cisco_exporter.metrics import Scrape, render
from cisco_exporter.parsers import parse_snapshot
from fakes import snapshot

_OK = Scrape(ok=True, age_seconds=1.5, duration_seconds=2.25)

#: The order the module promises, and which families a healthy switch fills.
_FAMILIES = [
    "cisco_exporter_scrape_ok",
    "cisco_exporter_scrape_age_seconds",
    "cisco_exporter_scrape_duration_seconds",
    "cisco_switch_command_ok",
    "cisco_switch_version_info",
    "cisco_interface_up",
    "cisco_interface_info",
    "cisco_interface_in_octets_total",
    "cisco_interface_in_pkts_total",
    "cisco_interface_out_octets_total",
    "cisco_interface_out_pkts_total",
    "cisco_poe_watts",
    "cisco_poe_info",
    "cisco_mac_entries_total",
    "cisco_mac_entries_by_port",
    "cisco_arp_entries_total",
    "cisco_ip_interface_up",
    "cisco_ip_interface_info",
    "cisco_port_address_info",
]


def samples(text: str) -> set[str]:
    """The sample lines, without the HELP/TYPE headers."""
    return {line for line in text.splitlines() if line and not line.startswith("#")}


def families(text: str) -> list[str]:
    """The families declared, in the order they appear."""
    return [line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")]


def test_a_healthy_switch_fills_every_family_in_a_fixed_order() -> None:
    text = render(parse_snapshot(snapshot()), _OK)
    assert families(text) == _FAMILIES
    assert len(families(text)) == len(set(families(text)))  # one HELP/TYPE each


def test_scrape_state_is_exported() -> None:
    assert samples(render({}, _OK)) >= {
        "cisco_exporter_scrape_ok 1",
        "cisco_exporter_scrape_age_seconds 1.500",
        "cisco_exporter_scrape_duration_seconds 2.250",
    }


def test_a_healthy_switch_state_is_exported() -> None:
    assert samples(render(parse_snapshot(snapshot()), _OK)) >= {
        'cisco_switch_command_ok{switch="north",command="version"} 1',
        ('cisco_switch_version_info{switch="north",version="15.2(7)E11",'
         'model="WS-C2960X-48LPS-L",serial="FOC1234ABCD"} 1'),
        'cisco_interface_up{switch="north",port="Gi1/0/1"} 1',
        'cisco_interface_up{switch="north",port="Gi1/0/2"} 0',
        'cisco_interface_up{switch="north",port="Gi1/0/3"} 0',
        ('cisco_interface_info{switch="north",port="Gi1/0/1",name="desk",'
         'status="connected",vlan="10",duplex="a-full",speed="a-100",'
         'type="10/100/1000BaseTX"} 1'),
        'cisco_interface_in_octets_total{switch="north",port="Gi1/0/1"} 1000',
        'cisco_interface_in_pkts_total{switch="north",port="Gi1/0/1"} 15',
        'cisco_interface_out_octets_total{switch="north",port="Gi1/0/1"} 2000',
        'cisco_interface_out_pkts_total{switch="north",port="Gi1/0/1"} 27',
        'cisco_poe_watts{switch="north",port="Gi1/0/1"} 6.400',
        'cisco_poe_watts{switch="north",port="Gi1/0/2"} 0.000',
        ('cisco_poe_info{switch="north",port="Gi1/0/2",admin="auto",oper="off",'
         'device="n/a",class="n/a",max="15.4"} 1'),
        'cisco_mac_entries_total{switch="north"} 3',
        'cisco_mac_entries_by_port{switch="north",port="Gi1/0/1"} 3',
        'cisco_arp_entries_total{switch="north"} 2',
        'cisco_ip_interface_up{switch="north",interface="Vlan10"} 1',
        'cisco_ip_interface_up{switch="north",interface="Gi1/0/1"} 0',
        'cisco_ip_interface_info{switch="north",interface="Vlan10",ip="192.0.2.1",'
        'method="NVRAM",status="up",protocol="up"} 1',
        # Both addresses are on Gi1/0/1: the same MAC in two VLANs, joined only
        # within its own VLAN, lands on the one port that learned it.
        'cisco_port_address_info{switch="north",port="Gi1/0/1",ip="192.0.2.4"} 1',
        'cisco_port_address_info{switch="north",port="Gi1/0/1",ip="192.0.2.5"} 1',
    }


def test_a_failed_command_is_reported_without_hiding_the_others() -> None:
    parsed = parse_snapshot({"switches": {"south": {
        "version": "__error__: ssh south: timed out",
        "interfaces": "Port      Name   Status       Vlan   Duplex  Speed Type\n"
                      "Gi0/1            connected   10     a-full a-100 10/100/1000BaseTX\n",
    }}})
    got = samples(render(parsed, _OK))
    assert 'cisco_switch_command_ok{switch="south",command="version"} 0' in got
    assert 'cisco_switch_command_ok{switch="south",command="interfaces"} 1' in got
    assert not any(line.startswith("cisco_switch_version_info") for line in got)
    assert 'cisco_interface_up{switch="south",port="Gi0/1"} 1' in got


def test_a_failed_scrape_serves_only_the_scrape_state() -> None:
    text = render({}, Scrape(ok=False, age_seconds=12.0, duration_seconds=0.5))
    assert families(text) == _FAMILIES[:3]
    assert samples(text) == {
        "cisco_exporter_scrape_ok 0",
        "cisco_exporter_scrape_age_seconds 12.000",
        "cisco_exporter_scrape_duration_seconds 0.500",
    }


def test_a_switch_id_is_escaped_as_a_label_value() -> None:
    parsed: dict[str, object] = {"switches": {'odd"name\\x\ny': {}}}
    text = render(parsed, _OK)
    assert 'switch="odd\\"name\\\\x\\ny"' in text


def test_render_is_byte_stable_for_the_same_input() -> None:
    parsed = parse_snapshot(snapshot())
    assert render(parsed, _OK) == render(parsed, _OK)
