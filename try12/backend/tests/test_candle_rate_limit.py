"""AngelOne historical calls are serialized, spaced and retried on a rate refusal."""
import time

import httpx
import pytest

from app.services.broker import angel_client as ac


class _FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json=None, headers=None):
        self.calls.append(time.monotonic())
        status, body = self.responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request("POST", ac.BASE_URL + url))


@pytest.fixture
def client(monkeypatch):
    c = ac.AngelClient()

    async def _noop():
        return None

    monkeypatch.setattr(c, "ensure_logged_in", _noop)
    monkeypatch.setattr(ac, "CANDLE_MIN_GAP_S", 0.05)
    monkeypatch.setattr(ac, "CANDLE_RATE_BACKOFF_S", 0.01)
    monkeypatch.setattr(ac, "_candle_lock", None)
    monkeypatch.setattr(ac, "_last_candle_call", 0.0)
    return c


def _use(monkeypatch, c, fake):
    async def _get():
        return fake
    monkeypatch.setattr(c, "_client", _get)


OK = (200, {"status": True, "data": [["2026-10-19T09:15:00+05:30", 1, 2, 0.5, 1.5, 10]]})
RATE = (403, {"message": "Access denied because of exceeding access rate"})


async def test_rate_refusal_is_retried(client, monkeypatch):
    fake = _FakeHTTP([RATE, OK])
    _use(monkeypatch, client, fake)
    rows = await client.get_candle_data("NFO", "1", "ONE_MINUTE", "2026-10-19 09:15", "2026-10-19 15:30")
    assert len(rows) == 1 and len(fake.calls) == 2


async def test_gives_up_after_retries(client, monkeypatch):
    fake = _FakeHTTP([RATE] * ac.CANDLE_RATE_RETRIES)
    _use(monkeypatch, client, fake)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_candle_data("NFO", "1", "ONE_MINUTE", "a", "b")
    assert len(fake.calls) == ac.CANDLE_RATE_RETRIES


async def test_other_403_is_not_retried(client, monkeypatch):
    fake = _FakeHTTP([(403, {"message": "Invalid token"})])
    _use(monkeypatch, client, fake)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_candle_data("NSE", "1", "ONE_MINUTE", "a", "b")
    assert len(fake.calls) == 1


async def test_concurrent_calls_are_spaced(client, monkeypatch):
    import asyncio
    fake = _FakeHTTP([OK] * 4)
    _use(monkeypatch, client, fake)
    await asyncio.gather(*[client.get_candle_data("NFO", str(i), "ONE_MINUTE", "a", "b") for i in range(4)])
    gaps = [b - a for a, b in zip(fake.calls, fake.calls[1:])]
    assert len(fake.calls) == 4 and min(gaps) >= 0.045
