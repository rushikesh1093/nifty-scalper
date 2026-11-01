"""
Part 3 regime-classification switches, all off by default:
  regime_atr_pct_lookback  ATR values the ATR percentile ranks against (20 = original)
  regime_range_bound       RANGE_BOUND for low-ADX minutes that were UNCLASSIFIED
  regime_hysteresis        the ADX 18-22 dead zone keeps the previous label,
                           RANGE_BOUND included
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.trading_params import TradingParams
from app.services.trading.regime_config import RegimeConfig
from app.services.trading.regime_indicators import (
    RegimeIndicators,
    build_regime_indicators,
    compute_atr_percentile,
)
from app.services.trading.regime_rules import REGIME_RULES, classify_regime
from app.services.trading.regime_stabilizer import RegimeStabilizer

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 10, 14)
CFG = RegimeConfig()
RB, UNCL, RANGE = "RANGE_BOUND", "UNCLASSIFIED", "RANGE_NORMAL_VOL"


def ind(adx=15.0, atr_pct=80.0, price=22010.0, vwap=22000.0, crosses=2):
    return RegimeIndicators(adx=adx, atr=5.0, atr_percentile=atr_pct, vwap=vwap, price=price,
                            vwap_cross_count=crosses, volume=0, avg_volume=0, orb_high=None,
                            orb_low=None, pdh=None, pdl=None)


def classify(i, range_bound=False, cfg=CFG):
    return classify_regime(i, "NIFTY", False, None, cfg, range_bound=range_bound)


def P(**kw):
    return replace(TradingParams(), **kw)


# ── defaults and validation ──────────────────────────────────────────────────

def test_defaults_keep_the_current_behaviour():
    p = TradingParams()
    # 486 since 31 Oct 2026: the 20-value rank did not predict range (REGIME_REDESIGN.md)
    assert p.regime_atr_pct_lookback == 486
    assert p.regime_range_bound is False
    assert p.regime_hysteresis is False
    assert p.validate() == []


@pytest.mark.parametrize("n,ok", [(4, False), (5, True), (375, True), (486, True), (487, False)])
def test_atr_lookback_bounds(n, ok):
    bad = [m for m in P(regime_atr_pct_lookback=n).validate() if "regime_atr_pct_lookback" in m]
    assert (bad == []) is ok


def test_params_file_overrides_are_accepted():
    p = TradingParams.from_mapping({"regime_atr_pct_lookback": 375, "regime_range_bound": True,
                                    "regime_hysteresis": True})
    assert (p.regime_atr_pct_lookback, p.regime_range_bound, p.regime_hysteresis) == \
        (375, True, True)


# ── (a) ATR-percentile lookback ──────────────────────────────────────────────

def _candles(ranges):
    out, px = [], 22000.0
    for r in ranges:
        out.append({"open": px, "high": px + r / 2, "low": px - r / 2, "close": px})
    return out


def test_longer_lookback_ranks_against_the_whole_session():
    # A quiet morning (range 10) then a 40-bar burst (range 30): ATR is high
    # against the session, but ordinary against the last 20 values.
    candles = _candles([10.0] * 400 + [30.0] * 40)
    atr_now = 30.0
    assert compute_atr_percentile(atr_now, candles) == 100.0          # 20: top of 20 values
    short = compute_atr_percentile(28.0, candles)
    long = compute_atr_percentile(28.0, candles, period=375)
    assert short == 0.0                    # the last 20 ATRs are 29.0-29.9 -> 28 ranks bottom
    assert long > 85.0                     # vs the session, 28 is a high reading
    assert len({compute_atr_percentile(v, candles, period=375) for v in (11, 15, 20, 25)}) > 1


def test_longer_lookback_without_enough_history_is_neutral():
    candles = _candles([10.0] * 200)
    assert compute_atr_percentile(50.0, candles, period=375) == 50.0
    assert compute_atr_percentile(50.0, candles) == 100.0             # 20 still has enough


def test_builder_default_is_the_20_value_rank():
    candles = _candles([10.0] * 400 + [30.0] * 40)
    indicators = {"atr": 28.0, "vwap": 22000.0, "close": 22000.0}
    assert build_regime_indicators(candles, indicators, CFG).atr_percentile == 0.0
    assert build_regime_indicators(candles, indicators, CFG,
                                   atr_pct_lookback=375).atr_percentile > 85.0


# ── (b) RANGE_BOUND ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,i", [
    ("high ATR%ile, low ADX, few crosses", ind(adx=15, atr_pct=80)),
    ("low ATR%ile, far from VWAP", ind(adx=12, atr_pct=10, price=22100)),
])
def test_range_bound_covers_low_adx_minutes_that_were_unclassified(label, i):
    assert classify(i) == UNCL
    assert classify(i, range_bound=True) == RB


@pytest.mark.parametrize("i,expected", [
    (ind(adx=20, atr_pct=80), UNCL),                       # dead zone: hysteresis's job
    (ind(adx=None, atr_pct=80), UNCL),                     # ADX not warmed up
    (ind(adx=15, atr_pct=80, crosses=8), "HIGH_VOL_CHOP"),  # chop still wins
    (ind(adx=15, atr_pct=10, price=22100, crosses=8), UNCL),  # many crosses: not a range
    (ind(adx=15, atr_pct=50), RANGE),                      # normal band keeps its label
    (ind(adx=15, atr_pct=10, price=22010), "LOW_VOL_COMPRESSION"),
    (ind(adx=30, atr_pct=50), "BULL_TREND_NORMAL_VOL"),
])
def test_range_bound_leaves_every_other_label_alone(i, expected):
    assert classify(i, range_bound=True) == expected
    assert classify(i) == expected


def test_range_bound_rule_trades_the_range_strategies():
    rb, rng = REGIME_RULES[RB], REGIME_RULES[RANGE]
    assert rb.enabled_strategies == rng.enabled_strategies
    assert rb.allowed_directions == rng.allowed_directions
    assert rb.position_multiplier == 1.0


def test_startup_regime_map_accepts_range_bound():
    from app.services.trading.startup_checks import check_regime_map
    assert check_regime_map() == []


# ── (c) hysteresis over the ADX 18-22 dead zone ──────────────────────────────

def _run(p, seq):
    s = RegimeStabilizer()
    rb = p.regime_range_bound
    return [s.step(underlying="NIFTY", day=DAY,
                   now_ist=datetime(2026, 10, 14, 10, m, tzinfo=IST), indicators=i, config=CFG,
                   classify=lambda x, c: classify_regime(x, "NIFTY", False, None, c,
                                                         range_bound=rb), p=p)[0]
            for m, i in enumerate(seq, start=1)]


def test_dead_zone_keeps_a_range_bound_label():
    seq = [ind(adx=15), ind(adx=20), ind(adx=21.5), ind(adx=23)]
    assert _run(P(regime_range_bound=True), seq) == [RB, UNCL, UNCL, "TREND_HIGH_VOL"]
    assert _run(P(regime_range_bound=True, regime_hysteresis=True), seq) == \
        [RB, RB, RB, "TREND_HIGH_VOL"]


def test_hysteresis_alone_does_not_emit_range_bound():
    seq = [ind(adx=15), ind(adx=20)]
    assert _run(P(regime_hysteresis=True), seq) == [UNCL, UNCL]


# ── the detector passes the knobs through ────────────────────────────────────

@pytest.mark.usefixtures("v1_regime")    # tests the pre-31-Oct regime map
@pytest.mark.parametrize("params,lookback,label", [
    ({}, 20, UNCL),
    ({"regime_atr_pct_lookback": 375, "regime_range_bound": True}, 375, RB),
])
def test_detector_reads_the_knobs_from_params(monkeypatch, params, lookback, label):
    import app.services.trading.regime_detector as rd
    from app.runtime.context import ctx, using
    from tests.helpers import run as arun, with_params
    from tests.test_regime_state_machine import _NoopPersistence

    seen = {}

    def fake_build(*a, **k):
        seen.update(k)
        return ind(adx=15, atr_pct=80)
    monkeypatch.setattr(rd, "build_regime_indicators", fake_build)
    det = rd.RegimeDetector()
    det._persistence = _NoopPersistence()
    with using(with_params(ctx(), **params)):
        state = arun(det.detect_regime([], {}, "NIFTY"))
    assert seen["atr_pct_lookback"] == lookback
    assert state.regime == label
