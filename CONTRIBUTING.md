# Contributing

## Build and test

```sh
cargo build --release --locked
cargo test
```

`cargo test` needs no network, no switch and no ssh-agent: every test is a unit
test in `src/main.rs` and they exercise pure functions (the IOS prompt protocol
through `ios_exchange` with a scripted prompt source, the SSH argument list, the
bearer-token check, the response envelope, the config parser). Keep it that way
— a test that needs a switch cannot run in CI-less development, and there is no
CI here.

Formatting and lints, if you have the components installed:

```sh
cargo fmt
cargo clippy --all-targets
```

## Ground rules

* **Keep the dependency list at one.** `serde_json` is the only crate. Adding an
  HTTP framework, an async runtime or an SSH library changes the deployment
  story (see [docs/architecture.md](docs/architecture.md)) and needs a
  justification in the commit message. Do not upgrade dependencies as a side
  effect of an unrelated change.
* **Keep the HTTP server hand-written and small.** Every route, header and
  status code that exists is documented in [../README.md](../README.md); adding
  one means updating that table.
* **Keep parsing out of this binary.** The exporter returns raw `show` output.
  The consumer parses it, and a parser change must not require a new binary on
  the bastion.
* **No secret material, ever.** No tokens, no private keys, no `enable` secrets,
  no real `config.json`, and no fixture with a realistic-looking value — use
  `test-token`, `test-password`, `ha-tok`. See
  [docs/credentials.md](docs/credentials.md).
* **No real network details in tests, examples or documentation.** Use the
  documentation address ranges (`192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24`) and example hostnames (`sw-north`, `bastion-1`,
  `example.ts.net`). No real switch hostnames, addresses or paths from a live
  deployment.

## What the deployment expects from this crate

The deployment automation that installs the exporter consumes this crate at a
fixed shape, so treat these as interfaces:

* the crate is built **on the target host** (`Cargo.toml`, `Cargo.lock` and
  `src/**/*.rs` are shipped there; `Cargo.lock` must therefore stay committed
  and consistent);
* the binary is called `cisco-exporter` and is installed at
  `/var/local/cisco-exporter/cisco-exporter`;
* it is started as `cisco-exporter --config <path>` and gets `SSH_AUTH_SOCK`
  from the unit, so the environment handling in `main` is load-bearing;
* the config file keys and the `GET /api/status` response shape are the
  contract with the Home Assistant poller. Changing either is a breaking change
  on both sides of the link, not a local refactor.

## Commit style

Subjects follow the shape used in this repository's history:

```
type(scope): lower-case imperative subject
```

with `type` in `{feat, fix, refactor, docs, chore, test}` and `scope` naming the
area (`cisco`, `deploy`, `exporter`). Keep the subject under ~72 characters and
put the reasoning in the body when the change is not self-evident — especially
for anything touching the SSH option set or the IOS session script, where the
reason for a line is usually an observed switch behaviour rather than a
preference.

Documentation that records an empirical IOS or OpenSSH quirk belongs in
[docs/cisco-ios.md](docs/cisco-ios.md) with the observation that produced it.