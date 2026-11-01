"""Fix 1 (B1): regime state machine must not lock up in TRANSITION loops."""
from datetime import datetime, timezone

import pytest

from app.services.trading import regime_detector as rd_mod
from app.services.trading.regime_detector import RegimeDetector
from app.services.trading.regime_indicators import RegimeIndicators
from app.services.trading.regime_persistence import RegimeState
from app.services.trading.strategy_orchestrator import StrategyOrchestrator
from tests.helpers import run

T = "REGIME_TRANSITION"
U = "UNCLASSIFIED"      # the classifier's catch-all (was UNKNOWN before the §6.2 split)
B = "BULL_TREND_NORMAL_VOL"


@pytest.fixture(autouse=True)
def _cooldown_on():
    """These tests specify the cooldown when it is ON (3 candles, the value it
    had before it was turned off by default on 23 Oct)."""
    from app.runtime.context import ctx, using
    from tests.helpers import with_params
    with using(with_params(ctx(), transition_cooldown_candles=3)):
        yield


def _ind():
    return RegimeIndicators(
        adx=25, atr=5, atr_percentile=50, vwap=100, price=101, vwap_cross_count=0,
        volume=0, avg_volume=0, orb_high=None, orb_low=None, pdh=None, pdl=None,
    )


class _NoopPersistence:
    async def save_regime_state(self, state):
        pass

    async def save_regime_history(self, underlying, history):
        pass

    async def persist_to_db(self, state, db_session):
        pass

    async def load_regime_history(self, underlying):
        return []


def _replay(monkeypatch, script):
    seq = list(script)
    monkeypatch.setattr(rd_mod, "classify_regime", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(rd_mod, "build_regime_indicators", lambda *a, **k: _ind())
    det = RegimeDetector()
    det._persistence = _NoopPersistence()
    orch = StrategyOrchestrator()

    regimes, active = [], 0

    async def _go():
        nonlocal active
        for _ in range(len(script)):
            state = await det.detect_regime([], {}, "NIFTY")
            flags = await orch.on_regime_detected(state)
            regimes.append(state.regime)
            if any(flags.values()) and not orch.is_in_cooldown("NIFTY"):
                active += 1

    run(_go())
    return regimes, active


def _entries_into_transition(regimes):
    return sum(1 for i, r in enumerate(regimes) if r == T and (i == 0 or regimes[i - 1] != T))


@pytest.mark.usefixtures("v1_regime")    # tests the pre-31-Oct regime map
def test_unknown_then_bull_settles(monkeypatch):
    """Readiness-report repro: UNCLASSIFIED x3 then BULL x21 (old code looped T x3 / B x1)."""
    regimes, active = _replay(monkeypatch, [U] * 3 + [B] * 21)

    assert regimes == [U] * 3 + [T] * 3 + [B] * 18
    assert _entries_into_transition(regimes) == 1
    assert T not in regimes[6:]
    assert regimes[-1] == B
    assert active == 18   # UNCLASSIFIED is no-trade (D7); the 18 BULL candles trade


def test_single_flip_one_transition_block(monkeypatch):
    a = "RANGE_NORMAL_VOL"
    regimes, _ = _replay(monkeypatch, [a] * 6 + [B] * 10)
    assert _entries_into_transition(regimes) == 1
    assert regimes.count(T) == 3   # TRANSITION_COOLDOWN_CANDLES default
    assert regimes[-1] == B
    assert regimes[:6] == [a] * 6


def _st(regime):
    return RegimeState(regime=regime, underlying="NIFTY", timestamp=datetime.now(timezone.utc),
                       indicators=_ind())


@pytest.mark.parametrize("history, candidate, expected", [
    ([], B, False),                          # no stable regime yet
    ([_st(T), _st(T)], B, False),            # only transition entries: nothing to differ from
    ([_st(U)], B, True),                     # one stable entry is enough (window unused, §5)
    ([_st(U), _st(U)], U, False),            # unchanged
    ([_st(U), _st(U)], B, True),             # real flip
    ([_st(U), _st(T), _st(T)], B, True),     # compared against the last stable regime
    ([_st(U), _st(U), _st(T), _st(T), _st(B)], B, False),  # T entries ignored
    ([_st(U), _st(T), _st(T), _st(T), _st(B)], B, False),
    ([_st(B), _st(B), _st(T)], U, True),
])
def test_check_regime_transition(history, candidate, expected):
    assert RegimeDetector.check_regime_transition(candidate, history, 2) is expected


# ── cooldown OFF (transition_cooldown_candles = 0, the default since 23 Oct) ──

@pytest.mark.parametrize("script, expected", [
    ([U] * 3 + [B] * 5, [U] * 3 + [B] * 5),
    (["RANGE_NORMAL_VOL"] * 2 + [B] * 2 + ["RANGE_NORMAL_VOL"] * 2,
     ["RANGE_NORMAL_VOL"] * 2 + [B] * 2 + ["RANGE_NORMAL_VOL"] * 2),
])
def test_with_the_cooldown_off_a_flip_is_adopted_on_its_bar(monkeypatch, script, expected):
    from app.runtime.context import ctx, using
    from tests.helpers import with_params
    with using(with_params(ctx(), transition_cooldown_candles=0)):
        regimes, _ = _replay(monkeypatch, script)
    assert regimes == expected and T not in regimes


@pytest.mark.usefixtures("v1_regime")    # tests the pre-31-Oct regime map
def test_the_cooldown_is_off_by_default(monkeypatch):
    from app.core.trading_params import TradingParams
    from app.runtime.context import ctx, using
    from tests.helpers import with_params
    off = TradingParams().transition_cooldown_candles
    assert off == 0
    with using(with_params(ctx(), transition_cooldown_candles=off)):
        regimes, active = _replay(monkeypatch, [U] * 3 + [B] * 21)
    assert regimes == [U] * 3 + [B] * 21 and active == 21
