"""No test may reach the network. A background refresh fired by an endpoint
test once ran a real nine-source collection (and earned Open-Meteo 429s for
the live site, which shares this box's address). Any connect now fails fast,
so a test that accidentally triggers a collection sees an error, not upstream."""
import socket
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    # curl_cffi (the AccuWeather scrape) opens sockets in libcurl, below the
    # Python socket module, so it needs its own stub.
    fake = types.ModuleType("curl_cffi")
    fake.requests = types.SimpleNamespace(Session=refuse)
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
