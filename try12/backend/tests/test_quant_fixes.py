"""
Fixes from the real-NIFTY-path review:
  2. lots are capped by the loss at the premium stop (max_risk_per_trade_pct)
  3. Supertrend premium target +8 (was +20, rarely reachable in 10 min)
  4. the regime's volume-surge test reads the index future's volume
  5. ORB stop a quarter of the range inside it, 90-min time exit
  6. Kelly only after 100 decided trades
  7. brokerage is a flat amount per order, not per lot
"""
from datetime import timedelta

import pytest

from app.core.trading_params import TradingParams
from app.runtime.context import using
from app.services.trading import fills
from app.services.trading.risk_gate import FractionDecision, kelly_from_stats, sizing_decision
from app.strategies.strategies import ORBBreakoutStrategy, SupertrendEMAStrategy
from tests.helpers import run, with_params
from tests.test_three_strategies import OPEN, at, evaluate, orb_candles

BREAK = [(22045, 22058, 22030, 22050), (22050, 22080, 22048, 22075)]


# ── 2. risk per trade ────────────────────────────────────────────────────────

def _size(rc, *, fill, risk, capital_pct=12.5, base=21_000.0):
    with using(with_params(rc, max_risk_per_trade_pct=capital_pct)):
        return sizing_decision(
            fraction=FractionDecision("FIXED_FRACTION", 1.0, base), regime_multiplier=1.0,
            expected_entry_fill=fill, lot_size=65, available_capital=base,
            open_exposure=0.0, initial_risk=risk)


def test_lots_are_capped_by_the_loss_at_the_premium_stop(runtime_ctx):
    import dataclasses
    rc = dataclasses.replace(runtime_ctx, cfg=dataclasses.replace(
        runtime_ctx.cfg, capital=21_000.0, max_lots_per_trade=3, open_exposure_cap_pct=100.0))
    # Rs125 ATM put: capital buys 2 lots (Rs16,250); a 20% stop risks Rs25 x 65 = Rs1,625 a lot
    two = _size(rc, fill=125.0, risk=25.0)                       # budget 12.5% = Rs2,625
    assert two.lots == 1 and two.binding_clamp == "max_risk_per_trade"
    loose = _size(rc, fill=125.0, risk=25.0, capital_pct=20.0)   # Rs4,200 fits 2 lots
    assert loose.lots == 2 and "max_risk_per_trade" not in loose.clamps
    none = _size(rc, fill=160.0, risk=40.0, capital_pct=5.0)     # Rs2,600 > Rs1,050
    assert none.lots == 0 and none.reject is not None
    assert none.binding_clamp == "max_risk_per_trade"


def test_without_a_risk_figure_sizing_is_unchanged(runtime_ctx):
    with using(runtime_ctx):
        s = sizing_decision(fraction=FractionDecision("FIXED_FRACTION", 0.1, 20_000.0),
                            regime_multiplier=1.0, expected_entry_fill=40.0, lot_size=65,
                            available_capital=200_000.0, open_exposure=0.0)
    assert "max_risk_per_trade" not in s.clamps


def test_risk_per_trade_bound_is_validated():
    assert TradingParams().max_risk_per_trade_pct == 12.5
    for bad in (0.0, -1.0, 100.01):
        assert any("max_risk_per_trade_pct" in v
                   for v in TradingParams(max_risk_per_trade_pct=bad).validate())


# ── 3. Supertrend target ─────────────────────────────────────────────────────

def test_supertrend_premium_target_is_8_points():
    assert SupertrendEMAStrategy.PREMIUM_TARGET_PTS == 8.0
    assert SupertrendEMAStrategy.TIME_STOP_MINUTES == 10


# ── 5. ORB stop and time exit ────────────────────────────────────────────────

def test_orb_stop_is_a_quarter_of_the_range_inside_it():
    c = orb_candles(*BREAK)
    with at(25):
        s = ORBBreakoutStrategy()
        hi, lo = s.opening_range(c)
        sig = evaluate(s, c)
    plan = sig.exit_plan
    assert plan["spot_stop"] == round(hi - 0.25 * (hi - lo), 2)
    assert plan["spot_stop"] > (hi + lo) / 2                    # tighter than the old mid stop
    assert plan["time_stop_minutes"] == 90
    assert plan["spot_target"] == round(hi + (hi - lo), 2)


# ── 4. regime volume from the future ─────────────────────────────────────────

def _fut(n, last_vol, base=100.0):
    rows = []
    for k in range(n):
        rows.append({"timestamp": (OPEN + timedelta(minutes=k)).isoformat(),
                     "open": 1, "high": 1, "low": 1, "close": 1,
                     "volume": last_vol if k == n - 1 else base})
    return rows


def test_regime_reads_futures_volume_for_the_latest_minute(runtime_ctx, monkeypatch):
    from app.services.trading.strategy_runner import strategy_runner
    spot = [{"timestamp": (OPEN + timedelta(minutes=k)).isoformat()} for k in range(25)]

    async def _f(_):
        return _fut(25, 300.0)
    monkeypatch.setattr(strategy_runner, "_futures_candles", _f)
    with using(runtime_ctx):
        out = run(strategy_runner._regime_indicators("NIFTY", spot, {"volume": 0.0, "vwap": 1.0}))
    assert out["volume"] == 300.0 and out["avg_volume"] == 100.0 and out["vwap"] == 1.0


@pytest.mark.parametrize("fut", [[], "stale"])
def test_regime_indicators_unchanged_without_matching_futures(runtime_ctx, monkeypatch, fut):
    from app.services.trading.strategy_runner import strategy_runner
    spot = [{"timestamp": (OPEN + timedelta(minutes=k)).isoformat()} for k in range(25)]
    rows = _fut(20, 300.0) if fut == "stale" else []            # ends 5 minutes early

    async def _f(_):
        return rows
    monkeypatch.setattr(strategy_runner, "_futures_candles", _f)
    ind = {"volume": 0.0}
    with using(runtime_ctx):
        assert run(strategy_runner._regime_indicators("NIFTY", spot, ind)) is ind


# ── 6. Kelly sample ──────────────────────────────────────────────────────────

def test_kelly_waits_for_100_decided_trades(runtime_ctx):
    with using(runtime_ctx):
        assert kelly_from_stats(60, 39, 90.0, -39.0) == "FIXED_FRACTION"     # 99 trades
        assert kelly_from_stats(60, 40, 90.0, -40.0) != "FIXED_FRACTION"     # 100


# ── 7. brokerage per order ───────────────────────────────────────────────────

def test_brokerage_is_flat_per_order():
    p = TradingParams()
    assert fills.brokerage(1, p) == fills.brokerage(3, p) == 20.0
    assert fills.brokerage(0, p) == 0.0
    per_lot = TradingParams(brokerage_flat_per_order=False)
    assert fills.brokerage(3, per_lot) == 60.0
