"""
Regression tests for the signal-quality fixes:

  1. VWAP no longer collapses to the current candle when volume is 0
     (session TWAP fallback) and is weighted by index-FUTURES volume when present.
  2. Volume-surge checks fail closed without volume.
  3. Duplicate minutes in the candle cache are removed (last write wins).
  4. Breakout strategies fire on a FRESH cross, once per side per day.
  5. The ATR floor is a fraction of price, shared by runner and strategies.
  6. A strategy's post-signal cooldown starts only when a position opened.
  7. CandleBuilder stores per-candle volume, not the day's cumulative volume.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tests.helpers import run

IST = timezone(timedelta(hours=5, minutes=30))


# ── 1. VWAP ──────────────────────────────────────────────────────────────────

def test_vwap_without_volume_is_session_twap_not_current_candle():
    from app.strategies.indicators import vwap
    h = np.array([101.0, 111.0, 121.0])
    l = np.array([99.0, 109.0, 119.0])
    c = np.array([100.0, 110.0, 120.0])
    out = vwap(h, l, c, np.zeros(3))
    assert out[-1] == pytest.approx(110.0)        # mean of typical 100, 110, 120
    assert out[-1] != pytest.approx(120.0)        # NOT the current candle's typical


def test_vwap_is_weighted_by_volume():
    from app.strategies.indicators import vwap
    h = np.array([101.0, 111.0]); l = np.array([99.0, 109.0]); c = np.array([100.0, 110.0])
    out = vwap(h, l, c, np.array([300.0, 100.0]))
    assert out[-1] == pytest.approx((100 * 300 + 110 * 100) / 400)


def test_merge_futures_volume_keeps_spot_prices_and_matches_minutes():
    from app.services.trading.strategy_runner import merge_futures_volume
    spot = [{"timestamp": "2026-10-19T09:30:00+05:30", "close": 23100.0, "volume": 0},
            {"timestamp": "2026-10-19T09:31:00+05:30", "close": 23110.0, "volume": 0}]
    k = datetime(2026, 10, 19, 9, 30, tzinfo=IST)
    merged, matched = merge_futures_volume(spot, {k: 5200.0})
    assert matched == 1
    assert merged[0]["volume"] == 5200.0 and merged[0]["close"] == 23100.0
    assert merged[1]["volume"] == 0.0
    assert spot[0]["volume"] == 0                 # originals untouched


# ── 2. volume surge fails closed ─────────────────────────────────────────────

def test_vol_surge_needs_real_volume():
    from app.strategies.strategies import _vol_surge
    assert _vol_surge(0, 0) is False
    assert _vol_surge(100, None) is False
    assert _vol_surge(200, 100, 1.5) is True
    assert _vol_surge(140, 100, 1.5) is False


# ── 3. duplicate minutes ─────────────────────────────────────────────────────

def test_dedupe_session_candles_keeps_last_write_per_minute():
    from app.services.trading.strategy_runner import dedupe_session_candles
    t0 = datetime(2026, 10, 19, 9, 39, tzinfo=IST)
    rows = [(t0, {"close": 1}), (t0 - timedelta(minutes=1), {"close": 0}),
            (t0, {"close": 2})]
    out = dedupe_session_candles(rows)
    assert [c["close"] for c in out] == [0, 2]


# ── 4. fresh-cross breakouts, once per day ───────────────────────────────────

def _candles(closes, start=datetime(2026, 9, 1, 9, 15)):
    return [{"timestamp": (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:00+05:30"),
             "open": c, "high": c + 2, "low": c - 2, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]


def _ind(close, **kw):
    base = {"atr": 50.0, "close": close, "vwap": close - 20, "avg_volume": 100.0,
            "orb_high": 22650.0, "orb_low": 22550.0}
    base.update(kw)
    return base


def test_orb_fires_on_the_fresh_cross_only_once(runtime_ctx):
    from app.strategies.strategies import ORBVWAPStrategy
    s = ORBVWAPStrategy("NIFTY")
    closes = [22600.0] * 20 + [22640.0, 22660.0]            # 09:15 .. 09:36, cross on last
    sig = run(s.evaluate(_candles(closes), _ind(22660.0)))
    assert sig is not None and sig.direction.value == "BUY"
    # still above the range next candle: no second entry on the same breakout
    closes2 = closes + [22670.0]
    assert run(s.evaluate(_candles(closes2), _ind(22670.0))) is None
    # pull back inside and cross again later the same day: still once per day
    closes3 = closes2 + [22640.0, 22665.0]
    assert run(s.evaluate(_candles(closes3), _ind(22665.0))) is None


def test_orb_does_not_chase_a_move_already_beyond_the_range(runtime_ctx):
    from app.strategies.strategies import ORBVWAPStrategy
    s = ORBVWAPStrategy("NIFTY")
    closes = [22600.0] * 20 + [22680.0, 22690.0]            # previous close already above
    assert run(s.evaluate(_candles(closes), _ind(22690.0))) is None


def test_orb_requires_volume(runtime_ctx):
    from app.strategies.strategies import ORBVWAPStrategy
    s = ORBVWAPStrategy("NIFTY")
    closes = [22600.0] * 20 + [22640.0, 22660.0]
    c = _candles(closes)
    for x in c:
        x["volume"] = 0
    assert run(s.evaluate(c, _ind(22660.0, avg_volume=None))) is None


def test_pdh_two_candle_fresh_break_once(runtime_ctx):
    from app.strategies.strategies import PDHPDLStrategy
    s = PDHPDLStrategy("NIFTY")
    s.set_levels(22700.0, 22400.0)
    closes = [22650.0] * 18 + [22690.0, 22710.0, 22720.0]   # c3<=pdh<c2, c1>pdh
    sig = run(s.evaluate(_candles(closes), _ind(22720.0)))
    assert sig is not None and sig.direction.value == "BUY"
    closes2 = closes + [22730.0]
    assert run(s.evaluate(_candles(closes2), _ind(22730.0))) is None


# ── 5. ATR floor ─────────────────────────────────────────────────────────────

def test_atr_floor_scales_with_price():
    from app.strategies.strategies import atr_floor, _atr_ok
    assert atr_floor(23100.0) == pytest.approx(5.775)
    assert atr_floor(52000.0) == pytest.approx(13.0)
    assert atr_floor(None) == 0.0
    assert _atr_ok({"atr": 6.0, "close": 23100.0}, "NIFTY") is True
    assert _atr_ok({"atr": 1.6, "close": 23100.0}, "NIFTY") is False   # old floor was 1.5


# ── 6. cooldown only after an opened position ────────────────────────────────

class _Always:
    pass


def test_cooldown_starts_only_when_a_position_opened(runtime_ctx, monkeypatch):
    from app.strategies import base
    from app.strategies.base import BaseStrategy, Direction

    class S(BaseStrategy):
        name = "VWAP_PULLBACK"
        async def evaluate(self, candles, indicators):
            return self._make_signal(Direction.BUY, 100.0, indicators)

    opened = {"v": False}

    async def _publish(signal):
        return opened["v"]

    monkeypatch.setattr(base.SignalBus, "publish", staticmethod(_publish))
    s = S("NIFTY")
    run(s.on_candle([], {}))
    assert s._last_signal_time is None and s._cooldown_ok()     # refused: no cooldown
    opened["v"] = True
    run(s.on_candle([], {}))
    assert s._last_signal_time is not None and not s._cooldown_ok()


# ── 7. per-candle volume from cumulative ticks ───────────────────────────────

def test_candle_builder_volume_is_per_candle():
    from app.services.market_data.feed import TickData
    from app.services.market_data.tick_writer import CandleBuilder

    b = CandleBuilder()
    t0 = datetime(2026, 10, 19, 4, 0, 5, tzinfo=timezone.utc)

    def tick(sec, cum):
        return TickData(token="FUT1", exchange="NFO", ltp=23100.0, volume=cum,
                        received_at=t0 + timedelta(seconds=sec))

    assert b.update(tick(0, 10_000)) is None
    assert b.update(tick(20, 10_600)) is None
    done = b.update(tick(65, 11_000))          # next minute: first candle completes
    assert done["volume"] == 600               # 10,600 − 10,000, not 10,600
    assert "_vol_base" not in done
    done2 = b.update(tick(125, 11_900))
    assert done2["volume"] == 400              # 11,000 − 10,600 (counts from prior candle's end)


# ── 8. option momentum check uses the last 1-min candle, not the DAY open ────

def _momentum_run(runtime_ctx, sqlite_db, fake_redis, monkeypatch, candles):
    from sqlalchemy import select
    from datetime import time as dtime
    from app.db.models.paper_trade import PaperPosition, StrategySignal
    from app.runtime.context import using
    from app.services.trading.paper_engine import paper_engine
    from app.strategies.base import Direction
    from tests.helpers import (allow_regime, fake_candidate, make_signal, pin_ist_time,
                               seed_config, stub_select)

    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch, regime="RANGE_NORMAL_VOL")
    # tick "open" = the DAY's open (AngelOne SnapQuote): far above the current LTP
    cand = fake_candidate(ltp=100.0, tick_open=150.0, token="OPTM")
    stub_select(monkeypatch, cand)
    if candles is not None:
        fake_redis.kv["ohlc:OPTM:ONE_MINUTE"] = candles
    ok = run(paper_engine.handle_signal(make_signal(strategy="RSI_VWAP",
                                                    direction=Direction.BUY)))

    async def _rows():
        async with sqlite_db() as db:
            pos = (await db.execute(select(PaperPosition))).scalars().all()
            sig = (await db.execute(select(StrategySignal))).scalars().all()
            return pos, sig
    return ok, run(_rows())


def test_a_drop_since_the_day_open_no_longer_blocks_entry(runtime_ctx, sqlite_db, fake_redis,
                                                          monkeypatch):
    rising = [{"open": 97.0, "high": 99.0, "low": 96.5, "close": 98.5}]   # last minute: rising
    ok, (pos, _) = _momentum_run(runtime_ctx, sqlite_db, fake_redis, monkeypatch, rising)
    assert ok is True and len(pos) == 1


def test_a_drop_in_the_last_minute_still_blocks_entry(runtime_ctx, sqlite_db, fake_redis,
                                                      monkeypatch):
    falling = [{"open": 106.0, "high": 106.5, "low": 101.0, "close": 101.5}]   # −5.7% now
    ok, (pos, sig) = _momentum_run(runtime_ctx, sqlite_db, fake_redis, monkeypatch, falling)
    assert ok is False and pos == []
    assert "last minute" in (sig[-1].skip_reason or "")


def test_gap_fill_needs_the_0915_open(runtime_ctx):
    """Gap measured from the 09:15 open; after a late start the first cached
    candle is a later minute, so the gap is unknown and nothing fires."""
    from app.strategies.strategies import GapVWAPFillStrategy
    closes = [22500.0] * 10 + [22540.0]                     # gap down vs 22700, then recovery
    ind = _ind(22540.0, vwap=22510.0)

    s = GapVWAPFillStrategy("NIFTY"); s.set_prev_close(22700.0)
    c = _candles(closes)
    c[-1]["open"] = 22500.0                                 # bullish last candle
    assert run(s.evaluate(c, ind)) is not None              # 09:15 open present

    s2 = GapVWAPFillStrategy("NIFTY"); s2.set_prev_close(22700.0)
    late = _candles(closes, start=datetime(2026, 9, 1, 9, 27))
    late[-1]["open"] = 22500.0
    assert run(s2.evaluate(late, ind)) is None              # 09:15 candle missing
