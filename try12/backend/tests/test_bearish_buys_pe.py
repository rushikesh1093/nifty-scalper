"""The long-premium invariant (design R1, blockers-plan item 4): a bearish (SELL)
signal BUYS a PE, a bullish (BUY) signal BUYS a CE, and the engine never sells
or shorts an option. Driven end to end through paper_engine.handle_signal over
a universe holding CE and PE at the same strikes."""
from datetime import time as dtime, timedelta

from sqlalchemy import select

from app.core.trading_params import TradingParams
from app.db.models.paper_trade import PaperPosition, SignalDirection
from app.runtime.context import using
from app.services.trading.option_selector import option_selector
from app.services.trading.paper_engine import paper_engine
from app.strategies.base import Direction
from tests.helpers import (
    allow_regime, make_signal, option_row, pin_ist_time, run, seed_config, with_instruments,
)

# RANGE_NORMAL_VOL allows BUY and SELL, and enables VWAP_REVERSAL and RSI_VWAP
BOTH_WAYS_REGIME = "RANGE_NORMAL_VOL"
STRIKES = (22550, 22600, 22650)
SPOT = 22600.0
KW = dict(sl_pct=0.15, target_pct=0.30, params=TradingParams(), base_amount=20_000.0,
          regime_multiplier=1.0)


def _universe(rc):
    expiry = rc.clock.trading_day() + timedelta(days=7)
    return [option_row(k, expiry, kind=kind) for k in STRIKES for kind in ("CE", "PE")]


def _ticks(redis, rows):
    for r in rows:
        redis.put_tick(r.token, 100.0, bid=99.5, ask=100.5, oi=300_000, volume=20_000)


def _positions(Session):
    async def _go():
        async with Session() as db:
            return (await db.execute(select(PaperPosition))).scalars().all()
    return run(_go())


def _open(runtime_ctx, sqlite_db, fake_redis, monkeypatch, direction, strategy):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch, regime=BOTH_WAYS_REGIME)
    rows = _universe(runtime_ctx)
    _ticks(fake_redis, rows)
    with using(with_instruments(runtime_ctx, rows)):
        ok = run(paper_engine.handle_signal(make_signal(strategy=strategy, direction=direction)))
    return ok, _positions(sqlite_db)


def test_a_bearish_signal_buys_a_pe_and_never_sells_or_shorts_a_ce(
        runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    ok, positions = _open(runtime_ctx, sqlite_db, fake_redis, monkeypatch,
                          Direction.SELL, "ORB_BREAKOUT")
    assert ok is True and len(positions) == 1
    pos = positions[0]
    assert pos.direction == SignalDirection.BUY            # bought, never sold
    assert pos.option_type == "PE" and pos.symbol.endswith("PE")
    assert not [p for p in positions if p.direction == SignalDirection.SELL]
    assert not [p for p in positions if p.option_type == "CE"]


def test_a_bullish_signal_buys_a_ce(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    ok, positions = _open(runtime_ctx, sqlite_db, fake_redis, monkeypatch,
                          Direction.BUY, "ORB_BREAKOUT")
    assert ok is True and len(positions) == 1
    pos = positions[0]
    assert pos.direction == SignalDirection.BUY and pos.option_type == "CE"
    assert pos.symbol.endswith("CE")


def test_the_selector_maps_sell_to_pe_and_buy_to_ce(runtime_ctx, fake_redis):
    rows = _universe(runtime_ctx)
    _ticks(fake_redis, rows)
    with using(with_instruments(runtime_ctx, rows)):
        sell = run(option_selector.select(None, Direction.SELL, "NIFTY", SPOT, **KW))
        sell_top = run(option_selector.select_top_n(None, Direction.SELL, "NIFTY", SPOT,
                                                    n=10, **KW))
        buy = run(option_selector.select(None, Direction.BUY, "NIFTY", SPOT, **KW))
        buy_top = run(option_selector.select_top_n(None, Direction.BUY, "NIFTY", SPOT,
                                                   n=10, **KW))
    assert sell.candidate is not None and sell.candidate.option_type == "PE"
    assert sell_top.ranked and {c.option_type for c in sell_top.ranked} == {"PE"}
    assert buy.candidate is not None and buy.candidate.option_type == "CE"
    assert buy_top.ranked and {c.option_type for c in buy_top.ranked} == {"CE"}
