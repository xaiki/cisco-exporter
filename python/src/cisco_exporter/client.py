"""Where the exporter is, and how to authenticate to it."""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

#: Header the exporter stamps on every response, its errors included.
EXPORTER_TOKEN_HEADER = "X-Exporter-Token"


@dataclass(frozen=True)
class Exporter:
    """The exporter's address and the two tokens it shares with its consumer.

    ``ha_token`` is the credential the exporter checks on the request, and
    ``exporter_token`` is the identity it stamps on the response — the
    consumer compares the two it holds, so a different HTTP server answering
    on that address cannot be mistaken for the exporter.
    """

    url: str
    ha_token: str
    exporter_token: str
    timeout: float = 25.0

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> Exporter:
        """Build from a settings mapping, as the deployment writes it.

        The keys are the ones the Home Assistant deployment already writes
        into its poller settings file: ``url``, ``ha_token``,
        ``exporter_token``, and optionally ``timeout``. A zero timeout is kept
        as written rather than replaced, so it can be rejected as invalid.
        """
        timeout = _seconds(settings.get("timeout"))
        return cls(
            url=_text(settings, "url"),
            ha_token=_text(settings, "ha_token"),
            exporter_token=_text(settings, "exporter_token"),
            timeout=25.0 if timeout is None else timeout,
        )

    def fetch(self) -> dict[str, object]:
        """Fetch the raw snapshot, verifying that the responder is the exporter.

        Anything else answering on that address does not know the exporter
        token, so a mismatch is an error. The envelope is narrowed at the read
        rather than trusted: ``json.loads`` is untyped, and a body that is not
        an object is not a snapshot.
        """
        request = urllib.request.Request(
            self.url, headers={"Authorization": f"Bearer {self.ha_token}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            got = response.headers.get(EXPORTER_TOKEN_HEADER, "")
            if got != self.exporter_token:
                raise RuntimeError("exporter token mismatch")
            payload: object = json.loads(response.read().decode())
        if not isinstance(payload, Mapping):
            return {}
        return {str(key): item for key, item in payload.items()}


def _text(settings: Mapping[str, object], key: str) -> str:
    value = settings.get(key)
    return value if isinstance(value, str) else ""


def _seconds(value: object) -> float | None:
    """A timeout as written in JSON (a number, or a numeric string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
