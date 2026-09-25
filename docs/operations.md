# Operations

Deployment layout, sandboxing, what the service does and does not tell you, and
how each failure looks from the outside.

## Deployment layout

| path | contents | mode |
| --- | --- | --- |
| `/var/local/cisco-exporter/cisco-exporter` | the binary | `755` |
| `/var/local/cisco-exporter/config.json` | tokens, switch list | `600`, owned by `cisco-exporter` |
| `/etc/systemd/system/cisco-agent.service` | agent unit | `644` |
| `/etc/systemd/system/cisco-exporter.service` | exporter unit | `644` |
| `/run/cisco-agent/agent.sock` | agent socket, created by systemd | `0700` (runtime directory) |
| `cisco-exporter` | dedicated system user, `nologin`, no home | — |

Neither unit needs root: the HTTP port (8788) is above 1024, the agent socket
lives in a systemd-managed runtime directory, and the exporter reads only its own
files. Root is used to install them.

## Unit files

```ini
# /etc/systemd/system/cisco-agent.service
#
# The agent holds the switch key across reboots, so the exporter has it on every
# poll. systemd creates /run/cisco-agent (mode 0700) owned by the service user
# before the agent starts and removes it afterwards, so the agent never has to
# create or own its own socket directory.
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
#
# Requires= the agent: SSH_AUTH_SOCK names a socket that must exist before the
# first poll, and a poll made without it fails per command rather than loudly.
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

## Install

As root, in this order. The agent has to be running before a key can be loaded
into it.

```sh
useradd --system --no-create-home --shell /usr/sbin/nologin cisco-exporter
install -d -m 755 /var/local/cisco-exporter
install -m 755 target/release/cisco-exporter /var/local/cisco-exporter/cisco-exporter
install -m 600 -o cisco-exporter -g cisco-exporter config.json /var/local/cisco-exporter/config.json
install -m 644 cisco-agent.service cisco-exporter.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cisco-agent.service
SSH_AUTH_SOCK=/run/cisco-agent/agent.sock ssh-add -    # private key on stdin
systemctl enable --now cisco-exporter.service
```

`ssh-add -` reads the key from stdin, so it never exists as a file on the
bastion. If the key is already loaded in your own agent, point
`SSH_AUTH_SOCK` at that agent instead.

Verify:

```sh
SSH_AUTH_SOCK=/run/cisco-agent/agent.sock ssh-add -L      # must list the switch key
curl -s -D- -H 'Authorization: Bearer test-token' http://127.0.0.1:8788/api/status
journalctl -u cisco-exporter -n 20
```

The `curl` prints the response headers, including `X-Exporter-Token`; run it on
the bastion. The `Bearer` scheme and the token are matched case-sensitively.

## Hardening

The exporter needs very little: read two files, talk to a unix socket, exec
`ssh`, use the network. The directives below are compatible with exactly that.
They are suggestions, not a tested unit — if something fails to start, the
first two to relax are `SystemCallFilter=` and `RestrictAddressFamilies=`.

```ini
# add to the [Service] section of cisco-exporter.service
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077
LimitCORE=0

# no writes at all; PrivateTmp gives the ssh child a writable /tmp if it wants one
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ReadWritePaths=

# unix socket for the agent, inet for the switches. Do NOT use PrivateNetwork=yes
# here: this unit exists to reach the switches.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
# Optional, only if the switch addresses are static and you also allow the
# poller's address: IPAddressAllow=192.0.2.10 192.0.2.11 100.64.0.0/10

RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
```

Notes on the ones that are not obviously safe:

* `@system-service` is a broad group, and it is broad on purpose: the process
  must fork, exec `/usr/bin/ssh`, and use sockets. Tightening it means naming
  `execve`, `clone`, `socket`, `connect` and the agent's socket calls yourself.
* `ProtectSystem=strict` makes everything read-only. The exporter writes
  nothing, and its fixed `ssh` option set (no control master, `-F /dev/null`,
  `UserKnownHostsFile=/dev/null`) means the child creates no files either.
* `MemoryDenyWriteExecute=yes` is fine here: no JIT, no generated code.
* `LimitCORE=0` keeps a core dump from carrying the tokens and the enable
  secret out of the process; the exporter does not zeroize its in-memory
  secrets (see [credentials.md](credentials.md)).

For the agent unit, the same sandbox minus the network:

```ini
# add to the [Service] section of cisco-agent.service
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectProc=invisible
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
```

`RuntimeDirectory=` stays writable under `ProtectSystem=strict` — systemd makes
it available to the unit itself.

## Observability

Almost everything this service knows is in the response body, not in the
journal.

* **Startup.** One line: `[exporter] serving on <host>:<port>`. One line per
  start, nothing per request, nothing per switch.
* **Per request.** Nothing is logged — not the poller's address, not the
  switches visited, not per-command errors. The journal cannot tell you that a
  switch went down.
* **What the journal can tell you.** That the service is down or restarting
  (`Restart=on-failure` after a bind failure), that the config could not be read
  or parsed (`read config <path>: ...`, `parse config <path>: ...`, then exit 1),
  and that the tokens are missing (`config missing ha_token/exporter_token`).
* **What has to be alerted elsewhere.** Switch health, per-command failures and
  stale data are visible only in the payload (`__error__: ...`) and are the
  consumer's business. Home Assistant alerts from its own sensors; a Prometheus
  consumer alerts on `cisco_switch_command_ok`, `cisco_exporter_scrape_ok` and
  `cisco_exporter_scrape_age_seconds` — see [metrics.md](metrics.md).
* **Liveness without a token.** Any unauthorised path answers `404`:
  `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8788/-` returning
  `404` proves the process is up.
* **Reproducing a failed command by hand.** Run the same `ssh` the exporter
  runs, with the same options, and drive the session yourself:

  ```sh
  SSH_AUTH_SOCK=/run/cisco-agent/agent.sock \
  ssh -t -t -F /dev/null \
      -o BatchMode=yes -o IdentityFile=none \
      -o PreferredAuthentications=publickey \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=10 -o HostKeyAlgorithms=+ssh-rsa \
      -o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1 \
      -o ServerAliveInterval=5 -o ServerAliveCountMax=3 \
      -p 22 admin@192.0.2.10
  # then, at the prompt:
  #   enable                      (only if the prompt ends in '>')
  #   terminal length 0
  #   show version
  ```

  Add `-v` to turn on `ssh`'s own debugging. The exporter captures the stderr of
  this child and appends it to every error message, so a `Permission denied
  (publickey)` seen by hand is also what the payload will show.

## Failure modes

| symptom | cause | what to do |
| --- | --- | --- |
| journal shows `bind <host>:<port>: Address already in use`, process exits 1, unit restarts and fails again | something else holds the port | change `--port`, or free the port |
| journal shows `read config ...` / `parse config ...`, exit 1 | wrong path, mode too tight, or malformed JSON | fix the path or the file |
| journal shows `config missing ha_token/exporter_token`, exit 1 | tokens absent or empty | write them into the config |
| service active, every field `__error__: ... Cisco SSH agent socket is not configured` | `SSH_AUTH_SOCK` not set for the unit | set `Environment=SSH_AUTH_SOCK=...` |
| every field `__error__: ssh <host>: ...: Permission denied (publickey)` | key not in the agent, or the switch rejects that user/key | `ssh-add -L` against the agent socket, load the key with `ssh-add -` |
| every field `__error__` containing `SSH closed or timed out waiting for IOS prompt` | switch unreachable, blocked, or the session died | reachability first, then the hand-run above |
| `IOS enable password is not configured` | login lands at `>` and `enable_secret` is empty | set `enable_secret`, or use a user that logs in privileged |
| `IOS paging could not be disabled` / `IOS rejected the status command` mentioning `% Authorization failed` | AAA rejects those commands for this user | fix authorization on the switch |
| one switch's fields fail, the rest are fine | that switch only; failures are per command | inspect that switch |
| a poll takes minutes | collection is serial: each dead switch costs a connect timeout plus a prompt timeout per command, seven commands per switch, twice over when the legacy retry fires | keep the switch list small, raise the consumer's HTTP timeout, and alert on missing data rather than waiting for a fast answer |
| the poller times out but the switches see logins anyway | the exporter has no overall request deadline: an abandoned poll keeps running, and its `ssh` children keep going after the client has hung up | expect this when the consumer's timeout is shorter than a slow poll; it is load, not an error |

`Restart=on-failure` covers the bind and config cases (and a panic) only. A
switch going away does not stop the service — it turns into `__error__` strings
in the next response.

## Upgrading

The binary is dynamically linked against the target's libc, so build it for the
machine it will run on, or on a host with the same libc. A build from a
different platform (or a much newer glibc) will not start.

```sh
cargo build --release --locked          # on, or for, the bastion
systemctl stop cisco-exporter.service
install -m 755 target/release/cisco-exporter /var/local/cisco-exporter/cisco-exporter
systemctl start cisco-exporter.service
curl -s -D- -H 'Authorization: Bearer test-token' http://127.0.0.1:8788/api/status
```

The config and the agent are untouched by an upgrade. Secret rotation is in
[credentials.md](credentials.md); the agent keeps the key across the exporter's
restart, so no key reload is needed.

Two things make an upgrade legible rather than guesswork:

* the binary names its own build — `cisco-exporter --version` prints
  `v0.2.0-<digest16>-core`: the release, a digest of exactly the sources that
  compile into it, and the feature tags. A hand-copied binary that was never
  stamped says `dev` rather than claiming a build it is not;
* the deployment writes that same string to `/var/local/cisco-exporter/version`
  beside the binary, and asks the host for it before pushing or compiling
  anything. A host already running the build the tree produces is neither
  re-sent to nor rebuilt, which is what keeps a config-only change from costing
  a compile on every bastion.

Editing only `src/tests.rs` does not change the version: what compiles into the
binary is what defines it (`--version` is the authority; the `version` file only
covers a build old enough not to have been stamped).