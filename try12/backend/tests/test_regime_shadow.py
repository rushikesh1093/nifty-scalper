"""
Regime in shadow mode (TradingParams.regime_gating = False, the default since
31 Oct 2026): the regime is still detected, but the map no longer enables or
disables strategies, restricts direction or scales size. A detector failure
still blocks. With regime_gating = True the old map applies unchanged.
Also: regime v2's labels.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.trading_params import TradingParams
from app.runtime.context import using
from app.services.trading.regime_indicators import RegimeIndicators
from app.services.trading.regime_persistence import RegimeState
from app.services.trading.regime_v2 import classify_v2, vol_bucket
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.strategies.base import Direction
from app.strategies.names import ALL_STRATEGY_NAMES
from tests.helpers import make_signal, make_test_context, run

IST = timezone(timedelta(hours=5, minutes=30))


def _state(regime: str, price=100.0, vwap=101.0) -> RegimeState:
    ind = RegimeIndicators(adx=30.0, atr=5.0, atr_percentile=50.0, vwap=vwap, price=price,
                           vwap_cross_count=0, volume=0.0, avg_volume=0.0,
                           orb_high=None, orb_low=None, pdh=None, pdl=None)
    return RegimeState(regime=regime, underlying="NIFTY", timestamp=datetime.now(timezone.utc),
                       indicators=ind, previous_regime=None, transition_cooldown_remaining=0)


def _ctx(gating: bool):
    return using(make_test_context(params=TradingParams(regime_gating=gating)))


def test_shadow_is_the_default():
    assert TradingParams().regime_gating is False
    assert TradingParams().regime_atr_pct_lookback == 486
    assert TradingParams().validate() == []


def test_shadow_enables_every_strategy_in_a_no_trade_regime():
    with _ctx(False):
        flags = run(strategy_orchestrator.on_regime_detected(_state("HIGH_VOL_CHOP")))
        assert all(flags[n] for n in ALL_STRATEGY_NAMES)
        assert strategy_orchestrator.get_position_multiplier("NIFTY") == 1.0
    with _ctx(True):
        flags = run(strategy_orchestrator.on_regime_detected(_state("HIGH_VOL_CHOP")))
        assert not any(flags.values())
        assert strategy_orchestrator.get_position_multiplier("NIFTY") == 0.0


def test_shadow_allows_both_directions():
    sell = make_signal(strategy="ORB_BREAKOUT", direction=Direction.SELL)
    with _ctx(False):
        ok, code, _ = strategy_orchestrator.should_allow_signal(sell, _state("BULL_TREND_NORMAL_VOL"))
        assert ok and code is None
    with _ctx(True):
        ok, _, reason = strategy_orchestrator.should_allow_signal(sell, _state("BULL_TREND_NORMAL_VOL"))
        assert not ok and "direction" in reason


def test_a_detector_failure_still_blocks_in_shadow():
    buy = make_signal(strategy="ORB_BREAKOUT", direction=Direction.BUY)
    with _ctx(False):
        flags = run(strategy_orchestrator.on_regime_detected(_state("DETECTOR_ERROR")))
        assert not any(flags.values())
        assert strategy_orchestrator.get_position_multiplier("NIFTY") == 0.0
        assert not strategy_orchestrator.should_allow_signal(buy, _state("DETECTOR_ERROR"))[0]
        assert not strategy_orchestrator.should_allow_signal(buy, None)[0]


def test_the_expiry_day_policy_is_still_enforced_in_shadow():
    """EXPIRY_DAY exists only when trade_on_expiry_day is False: an operator
    policy, so shadow mode keeps it."""
    buy = make_signal(strategy="ORB_BREAKOUT", direction=Direction.BUY)
    with _ctx(False):
        flags = run(strategy_orchestrator.on_regime_detected(_state("EXPIRY_DAY")))
        assert not any(flags.values())
        assert not strategy_orchestrator.should_allow_signal(buy, _state("EXPIRY_DAY"))[0]


# ── regime v2 ────────────────────────────────────────────────────────────────

def _candles(day, n, start_px=22000.0, step=0.0, rng=4.0):
    t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    out, p = [], start_px
    for i in range(n):
        out.append({"timestamp": (t0 + timedelta(minutes=i)).isoformat(), "open": p,
                    "high": p + rng / 2, "low": p - rng / 2, "close": p + step, "volume": 0})
        p += step
    return out


def test_v2_needs_data_and_buckets_volatility():
    from datetime import date
    assert vol_bucket(None) == "UNKNOWN" and vol_bucket(10) == "LOW" and vol_bucket(90) == "HIGH"
    short = _candles(date(2026, 9, 1), 5)
    assert classify_v2(short, short).label == "INSUFFICIENT_DATA"
    quiet_hist = _candles(date(2026, 8, 31), 375, rng=4.0)
    loud_today = _candles(date(2026, 9, 1), 60, rng=30.0, step=2.0)
    v = classify_v2(loud_today, (quiet_hist + loud_today)[-500:])
    assert v.data_ok and v.vol == "HIGH" and v.or_side == "ABOVE"
    assert v.label == "VOL_HIGH|OR_ABOVE"


def test_v2_opening_range_is_forming_before_0930():
    from datetime import date
    hist = _candles(date(2026, 8, 31), 375)
    today = _candles(date(2026, 9, 1), 16)                # 09:15 - 09:30
    assert classify_v2(today[:14], (hist + today[:14])[-500:]).or_side == "FORMING"     # to 09:28
    assert classify_v2(today[:15], (hist + today[:15])[-500:]).or_side == "INSIDE"      # 09:29 closed
