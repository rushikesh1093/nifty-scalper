"""
TREND_ENGINE acceptance tests (the "Combined Trend-Following Options Strategy"
spec, section 9). Synthetic 1-minute markets drive the real engine, the real
paper engine and the real strategy runner.
"""
from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from tests.helpers import run

IST = timezone(timedelta(hours=5, minutes=30))
START = datetime(2026, 10, 20, 9, 15, tzinfo=IST)


# ── synthetic markets ────────────────────────────────────────────────────────

def _bar(i, o, c, wick=2.0, vol=1000.0):
    return {"timestamp": (START + timedelta(minutes=i)).isoformat(), "open": round(o, 2),
            "high": round(max(o, c) + wick, 2), "low": round(min(o, c) - wick, 2),
            "close": round(c, 2), "volume": vol}


def uptrend_pullback(confirm=True, base=23000.0):
    """Zig-zag uptrend (higher highs / higher lows), a pullback toward EMA21/VWAP,
    then either a bullish rejection candle (confirm) or a weak candle."""
    out, px = [], base
    for i in range(40):
        step = 4.0 if (i % 8) < 6 else -5.0
        out.append(_bar(i, px, px + step, wick=2.5)); px += step
    for j in range(5):
        out.append(_bar(40 + j, px, px - 3.0)); px -= 3.0
    if confirm:
        o, c = px - 1.0, px + 9.0
        out.append({"timestamp": (START + timedelta(minutes=45)).isoformat(), "open": o,
                    "high": c + 0.5, "low": o - 2.0, "close": c, "volume": 1600.0})
    else:
        out.append(_bar(45, px, px - 0.5, wick=1.0))
    return out


def mirror(candles, base=23000.0):
    f = lambda x: 2 * base - x   # noqa: E731
    return [dict(c, open=f(c["open"]), close=f(c["close"]), high=f(c["low"]), low=f(c["high"]))
            for c in candles]


def chop(n=50, base=23000.0):
    out, px = [], base
    for i in range(n):
        step = 6.0 if i % 2 == 0 else -6.0
        out.append(_bar(i, px, px + step, wick=3.0)); px += step
    return out


def v_bounce(base=23000.0):
    """Slide, then a 4-bar bounce back above VWAP while EMA9 is still below EMA21."""
    out, px = [], base + 100
    for i in range(40):
        step = -2.0 if i % 6 < 5 else 1.0
        out.append(_bar(i, px, px + step)); px += step
    for j in range(4):
        out.append(_bar(40 + j, px, px + 9.0, wick=1.5)); px += 9.0
    return out


def _ind(candles):
    from app.strategies.indicators import compute_all
    ind = compute_all(candles)
    prior = [c["volume"] for c in candles[-11:-1]]
    ind["avg_volume"] = sum(prior) / len(prior)
    return ind


@pytest.fixture
def at_close(runtime_ctx):
    """Move the ctx clock to just after the last candle of *candles* closed."""
    def _set(candles, late_s=5):
        last = datetime.fromisoformat(candles[-1]["timestamp"])
        runtime_ctx.clock.set(last + timedelta(minutes=1, seconds=late_s))
    return _set


def _decide(candles, und="NIFTY", engine=None):
    from app.strategies.trend_engine import TrendFollowingEngine
    eng = engine or TrendFollowingEngine(und)
    return eng.decide(candles, _ind(candles))


# ── classification and entries ───────────────────────────────────────────────

def test_bullish_trend_buys_ce_only_after_entry_confirmation(at_close):
    weak = uptrend_pullback(confirm=False)
    at_close(weak)
    d = _decide(weak)
    assert d.trend == "UPTREND" and d.decision == "NO TRADE"
    assert d.reason_code == "NO_ENTRY_CONFIRMATION"

    good = uptrend_pullback(confirm=True)
    at_close(good)
    d = _decide(good)
    assert d.trend == "UPTREND" and d.decision == "BUY CE"
    assert "pullback rejection" in d.entry_trigger
    assert d.stop_level < d.spot < d.target1_level             # index levels, CE side
    assert d.risk_reward >= 2.0 - 1e-9


def test_bearish_trend_buys_pe_only_after_entry_confirmation(at_close):
    weak = mirror(uptrend_pullback(confirm=False))
    at_close(weak)
    assert _decide(weak).reason_code == "NO_ENTRY_CONFIRMATION"
    good = mirror(uptrend_pullback(confirm=True))
    at_close(good)
    d = _decide(good)
    assert d.trend == "DOWNTREND" and d.decision == "BUY PE"
    assert d.target1_level < d.spot < d.stop_level             # PE side


def test_sideways_market_is_no_trade(at_close):
    c = chop()
    at_close(c)
    d = _decide(c)
    assert (d.trend, d.decision, d.reason_code) == ("SIDEWAYS", "NO TRADE", "SIDEWAYS_MARKET")


def test_conflicting_indicators_are_no_trade(at_close):
    c = v_bounce()
    at_close(c)
    d = _decide(c)
    assert d.decision == "NO TRADE" and d.reason_code == "CONFLICTING_SIGNALS"


def test_missing_stale_or_malformed_data_is_no_trade(at_close):
    good = uptrend_pullback()
    at_close(good, late_s=10 * 60)                               # feed stopped 10 min ago
    assert _decide(good).reason_code == "STALE_DATA"

    at_close(good)
    assert _decide(good[:20]).reason_code == "INSUFFICIENT_DATA"

    bad = [dict(c) for c in good]
    bad[-3]["high"] = bad[-3]["low"] - 5                         # inverted candle
    assert _decide(bad).reason_code == "MALFORMED_DATA"

    from app.strategies.trend_engine import TrendFollowingEngine
    ind = _ind(good)
    ind["atr"] = None
    assert TrendFollowingEngine("NIFTY").decide(good, ind).reason_code == "MALFORMED_DATA"


def test_both_underlyings_are_independent_and_scaled(at_close):
    from app.strategies.trend_engine import TrendFollowingEngine, decision_log
    decision_log.reset()
    nifty = uptrend_pullback()
    bank = [dict(c, open=c["open"] * 2.4, high=c["high"] * 2.4, low=c["low"] * 2.4,
                 close=c["close"] * 2.4) for c in mirror(uptrend_pullback())]
    at_close(nifty)
    n_eng, b_eng = TrendFollowingEngine("NIFTY"), TrendFollowingEngine("BANKNIFTY")
    run(n_eng.evaluate(nifty, _ind(nifty)))
    run(b_eng.evaluate(bank, _ind(bank)))
    n, b = decision_log.latest("NIFTY"), decision_log.latest("BANKNIFTY")
    assert n.decision == "BUY CE" and b.decision == "BUY PE"
    assert n.underlying == "NIFTY" and b.underlying == "BANKNIFTY"
    assert b.target1_points >= 50.0 > n.target1_points >= 20.0      # per-underlying targets


def test_levels_are_labelled_index_points(at_close):
    c = uptrend_pullback()
    at_close(c)
    d = _decide(c)
    assert d.units == "INDEX_POINTS"
    text = d.as_text()
    assert "Stop-Loss (index)" in text and "index pts" in text
    assert "guarantee" not in text.lower()


# ── duplicates and cooldown ──────────────────────────────────────────────────

def test_same_candle_twice_does_not_duplicate(at_close):
    from app.strategies.trend_engine import TrendFollowingEngine, decision_log
    decision_log.reset()
    c = uptrend_pullback()
    at_close(c)
    eng = TrendFollowingEngine("NIFTY")
    first = run(eng.evaluate(c, _ind(c)))
    second = run(eng.evaluate(c, _ind(c)))
    assert first is not None and second is None
    assert len(decision_log.recent("NIFTY")) == 1


def test_cooldown_after_an_opened_position_and_aggregator_still_apply(at_close, monkeypatch):
    from app.strategies import base
    from app.strategies.base import signal_aggregator
    from app.strategies.trend_engine import TrendFollowingEngine

    published = []

    async def _publish(sig):
        published.append(sig)
        return True                                   # a position opened

    monkeypatch.setattr(base.SignalBus, "publish", staticmethod(_publish))
    c = uptrend_pullback()
    at_close(c)
    eng = TrendFollowingEngine("NIFTY")
    run(eng.on_candle(c, _ind(c)))
    assert len(published) == 1 and not eng._cooldown_ok()        # cooldown armed
    c2 = c + [dict(c[-1], timestamp=(START + timedelta(minutes=46)).isoformat())]
    at_close(c2)
    run(eng.on_candle(c2, _ind(c2)))
    assert len(published) == 1                                   # suppressed by the cooldown

    # the shared aggregator still refuses the same direction moments later
    signal_aggregator.reset()
    sig = published[0]
    signal_aggregator.record_acted(sig)
    ok, code, _ = signal_aggregator.should_act(sig)
    assert ok is False and code.value == "AGGREGATOR_SAME_DIRECTION"


# ── runner mode switch ───────────────────────────────────────────────────────

class _Stub:
    sl_pct = 0.15

    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def on_candle(self, candles, indicators):
        self.calls += 1


def _runner_cycle(runtime_ctx, sqlite_db, fake_redis, monkeypatch, mode):
    from dataclasses import replace
    from app.core.config import settings
    from app.runtime.context import using
    from app.services.trading import strategy_runner as sr
    from app.strategies.names import ALL_STRATEGY_NAMES
    from tests.helpers import seed_config, with_params

    class _NoInstruments:
        async def instruments_for(self, db, underlying):
            return []

        def invalidate(self):
            pass

    run(seed_config(sqlite_db))
    monkeypatch.setattr(sr, "MIN_TODAY_CANDLES", 1)
    monkeypatch.setattr(sr, "atr_floor", lambda price: 0.0)
    runner = sr.StrategyRunner()
    stubs = {n: _Stub(n) for n in ALL_STRATEGY_NAMES}
    runner._strategies["NIFTY"] = list(stubs.values())

    async def _merge(underlying, candles, indicators, is_expiry_day, flags):
        return {n: True for n in ALL_STRATEGY_NAMES}, None

    monkeypatch.setattr(runner, "_detect_and_merge_regime", _merge)
    start = datetime(2026, 9, 1, 9, 15)
    fake_redis.kv[f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"] = [
        {"timestamp": (start + timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M:%S"),
         "open": 22600.0, "high": 22603.0, "low": 22597.0, "close": 22601.0, "volume": 0}
        for k in range(6)]
    runtime_ctx.clock.set((start + timedelta(minutes=5)).replace(tzinfo=IST))
    rc = with_params(replace(runtime_ctx, instruments=_NoInstruments()), decision_engine=mode)
    with using(rc):
        run(runner.run_cycle("NIFTY"))
    return stubs


def test_unified_mode_runs_only_the_trend_engine(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    stubs = _runner_cycle(runtime_ctx, sqlite_db, fake_redis, monkeypatch, "UNIFIED")
    assert stubs["TREND_ENGINE"].calls == 1
    assert all(s.calls == 0 for n, s in stubs.items() if n != "TREND_ENGINE")


def test_legacy_mode_runs_only_the_twelve(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    stubs = _runner_cycle(runtime_ctx, sqlite_db, fake_redis, monkeypatch, "LEGACY")
    assert stubs["TREND_ENGINE"].calls == 0
    assert sum(s.calls for s in stubs.values()) == 12


# ── paper engine: premium conversion, risk sizing, opposing positions ────────

def _engine_signal(direction="BUY", stop_pts=20.0, target_pts=40.0):
    from app.strategies.base import Direction
    from tests.helpers import make_signal
    sig = make_signal(strategy="TREND_ENGINE",
                      direction=Direction.BUY if direction == "BUY" else Direction.SELL)
    sig.indicators.update({
        "engine_units": "INDEX_POINTS", "engine_stop_pts": stop_pts,
        "engine_target_pts": target_pts,
        "engine_trail": {"activation_reward": 0.6, "swing_strength": 2, "buffer_pts": 2.0},
    })
    return sig


def _open(runtime_ctx, sqlite_db, monkeypatch, sig, regime="BULL_TREND_NORMAL_VOL", **param):
    from app.runtime.context import using
    from app.services.trading.paper_engine import paper_engine
    from tests.helpers import (allow_regime, fake_candidate, pin_ist_time, seed_config,
                               stub_select, with_params)
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch, regime=regime)
    stub_select(monkeypatch, fake_candidate(ltp=100.0, token="OPTE"))   # delta_est 0.5
    with using(with_params(runtime_ctx, **param)):
        return run(paper_engine.handle_signal(sig))


def _positions(Session):
    from sqlalchemy import select
    from app.db.models.paper_trade import PaperPosition

    async def _go():
        async with Session() as db:
            return (await db.execute(select(PaperPosition))).scalars().all()
    return run(_go())


def test_index_stop_is_converted_to_premium_with_delta(runtime_ctx, sqlite_db, monkeypatch):
    ok = _open(runtime_ctx, sqlite_db, monkeypatch, _engine_signal(stop_pts=20.0, target_pts=40.0))
    assert ok is True
    [pos] = _positions(sqlite_db)
    # entry 100.2 (LTP + 0.2%): premium stop ≈ 0.5 × 20 = 10, target ≈ 0.5 × 40 = 20
    assert pos.actual_entry - pos.stop_loss == pytest.approx(10.0, abs=0.1)
    assert pos.target - pos.actual_entry == pytest.approx(20.0, abs=0.1)
    lv = pos.entry_indicators["engine_premium_levels"]
    assert lv["units"].startswith("PREMIUM_POINTS") and lv["index_stop_pts"] == 20.0
    assert pos.entry_indicators["engine_units"] == "INDEX_POINTS"
    # the trail settings survive onto the position (the position manager reads them)
    assert pos.entry_indicators["engine_trail"]["swing_strength"] == 2
    assert pos.entry_indicators["engine_delta"] == 0.5


def test_risk_per_trade_caps_lots_and_refuses_an_oversized_lot(runtime_ctx, sqlite_db, monkeypatch):
    # one lot risks ≈ 10 × 65 = ₹650; budget 0.5% of 200,000 = ₹1,000 → 1 lot (not 3)
    ok = _open(runtime_ctx, sqlite_db, monkeypatch, _engine_signal(), max_risk_per_trade_pct=0.5)
    assert ok is True
    [pos] = _positions(sqlite_db)
    assert pos.lots == 1


def test_risk_per_trade_refuses_when_one_lot_is_too_risky(runtime_ctx, sqlite_db, monkeypatch):
    from sqlalchemy import select
    from app.db.models.paper_trade import StrategySignal
    ok = _open(runtime_ctx, sqlite_db, monkeypatch, _engine_signal(), max_risk_per_trade_pct=0.2)
    assert ok is False and _positions(sqlite_db) == []

    async def _sig():
        async with sqlite_db() as db:
            return (await db.execute(select(StrategySignal))).scalars().all()
    assert "RISK_LIMIT_REACHED" in (run(_sig())[-1].skip_reason or "")


def test_no_opposing_position_on_the_same_underlying(runtime_ctx, sqlite_db, monkeypatch):
    from tests.helpers import add_position
    run(add_position(sqlite_db, strategy_name="TREND_ENGINE", option_type="CE", token="OPTCE"))
    ok = _open(runtime_ctx, sqlite_db, monkeypatch, _engine_signal("SELL"),
               regime="BEAR_TREND_NORMAL_VOL")
    assert ok is False
    assert [p.option_type for p in _positions(sqlite_db)] == ["CE"]


# ── trailing stop: thresholds, structure, one-way, persistence ───────────────

def _engine_position(sqlite_db, runtime_ctx, option_type="CE", minutes_ago=20):
    from tests.helpers import add_position
    return run(add_position(
        sqlite_db, strategy_name="TREND_ENGINE", option_type=option_type, token="OPTE",
        entry_time=runtime_ctx.clock.now_utc() - timedelta(minutes=minutes_ago),
        entry_indicators={"engine_delta": 0.5, "engine_units": "INDEX_POINTS",
                          "engine_trail": {"activation_reward": 0.6, "swing_strength": 2,
                                           "buffer_pts": 2.0}}))


def _spot_path(fake_redis, runtime_ctx, lows, highs=None, spot=None):
    from app.core.config import settings
    t0 = runtime_ctx.clock.now_utc().astimezone(IST) - timedelta(minutes=len(lows) + 1)
    rows = []
    for i, lo in enumerate(lows):
        hi = (highs[i] if highs else lo + 6.0)
        rows.append({"timestamp": (t0 + timedelta(minutes=i)).isoformat(), "open": lo + 2,
                     "high": hi, "low": lo, "close": lo + 3, "volume": 0})
    fake_redis.kv[f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"] = rows
    if spot is not None:
        fake_redis.kv[f"ltp:{settings.NIFTY_TOKEN}"] = float(spot)


def _tick(sqlite_db, fake_redis, pid, ltp):
    from app.services.trading.paper_engine import paper_engine
    from tests.helpers import get_position
    fake_redis.put_tick("OPTE", ltp, age_s=1.0)
    pos = run(get_position(sqlite_db, pid))
    run(paper_engine._check_position(pos, dtime(11, 0), dtime(15, 15)))
    return run(get_position(sqlite_db, pid))


def test_ce_trail_follows_higher_lows_and_never_widens(runtime_ctx, sqlite_db, fake_redis):
    pid = _engine_position(sqlite_db, runtime_ctx)                 # entry 100, SL 85, target 130
    # swing low 23050 far below: structure stop (120 − 0.5×52 = 94) is BELOW the
    # 60% break-even (101.5) → break-even applies, the structure stop is ignored
    _spot_path(fake_redis, runtime_ctx, [23060, 23055, 23050, 23058, 23066, 23075, 23085],
               spot=23100)
    pos = _tick(sqlite_db, fake_redis, pid, 120.0)
    assert pos.stop_loss == 101.5
    # a confirmed HIGHER low at 23090: stop = 120 − 0.5 × (23100 − 23088) = 114
    _spot_path(fake_redis, runtime_ctx, [23092, 23095, 23090, 23096, 23098], spot=23100)
    pos = _tick(sqlite_db, fake_redis, pid, 120.0)
    assert pos.stop_loss == 114.0
    # a lower swing appears later: the stop does NOT come back down
    _spot_path(fake_redis, runtime_ctx, [23080, 23070, 23060, 23075, 23085], spot=23100)
    pos = _tick(sqlite_db, fake_redis, pid, 121.0)
    assert pos.stop_loss >= 114.0


def test_pe_trail_follows_lower_highs(runtime_ctx, sqlite_db, fake_redis):
    pid = _engine_position(sqlite_db, runtime_ctx, option_type="PE")
    highs = [23100, 23105, 23110, 23104, 23102]                     # swing high 23110
    _spot_path(fake_redis, runtime_ctx, [h - 6 for h in highs], highs=highs, spot=23098)
    pos = _tick(sqlite_db, fake_redis, pid, 120.0)
    # 120 − 0.5 × ((23110 + 2) − 23098) = 113
    assert pos.stop_loss == 113.0


def test_no_trail_before_the_configured_threshold(runtime_ctx, sqlite_db, fake_redis):
    pid = _engine_position(sqlite_db, runtime_ctx)
    _spot_path(fake_redis, runtime_ctx, [23092, 23095, 23090, 23096, 23098], spot=23100)
    pos = _tick(sqlite_db, fake_redis, pid, 115.0)                  # 50% of reward < 60%
    assert pos.stop_loss == 85.0


def test_trailed_stop_persists_for_the_next_evaluation(runtime_ctx, sqlite_db, fake_redis):
    """The stop lives on the position row: a fresh read (as after a restart)
    sees it, and a later tick that implies a lower stop leaves it in place."""
    from tests.helpers import get_position
    pid = _engine_position(sqlite_db, runtime_ctx)
    _spot_path(fake_redis, runtime_ctx, [23092, 23095, 23090, 23096, 23098], spot=23100)
    _tick(sqlite_db, fake_redis, pid, 120.0)
    assert run(get_position(sqlite_db, pid)).stop_loss == 114.0
    pos = _tick(sqlite_db, fake_redis, pid, 119.0)
    assert pos.stop_loss == 114.0 and pos.is_open


def test_engine_invalidation_rule(at_close):
    from app.strategies.trend_engine import TrendFollowingEngine
    eng = TrendFollowingEngine("NIFTY")
    up = uptrend_pullback()
    assert eng.invalidation("CE", up) is None
    down = mirror(uptrend_pullback())
    assert "invalidated" in eng.invalidation("CE", down)
    assert eng.invalidation("PE", down) is None


def test_trend_decisions_endpoint_summarises_today_only(runtime_ctx):
    """GET /trading/trend-decisions: per-index counts for the ctx trading day
    and the most common NO TRADE reasons (yesterday's decisions excluded)."""
    from app.api.routes.trading import trend_decisions
    from app.strategies.trend_engine import TrendDecision, decision_log

    decision_log.reset()
    day = str(runtime_ctx.clock.trading_day())

    def d(t, decision, code=None, und="NIFTY", date=day):
        decision_log.record(TrendDecision(und, f"{date}T{t}:00+05:30", f"{date}T{t}:03",
                                          "UPTREND", "MODERATE", decision, code, "r"))
    d("09:45", "NO TRADE", "SIDEWAYS_MARKET")
    d("09:46", "NO TRADE", "SIDEWAYS_MARKET")
    d("09:47", "NO TRADE", "WEAK_TREND")
    d("09:48", "BUY CE")
    d("15:00", "NO TRADE", "SIDEWAYS_MARKET", date="2026-08-31")      # another day
    out = run(trend_decisions(underlying=None, limit=50))
    s = out["summary"]["NIFTY"]
    assert (s["evaluated"], s["buy_ce"], s["buy_pe"], s["no_trade"]) == (4, 1, 0, 3)
    assert s["top_reasons"][0] == ["SIDEWAYS_MARKET", 2]
    assert out["summary"]["BANKNIFTY"]["evaluated"] == 0
    decision_log.reset()
