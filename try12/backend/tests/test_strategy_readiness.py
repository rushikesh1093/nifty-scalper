"""
Per-strategy readiness (24 Oct): the global 20-candle wait kept every strategy
idle until 09:35, so the 09:15 breakout never saw its 09:25 and 09:30 closes.
Each strategy now declares the today-only candles it needs; the cycle starts at
the smallest among the enabled ones, and the ATR floor reads the multi-day
history until today has 14 candles.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from tests.helpers import IST, run, seed_config
from tests.test_no_trade import _NoInstruments, _Stub

START = datetime(2026, 9, 1, 9, 15)          # naive IST, like the feed
YESTERDAY = datetime(2026, 8, 31, 9, 15)


class _Ready(_Stub):
    def __init__(self, name, need=None):
        super().__init__(name)
        self.MIN_TODAY_CANDLES = need


def _bars(start, n, base=22600.0, step=0.0):
    out = []
    for k in range(n):
        px = base + k * step + (k % 7)
        ts = start + timedelta(minutes=k)
        out.append({"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": px, "high": px + 3,
                    "low": px - 3, "close": px + 1, "volume": 0})
    return out


@pytest.fixture
def runner_env(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    """A runner whose stubs we choose, every gate open except the real ATR floor."""
    from dataclasses import replace

    from app.core.config import settings
    from app.runtime.context import using
    from app.services.trading import strategy_runner as sr
    from app.strategies.names import ALL_STRATEGY_NAMES

    run(seed_config(sqlite_db))
    runner = sr.StrategyRunner()

    async def _merge(underlying, candles, indicators, is_expiry_day, flags, history=None):
        return {n: flags.get(n, True) for n in ALL_STRATEGY_NAMES}, None
    monkeypatch.setattr(runner, "_detect_and_merge_regime", _merge)
    rc = replace(runtime_ctx, instruments=_NoInstruments())

    def feed(today_n, yesterday_n=375):
        """yesterday's session plus today's first *today_n* bars; clock at the last close."""
        fake_redis.kv[f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"] = \
            _bars(YESTERDAY, yesterday_n) + _bars(START, today_n)
        runtime_ctx.clock.set((START + timedelta(minutes=today_n)).replace(tzinfo=IST))

    with using(rc):
        yield runner, feed, sr


def _cycle_rows(Session, code):
    from app.db.models.risk import NoTradeDecision

    async def _go():
        async with Session() as db:
            return (await db.execute(select(NoTradeDecision).where(
                NoTradeDecision.code == code))).scalars().all()
    return run(_go())


def test_an_early_strategy_runs_at_its_readiness_and_the_rest_wait(runner_env):
    runner, feed, _ = runner_env
    early, late = _Ready("FIRST_CANDLE_BREAKOUT", 10), _Ready("ORB_BREAKOUT")
    runner._strategies["NIFTY"] = [early, late]
    for n in (9, 10, 15, 19, 20):
        feed(n)
        run(runner.run_cycle("NIFTY"))
    assert early.calls == 4                    # at 10, 15, 19, 20 candles (09:25 .. 09:35)
    assert late.calls == 1                     # only at 20 (09:35), as before


def test_before_any_strategy_is_ready_the_cycle_gate_records_the_smallest_need(
        runner_env, sqlite_db):
    runner, feed, _ = runner_env
    runner._strategies["NIFTY"] = [_Ready("FIRST_CANDLE_BREAKOUT", 10), _Ready("ORB_BREAKOUT")]
    feed(9)
    run(runner.run_cycle("NIFTY"))
    (row,) = _cycle_rows(sqlite_db, "INSUFFICIENT_CANDLES")
    assert row.context["required"] == 10 and row.context["candles"] == 9


def test_a_user_disabled_early_strategy_does_not_start_the_cycle_early(
        runner_env, sqlite_db, monkeypatch):
    from dataclasses import replace

    from app.runtime.context import ctx, using
    from app.runtime.live import FrozenUserFlags

    runner, feed, _ = runner_env
    early, late = _Ready("FIRST_CANDLE_BREAKOUT", 10), _Ready("ORB_BREAKOUT")
    runner._strategies["NIFTY"] = [early, late]
    with using(replace(ctx(), user_flags=FrozenUserFlags({"FIRST_CANDLE_BREAKOUT": False}))):
        feed(12)
        run(runner.run_cycle("NIFTY"))
    (row,) = _cycle_rows(sqlite_db, "INSUFFICIENT_CANDLES")
    assert row.context["required"] == 20 and early.calls == late.calls == 0


def test_the_atr_floor_reads_the_history_before_14_candles(runner_env, sqlite_db):
    """At 09:25 today's ATR(14) is undefined; with yesterday's tail the floor
    (1.5) passes, and without a history the cycle is still refused."""
    runner, feed, _ = runner_env
    early = _Ready("FIRST_CANDLE_BREAKOUT", 10)
    runner._strategies["NIFTY"] = [early]
    feed(10)
    run(runner.run_cycle("NIFTY"))
    assert early.calls == 1 and _cycle_rows(sqlite_db, "ATR_BELOW_FLOOR") == []
    feed(11, yesterday_n=0)                    # no history: today's ATR is still None
    run(runner.run_cycle("NIFTY"))
    assert early.calls == 1 and len(_cycle_rows(sqlite_db, "ATR_BELOW_FLOOR")) == 1


def test_the_declared_readiness_of_the_real_strategies():
    from app.services.trading.strategy_runner import MIN_TODAY_CANDLES, _readiness
    from app.strategies.strategies import build_strategies

    need = {s.name: _readiness(s) for s in build_strategies("NIFTY")}
    assert MIN_TODAY_CANDLES == 20
    assert need["FIRST_CANDLE_BREAKOUT"] == 10
    assert {n: v for n, v in need.items() if n != "FIRST_CANDLE_BREAKOUT"} == \
        {n: 20 for n in need if n != "FIRST_CANDLE_BREAKOUT"}     # everything else unchanged


def test_the_real_breakout_fires_on_the_0925_close_through_the_runner(runner_env, monkeypatch):
    from app.strategies.base import SignalBus
    from app.strategies.strategies import FirstCandleBreakoutStrategy

    runner, _, _ = runner_env
    seen = []

    async def _capture(signal):
        seen.append(signal)
        return False
    monkeypatch.setattr(SignalBus, "_handlers", [_capture])
    from app.core.config import settings
    from app.runtime.context import ctx, market

    runner._strategies["NIFTY"] = [FirstCandleBreakoutStrategy("NIFTY")]
    bars = _bars(YESTERDAY, 375) + _bars(START, 5)                     # 09:15 bar: ~22597-22609
    bars += _bars(START + timedelta(minutes=5), 5, base=22640.0)       # 09:20 bar closes ~22645
    market().kv[f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"] = bars
    ctx().clock.set((START + timedelta(minutes=10)).replace(tzinfo=IST))   # 09:25, 10 candles
    run(runner.run_cycle("NIFTY"))
    assert [s.strategy_name for s in seen] == ["FIRST_CANDLE_BREAKOUT"]
    assert seen[0].direction.value == "BUY"
