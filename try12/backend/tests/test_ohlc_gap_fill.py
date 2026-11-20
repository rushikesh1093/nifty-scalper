"""REST history fills the ohlc table's missing minutes without touching live candles."""
import asyncio
from datetime import datetime, timezone

import pytest

from tests.conftest import _patch_session_factory


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ohlc_db(tmp_path, monkeypatch):
    pytest.importorskip("aiosqlite")
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.db.base import Base
    from app.db.models.ohlc import OHLC

    f = tmp_path / "o.db"
    eng = create_engine(f"sqlite:///{f}")
    Base.metadata.create_all(eng, tables=[OHLC.__table__])
    eng.dispose()
    Session = sessionmaker(create_async_engine(f"sqlite+aiosqlite:///{f}", poolclass=NullPool),
                           class_=AsyncSession, expire_on_commit=False)
    _patch_session_factory(monkeypatch, Session)
    return Session


def _rows(Session):
    from sqlalchemy import select
    from app.db.models.ohlc import OHLC

    async def q():
        async with Session() as db:
            return [(r.timestamp.replace(tzinfo=None), r.close) for r in
                    (await db.execute(select(OHLC).order_by(OHLC.timestamp))).scalars()]
    return run(q())


def test_rest_history_fills_missing_minutes_and_keeps_live_ones(ohlc_db):
    import app.services.market_data.market_manager as mm
    from app.db.models.ohlc import OHLC

    async def live():
        async with ohlc_db() as db:
            db.add(OHLC(token="26000", symbol="NIFTY", exchange="NSE", interval="ONE_MINUTE",
                        open=1, high=1, low=1, close=111.0,
                        timestamp=datetime(2026, 10, 29, 3, 46, tzinfo=timezone.utc)))
            await db.commit()
    run(live())

    rest = [{"token": "26000", "symbol": "NIFTY", "interval": "ONE_MINUTE",
             "timestamp": f"2026-10-29T09:{m}:00+05:30", "open": 1, "high": 2, "low": 0.5,
             "close": float(m), "volume": 0} for m in (15, 16, 17)]
    assert run(mm.fill_ohlc_gaps(rest, "NSE")) == 3
    assert run(mm.fill_ohlc_gaps(rest, "NSE")) == 3            # second run: no duplicates
    assert _rows(ohlc_db) == [(datetime(2026, 10, 29, 3, 45), 15.0),
                              (datetime(2026, 10, 29, 3, 46), 111.0),   # live candle kept
                              (datetime(2026, 10, 29, 3, 47), 17.0)]


def test_gap_fill_never_raises_without_a_database():
    import app.services.market_data.market_manager as mm
    rest = [{"token": "26000", "symbol": "NIFTY", "timestamp": "2026-10-29T09:15:00+05:30",
             "open": 1, "high": 1, "low": 1, "close": 1}]
    assert run(mm.fill_ohlc_gaps(rest, "NSE")) == 0           # DB blocked in tests
