"""Daily snapshots (tomorrow's PDH / PDL / CPR) come from one real session only."""
from datetime import date, datetime, timedelta, timezone

from app.services.market_data import study_collector as sc
from tests.helpers import run

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 10, 19)


def _c(day, hh, mm, px):
    ts = (datetime(day.year, day.month, day.day, hh, 0, tzinfo=IST) + timedelta(minutes=mm)).isoformat()
    return {"timestamp": ts, "open": px, "high": px + 2, "low": px - 2, "close": px + 1, "volume": 0}


def test_session_candles_drop_previous_day_off_hours_and_duplicates():
    prev = date(2026, 10, 16)
    raw = ([_c(prev, 14, m, 23000.0) for m in range(30)]          # previous session tail
           + [_c(DAY, 7, 54, 1.0)]                                  # pre-open snapshot tick
           + [_c(DAY, 9, 15 + m, 23100.0 + m) for m in range(40)]
           + [_c(DAY, 9, 20, 99999.0)]                              # duplicate minute (last wins)
           + [_c(DAY, 21, 33, 2.0)])                                # evening snapshot tick
    out = sc.session_candles_for_day(raw, DAY)
    assert len(out) == 40
    assert out[0]["open"] == 23100.0 and out[-1]["open"] == 23139.0
    assert out[5]["open"] == 99999.0
    assert all(DAY.isoformat() in c["timestamp"] for c in out)


def test_holiday_or_short_session_writes_no_snapshot(monkeypatch):
    """Only off-hours candles today (a holiday): nothing is written, so the
    previous real session stays tomorrow's PDH/PDL."""
    raw = [_c(DAY, 7, 54, 1.0)] + [_c(date(2026, 10, 16), 10, m, 23000.0) for m in range(59)]

    async def _candles(*a, **k):
        return raw

    attempted = []

    def _no_db():
        attempted.append(True)
        raise RuntimeError("a snapshot write was attempted")

    monkeypatch.setattr(sc.redis_cache, "get_candles", _candles)
    monkeypatch.setattr(sc, "AsyncSessionLocal", _no_db)
    monkeypatch.setattr(sc, "date", type("D", (), {"today": staticmethod(lambda: DAY)}))
    run(sc.study_collector._save_daily_summary("NIFTY"))
    assert attempted == []


def test_a_real_session_reaches_the_db(monkeypatch):
    """Control for the test above: with a real session the writer gets to the DB."""
    raw = [_c(DAY, 9, 15 + m, 23100.0 + m) for m in range(120)]

    async def _candles(*a, **k):
        return raw

    attempted = []

    def _db():
        attempted.append(True)
        raise RuntimeError("stop here")

    monkeypatch.setattr(sc.redis_cache, "get_candles", _candles)
    monkeypatch.setattr(sc, "AsyncSessionLocal", _db)
    monkeypatch.setattr(sc, "date", type("D", (), {"today": staticmethod(lambda: DAY)}))
    run(sc.study_collector._save_daily_summary("NIFTY"))
    assert attempted == [True]


def test_pre_market_skips_a_single_price_snapshot(runtime_ctx, sqlite_db, monkeypatch):
    """15 Oct was saved as open = high = low = close; 16 Oct must take its
    PDH / PDL from the last real session (14 Oct), never PDH = PDL."""
    from sqlalchemy import create_engine

    from app.db.models.market_study import MarketDailySnapshot
    from app.services.trading.strategy_runner import strategy_runner
    from app.strategies.trend_engine import TrendFollowingEngine

    async def _mk():
        async with sqlite_db() as db:
            conn = await db.connection()
            await conn.run_sync(lambda c: MarketDailySnapshot.__table__.create(c, checkfirst=True))
            db.add_all([
                MarketDailySnapshot(id=1, date=date(2026, 10, 14), underlying="NIFTY",
                                    open=22705.1, high=22747.2, low=22558.95, close=22647.8),
                MarketDailySnapshot(id=2, date=date(2026, 10, 15), underlying="NIFTY",
                                    open=22997.35, high=22997.35, low=22997.35, close=22997.35),
            ])
            await db.commit()
    run(_mk())
    runtime_ctx.clock.set(datetime(2026, 10, 16, 9, 0, tzinfo=IST))
    eng = TrendFollowingEngine("NIFTY")
    monkeypatch.setattr(strategy_runner, "_strategies", {"NIFTY": [eng], "BANKNIFTY": []})
    run(strategy_runner._pre_market_setup())
    assert (eng.pdh, eng.pdl) == (22747.2, 22558.95)
