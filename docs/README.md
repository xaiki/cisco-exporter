# Documentation

| document | contents |
| --- | --- |
| [../README.md](../README.md) | what the exporter is, build, configuration, HTTP surface, auth model, `--dump`, the standalone consumer, systemd install |
| [architecture.md](architecture.md) | why a Rust pipe plus a Python consumer, how `ssh` is driven as a subprocess, the deliberate absence of TLS/async/SSH libraries and what it means for deployment |
| [cisco-ios.md](cisco-ios.md) | SSH negotiation and the algorithm pinning older IOS needs, the privileged-EXEC session script, prompt handling, IOS quirks, failure signatures |
| [credentials.md](credentials.md) | what the process reads, what it never stores, blast radius per secret, rotation, and the rule that no secret material belongs in this repository |
| [operations.md](operations.md) | unit files, install steps, sandboxing, observability, failure modes, upgrading |
| [metrics.md](metrics.md) | the Python package: the parsers, the client, `--dump` consumption, the `/metrics` bridge and its flags, the metric reference, alerting |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | build, test and commit conventions |

Start with the [README](../README.md) for the interface, then
[architecture.md](architecture.md) for the reason it looks the way it does. If
you are deploying it, read [operations.md](operations.md) and
[credentials.md](credentials.md) before the first poll; if you are debugging a
switch, read [cisco-ios.md](cisco-ios.md); if you are consuming the snapshot —
from Home Assistant, Prometheus or your own code — read
[metrics.md](metrics.md).