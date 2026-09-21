# Architecture

`cisco-exporter` is a single-process, single-binary HTTP server that turns a
poll into a sequence of SSH sessions. This document explains the shape and the
deliberate omissions behind it.

## What the process is

```
Home Assistant  ──HTTP GET /api/status──▶  cisco-exporter  ──ssh subprocess──▶  switch
       │                                        │
       │◀── {"switches": {sid: {raw show text}}}│
       │
       └── parses the raw text with the Home Assistant side's own IOS parsers
```

* One `TcpListener`, one accept loop.
* One thread per accepted connection; the connection is handled to completion
  and closed (`Connection: close`, no keep-alive, no pipelining).
* One dependency: `serde_json`, used to parse the config file and to build the
  response. The HTTP server is hand-written against `std::net` — roughly one
  read, one request line, a scan of the header lines, one write.
* No persistent state. The config is read once at startup and then lives in an
  `Arc`; the switches are `HashMap<String, Switch>`. Nothing is cached between
  polls, so two polls always mean two sets of `show` commands.
* Nothing is written to disk, ever. There is no log file, no cache, no temp
  file, no state directory.

## Why a Rust binary instead of a Python service

The exporter started as a Python service on the bastion, deployed from the same
Home Assistant configuration repository that consumes it. It was rewritten as
this binary because of where it sits:

* It is the only long-running, network-facing process on a host that is also
  the SSH ingress to the switch management network. Keeping the process small
  and memory-safe shrinks what a bug in it can cost.
* A compiled binary removes the runtime the deployment would otherwise have to
  keep on the bastion: no interpreter version, no virtualenv, no dependency
  tree, and no requirement for the deployment repository to be present on the
  host at runtime. The deployment's job becomes "put this file there".
* Parsing was moved out of the network path. The exporter returns raw text, so
  the IOS parsers — the part that changes when Home Assistant's needs change —
  stay on the Home Assistant side, where they are covered by that project's
  test suite. A parser fix is a Home Assistant change, not a redeploy of a
  binary on the bastion.
* The security-critical parts are the ones kept in this process: what
  credentials it handles (an agent socket reference and two tokens), what it
  answers, and what it refuses to answer. That is the whole surface above.

## How `ssh` is driven

There is no SSH implementation in this binary. Each `show` command runs the
system `ssh` client as a child process with a fixed argument set (the full list
and the reasons for each option are in [cisco-ios.md](cisco-ios.md)):

* `-t -t` forces a pty, because the privileged-EXEC session is scripted like a
  console session: `enable`, the secret, then the command.
* `-F /dev/null` makes the connection independent of whatever is in the host's
  ssh config; every option that matters is on the command line.
* `IdentityAgent=<SSH_AUTH_SOCK>` and `IdentityFile=none` mean the key comes
  from the agent. This process never opens a private key.
* stdout is piped. A reader thread forwards the bytes of the child's stdout
  into an `mpsc` channel in chunks, and the prompt reader accumulates from that
  channel with a deadline. Reading stdout directly in the command loop would
  block forever on a switch that has stopped answering; the deadline is what
  turns "the switch is gone" into an error string.
* When the response is complete — or has failed — the stdin pipe is dropped,
  the child is killed and reaped, and the reader thread is joined. The SSH
  connection is not left open, and a child that ignores EOF cannot outlive the
  request.

Boundaries: 15 seconds per prompt, 2 MiB of accumulated response per prompt,
`ConnectTimeout=10` for the TCP/SSH connect itself. A command that fails over
a modern SSH negotiation is retried once with the legacy algorithm set, on a
new connection.

## Deliberate absences

**No TLS.** Transport encryption is the private network's job (a tailnet in the
reference deployment). The process speaks plain HTTP to whoever connects. The
consequences are not hypothetical: the exporter token is returned on every
response, including unauthenticated ones, and the bearer check is a plain
string comparison. The listener is therefore expected to be reachable only from
the Home Assistant instance — bind it to a private interface or loopback, or
firewall it. Never port-forward it, and never put it directly on an untrusted
network.

**No async runtime.** One thread per connection is enough for one poller, and
it keeps the request path readable end to end. The cost is that a poll is
serial: switches are visited one at a time and their commands run one at a
time, so a poll's duration is the sum of every switch's every command.

**No SSH library.** Using the system client means the key never enters this
process, the negotiation behaviour is whatever the host's OpenSSH does (and can
be reproduced by hand for debugging), and there is no crypto dependency to
track. The cost is a hard runtime requirement on `ssh` being installed, and SSH
options that are strings and therefore version-sensitive.

**No host-key verification.** The SSH options disable known-hosts checking and
provide no host-key store (`StrictHostKeyChecking=no`,
`UserKnownHostsFile=/dev/null`). Switch identity is assumed from the private
management segment: the switches are on it, and nothing else is. If that
assumption does not hold for your network, this is the option that must change
first.

**No parsing, no history, no alerts.** The exporter is a pipe. It does not
decide what is interesting, does not keep the previous poll, and has no opinion
about whether a switch answered. Alerting and graphing belong to the consumer.

## Resource profile

* Steady state: the accept loop plus a thread per in-flight request.
* Per request: one 4096-byte request buffer, one JSON response in memory (the
  full snapshot is materialized before it is sent), and, per running command, a
  child `ssh` process plus up to 2 MiB of accumulated output.
* Startup: a single config read. A poll that returns nothing still costs a
  process spawn per command.

This is small enough to run as an unprivileged system user on the bastion with
no privileges beyond the agent socket — see
[operations.md](operations.md) for the unit files and sandboxing.