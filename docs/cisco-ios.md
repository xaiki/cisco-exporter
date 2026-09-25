# Cisco / IOS operational notes

What this exporter has to do to get one `show` command out of an IOS switch, and
why each SSH option is there. The reference hardware is the 2960X family running
IOS 15.2(7)E, but nothing here is specific to a model — only to a generation of
IOS and of OpenSSH.

## SSH negotiation

The exporter runs the host's `ssh` client with this fixed option set. It does
not read the host's ssh config; `-F /dev/null` throws it away, so the argument
list below is the entire configuration.

| option | purpose |
| --- | --- |
| `-F /dev/null` | ignore `~/.ssh/config` and `/etc/ssh/ssh_config` |
| `-o BatchMode=yes` | never prompt; fail instead of asking for a passphrase |
| `-o IdentityFile=none` | no key file takes part, even a default one |
| `-o IdentityAgent=<SSH_AUTH_SOCK>` | take the key from the agent named by the environment |
| `-o PreferredAuthentications=publickey` | no password or keyboard-interactive fallback |
| `-o StrictHostKeyChecking=no` | no host-key verification (see below) |
| `-o UserKnownHostsFile=/dev/null` | no known-hosts file is read or written |
| `-o ConnectTimeout=10` | bound the TCP/SSH connect |
| `-o HostKeyAlgorithms=+ssh-rsa` | older switches offer only an `ssh-rsa` host key; OpenSSH disables that algorithm by default. The `+` appends it, so modern host keys stay available. |
| `-o KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1` | the same for the SHA-1 key-exchange groups older IOS offers |
| `-o PubkeyAcceptedAlgorithms=ssh-rsa` | **legacy attempt only** — see the retry below |
| `-t -t` | force a pty even though there is no local terminal; the privileged-EXEC session is scripted like a console session |
| `-p <port>` | from the switch's config entry |
| `<username>@<host>` | from the config, last argument |

Two negotiation attempts are made per command. The first is the option set
above; if it fails for any reason, the whole command is retried on a **new
connection** with `PubkeyAcceptedAlgorithms=ssh-rsa`, which replaces (rather
than appends to) the accepted-signature list. The reason for a new connection
is that the failure this retry exists for is firmware that drops the session on
an `rsa-sha2-*` user signature — at which point there is nothing left to retry
on. Both failures are reported together as
`modern SSH: <error>; legacy SSH: <error>`, so a genuine failure (bad key, wrong
user) is not hidden behind the retry.

The `+` prefix in `HostKeyAlgorithms` and `KexAlgorithms` matters: it is an
append to OpenSSH's default list, not a downgrade of it. Hosts that support
modern algorithms keep using them.

Host-key verification is off, and no host-key state exists on the bastion. This
is a deliberate assumption about the network — the switches are on a private
management segment and nothing else is — not a statement about the switches. If
that assumption is wrong, this is the first thing to change.

## The privileged-EXEC session

The key logs into a normal, unprivileged EXEC session (a `>` prompt). The `show`
commands in this exporter need privileged EXEC (a `#` prompt), so the session is
scripted. Nothing is written to the switch until the switch has asked for it:

1. Read the initial prompt.
2. If it ends with `>`, send `enable`.
3. Read again. If it ends with `Password:` (any case), send `enable_secret`.
   If `enable_secret` is empty, stop here with
   `IOS enable password is not configured` — the command is never typed into a
   password prompt.
4. The prompt must now end with `#`. Otherwise:
   `IOS privileged prompt was not reached`.
5. Send `terminal length 0`. If the reply does not end with `#`, or contains
   `% `, stop with `IOS paging could not be disabled`.
6. Send the `show` command. If the reply does not end with `#`, or contains
   `% `, stop with `IOS rejected the status command`.
7. The reply to step 6 is the captured text.

Consequences of this order:

* No blind pipelining. The secret is sent only after a password prompt is
  observed, so a switch whose login already lands privileged never sees it, and
  the secret cannot end up echoed into the output that gets returned.
* `terminal length 0` is mandatory and its acceptance is verified. Without it
  IOS pages the output with `--More--` and the captured text would be a fragment.
* A landing at `#` straight from the key (a user with privilege 15, or an
  auto-command) skips step 2 and 3 entirely, so `enable_secret` may be left
  empty in that case.

## Prompt recognition

While a session is scripted, the exporter reads the child's stdout and considers
a prompt complete when the accumulated text, with trailing whitespace removed,
ends with `#`, ends with `>`, or ends (case-insensitively) with `password:`.

* Every prompt has a 15-second deadline, measured from the start of that read.
  A switch that goes quiet mid-session is an error, not a hang.
* Accumulated text is capped at 2 MiB per prompt; exceeding it is an error.
* The capture is decoded as UTF-8 lossily. IOS output is not guaranteed to be
  UTF-8 (accented interface descriptions, box-drawing in `show` tables), and
  non-UTF-8 bytes arrive at the consumer as U+FFFD. The consumer's parsers see
  the replacement characters in those fields.

## Quirks to keep in mind

* Any `% ` (percent, space) in a captured reply is treated as a CLI error. On a
  switch, `% ...` is how IOS reports a rejected command, so this is the right
  default — but it is a substring test: a legitimate line containing `% ` in
  that exact form fails the command too.
* The `#` expectation is on the prompt, not on a marker. A switch configured
  with a non-default prompt shape fails at step 4 or 6.
* One SSH connection is used **per command** unless `ssh_control_dir` is set in
  the config: seven sessions per switch per poll on the happy path, up to
  fourteen when the legacy retry fires. With a control directory the commands
  share one session (and the next poll re-uses it while the master lives), so
  the switch sees one login per poll instead of one per `show` — which matters
  because the switch's own side counts them: session limits, `show users`,
  TACACS+/RADIUS accounting and syslog all see each login. It also means a poll
  is slow by construction without it — see the serial-collection notes in
  [../README.md](../README.md).
* `show power inline` and `show mac address-table` may require privilege 15 on
  some platforms; if the enable step is skipped because the login is not
  privileged, those two fields are the ones that come back rejected.

## Failure signatures

Every one of these appears as the string `"__error__: <message>"` in place of
the raw output for that single command; the rest of the snapshot is still
returned.

| message | what happened |
| --- | --- |
| `Cisco SSH agent socket is not configured` | `SSH_AUTH_SOCK` is unset or empty for the service |
| `ssh spawn: <os error>` | `ssh` is not on `PATH` or not executable |
| `SSH closed or timed out waiting for IOS prompt` | no prompt within 15 s: the switch is unreachable, the key was rejected, or the session dropped. The `ssh` stderr is appended, so `Permission denied (publickey)` shows up here. |
| `IOS response exceeds size limit` | more than 2 MiB arrived without a prompt |
| `IOS enable password is not configured` | a password prompt was seen and `enable_secret` is empty |
| `IOS privileged prompt was not reached` | `enable` was rejected, or the session did not reach a `#` prompt |
| `IOS paging could not be disabled` | `terminal length 0` was rejected (typically `% Authorization failed` from AAA) |
| `IOS rejected the status command` | the `show` itself returned a `% ` line |
| `modern SSH: <e1>; legacy SSH: <e2>` | both negotiation attempts failed; `e1` is the modern attempt, `e2` the `ssh-rsa` retry |

The message is prefixed with the switch: `ssh <host>: <message>: <ssh stderr>`.
That includes the switch's address, and whatever the local `ssh` printed on
stderr. It is useful for diagnosis, and it is why these strings belong on the
private link to the consumer, not on a route to anywhere else.