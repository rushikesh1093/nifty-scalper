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
U = "UNKNOWN"
B = "BULL_TREND_NORMAL_VOL"


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


def test_unknown_then_bull_settles(monkeypatch):
    """Readiness-report repro: UNKNOWN x3 then BULL x21 (old code looped T x3 / B x1)."""
    regimes, active = _replay(monkeypatch, [U] * 3 + [B] * 21)

    assert regimes == [U] * 3 + [T] * 3 + [B] * 18
    assert _entries_into_transition(regimes) == 1
    assert T not in regimes[6:]
    assert regimes[-1] == B
    assert active >= 18
    assert active == 21   # 3 UNKNOWN + 18 BULL candles allow strategies


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
    ([], B, False),                          # no history
    ([_st(U)], B, False),                    # < window stable entries
    ([_st(U), _st(U)], U, False),            # unchanged
    ([_st(U), _st(U)], B, True),             # real flip
    ([_st(U), _st(T), _st(T)], B, False),    # only 1 stable entry → not enough data
    ([_st(U), _st(U), _st(T), _st(T), _st(B)], B, False),  # T entries ignored
    ([_st(U), _st(T), _st(T), _st(T), _st(B)], B, False),
    ([_st(B), _st(B), _st(T)], U, True),
])
def test_check_regime_transition(history, candidate, expected):
    assert RegimeDetector.check_regime_transition(candidate, history, 2) is expected
