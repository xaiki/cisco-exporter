"""The exporter's HTTP client: identity, narrowing, and settings."""
from __future__ import annotations

import urllib.request

import pytest

from cisco_exporter.client import Exporter
from fakes import FakeResponse, urlopen_returning


def test_from_settings_reads_the_deployment_keys() -> None:
    target = Exporter.from_settings({
        "url": "http://x/api/status", "ha_token": "ha", "exporter_token": "ex",
        "timeout": "5"})
    assert target == Exporter("http://x/api/status", "ha", "ex", 5.0)


def test_from_settings_defaults_what_is_missing_or_junk() -> None:
    assert Exporter.from_settings({}) == Exporter("", "", "", 25.0)
    assert Exporter.from_settings({"timeout": True}).timeout == 25.0
    assert Exporter.from_settings({"timeout": "soon"}).timeout == 25.0
    assert Exporter.from_settings({"url": 3, "ha_token": None}).url == ""


def test_fetch_sends_the_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    monkeypatch.setattr(urllib.request, "urlopen", urlopen_returning(FakeResponse(), seen))
    Exporter("http://x/api/status", "ha-tok", "ex-tok").fetch()
    request = seen[0]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == "http://x/api/status"
    assert request.headers == {"Authorization": "Bearer ha-tok"}


def test_fetch_rejects_a_responder_that_does_not_know_the_token(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen",
                        urlopen_returning(FakeResponse("{}", exporter_token="wrong")))
    with pytest.raises(RuntimeError, match="token mismatch"):
        Exporter("http://x", "ha", "ex-tok").fetch()


def test_fetch_returns_the_switch_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen",
                        urlopen_returning(FakeResponse('{"switches": {"north": {}}}')))
    assert Exporter("http://x", "ha", "ex-tok").fetch() == {"switches": {"north": {}}}


def test_fetch_treats_a_body_that_is_not_an_object_as_no_snapshot(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", urlopen_returning(FakeResponse("[1, 2]")))
    assert Exporter("http://x", "ha", "ex-tok").fetch() == {}
