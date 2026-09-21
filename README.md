# cisco-exporter

A small HTTP exporter that serves the raw output of Cisco IOS `show` commands,
plus the Python package that turns that output into switch state and Prometheus
metrics.

The binary runs on a bastion host: the one machine allowed to reach the switch
management network. Whoever wants the switch data — Home Assistant, Prometheus,
a script — makes one read-only HTTP call to that host instead of holding SSH
credentials for the switches themselves.

Two artifacts, both MIT:

| artifact | what it is |
| --- | --- |
| `cisco-exporter` (Rust) | the binary on the bastion. One endpoint, `GET /api/status`, returning the raw text of seven `show` commands per switch. `--dump` does the same collection without a listener. |
| `cisco_exporter` (Python) | the consumer side: the show parsers, the exporter's HTTP client, a Prometheus renderer, and a cached `/metrics` bridge. |

* Rust edition 2021, one dependency (`serde_json`). No TLS, no async runtime, no
  SSH library: the system `ssh` client is driven as a subprocess and
  authenticates out of the ssh-agent.
* The binary does not parse IOS output. The parsers are in the Python package in
  this repository, so a parser change never requires a new binary on the
  bastion.
* The Python package has no third-party dependencies, and two of its modules are
  standard-library-only *by contract* so a host with no Python environment can
  run them as plain script files. See [docs/metrics.md](docs/metrics.md).

Documentation: [docs/](docs/README.md).

## Why

Home Assistant needs a few switch facts (interface status, MAC table, ARP, PoE,
counters). The obvious way to get them is to let Home Assistant log into the
switches. This exporter exists to avoid that: Home Assistant gets one
read-only HTTP call to one host, and the switch credentials stay on that host,
in an ssh-agent, never in the Home Assistant configuration.

Splitting it in two — a raw-text pipe in Rust, the parsing in Python — keeps the
process on the network-facing host tiny, and keeps the part that changes with
each consumer's needs out of the deployed binary.

## Requirements

* Linux (the deployment target; the binary links against the target's libc, so
  build for the machine you run it on).
* An OpenSSH client on `PATH` as `ssh`.
* An ssh-agent holding a key that logs into the switches, reachable through
  `SSH_AUTH_SOCK`. The exporter never reads a private key file itself.
* A switch user that can reach privileged EXEC (see
  [docs/cisco-ios.md](docs/cisco-ios.md)); its `enable` secret goes into the
  config file.
* A Rust toolchain able to build edition 2021 (built and tested with
  `rustc 1.96.0`).

The Python package needs none of that: only Python 3.11 or newer, and no third
party packages at all. Working on it needs `uv` (see
[CONTRIBUTING.md](CONTRIBUTING.md)).

## Build

### The exporter binary

```sh
cargo build --release
```

The binary lands at `target/release/cisco-exporter`. `Cargo.lock` is committed;
`--locked` is safe to use.

### The Python package

```sh
cd python
uv build            # sdist and wheel in python/dist/
uv run pytest       # parsers, client, renderer and bridge
uv run ruff check . && uv run mypy
```

Install it from git, pinned to a tag — this is how a consumer depends on it:

```sh
pip install "cisco-exporter @ git+https://github.com/xaiki/cisco-exporter@v0.1.0#subdirectory=python"
```

Then `from cisco_exporter.parsers import parse_snapshot` works, and the
`cisco-exporter-metrics` command is on `PATH`.

## Running

```sh
cisco-exporter --config /var/local/cisco-exporter/config.json [--host 0.0.0.0] [--port 8788]
```

### Command line

| flag | default | meaning |
| --- | --- | --- |
| `--config <path>` | — | Required. Path to the JSON config file. Missing, or a value that is not readable JSON, exits non-zero. |
| `--host <addr>` | `0.0.0.0` | Listen address. Ignored by `--dump`, which opens no listener. |
| `--port <port>` | `8788` | Listen port. A value that does not parse as a `u16` silently falls back to 8788. Ignored by `--dump`. |
| `--dump` | off | Collect once, print the snapshot to stdout and exit instead of serving. Requires no tokens: there is no request to authenticate. |
| `--switch <id>` | all | With `--dump`, collect only this switch. An id that is not in the config exits `1`; an empty value exits `2`. |

Any argument that is not one of those flags is ignored. A flag at the end
of the argument list with no following value is treated as an empty string:

* no arguments at all prints the usage line and exits `2`;
* `--config ""` prints `--config required` and exits `2`;
* `--switch` with no value prints `--switch requires a switch id` and exits `2`;
* a `--config` path that cannot be read or parsed prints the error and exits `1`.

### One-shot collection with `--dump`

```sh
cisco-exporter --config /var/local/cisco-exporter/config.json --dump --switch north
```

Runs the seven commands once, prints the envelope `GET /api/status` would have
returned (pretty-printed), and exits. Useful for testing a switch, an SSH agent
or a config change without a poller, and for capturing real `show` output to
write parser tests against.

A command that fails is not a dump failure: its value is the
`"__error__: <message>"` string, exactly as over HTTP, and the process still
exits `0`. Only an unknown `--switch` id or an unreadable config stops it.

### Config file

Read once at startup. Expected to be mode `0600`, owned by the service user.

```json
{
  "ha_token": "test-token",
  "exporter_token": "test-token",
  "username": "admin",
  "enable_secret": "",
  "switches": {
    "north": { "host": "192.0.2.10", "port": 22 },
    "south": { "host": "192.0.2.11", "port": 22 }
  }
}
```

| key | required | default | meaning |
| --- | --- | --- | --- |
| `ha_token` | yes | — | Bearer token the poller must present. Startup exits `1` if it is empty or absent. |
| `exporter_token` | yes | — | Token echoed in `X-Exporter-Token` on every response. Startup exits `1` if it is empty or absent. |
| `username` | no | `admin` | SSH user for the switches. |
| `enable_secret` | no | `""` | IOS privileged-EXEC password. Empty means a switch that demands a password cannot be escalated (see [docs/cisco-ios.md](docs/cisco-ios.md)). |
| `switches` | no | `{}` | Map of switch id → `{ "host": <addr>, "port": <n> }`. A missing `host` becomes `""`, a missing `port` becomes 22. |

Unknown keys are ignored. The switch id is the key under which that switch's
output appears in the response; the id is not sent anywhere else.

### Environment

The only environment variable the process reads is `SSH_AUTH_SOCK`, and only at
the moment it needs to run a `show` command. If it is unset or empty, every
command fails with `Cisco SSH agent socket is not configured`.

Startup writes exactly one line to stderr: `[exporter] serving on <host>:<port>`.
Nothing else is logged; per-switch failures travel in the response body.

### Exit codes

| code | condition |
| --- | --- |
| 0 | normal shutdown (there is no shutdown path: the process only ends on a signal), or a completed `--dump` — including one whose commands all failed |
| 1 | config file unreadable or unparsable, `ha_token`/`exporter_token` missing, `--switch` id not in the config, or bind failure |
| 2 | missing/empty `--config`, empty `--switch`, or no arguments at all |

## HTTP surface

One route. The response is generated per request; nothing is cached, and each
request runs the `show` commands again.

| method | path | auth | response |
| --- | --- | --- | --- |
| `GET` | `/api/status` | `Authorization: Bearer <ha_token>` | `200` with the snapshot |
| any other method or path | — | ignored | `404` `{"error":"not found"}` |
| `GET` | `/api/status` with a missing or wrong bearer token | — | `401` `{"error":"unauthorized"}` |

Every response — including the `401` and `404` — carries:

```
Content-Type: application/json
X-Exporter-Token: <exporter_token>
Content-Length: <n>
Connection: close
```

Response body for a successful poll:

```json
{
  "switches": {
    "north": {
      "version": "Cisco IOS Software, ...\n",
      "interfaces": "...",
      "macs": "...",
      "arp": "...",
      "counters": "...",
      "poe": "...",
      "ip_brief": "..."
    }
  }
}
```

Each value is the raw text captured from the switch. The commands, in the order
they are run per switch:

| key | command |
| --- | --- |
| `version` | `show version` |
| `interfaces` | `show interfaces status` |
| `macs` | `show mac address-table` |
| `arp` | `show ip arp` |
| `counters` | `show interfaces counters` |
| `poe` | `show power inline` |
| `ip_brief` | `show ip interface brief` |

A command that fails is not fatal. Its value becomes the string
`"__error__: <message>"`, which is how the consumer tells a captured error from
captured output; the other keys still carry data. See
[docs/operations.md](docs/operations.md) for the failure messages and
[docs/cisco-ios.md](docs/cisco-ios.md) for what causes them.

Properties of this surface worth knowing before you expose it:

* The path match is exact, so `/api/status?x=1` is a `404`. There is no
  `HEAD`, no other path, no redirect and no error page.
* The request is read with a single 4096-byte read of the socket. Headers that
  fall beyond that are not seen, and `Authorization` is looked for in what was
  read.
* The route and the auth check happen before anything is sent to a switch, so an
  unauthenticated request never triggers an SSH login.
* Switches are collected serially on the request's own thread: a slow switch
  delays the whole response, and its neighbours' data with it. One thread is
  spawned per connection, with no limit.
* The bearer token is compared as a plain string, not in constant time.

## Auth model

Two tokens, both from the config file, both generated by the deployment
automation that writes that file:

* `ha_token` — the poller presents it as `Authorization: Bearer <ha_token>`. This
  is the only thing that authorizes a poll; what it authorizes is a set of SSH
  logins from the bastion to the switches.
* `exporter_token` — returned in `X-Exporter-Token` on every response, and
  checked by the poller against the same value it holds. The poller rejects the
  payload if it does not match.

The header name is matched case-insensitively, but `Bearer`, the token itself
and the header's placement on its own line are not. A header line that merely
ends with `Bearer <ha_token>` (e.g. `X-Comment: Bearer <ha_token>`) does not
authenticate.

Note what the second token is and is not: it is a *response* token, not a
client credential. It is handed to anyone who can connect to the port — a wrong
`ha_token` still gets a `401` carrying the real `X-Exporter-Token`. It lets an
already-configured poller detect that it is talking to the exporter it was
configured for, rather than to something else that answered on that address. It
is not a secret from anything that can reach the listener, and the listener is
therefore expected to be on a private network only — see
[docs/architecture.md](docs/architecture.md).

Neither token is a switch credential. The switch key lives in the ssh-agent;
the IOS `enable` secret lives in the config file and is written only into the
`enable` prompt (see [docs/credentials.md](docs/credentials.md)).

## Using it without Home Assistant

Home Assistant is one consumer of this exporter, not a requirement. The snapshot
is a plain JSON document, and the Python package in `python/` is the generic
consumer: it fetches, verifies the exporter's identity, parses the IOS output
and renders Prometheus metrics.

```sh
cisco-exporter-metrics --once \
  --url http://bastion-1:8788/api/status \
  --ha-token "$HA_TOKEN" --exporter-token "$EXPORTER_TOKEN"
```

That prints one exposition-format document and exits, which is all a
textfile-collector or a cron job needs. Without `--once` it serves `/metrics` on
`127.0.0.1:9101`, fetching the exporter on its own `--interval` and serving the
last result — so a scrape never waits on a switch, and scraping more often does
not make the switches run more commands.

Three properties worth knowing before pointing anything at it:

* a failed fetch drops the switch series and leaves only
  `cisco_exporter_scrape_ok 0` with a live age, so stale data is never served as
  if it were current;
* the exporter token is what the bridge checks to be sure it is talking to this
  exporter — the same check the Home Assistant poller makes;
* `/metrics` carries no credential of its own, which is why it binds loopback by
  default. Expose it deliberately, or scrape it from the same host.

[docs/metrics.md](docs/metrics.md) has the metric reference, the flags, the exit
codes and the alerting notes.

## Install as a systemd service

Layout used throughout this documentation:

| path | contents |
| --- | --- |
| `/var/local/cisco-exporter/cisco-exporter` | the binary, mode 755 |
| `/var/local/cisco-exporter/config.json` | the config above, mode 600, owned by the service user |
| `/etc/systemd/system/cisco-agent.service` | the agent unit |
| `/etc/systemd/system/cisco-exporter.service` | the exporter unit |
| `/run/cisco-agent/agent.sock` | the agent socket, created by systemd (`RuntimeDirectory=`) |
| `cisco-exporter` | dedicated system user, no login shell, both units run as it |

Both units are unprivileged: the port is above 1024 and the agent socket lives
in a systemd-managed runtime directory. Root is needed only to install the
files.

```ini
# /etc/systemd/system/cisco-agent.service
[Unit]
Description=Persistent ssh-agent holding the Cisco switch key

[Service]
Type=simple
User=cisco-exporter
Group=cisco-exporter
RuntimeDirectory=cisco-agent
RuntimeDirectoryMode=0700
ExecStart=/usr/bin/ssh-agent -D -a /run/cisco-agent/agent.sock
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/cisco-exporter.service
[Unit]
Description=Cisco switch exporter (serves status to Home Assistant)
Requires=cisco-agent.service
After=network-online.target cisco-agent.service
Wants=network-online.target

[Service]
Type=simple
User=cisco-exporter
Group=cisco-exporter
Environment=SSH_AUTH_SOCK=/run/cisco-agent/agent.sock
ExecStart=/var/local/cisco-exporter/cisco-exporter --config /var/local/cisco-exporter/config.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Install, with root, in this order — the agent must be up before a key can be
loaded into it:

```sh
useradd --system --no-create-home --shell /usr/sbin/nologin cisco-exporter
install -d -m 755 /var/local/cisco-exporter
install -m 755 cisco-exporter /var/local/cisco-exporter/cisco-exporter
install -m 600 -o cisco-exporter -g cisco-exporter config.json /var/local/cisco-exporter/config.json
install -m 644 cisco-agent.service cisco-exporter.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cisco-agent.service
SSH_AUTH_SOCK=/run/cisco-agent/agent.sock ssh-add -   # private key on stdin, never a file
systemctl enable --now cisco-exporter.service
```

`ssh-add -` reads the key from stdin, so the key never has to be written to
disk on the bastion. To hand the exporter a key that is already loaded in your
own agent, use the `SSH_AUTH_SOCK` of that agent instead.

[docs/operations.md](docs/operations.md) has the same units with comments,
sandboxing directives that are compatible with the code, the failure modes, and
the upgrade/rotation procedure.

## Tests

```sh
cargo test
cd python && uv run pytest
```

The Rust tests are unit tests inside `src/main.rs`: the IOS prompt
protocol, the SSH argument set, the token check, the argument parser, the
`--dump` envelope, the response envelope and the config parser. The Python tests
cover the parsers, the client's identity check, the renderer, and the bridge
including a real loopback HTTP request. Neither suite needs network, switch or
agent: the exporter is replaced by a stub returning canned `show` output.

## Documentation

| document | contents |
| --- | --- |
| [docs/README.md](docs/README.md) | index |
| [docs/architecture.md](docs/architecture.md) | why a Rust pipe plus a Python consumer, how `ssh` is driven, what is deliberately absent |
| [docs/cisco-ios.md](docs/cisco-ios.md) | SSH negotiation, privileged-EXEC session, IOS quirks |
| [docs/credentials.md](docs/credentials.md) | what is read, what is never stored, what must never be committed |
| [docs/operations.md](docs/operations.md) | units, hardening, observability, failure modes, rotation |
| [docs/metrics.md](docs/metrics.md) | the Python package: parsers, client, `--dump` consumption, Prometheus metric reference, bridge flags and exit codes |
| [CONTRIBUTING.md](CONTRIBUTING.md) | build, test and commit conventions |

## Licence

MIT — see [LICENSE](LICENSE).