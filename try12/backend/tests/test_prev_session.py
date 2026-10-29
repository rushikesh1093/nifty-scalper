"""
PIVOT_BOUNCE's previous-session levels (29 Oct investigation): they were read
from the rolling 500-candle cache, so they vanished once today passed ~200
candles (about 12:35) and were missing all day after a partial session. They
are now resolved once a day (app.services.market_data.prev_session) and kept on
the strategy, independent of the cache.
"""
import logging
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.services.market_data import prev_session as ps
from tests.helpers import IST, run, seed_config
from tests.test_no_trade import _NoInstruments

TODAY = datetime(2026, 9, 1, 9, 15)           # Tuesday, naive IST like the feed
YESTERDAY = datetime(2026, 8, 31, 9, 15)      # Monday


def _session(start, n=375, base=22000.0, hi_at=50, lo_at=100, hi=22100.0, lo=21900.0):
    """n one-minute candles from *start*: flat at *base* with one high and one low spike."""
    out = []
    for k in range(n):
        h = hi if k == hi_at else base + 2
        l = lo if k == lo_at else base - 2
        ts = start + timedelta(minutes=k)
        out.append({"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": base, "high": h,
                    "low": l, "close": base, "volume": 0})
    return out


def _daily(day, h, l, c):
    return [f"{day.isoformat()}T00:00:00+05:30", c, h, l, c, 0]


# ── resolve(): the pure rules ────────────────────────────────────────────────

def test_a_complete_previous_session_from_one_minute_candles():
    res = ps.resolve(TODAY.date(), _session(YESTERDAY))
    lv = res.levels
    assert (lv.day, lv.high, lv.low, lv.close, lv.source) == \
        (YESTERDAY.date(), 22100.0, 21900.0, 22000.0, ps.MINUTE_1M)
    assert res.reason is None and res.note is None


def test_monday_uses_friday_not_calendar_yesterday():
    monday, friday = date(2026, 8, 31), datetime(2026, 8, 28, 9, 15)
    res = ps.resolve(monday, _session(friday))
    assert res.levels.day == friday.date() and res.note is None       # a weekend is not a gap
    broker = ps.resolve(monday, [], [_daily(date(2026, 8, 27), 1, 1, 1),
                                     _daily(friday.date(), 22200.0, 21800.0, 22050.0)])
    assert (broker.levels.day, broker.levels.source) == (friday.date(), ps.BROKER_DAILY)
    assert broker.levels.high == 22200.0


def test_the_day_after_a_holiday_uses_the_last_real_session():
    wed = date(2026, 9, 2)                     # Tuesday 1 Sep a holiday: no data
    res = ps.resolve(wed, [], [_daily(date(2026, 8, 31), 22300.0, 21950.0, 22100.0)])
    assert res.levels.day == date(2026, 8, 31) and res.levels.source == ps.BROKER_DAILY
    assert res.note is None                    # the broker only lists real sessions
    minute = ps.resolve(wed, _session(YESTERDAY))
    assert minute.levels.day == date(2026, 8, 31)
    assert "2026-09-01" in minute.note and "holiday" in minute.note   # said out loud


def test_a_partial_previous_session_gives_no_levels_and_never_an_older_day():
    older = _session(datetime(2026, 8, 28, 9, 15))                    # Friday, complete
    partial = _session(datetime(2026, 8, 31, 12, 38), n=172)          # Monday from 12:38
    res = ps.resolve(TODAY.date(), older + partial)
    assert res.levels is None
    assert "2026-08-31 is incomplete" in res.reason and "172/375" in res.reason


def test_a_partial_previous_session_is_completed_by_the_broker_daily_candle():
    partial = _session(datetime(2026, 8, 31, 12, 38), n=172)
    res = ps.resolve(TODAY.date(), partial,
                     [_daily(date(2026, 8, 31), 22400.0, 21700.0, 22010.0)])
    lv = res.levels
    assert (lv.day, lv.high, lv.low, lv.close, lv.source) == \
        (date(2026, 8, 31), 22400.0, 21700.0, 22010.0, ps.BROKER_DAILY)


def test_a_lagging_broker_does_not_win_over_a_newer_known_session():
    res = ps.resolve(TODAY.date(), _session(YESTERDAY),
                     [_daily(date(2026, 8, 28), 1.0, 1.0, 1.0)])
    assert res.levels.day == YESTERDAY.date() and res.levels.source == ps.MINUTE_1M


def test_no_data_at_all_is_a_reason_not_levels():
    res = ps.resolve(TODAY.date(), [], None)
    assert res.levels is None and "no previous-session data" in res.reason
    assert ps.resolve(TODAY.date(), _session(TODAY)).levels is None      # today's candles never count


# ── through the runner ───────────────────────────────────────────────────────

@pytest.fixture
def pivot_env(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    """A runner with the real PivotBounceStrategy, every gate open (regime merged
    to all-enabled), and an ohlc table in the SQLite DB."""
    from dataclasses import replace

    from sqlalchemy import create_engine

    from app.core.config import settings
    from app.db.base import Base
    from app.db.models.ohlc import OHLC
    from app.runtime.context import using
    from app.services.trading import strategy_runner as sr
    from app.strategies.base import SignalBus
    from app.strategies.names import ALL_STRATEGY_NAMES
    from app.strategies.strategies import PivotBounceStrategy

    eng = create_engine(str(sqlite_db.kw["bind"].url).replace("+aiosqlite", ""))
    Base.metadata.create_all(eng, tables=[OHLC.__table__])
    eng.dispose()
    run(seed_config(sqlite_db))
    runner = sr.StrategyRunner()

    async def _merge(underlying, candles, indicators, is_expiry_day, flags, history=None):
        return {n: True for n in ALL_STRATEGY_NAMES}, None
    monkeypatch.setattr(runner, "_detect_and_merge_regime", _merge)

    async def _capture(signal):
        return False
    monkeypatch.setattr(SignalBus, "_handlers", [_capture])

    broker_calls = []

    async def _no_broker(token, day):
        broker_calls.append(day)
        return None
    # the live port's optional broker method (RedisMarketData.get_daily_candles)
    monkeypatch.setattr(fake_redis, "get_daily_candles", _no_broker, raising=False)

    strat = PivotBounceStrategy("NIFTY")
    seen = []
    real_eval = strat.evaluate

    async def _spy(candles, indicators):
        seen.append(strat.pivots())
        return await real_eval(candles, indicators)
    strat.evaluate = _spy
    runner._strategies = {"NIFTY": [strat]}
    key = f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"

    def cache(candles, at):
        fake_redis.kv[key] = candles[-500:]                 # the runner's 500-candle buffer
        runtime_ctx.clock.set(at.replace(tzinfo=IST))

    with using(replace(runtime_ctx, instruments=_NoInstruments())):
        yield runner, strat, cache, seen, broker_calls


def _gate_rows(Session):
    from app.db.models.risk import NoTradeDecision

    async def _go():
        async with Session() as db:
            return (await db.execute(select(NoTradeDecision).where(
                NoTradeDecision.code == "PREV_SESSION_UNAVAILABLE"))).scalars().all()
    return run(_go())


def test_pivots_last_all_session_with_a_500_candle_buffer(pivot_env, sqlite_db):
    runner, strat, cache, seen, broker_calls = pivot_env
    yday = _session(YESTERDAY)
    cache(yday, TODAY.replace(hour=9, minute=0))
    run(runner._pre_market_setup())                         # before the open
    expected = {"S1": 21900.0, "PP": 22000.0, "R1": 22100.0}
    assert strat.pivots() == expected and broker_calls == [TODAY.date()]

    for hh, mm in ((9, 30), (9, 35), (12, 40), (14, 0)):
        at = TODAY.replace(hour=hh, minute=mm)
        n = int((at - TODAY).total_seconds() // 60)
        cache(yday + _session(TODAY, n=n, base=22050.0, hi_at=-1, lo_at=-1), at)
        prev_in_buffer = sum(1 for c in (yday + _session(TODAY, n=n))[-500:]
                             if c["timestamp"].startswith("2026-08-31"))
        assert strat.pivots() == expected, f"{hh:02d}:{mm:02d}"
        run(runner.run_cycle("NIFTY"))
        if (hh, mm) == (14, 0):
            assert prev_in_buffer < 300                     # the old rule would have failed here
    assert seen == [expected] * 3                           # 09:35, 12:40, 14:00 evaluated
    assert _gate_rows(sqlite_db) == []
    assert broker_calls == [TODAY.date()]                   # resolved once, not per cycle


def test_a_mid_session_start_uses_the_stored_session_not_the_buffer(pivot_env, sqlite_db):
    """Started at 12:40: the buffer holds only 295 of yesterday's candles, the
    ohlc table holds the whole session, so the levels come from the table."""
    from app.core.config import settings
    from app.db.models.ohlc import OHLC
    from datetime import timezone

    runner, strat, cache, seen, _ = pivot_env
    yday = _session(YESTERDAY)

    async def _store():
        async with sqlite_db() as db:
            for c in yday:
                ts = datetime.fromisoformat(c["timestamp"]).replace(tzinfo=IST)
                db.add(OHLC(token=settings.NIFTY_TOKEN, symbol="NIFTY", exchange="NSE",
                            interval="ONE_MINUTE", open=c["open"], high=c["high"], low=c["low"],
                            close=c["close"], volume=0,
                            timestamp=ts.astimezone(timezone.utc).replace(tzinfo=None)))
            await db.commit()
    run(_store())
    at = TODAY.replace(hour=12, minute=40)
    cache(yday + _session(TODAY, n=205, base=22050.0, hi_at=-1, lo_at=-1), at)
    run(runner._pre_market_setup())                         # the startup call
    assert strat.pivots() == {"S1": 21900.0, "PP": 22000.0, "R1": 22100.0}
    assert strat._prev.source == ps.MINUTE_1M and strat._prev.day == YESTERDAY.date()


def test_a_mid_session_start_after_a_partial_session_uses_the_broker(
        pivot_env, fake_redis, monkeypatch):
    runner, strat, cache, _, _ = pivot_env

    async def _broker(token, day):
        return [_daily(YESTERDAY.date(), 22400.0, 21700.0, 22010.0)]
    monkeypatch.setattr(fake_redis, "get_daily_candles", _broker, raising=False)
    partial = _session(YESTERDAY.replace(hour=12, minute=38), n=172)
    cache(partial + _session(TODAY, n=205), TODAY.replace(hour=12, minute=40))
    run(runner._pre_market_setup())
    assert strat._prev.source == ps.BROKER_DAILY and strat._prev.high == 22400.0
    assert strat.prev_session_problem() is None


def test_missing_levels_disable_the_strategy_with_one_reason_and_one_log(
        pivot_env, sqlite_db, caplog):
    runner, strat, cache, seen, broker_calls = pivot_env
    caplog.set_level(logging.WARNING, logger="app.services.trading.strategy_runner")
    partial = _session(YESTERDAY.replace(hour=12, minute=38), n=172)
    for minutes in (25, 26, 27, 200):
        at = TODAY + timedelta(minutes=minutes)
        cache(partial + _session(TODAY, n=minutes), at)
        run(runner.run_cycle("NIFTY"))
    assert seen == [] and strat.pivots() is None             # never evaluated
    (row,) = _gate_rows(sqlite_db)                           # one row for the day
    assert row.strategy_name == "PIVOT_BOUNCE" and "incomplete" in row.reason
    disabled = [r for r in caplog.records if "disabled today" in r.getMessage()]
    assert len(disabled) == 1                                # logged once, not every minute
    assert broker_calls == [TODAY.date()]


def test_the_next_day_resolves_again(pivot_env):
    runner, strat, cache, _, broker_calls = pivot_env
    cache(_session(YESTERDAY), TODAY.replace(hour=9, minute=0))
    run(runner._pre_market_setup())
    assert strat._prev.day == YESTERDAY.date()
    wed = TODAY + timedelta(days=1)
    cache(_session(TODAY, base=22500.0, hi=22600.0, lo=22400.0), wed.replace(hour=9, minute=0))
    assert strat.prev_session_problem() is not None          # yesterday's levels are not today's
    run(runner._pre_market_setup())
    assert strat._prev.day == TODAY.date() and strat._prev.high == 22600.0


def test_only_the_live_port_can_reach_the_broker():
    from app.runtime.live import RedisMarketData
    from app.services.replay.ports_impl import ReplayMarketData

    assert hasattr(RedisMarketData, "get_daily_candles")
    assert not hasattr(ReplayMarketData, "get_daily_candles")


def test_without_a_session_the_broker_is_not_called(monkeypatch):
    from app.services.broker import angel_client as ac

    async def _boom(*a, **k):
        raise AssertionError("broker called without a session")
    monkeypatch.setattr(ac.angel_client, "get_candle_data", _boom)
    monkeypatch.setattr(ac._session, "jwt_token", None)
    assert run(ps.broker_daily_rows("26000", TODAY.date())) is None


def test_a_port_without_the_broker_method_resolves_from_candles(
        pivot_env, fake_redis, monkeypatch):
    runner, strat, cache, _, broker_calls = pivot_env
    monkeypatch.delattr(fake_redis, "get_daily_candles")      # like ReplayMarketData
    cache(_session(YESTERDAY), TODAY.replace(hour=9, minute=0))
    run(runner._pre_market_setup())
    assert broker_calls == [] and strat._prev.source == ps.MINUTE_1M
