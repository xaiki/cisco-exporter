"""Canned show output and a stand-in for the exporter's HTTP responses."""
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

#: A switch whose every command answered.
VERSION = (
    "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), "
    "Version 15.2(7)E11, RELEASE SOFTWARE (fc1)\n"
    "Model number : WS-C2960X-48LPS-L\n"
    "System serial number : FOC1234ABCD\n"
)

INTERFACES = (
    "Port      Name     Status       Vlan   Duplex  Speed Type\n"
    "Gi1/0/1   desk     connected    10     a-full  a-100 10/100/1000BaseTX\n"
    "Gi1/0/2            notconnect   10     auto    auto  10/100/1000BaseTX\n"
    "Gi1/0/3            err-disabled 10     auto    auto  10/100/1000BaseTX\n"
)

MACS = (
    "Vlan    Mac Address       Type        Ports\n"
    "----    -----------       --------    -----\n"
    "  10    0011.2233.4455    DYNAMIC     Gi1/0/1\n"
    "  10    0011.2233.4456    DYNAMIC     Gi1/0/1\n"
    "  20    0011.2233.4455    DYNAMIC     Gi1/0/1\n"
)

ARP = (
    "Protocol  Address          Age (min)  Hardware Addr   Type   Interface\n"
    "Internet  192.0.2.4                1  0011.2233.4455  ARPA   Vlan10\n"
    "Internet  192.0.2.5                1  0011.2233.4455  ARPA   Vlan20\n"
)

COUNTERS = (
    "Port InOctets InUcastPkts InMcastPkts InBcastPkts\n"
    "Gi1/0/1 1000 10 2 3\n"
    "Port OutOctets OutUcastPkts OutMcastPkts OutBcastPkts\n"
    "Gi1/0/1 2000 20 3 4\n"
)

POE = (
    "Interface   Admin   Oper    Power   Device            Class Max\n"
    "Gi1/0/1     auto    on      6.4     IP Phone 7960     3     15.4\n"
    "Gi1/0/2     auto    off     0.0     n/a               n/a   15.4\n"
)

IP_BRIEF = (
    "Interface              IP-Address      OK? Method Status                Protocol\n"
    "Vlan10                 192.0.2.1       YES NVRAM  up                    up\n"
    "Gi1/0/1                unassigned      YES unset  up                    down\n"
)

#: The reply of a switch that answered every command.
SWITCH: dict[str, str] = {
    "version": VERSION,
    "interfaces": INTERFACES,
    "macs": MACS,
    "arp": ARP,
    "counters": COUNTERS,
    "poe": POE,
    "ip_brief": IP_BRIEF,
}


def snapshot(switch: dict[str, str] | None = None, sid: str = "north") -> dict[str, object]:
    """A raw snapshot envelope, as the exporter's body carries it."""
    return {"switches": {sid: dict(SWITCH if switch is None else switch)}}


class FakeResponse:
    """What ``urllib.request.urlopen`` returns, as far as the client reads it."""

    def __init__(self, body: bytes | str = "{}", exporter_token: str = "ex-tok") -> None:
        self.headers = {"X-Exporter-Token": exporter_token}
        self._body = body.encode() if isinstance(body, str) else body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> Literal[False]:
        return False


def urlopen_returning(response: FakeResponse,
                      seen: list[object] | None = None,
                      ) -> Callable[[object, float], FakeResponse]:
    """A ``urlopen`` replacement handing back ``response``, recording requests."""
    def fake_urlopen(request: object, timeout: float = 30.0) -> FakeResponse:
        if seen is not None:
            seen.append(request)
        return response
    return fake_urlopen


def urlopen_failing(error: Exception) -> Callable[[object, float], FakeResponse]:
    """A ``urlopen`` replacement that always fails, like an unreachable host."""
    def fake_urlopen(request: object, timeout: float = 30.0) -> FakeResponse:
        raise error
    return fake_urlopen
