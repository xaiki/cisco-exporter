# cisco-exporter

A small HTTP exporter that serves the raw output of Cisco IOS `show` commands to
a Home Assistant instance.

It is meant to run on a bastion host: the one machine allowed to reach the
switch management network. Home Assistant polls it over HTTP instead of holding
SSH credentials for the switches themselves.

* Rust (edition 2021), MIT, one dependency (`serde_json`).
* A single endpoint: `GET /api/status` returns the raw text of seven `show`
  commands per switch, as JSON.
* No TLS, no async runtime, no SSH library. The system `ssh` client is driven as
  a subprocess and authenticates out of the ssh-agent.
* The exporter does not parse IOS output. The Home Assistant side does, so a
  parser change never requires a new binary on the bastion.

Documentation: [docs/](docs/README.md).

## Why

Home Assistant needs a few switch facts (interface status, MAC table, ARP, PoE,
counters). The obvious way to get them is to let Home Assistant log into the
switches. This exporter exists to avoid that: Home Assistant gets one
read-only HTTP call to one host, and the switch credentials stay on that host,
in an ssh-agent, never in the Home Assistant configuration.

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

## Build

```sh
cargo build --release
```

The binary lands at `target/release/cisco-exporter`. `Cargo.lock` is committed;
`--locked` is safe to use.

## Running

```sh
cisco-exporter --config /var/local/cisco-exporter/config.json [--host 0.0.0.0] [--port 8788]
```

### Command line

| flag | default | meaning |
| --- | --- | --- |
| `--config <path>` | — | Required. Path to the JSON config file. Missing, or a value that is not readable JSON, exits non-zero. |
| `--host <addr>` | `0.0.0.0` | Listen address. |
| `--port <port>` | `8788` | Listen port. A value that does not parse as a `u16` silently falls back to 8788. |

Any argument that is not one of those three flags is ignored. A flag at the end
of the argument list with no following value is treated as an empty string:

* no arguments at all prints the usage line and exits `2`;
* `--config ""` prints `--config required` and exits `2`;
* a `--config` path that cannot be read or parsed prints the error and exits `1`.

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
| 0 | normal shutdown (there is no shutdown path: the process only ends on a signal) |
| 1 | config file unreadable or unparsable, `ha_token`/`exporter_token` missing, or bind failure |
| 2 | missing/empty `--config`, or no arguments at all |

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
```

The tests are unit tests inside `src/main.rs`. They cover the IOS prompt
protocol, the SSH argument set, the token check, the response envelope and the
config parser, and they need no network, no switch and no agent.

## Documentation

| document | contents |
| --- | --- |
| [docs/README.md](docs/README.md) | index |
| [docs/architecture.md](docs/architecture.md) | why a single Rust binary, how `ssh` is driven, what is deliberately absent |
| [docs/cisco-ios.md](docs/cisco-ios.md) | SSH negotiation, privileged-EXEC session, IOS quirks |
| [docs/credentials.md](docs/credentials.md) | what is read, what is never stored, what must never be committed |
| [docs/operations.md](docs/operations.md) | units, hardening, observability, failure modes, rotation |
| [CONTRIBUTING.md](CONTRIBUTING.md) | build, test and commit conventions |

## Licence

MIT — see [LICENSE](LICENSE).