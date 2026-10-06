"""Fix 2: websockets 17.1 compatibility and status endpoints that never 500."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from app.services.market_data import feed as feed_mod
from app.services.market_data.feed import MarketFeed, _ws_is_open, market_feed


class _StateWS:
    def __init__(self, state):
        self.state = state
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class _LegacyWS:
    def __init__(self, closed):
        self.closed = closed


class _Bare:
    pass


class _ExplodingState:
    @property
    def state(self):
        raise RuntimeError("boom")


@pytest.mark.parametrize("ws, expected", [
    (None, False),
    (_StateWS(State.OPEN), True),
    (_StateWS(State.CONNECTING), False),
    (_StateWS(State.CLOSING), False),
    (_StateWS(State.CLOSED), False),
    (_LegacyWS(False), True),
    (_LegacyWS(True), False),
    (_Bare(), False),
    (_ExplodingState(), False),
])
def test_ws_is_open_and_is_connected(ws, expected):
    assert _ws_is_open(ws) is expected
    f = MarketFeed()
    f._ws = ws
    assert f.is_connected is expected


def test_real_client_connection_has_state_not_closed():
    from websockets.asyncio.client import ClientConnection
    assert not hasattr(ClientConnection, "closed")
    assert hasattr(ClientConnection, "state")


def test_heartbeat_survives_send_errors(monkeypatch):
    monkeypatch.setattr(feed_mod, "HEARTBEAT_INTERVAL", 0.01)

    class _Flaky(_StateWS):
        def __init__(self):
            super().__init__(State.OPEN)
            self.calls = 0

        async def send(self, msg):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            self.sent.append(msg)

    f = MarketFeed()
    f._ws = _Flaky()

    async def _go():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(f._heartbeat_loop(), 0.2)

    asyncio.run(_go())
    assert len(f._ws.sent) >= 2


def test_heartbeat_stops_on_connection_closed(monkeypatch):
    monkeypatch.setattr(feed_mod, "HEARTBEAT_INTERVAL", 0.01)

    class _Closed(_StateWS):
        def __init__(self):
            super().__init__(State.OPEN)

        async def send(self, msg):
            raise ConnectionClosed(None, None)

    f = MarketFeed()
    f._ws = _Closed()
    asyncio.run(asyncio.wait_for(f._heartbeat_loop(), 1.0))   # returns, no timeout


def test_send_subscribe_noop_when_not_open():
    f = MarketFeed()
    f._ws = _Bare()
    asyncio.run(f.send_subscribe("NFO", ["123"]))
    asyncio.run(f.send_unsubscribe("NFO", ["123"]))
    f._ws = _StateWS(State.OPEN)
    asyncio.run(f.send_subscribe("NFO", ["456"]))
    assert len(f._ws.sent) == 1


@pytest.mark.parametrize("ws, connected", [
    (None, False),
    (_StateWS(State.OPEN), True),
    (_Bare(), False),
])
def test_status_endpoints_never_500(monkeypatch, ws, connected):
    from app.main import app

    monkeypatch.setattr(market_feed, "_ws", ws)
    client = TestClient(app)   # no `with` → lifespan (Redis/feed/DB startup) does not run

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["feed_connected"] is connected

    r = client.get("/api/v1/market/feed/status")
    assert r.status_code == 200
    assert r.json()["connected"] is connected

    r = client.get("/api/v1/auth/status")
    assert r.status_code == 200
    assert r.json()["feed_connected"] is connected
