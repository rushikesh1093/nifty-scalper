"""
Regime-label stabiliser (23 Oct): each switchable filter, against the real
classifier, and "all off" is exactly the classifier.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.trading_params import TradingParams
from app.services.trading.regime_config import RegimeConfig
from app.services.trading.regime_indicators import RegimeIndicators
from app.services.trading.regime_rules import classify_regime
from app.services.trading.regime_stabilizer import RegimeStabilizer

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 10, 14)
CFG = RegimeConfig()
BULL, RANGE, UNCL = "BULL_TREND_NORMAL_VOL", "RANGE_NORMAL_VOL", "UNCLASSIFIED"
COMP, BO = "LOW_VOL_COMPRESSION", "BREAKOUT_EVENT_BULLISH"


def ind(adx=25.0, atr_pct=50.0, price=22100.0, vwap=22000.0, orb_high=None, pdh=None):
    return RegimeIndicators(adx=adx, atr=5.0, atr_percentile=atr_pct, vwap=vwap, price=price,
                            vwap_cross_count=0, volume=0, avg_volume=0, orb_high=orb_high,
                            orb_low=None, pdh=pdh, pdl=None)


def at(minute, hour=10, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def classify(i, cfg):
    return classify_regime(i, "NIFTY", False, None, cfg)


def run(stab, p, steps, day=DAY):
    """steps: [(minute, indicators)] -> labels."""
    return [stab.step(underlying="NIFTY", day=day, now_ist=at(m, day=day), indicators=i,
                      config=CFG, classify=classify, p=p)[0] for m, i in steps]


def P(**kw):
    return replace(TradingParams(), **kw)


def test_all_off_is_exactly_the_classifier():
    p = TradingParams()
    calls = []

    def spy(i, cfg):
        calls.append(cfg)
        return classify(i, cfg)
    i = ind(adx=20)
    label, used = RegimeStabilizer().step(underlying="NIFTY", day=DAY, now_ist=at(1),
                                          indicators=i, config=CFG, classify=spy, p=p)
    assert label == UNCL and used is i and calls == [CFG]


def test_the_defaults_validate():
    assert TradingParams().validate() == []
    assert any("regime_eval_minutes" in m for m in P(regime_eval_minutes=7).validate())
    assert any("regime_confirm_evals" in m for m in P(regime_confirm_evals=0).validate())


def test_five_minute_cadence_holds_the_label_between_closes():
    s, p = RegimeStabilizer(), P(regime_eval_minutes=5)
    labels = run(s, p, [(20, ind()), (21, ind(adx=10)), (24, ind(adx=10)), (25, ind(adx=10))])
    assert labels == [BULL, BULL, BULL, RANGE]          # judged at 10:20 and 10:25 only


def test_the_first_label_of_the_day_is_not_delayed_to_a_close():
    s, p = RegimeStabilizer(), P(regime_eval_minutes=5)
    assert run(s, p, [(17, ind(adx=10))]) == [RANGE]


def test_adx_hysteresis_keeps_a_trend_and_a_range_inside_the_18_22_gap():
    plain = run(RegimeStabilizer(), P(regime_atr_smooth=1), [(1, ind()), (2, ind(adx=20))])
    assert plain == [BULL, UNCL]                         # the gap flips a trend to UNCLASSIFIED
    s, p = RegimeStabilizer(), P(regime_hysteresis=True)
    assert run(s, p, [(1, ind()), (2, ind(adx=20)), (3, ind(adx=19)), (4, ind(adx=17))]) == \
        [BULL, BULL, BULL, RANGE]
    s = RegimeStabilizer()
    assert run(s, p, [(1, ind(adx=15)), (2, ind(adx=21)), (3, ind(adx=23))]) == \
        [RANGE, RANGE, BULL]


def test_vwap_hysteresis_keeps_compression_out_to_the_exit_band():
    s, p = RegimeStabilizer(), P(regime_hysteresis=True, regime_vwap_exit_pct=0.35)
    near, wider, far = (ind(adx=20, atr_pct=20, price=22000 * (1 + x / 100)) for x in (0.1, 0.3, 0.4))
    assert run(s, p, [(1, near), (2, wider), (3, far)]) == [COMP, COMP, UNCL]
    assert run(RegimeStabilizer(), P(regime_atr_smooth=1), [(1, near), (2, wider)]) == [COMP, UNCL]


def test_atr_percentile_is_an_ema_over_evaluations():
    s, p = RegimeStabilizer(), P(regime_atr_smooth=3)          # alpha 0.5
    used = [s.step(underlying="NIFTY", day=DAY, now_ist=at(m), indicators=ind(atr_pct=v),
                   config=CFG, classify=classify, p=p)[1].atr_percentile
            for m, v in [(1, 50.0), (2, 90.0), (3, 50.0)]]
    assert used == [50.0, 70.0, 60.0]
    # a one-bar ATR spike to 90 no longer pushes a normal-vol trend into TREND_HIGH_VOL
    assert run(RegimeStabilizer(), p, [(1, ind()), (2, ind(atr_pct=80))]) == [BULL, BULL]


def test_breakout_latch_holds_until_it_fails_or_expires():
    p = P(regime_breakout_hold_minutes=15, regime_breakout_fail_pts=10.0)
    bo = ind(adx=20, price=22110, orb_high=22100)
    dip = ind(adx=20, price=22095, orb_high=22100)             # 5 pts back inside: held
    fail = ind(adx=20, price=22085, orb_high=22100)            # 15 pts back: released
    assert run(RegimeStabilizer(), p, [(1, bo), (2, dip), (3, dip)]) == [BO, BO, BO]
    assert run(RegimeStabilizer(), p, [(1, bo), (2, dip), (3, fail)]) == [BO, BO, UNCL]
    assert run(RegimeStabilizer(), p, [(1, bo), (16, dip)]) == [BO, UNCL]     # expired
    assert run(RegimeStabilizer(), P(), [(1, bo), (2, dip)]) == [BO, UNCL]    # off


def test_confirmation_needs_n_consecutive_evaluations():
    s, p = RegimeStabilizer(), P(regime_confirm_evals=3)
    r = ind(adx=10)
    assert run(s, p, [(1, ind()), (2, r), (3, r), (4, ind()), (5, r), (6, r), (7, r)]) == \
        [BULL, BULL, BULL, BULL, BULL, BULL, RANGE]           # an interruption restarts the count


def test_expiry_and_detector_error_are_never_delayed():
    s, p = RegimeStabilizer(), P(regime_confirm_evals=3, regime_eval_minutes=5)
    labels = []
    for m, lab in [(20, BULL), (25, "EXPIRY_DAY"), (30, "DETECTOR_ERROR")]:
        labels.append(s.step(underlying="NIFTY", day=DAY, now_ist=at(m), indicators=ind(),
                             config=CFG, classify=lambda i, c, lab=lab: lab, p=p)[0])
    assert labels == [BULL, "EXPIRY_DAY", "DETECTOR_ERROR"]


def test_a_new_day_starts_fresh():
    s, p = RegimeStabilizer(), P(regime_confirm_evals=3)
    assert run(s, p, [(1, ind())]) == [BULL]
    assert run(s, p, [(1, ind(adx=10))], day=DAY + timedelta(days=1)) == [RANGE]


@pytest.mark.parametrize("stabilised", [False, True])
def test_the_detector_uses_the_stabiliser(monkeypatch, stabilised):
    """End to end through RegimeDetector: with confirmation on, a one-candle
    flip never reaches the regime state."""
    import app.services.trading.regime_detector as rd
    from app.runtime.context import ctx, using
    from tests.helpers import run as arun, with_params
    from tests.test_regime_state_machine import _NoopPersistence

    seq = [ind(), ind(adx=10), ind()]
    monkeypatch.setattr(rd, "build_regime_indicators", lambda *a, **k: seq.pop(0))
    det = rd.RegimeDetector()
    det._persistence = _NoopPersistence()
    p = dict(regime_confirm_evals=2) if stabilised else {}
    with using(with_params(ctx(), **p)):
        out = [arun(det.detect_regime([], {}, "NIFTY")).regime for _ in range(3)]
    assert out == ([BULL, BULL, BULL] if stabilised else [BULL, RANGE, BULL])
