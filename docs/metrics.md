# The Python package

The binary in this repository is a pipe: it returns raw `show` output and has no
opinion about it. `cisco_exporter` is the consumer that makes that output useful
— the IOS parsers, the exporter's HTTP client, a Prometheus renderer, and a
cached `/metrics` endpoint. It has no third-party dependencies at all.

```sh
pip install "cisco-exporter @ git+https://github.com/xaiki/cisco-exporter@v0.1.0#subdirectory=python"
```

## What is in it

| module | what it does | ships to a host with no environment |
| --- | --- | --- |
| `parsers` | the seven IOS `show` parsers and `parse_snapshot()`: the raw envelope in, parsed switch state out | yes, as `cisco_parsers.py` |
| `client` | `Exporter`: fetch `GET /api/status`, check `X-Exporter-Token`, narrow the JSON | yes, as `cisco_exporter_client.py` |
| `metrics` | `render()`, `render_scrape()`, `render_switches()`: the Prometheus text format | no |
| `bridge` | `Bridge`: fetch on a schedule, serve the cached result on `/metrics` | no |
| `cli` | the `cisco-exporter-metrics` command | no |
| `version` | the build's own identity: `RELEASE`, `TAGS`, `version_id()` | no |

**Why two modules are special.** The Home Assistant deployment runs its poller
as a plain script on a host that has no Python environment to install into, so
it *copies* `parsers.py` and `client.py` next to the poller under the names
above and imports them as bare modules. That is why those two are
standard-library-only and free of package-relative imports, and why their
public names are an interface rather than an implementation detail. The copy on
that host carries a one-line banner naming the build it came from
(`# cisco-exporter parsers 0.2.0-<digest16>-core`), digested over the content
below it, so a file whose bytes did not change keeps the banner it had. Every
other consumer — including the bridge in the same package — imports the package
normally.

## Which build is this?

`cisco_exporter.version_id()` answers with a release, a digest of the package's
own modules and the feature tags — `0.2.0-<digest16>-core` — and the command
line prints the same for `--version`. Content rather than a revision, because
what runs is the tree: an edit to a parser is a different build, and a revision
would call the two the same. `TAGS` is empty and the stamp says `core`, because
this package has no build variants; a tag would be an invented capability.

The exporter binary prints its own stamp the same way (`cisco-exporter
--version`), and the deployment compares that answer against what the tree
builds before it pushes or compiles anything — which is why a host already
running this build is neither re-sent to nor rebuilt.

## Using the snapshot from your own code

```python
from cisco_exporter import Exporter, parse_snapshot

raw = Exporter("http://bastion-1:8788/api/status", ha_token, exporter_token).fetch()
switches = parse_snapshot(raw)["switches"]
status = switches["north"]["interfaces"]["Gi1/0/1"]["status"]
```

`Exporter.fetch()` is where the identity check happens: the exporter stamps
`X-Exporter-Token` on every response, and a responder that does not know the
token raises rather than being parsed. `parse_snapshot()` turns each raw value
into parser output, and each *failed* one into `{"error": <message>}` — one dead
command never hides the others.

Reading a parsed row is deliberately explicit, because the JSON is untyped:
`field_of(row, "status")` returns a `str` or `""`, and `tables_of(value)` returns
the list-of-tables a row-producing parser returned (or nothing).

### Without the HTTP hop

`--dump` prints the same envelope the endpoint returns, so a consumer can read
it from a file or a pipe:

```sh
cisco-exporter --config /var/local/cisco-exporter/config.json --dump --switch north > north.json
python3 -c "import json; from cisco_exporter import parse_snapshot; \
print(json.dumps(parse_snapshot(json.load(open('north.json'))), indent=2))"
```

## The metrics bridge

```sh
cisco-exporter-metrics --once --settings /etc/cisco-exporter/poller.json   # print once
cisco-exporter-metrics --settings /etc/cisco-exporter/poller.json          # serve /metrics
```

The settings file is the one the Home Assistant deployment already writes:
`{"url": ..., "ha_token": ..., "exporter_token": ...}`, plus an optional
`"timeout"`. Flags override it, so a one-off run needs no file.

| flag | default | meaning |
| --- | --- | --- |
| `--settings <path>` | — | JSON file with `url`, `ha_token` and `exporter_token` (and optionally `timeout`). |
| `--url <url>` | — | The exporter's URL, e.g. `http://bastion-1:8788/api/status`. |
| `--ha-token <token>` | — | The bearer token the exporter checks on the request. |
| `--exporter-token <token>` | — | The token the exporter stamps on its responses. |
| `--timeout <seconds>` | `25` | How long to wait for the exporter. |
| `--listen <host:port>` | `127.0.0.1:9101` | Where `/metrics` is served. |
| `--interval <seconds>` | `60` | How often the exporter is fetched. |
| `--once` | off | Print the metrics once and exit instead of serving. |
| `--version` | — | Print the build of this package and exit, e.g. `0.2.0-<digest16>-core`. Needs no settings: it describes the build, not a run. |

Exit codes: `0` for a `--once` run whose fetch worked, or for a server stopped
by a signal; `1` for a settings file that cannot be read or is not a JSON
object, a failed fetch in `--once`, or a bind failure; `2` for a missing
`url`/`ha_token`/`exporter_token`, a bad `--listen`, a non-positive `--timeout`
or `--interval`, and for `argparse`'s own errors.

### What it serves

One route: `GET /metrics` answers `200` with
`Content-Type: text/plain; version=0.0.4; charset=utf-8`, and every other path
answers `404`. Requests are not logged; exactly one line goes to stderr at
startup, like the exporter itself. The path match is exact, so
`/metrics?x=1` is a `404`.

### Caching, staleness, and why it is not a scrape proxy

A poll costs one SSH login per switch per command — seven per switch — and the
exporter caches nothing. A bridge that fetched per scrape would put that whole
session in front of every scrape, repeatedly, and would usually exceed a
scraper's timeout. So the bridge fetches on `--interval` and serves the last
result: scraping more often does not make the switches work more, and a scrape
never waits on a switch.

Two consequences to design alerts around:

* **A failed fetch drops the switch series.** Rather than serving the previous
  values as if they were current, the body then carries only
  `cisco_exporter_scrape_ok 0` with a live age, so the per-switch series go stale
  in Prometheus instead of freezing at their last value.
* **The age is computed when you scrape, not when the data was fetched.** An age
  baked in at fetch time would always read zero, and a stalled refresher would
  look healthy.

Before the first fetch completes there is no switch state to serve, and the age
is the bridge's own uptime.

### Running it as a service

```ini
# /etc/systemd/system/cisco-exporter-metrics.service
[Unit]
Description=Prometheus metrics for the Cisco switches
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cisco-exporter
Group=cisco-exporter
ExecStart=/usr/local/bin/cisco-exporter-metrics --settings /etc/cisco-exporter/poller.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

It needs no privileges: no port below 1024, no ssh-agent (the SSH happens on the
bastion), and no writes. The settings file holds the two tokens, so it belongs
in the same `0600` treatment as the exporter's own config — see
[credentials.md](credentials.md).

## Metric reference

Every family carries a `switch` label, which is the switch id (the key in the
exporter's config). Port and interface names are in IOS abbreviated form
(`Gi1/0/1`), because every parser normalizes them to the form `show interfaces
status` prints.

| metric | labels | meaning |
| --- | --- | --- |
| `cisco_exporter_scrape_ok` | — | `1` when the last fetch of the exporter succeeded. |
| `cisco_exporter_scrape_age_seconds` | — | Seconds since that fetch finished, computed at scrape time. |
| `cisco_exporter_scrape_duration_seconds` | — | How long that fetch took. |
| `cisco_switch_command_ok` | `command` | `1` when the switch answered this `show` command, `0` when it failed. The message itself is not a label (it is unbounded text): read it from `GET /api/status` or `--dump`. |
| `cisco_switch_version_info` | `version`, `model`, `serial` | The switch's IOS version, model and serial, value `1`. |
| `cisco_interface_up` | `port` | `1` when the port's status is `connected`. |
| `cisco_interface_info` | `port`, `name`, `status`, `vlan`, `duplex`, `speed`, `type` | The port's whole row from `show interfaces status`, value `1`. |
| `cisco_interface_in_octets_total` | `port` | Bytes received since the switch booted. |
| `cisco_interface_in_pkts_total` | `port` | Packets received since the switch booted. |
| `cisco_interface_out_octets_total` | `port` | Bytes sent since the switch booted. |
| `cisco_interface_out_pkts_total` | `port` | Packets sent since the switch booted. |
| `cisco_poe_watts` | `port` | Power drawn by the device on the port. Absent when the column is not a number. |
| `cisco_poe_info` | `port`, `admin`, `oper`, `device`, `class`, `max` | The port's row from `show power inline`, value `1`. |
| `cisco_mac_entries_total` | — | Dynamic MAC addresses the switch has learned. |
| `cisco_mac_entries_by_port` | `port` | Dynamic MAC addresses learned on that port. |
| `cisco_arp_entries_total` | — | ARP entries the switch knows. |
| `cisco_ip_interface_up` | `interface` | `1` when the interface's status and protocol are both `up`. |
| `cisco_ip_interface_info` | `interface`, `ip`, `method`, `status`, `protocol` | The interface's row from `show ip interface brief`, value `1`. |
| `cisco_port_address_info` | `port`, `ip` | An IP whose MAC the switch learned on that port, value `1`. |

The four counter families are the switch's own 64-bit counters (they survive
past 2³², and they reset when the switch reboots, which `rate()` and `resets()`
handle).

## Alerting

* `cisco_exporter_scrape_ok == 0` — the bridge cannot fetch the exporter: the
  exporter is down, the tokens disagree, or the network between them is not
  working. Every other metric is absent while this is true.
* `cisco_exporter_scrape_age_seconds > 2 × --interval` — the refresher is no
  longer keeping its schedule (a hung fetch, or a switch that answers slower
  than the interval).
* `cisco_switch_command_ok == 0` — one `show` command failed on one switch. On
  the reference hardware this is usually the privileged-EXEC session, so read
  the message from the payload ([cisco-ios.md](cisco-ios.md) lists the
  signatures).
* `cisco_interface_up == 0` on a port you expect to be up — and
  `cisco_interface_info{status="err-disabled"}`, which is a port the switch shut
  after a fault.
* `cisco_mac_entries_by_port` jumping sharply — the usual first symptom of a
  loop or a flood.
* `cisco_poe_watts` near the `max` label of `cisco_poe_info` — a port close to
  its power budget.

## Home Assistant

The Home Assistant deployment is one consumer of this package, and it uses all
three layers: it depends on the package as a git dependency pinned to a tag,
ships `parsers.py` and `client.py` to the Home Assistant host under the bare
names above, and runs a small poller script there that fetches, parses, joins
each port's MACs to its ARP addresses, and writes the result as JSON for its
sensors. That poller is Home Assistant's own business; the fetch, the identity
check, the parsing and the join all come from this package.