"""
Bug 1 (design §5, R2.3-R2.8): a regime flip blocks exactly TRANSITION_COOLDOWN
candles, once, and the detector is the only cooldown owner.

The first two tests import nothing that only exists on this branch, so the same
file runs against master to show the fail-before (the regime lock-up).
"""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.trading import regime_detector as rd_mod
from app.services.trading.regime_detector import RegimeDetector
from app.services.trading.regime_indicators import RegimeIndicators
from app.services.trading.strategy_orchestrator import StrategyOrchestrator

APP_DIR = Path(__file__).resolve().parents[1] / "app"

T = "REGIME_TRANSITION"
A = "RANGE_NORMAL_VOL"
B = "BULL_TREND_NORMAL_VOL"
C = "BEAR_TREND_NORMAL_VOL"
X = "EXPIRY_DAY"


def _ind():
    return RegimeIndicators(
        adx=25, atr=5, atr_percentile=50, vwap=100, price=101, vwap_cross_count=0,
        volume=0, avg_volume=0, orb_high=None, orb_low=None, pdh=None, pdl=None,
    )


class _Persist:
    """No Redis/DB. Optionally serves a seeded Redis history for rehydration."""

    def __init__(self, seeded=None):
        self.seeded = list(seeded or [])

    async def save_regime_state(self, state):
        pass

    async def save_regime_history(self, underlying, history):
        pass

    async def persist_to_db(self, state, db_session):
        pass

    async def load_regime_history(self, underlying):
        return list(self.seeded)


def _drive(monkeypatch, script, persistence=None):
    """Feed one classifier output per candle; return [(detector regime, flags)]."""
    current = {"r": None}
    monkeypatch.setattr(rd_mod, "classify_regime", lambda *a, **k: current["r"])
    monkeypatch.setattr(rd_mod, "build_regime_indicators", lambda *a, **k: _ind())
    det = RegimeDetector()
    det._persistence = persistence or _Persist()
    orch = StrategyOrchestrator()
    out = []

    async def _go():
        for r in script:
            current["r"] = r
            state = await det.detect_regime([], {}, "NIFTY", is_expiry_day=False)
            flags = await orch.on_regime_detected(state)
            out.append((state.regime, flags, orch))

    asyncio.run(_go())
    return out


def _blocked_by_cooldown(out):
    return [r == T for r, _, _ in out]


# ── R2.7: 24 candles, one flip ───────────────────────────────────────────────

def test_one_flip_blocks_exactly_three_and_leaves_21_eligible(monkeypatch):
    out = _drive(monkeypatch, [A] * 6 + [B] * 18)
    # eligible = the detector is not in transition AND the orchestrator enables
    # at least one strategy AND does not report a cooldown (A and B both trade)
    eligible = [r != T and any(f.values()) and not o.is_in_cooldown("NIFTY")
                for r, f, o in out]
    assert sum(not e for e in eligible) == 3
    assert sum(eligible) == 21
    assert [r for r, _, _ in out][6:9] == [T, T, T]      # the flip candle and the next two
    assert [r for r, _, _ in out][9:] == [B] * 15        # adoption candle is eligible


# ── R2.8: 375 candles, N changes incl. into and out of EXPIRY_DAY ────────────

def test_375_candles_block_n_non_expiry_times_cooldown(monkeypatch):
    segments = [(A, 40), (B, 30), (C, 25), (X, 20), (A, 35), (B, 45), (A, 30),
                (C, 50), (X, 15), (B, 40), (A, 45)]
    script = [r for r, n in segments for _ in range(n)]
    assert len(script) == 375
    out = _drive(monkeypatch, script)
    changes = list(zip(segments, segments[1:]))
    n_non_expiry = sum(1 for _, (new, _) in changes if new != X)
    assert n_non_expiry == 8 and len(changes) == 10      # 2 changes into EXPIRY_DAY
    assert sum(_blocked_by_cooldown(out)) == n_non_expiry * 3


# ── cooldown bound (§5, R2 finding 9) ────────────────────────────────────────

class _StaticRegimeConfig:
    def __init__(self, cfg):
        self.cfg = cfg

    def for_underlying(self, underlying):
        return self.cfg


def _with_cooldown(n):
    from dataclasses import replace

    from app.runtime.context import ctx, using
    from app.services.trading.regime_config import RegimeConfig

    return using(replace(ctx(), regime_config=_StaticRegimeConfig(
        RegimeConfig(TRANSITION_COOLDOWN_CANDLES=n))))


def test_cooldown_seven_still_detects_the_flip_and_blocks_seven(monkeypatch):
    # A second flip one candle after adoption: with a 5-entry history the 7
    # TRANSITION entries leave at most one stable state, and the flip is missed.
    with _with_cooldown(7):
        out = _drive(monkeypatch, [A] * 10 + [B] * 8 + [C] * 12)
    regimes = [r for r, _, _ in out]
    assert regimes[10:17] == [T] * 7 and regimes[17] == B
    assert regimes[18:25] == [T] * 7 and regimes[25:] == [C] * 5   # second flip detected
    assert regimes.count(T) == 14


def test_cooldown_twelve_fails_validate():
    from app.core.trading_params import TradingParams
    from app.services.trading.regime_detector import TRANSITION_COOLDOWN_CANDLES_MAX, _history_len

    assert TRANSITION_COOLDOWN_CANDLES_MAX == 8
    assert all(c < _history_len(c) - 1 for c in range(1, TRANSITION_COOLDOWN_CANDLES_MAX + 1))
    v = TradingParams(transition_cooldown_candles=12).validate()
    assert any("transition_cooldown_candles" in m for m in v)


# ── A → B → C inside one window (review finding 16) ──────────────────────────

def test_third_regime_on_adoption_candle_is_adopted_without_new_cooldown(monkeypatch):
    # flip A→B at candle 5 (blocks 5, 6, 7); candle 8 classifies to C: adopted
    out = _drive(monkeypatch, [A] * 5 + [B] * 3 + [C] * 10)
    regimes = [r for r, _, _ in out]
    assert regimes[5:8] == [T, T, T]
    assert regimes[8:] == [C] * 10
    assert regimes.count(T) == 3


def test_first_candles_of_a_session_have_no_stable_regime(monkeypatch):
    out = _drive(monkeypatch, [B, A, A])
    assert [r for r, _, _ in out] == [B, T, T]   # a flip after one stable candle is a flip


# ── rehydrated history never crosses a session boundary (R4 finding 17) ──────

def _state(regime, ts, cooldown=0):
    from app.services.trading.regime_persistence import RegimeState
    return RegimeState(regime=regime, underlying="NIFTY", timestamp=ts, indicators=_ind(),
                       transition_cooldown_remaining=cooldown)


def test_previous_day_expiry_history_is_discarded(runtime_ctx, fake_redis, monkeypatch):
    from app.services.trading.regime_persistence import RegimePersistence, _state_to_dict

    yesterday = runtime_ctx.clock.now_utc() - timedelta(days=1)
    seeded = [_state(X, yesterday - timedelta(minutes=i)) for i in range(5, 0, -1)]
    fake_redis.kv["regime:NIFTY:history"] = [_state_to_dict(s) for s in seeded]

    loaded = asyncio.run(RegimePersistence().load_regime_history("NIFTY"))
    assert loaded == []

    out = _drive(monkeypatch, [A] * 5, persistence=RegimePersistence())
    assert [r for r, _, _ in out] == [A] * 5             # zero blocked candles


def test_same_day_history_is_rehydrated(runtime_ctx, fake_redis, monkeypatch):
    from app.services.trading.regime_persistence import RegimePersistence, _state_to_dict

    now = runtime_ctx.clock.now_utc()
    seeded = [_state(A, now - timedelta(minutes=i)) for i in range(3, 0, -1)]
    fake_redis.kv["regime:NIFTY:history"] = [_state_to_dict(s) for s in seeded]
    out = _drive(monkeypatch, [B] * 5, persistence=RegimePersistence())
    assert [r for r, _, _ in out] == [T, T, T, B, B]     # restart mid-session: flip still seen


# ── the orchestrator mirror expires (R2 finding 8) ───────────────────────────

def test_cooldown_mirror_ttl_expires(runtime_ctx):
    orch = StrategyOrchestrator()
    state = _state(T, runtime_ctx.clock.now_utc(), cooldown=3)
    asyncio.run(orch.on_regime_detected(state))
    assert orch.is_in_cooldown("NIFTY") is True
    runtime_ctx.clock.advance(seconds=121)                 # past cooldown_mirror_ttl_s = 120
    assert orch.is_in_cooldown("NIFTY") is False
    assert StrategyOrchestrator().is_in_cooldown("NIFTY") is False   # never detected


def test_orchestrator_holds_no_second_counter():
    orch = StrategyOrchestrator()
    assert not hasattr(orch, "_transition_cooldown")


def test_detector_error_fallback_is_no_trade_and_not_appended(monkeypatch):
    from app.services.trading.regime_rules import get_regime_rule

    def _boom(*a, **k):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(rd_mod, "build_regime_indicators", lambda *a, **k: _ind())
    monkeypatch.setattr(rd_mod, "classify_regime", _boom)
    det = RegimeDetector()
    det._persistence = _Persist()
    state = asyncio.run(det.detect_regime([], {}, "NIFTY"))
    assert state.regime == "DETECTOR_ERROR" and state.transition_cooldown_remaining == 0
    assert det.get_regime_history("NIFTY") == []
    rule = get_regime_rule("DETECTOR_ERROR")
    assert rule.enabled_strategies == frozenset() and rule.position_multiplier == 0.0


# ── static: is_in_cooldown gates nothing outside app/api/ (§5) ───────────────

def _gate_uses(tree, name):
    """Calls to *name* inside an if/while/assert/boolean test or a return."""
    hits = []
    for node in ast.walk(tree):
        tests = []
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            tests.append(node.test)
        elif isinstance(node, ast.Assert):
            tests.append(node.test)
        elif isinstance(node, ast.BoolOp):
            tests.extend(node.values)
        elif isinstance(node, ast.Return) and node.value is not None:
            tests.append(node.value)
        for t in tests:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if fname == name:
                        hits.append(sub.lineno)
    return hits


def test_is_in_cooldown_is_not_a_gate_outside_api():
    hits = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel.startswith("api/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits += [f"{rel}:{ln}" for ln in _gate_uses(tree, "is_in_cooldown")]
    assert hits == [], f"is_in_cooldown used as a gate: {hits}"


def test_gate_scan_catches_a_synthetic_gate():
    tree = ast.parse("def f(o, u):\n    if o.is_in_cooldown(u):\n        return False\n")
    assert _gate_uses(tree, "is_in_cooldown") == [2]
