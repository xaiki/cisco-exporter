# Contributing

## Build and test

The repository holds two artifacts: the Rust exporter (a binary) and the Python
package `cisco_exporter` (the show parsers, the exporter's HTTP client and the
Prometheus renderer). Each is built and tested on its own.

```sh
cargo build --release --locked
cargo test
```

`cargo test` needs no network, no switch and no ssh-agent: every test is a unit
test in `src/tests.rs` and they exercise pure functions (the IOS prompt protocol
through `ios_exchange` with a scripted prompt source, the SSH argument list, the
bearer-token check, the command line, the `--dump` envelope, the config
parser). Keep it that way — a test that needs a switch cannot run in
development.

The tests live in their own file on purpose: the deployment digests what
compiles into the binary to decide whether a host needs the new one, so a
test-only edit must not count as a new build (`machines.host_cisco_exporter.
version_id`, and the same rule in ghostd). Editing only `src/tests.rs` keeps
the stamp, and therefore every host, where it is.

```sh
cd python
uv run pytest
```

The Python tests need no network either: the exporter is replaced by a stub
returning canned `show` output, and the bridge is exercised through a real
loopback HTTP server. Parser tests run against captured `show` output, never
against a live switch — keep that property when adding cases.

Formatting and lints, the same commands CI runs
([.github/workflows/ci.yml](.github/workflows/ci.yml)):

```sh
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cd python && uv run ruff check . && uv run mypy
```

`mypy` runs strict, with `disallow_any_explicit`: a new `Any` is a build
failure, so narrow what you read instead of annotating it away.

## Ground rules

* **Keep the dependency list at one.** `serde_json` is the only crate. Adding an
  HTTP framework, an async runtime or an SSH library changes the deployment
  story (see [docs/architecture.md](docs/architecture.md)) and needs a
  justification in the commit message. Do not upgrade dependencies as a side
  effect of an unrelated change.
* **Keep the HTTP server hand-written and small.** Every route, header and
  status code that exists is documented in [README.md](README.md); adding one
  means updating that table.
* **Keep parsing out of the binary.** The exporter returns raw `show` output, so
  a parser change is never a new binary on the bastion.
* **The show parsers have one home: `python/src/cisco_exporter/`.** Consumers
  import them; nobody keeps a copy. Two of those modules — `parsers.py` and
  `client.py` — are additionally *shipped as flat files* to a host that has no
  environment to install into, so they must stay standard-library-only, free of
  package-relative imports, and importable under the names the deployment gives
  them (`cisco_parsers`, `cisco_exporter_client`). Moving code into or out of
  them changes what that host receives.
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
* `--dump` is the same collection path with no listener and no token check: it
  is the debugging and scripting entry point, and its envelope is the same one
  the HTTP endpoint returns;
* the config file keys and the `GET /api/status` response shape are the
  contract with the Home Assistant poller. Changing either is a breaking change
  on both sides of the link, not a local refactor;
* the Python package is consumed as a **git dependency pinned to a tag**, from
  the `python/` subdirectory, so a tag must be installable on its own:
  `cd python && uv build` and `uv lock --check` are part of the release;
* `cisco_exporter/parsers.py` and `cisco_exporter/client.py` are copied into
  the Home Assistant installation as `config/scripts/cisco_parsers.py` and
  `config/scripts/cisco_exporter_client.py`, and imported there as bare module
  names. Their filenames, their public names and their standard-library-only
  imports are therefore interfaces, not internals.

## Commit style

Subjects follow the shape used in this repository's history:

```
type(scope): lower-case imperative subject
```

with `type` in `{feat, fix, refactor, docs, chore, test}` and `scope` naming the
area (`cisco`, `deploy`, `exporter`, `python`, `metrics`). Keep the subject
under ~72 characters and put the reasoning in the body when the change is not
self-evident — especially for anything touching the SSH option set or the IOS
session script, where the reason for a line is usually an observed switch
behaviour rather than a preference.

Documentation that records an empirical IOS or OpenSSH quirk belongs in
[docs/cisco-ios.md](docs/cisco-ios.md) with the observation that produced it.