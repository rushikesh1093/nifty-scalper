"""Feedless-start resilience: a cut-off scrip-master download no longer leaves
the app without a feed when the instruments table already covers today, the
failing step is recorded for /health, and the download itself is retried."""
from datetime import date

import httpx
import pytest

from app.services.market_data import market_manager as mm_mod
from tests.helpers import run


@pytest.fixture
def mm(monkeypatch):
    m = mm_mod.MarketManager()
    monkeypatch.setattr(mm_mod.settings.__class__, "angel_credentials_configured",
                        property(lambda self: True), raising=False)

    async def _ok(*a, **k):
        return None

    monkeypatch.setattr(mm_mod.angel_client, "login", _ok)
    monkeypatch.setattr(mm_mod.redis_cache, "set_angel_session", _ok)

    class _S:
        jwt_token = refresh_token = feed_token = "x"
    import app.services.broker.angel_client as ac
    monkeypatch.setattr(ac, "_session", _S())

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(mm_mod, "AsyncSessionLocal", lambda: _DB())
    return m


def _cut_off(*a, **k):
    raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


def test_sync_failure_without_usable_instruments_is_recorded(mm, monkeypatch):
    monkeypatch.setattr(mm_mod, "sync_instruments", lambda db: _cut_off())

    async def _no(db, as_of):
        return False
    monkeypatch.setattr(mm_mod, "instruments_cover_today", _no)
    with pytest.raises(httpx.RemoteProtocolError):
        run(mm.startup())
    assert mm.startup_attempts == 1
    assert mm.startup_error.startswith("instrument sync: RemoteProtocolError")


def test_sync_failure_with_usable_instruments_continues(mm, monkeypatch):
    async def _fail(db):
        _cut_off()
    monkeypatch.setattr(mm_mod, "sync_instruments", _fail)

    async def _yes(db, as_of):
        return True
    monkeypatch.setattr(mm_mod, "instruments_cover_today", _yes)

    async def _stop(*a, **k):
        raise RuntimeError("reached watchlist")       # proves startup moved past the sync
    monkeypatch.setattr(mm, "_build_watchlist", _stop)
    with pytest.raises(RuntimeError, match="reached watchlist"):
        run(mm.startup())
    assert mm.startup_stage == "watchlist" and mm._instruments_synced is True
    # a retry does not download the scrip master again
    calls = []

    async def _count(db):
        calls.append(1)
        return 0
    monkeypatch.setattr(mm_mod, "sync_instruments", _count)
    with pytest.raises(RuntimeError):
        run(mm.startup())
    assert calls == [] and mm.startup_attempts == 2


def test_scrip_master_download_is_retried(monkeypatch):
    import app.services.broker.angel_client as ac
    attempts = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            attempts.append(url)
            if len(attempts) < 3:
                raise httpx.RemoteProtocolError("cut off")
            return httpx.Response(200, json=[{"token": "1"}], request=httpx.Request("GET", url))

    async def _nosleep(s):
        return None
    monkeypatch.setattr(ac.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(ac.asyncio, "sleep", _nosleep)
    assert run(ac.angel_client.get_instrument_dump()) == [{"token": "1"}]
    assert len(attempts) == 3


def test_instruments_cover_today_needs_options_and_future_for_both(sqlite_db):
    from app.db.base import Base
    from app.db.models.instrument import Instrument, InstrumentType, OptionType
    from sqlalchemy import create_engine

    async def _q():
        async with sqlite_db() as db:
            conn = await db.connection()
            await conn.run_sync(lambda c: Instrument.__table__.create(c, checkfirst=True))
            await db.commit()
            today = date(2026, 10, 21)
            assert await mm_mod.instruments_cover_today(db, today) is False
            for name in ("NIFTY", "BANKNIFTY"):
                db.add(Instrument(token=f"{name}F", symbol=f"{name}FUT", name=name,
                                  instrument_type=InstrumentType.FUTIDX, exchange="NFO",
                                  expiry=date(2026, 10, 27), lot_size=65, is_active=True))
                db.add(Instrument(token=f"{name}O", symbol=f"{name}CE", name=name,
                                  instrument_type=InstrumentType.OPTIDX, exchange="NFO",
                                  option_type=OptionType.CE, strike=23000.0,
                                  expiry=date(2026, 10, 27), lot_size=65, is_active=True))
            await db.commit()
            assert await mm_mod.instruments_cover_today(db, today) is True
            assert await mm_mod.instruments_cover_today(db, date(2026, 10, 28)) is False
    run(_q())
