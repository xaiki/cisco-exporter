# Credential and secret handling

The exporter handles two tokens and one switch password, and it deliberately
does not handle the switch's SSH private key at all. This document states where
each of them is, what the process does with it, and what must never be committed
to this repository.

## What the process reads

| input | when | where |
| --- | --- | --- |
| `--config <path>` | once, at startup | any path; expected mode `0600`, owned by the service user |
| `ha_token`, `exporter_token`, `username`, `enable_secret`, `switches[]` | once, at startup, from that file | held in memory for the process lifetime |
| `SSH_AUTH_SOCK` | each time a command is about to run | environment; only used to name the agent socket |

Nothing else is read. There is no key file, no keyring, no environment-carried
token, and no command-line secret: everything secret a poll needs comes from the
config file, and everything needed to authenticate to a switch comes from the
agent.

## What the process does not do

* It does not read, parse, write or copy a private key. `ssh` is told
  `IdentityFile=none` and `IdentityAgent=<SSH_AUTH_SOCK>`, so signing happens in
  the agent, outside this process.
* It does not write anything to disk: no cache, no temp file, no log file, no
  state directory.
* It does not log secrets. The startup line is
  `[exporter] serving on <host>:<port>`; the only other diagnostics are the
  startup errors (config path, missing tokens, bind failure) and the SSH
  failure messages embedded in the response body, which carry a switch address
  and the local `ssh` stderr, never a token or the enable secret.
* It does not put the enable secret in a command line or a file. The secret is
  written to the `ssh` child's stdin, and only after the switch has actually
  asked for a password, so it is never pipelined into a session that did not
  request it and cannot be echoed back into captured output. The text exchanged
  during the escalation is discarded; only the reply to the `show` command is
  returned.
* It does not zeroize the tokens or lock its memory. They are ordinary `String`s
  in the process's address space. Core dumps and swap are therefore the residual
  exposure; `LimitCORE=0` on the unit (see [operations.md](operations.md)) is
  the cheap mitigation.

## Blast radius

| secret | where it lives | who can obtain it | what it grants |
| --- | --- | --- | --- |
| `ha_token` | the exporter's config file, and the consumer's configuration | anyone who can read the `0600` file (root, the service user) and anyone able to read the HTTP request — there is no TLS | one poll: seven SSH logins per switch from the bastion, and the resulting switch output |
| `exporter_token` | same | anyone who can reach the port: it is returned in `X-Exporter-Token` on every response, including `401` and `404` | nothing on its own. It lets an already-configured poller confirm the responder is the exporter it was configured for. |
| `enable_secret` | the exporter's config file | anyone who can read the `0600` file; the switch's own AAA/accounting records see the `enable` attempt | privileged EXEC on the switches |
| switch private key | the ssh-agent, never this process | whoever can reach `/run/cisco-agent/agent.sock` (mode `0700`, service user) | login to the switches at whatever privilege the key's user holds |

Two consequences worth stating plainly. The `ha_token` is the only credential
here that authorizes an action on the switches, and it travels over plain HTTP:
that is why the listener must stay on a private network. The exporter token is
not a secret from anything that can reach the listener, and must not be treated
as one when designing the network around this service.

## Rules for this repository

* No secret material of any kind belongs in this repository — no real token, no
  private key, no `enable` secret, no switch password, no `config.json` from a
  real deployment, and no fixture that merely looks like a real value.
* Test fixtures use obvious placeholders (`ha-tok`, `ex-tok`, `test-token`,
  `test-password`). Keep it that way.
* The config file produced by a deployment is machine-specific and must stay on
  the machine that runs the exporter.
* Never add a mechanism that makes a secret a command-line argument: command
  lines are world-readable through the process table.

## Rotation

* **Tokens.** Rewrite the config file with the new values (mode `0600`, owner
  the service user) and restart the exporter: the file is read once, at
  startup. Rotate `ha_token` on both sides in the same change, or the poller
  gets `401` until it is updated.
* **`enable_secret`.** Same shape: rewrite the config, restart. The switch side
  must accept the old secret until the restart, so there is no window where a
  poll succeeds against one and fails against the other.
* **Switch key.** No exporter restart is needed: the agent is consulted for
  every command, so a key added to the running agent is used by the very next
  command. Load the new key, verify a poll, then remove the old one from the
  agent. `ssh-add -` (key on stdin) is the way to load it without ever writing
  the key to disk on the bastion.