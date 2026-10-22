"""
Bug 2, name half (design §6.1, R3.3, R3.9).

The orchestrator's flag universe must be exactly the strategies that
build_strategies() instantiates (three since the strategy rewrite). Before this fix it listed two phantom names
(PCR_SIGNAL, GAP_FILL) and omitted CPR_VWAP / GAP_VWAP_FILL, so those two
strategies were never gated by a regime rule.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.trading.regime_indicators import RegimeIndicators
from app.services.trading.regime_persistence import RegimeState
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.strategies.strategies import build_strategies
from tests.helpers import run

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# The expected set is written out here on purpose rather than imported, so the
# test cannot pass by both sides reading the same wrong source.
CANONICAL = {"VWAP_EMA_PULLBACK", "ORB_BREAKOUT", "SUPERTREND_EMA"}
PHANTOM = re.compile(r"\b(PCR_SIGNAL|GAP_FILL)\b")


def _state(regime: str = "RANGE_NORMAL_VOL") -> RegimeState:
    ind = RegimeIndicators(
        adx=15.0, atr=5.0, atr_percentile=50.0, vwap=22600.0, price=22610.0,
        vwap_cross_count=2, volume=0.0, avg_volume=0.0,
        orb_high=None, orb_low=None, pdh=None, pdl=None,
    )
    return RegimeState(regime=regime, underlying="NIFTY",
                       timestamp=datetime.now(timezone.utc), indicators=ind)


def test_built_strategies_are_the_canonical_names():
    assert {s.name for s in build_strategies("NIFTY")} == CANONICAL


def test_orchestrator_universe_equals_the_canonical_names():
    flags = run(strategy_orchestrator.on_regime_detected(_state()))
    assert set(flags) == CANONICAL


def test_strategy_name_enum_matches_canonical():
    from app.strategies.names import ALL_STRATEGY_NAMES, StrategyName
    assert set(ALL_STRATEGY_NAMES) == CANONICAL
    assert len(ALL_STRATEGY_NAMES) == 3
    assert {m.value for m in StrategyName} == CANONICAL


def test_strategies_module_reexports_the_canonical_tuple():
    from app.strategies.names import ALL_STRATEGY_NAMES
    from app.strategies.strategies import STRATEGY_NAMES
    assert tuple(STRATEGY_NAMES) == ALL_STRATEGY_NAMES


def test_regime_rules_only_name_canonical_strategies():
    from app.services.trading.regime_rules import REGIME_RULES
    for regime, rule in REGIME_RULES.items():
        unknown = set(rule.enabled_strategies) - CANONICAL
        assert not unknown, f"{regime} names unknown strategies {unknown}"


# ── Bug 2, map half (design §6.2-6.4, R3.4-R3.7) ─────────────────────────────

THIRTEEN_REGIMES = {
    "BULL_TREND_NORMAL_VOL", "BEAR_TREND_NORMAL_VOL", "TREND_HIGH_VOL",
    "BREAKOUT_EVENT_BULLISH", "BREAKOUT_EVENT_BEARISH", "LOW_VOL_COMPRESSION",
    "RANGE_NORMAL_VOL", "HIGH_VOL_CHOP", "REGIME_TRANSITION", "EXPIRY_DAY",
    "UNCLASSIFIED", "DETECTOR_ERROR", "UNKNOWN",
}


def test_every_strategy_is_enabled_in_some_regime():
    from app.services.trading.regime_rules import NEVER_ENABLED, REGIME_RULES
    union = set().union(*(r.enabled_strategies for r in REGIME_RULES.values()))
    assert union | set(NEVER_ENABLED) == CANONICAL
    assert NEVER_ENABLED == frozenset()


def test_regime_rules_have_exactly_13_keys():
    from app.services.trading.regime_rules import REGIME_RULES
    assert set(REGIME_RULES) == THIRTEEN_REGIMES


def test_multipliers_are_in_the_declared_set():
    from app.services.trading.regime_rules import POSITION_MULTIPLIERS, REGIME_RULES, get_regime_rule
    assert POSITION_MULTIPLIERS == frozenset({0.0, 0.5, 1.0})
    for name in REGIME_RULES:
        for atr in (10.0, 50.0, 71.0, 99.0):
            assert get_regime_rule(name, atr).position_multiplier in POSITION_MULTIPLIERS


@pytest.mark.parametrize("regime", ["HIGH_VOL_CHOP", "REGIME_TRANSITION", "EXPIRY_DAY",
                                    "UNCLASSIFIED", "DETECTOR_ERROR", "UNKNOWN"])
def test_no_trade_regimes(regime):
    from app.services.trading.regime_rules import REGIME_RULES
    rule = REGIME_RULES[regime]
    assert rule.enabled_strategies == frozenset() and rule.allowed_directions == frozenset()
    assert rule.position_multiplier == 0.0


def test_unrecognised_regime_falls_back_to_detector_error():
    from app.services.trading.regime_rules import REGIME_RULES, get_regime_rule
    assert get_regime_rule("NO_SUCH_REGIME") == REGIME_RULES["DETECTOR_ERROR"]


def test_classifier_catch_all_is_unclassified():
    from app.services.trading.regime_config import RegimeConfig
    from app.services.trading.regime_rules import classify_regime
    ind = RegimeIndicators(          # ADX 20: between range (18) and trend (22), normal ATR
        adx=20.0, atr=5.0, atr_percentile=50.0, vwap=22600.0, price=22610.0,
        vwap_cross_count=2, volume=0.0, avg_volume=0.0,
        orb_high=None, orb_low=None, pdh=None, pdl=None,
    )
    assert classify_regime(ind, "NIFTY", False, None, RegimeConfig()) == "UNCLASSIFIED"


def _merge(monkeypatch, regime, user_flags):
    """Run one detection through the real detector, orchestrator and runner merge."""
    from app.services.trading import regime_detector as rd_mod
    from app.services.trading.strategy_runner import strategy_runner

    monkeypatch.setattr(rd_mod, "classify_regime", lambda *a, **k: regime)
    monkeypatch.setattr(rd_mod, "build_regime_indicators", lambda *a, **k: _state().indicators)
    flags, _ = run(strategy_runner._detect_and_merge_regime(
        "NIFTY", [], {}, False, user_flags))
    return flags


def test_absent_regime_flag_means_disabled(monkeypatch):
    async def _only_one(state):
        return {"ORB_BREAKOUT": True}            # a map that forgot 2 strategies

    monkeypatch.setattr(strategy_orchestrator, "on_regime_detected", _only_one)
    flags = _merge(monkeypatch, "RANGE_NORMAL_VOL", {})
    assert set(flags) == CANONICAL
    assert [n for n, on in flags.items() if on] == ["ORB_BREAKOUT"]


def test_user_disable_overrides_the_regime(monkeypatch):
    flags = _merge(monkeypatch, "TREND_HIGH_VOL", {"SUPERTREND_EMA": False})
    assert flags["SUPERTREND_EMA"] is False
    assert flags["VWAP_EMA_PULLBACK"] is True
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    assert strategy_orchestrator.is_strategy_enabled("NIFTY", "SUPERTREND_EMA") == (False, "user_disabled")
    assert strategy_orchestrator.is_strategy_enabled("NIFTY", "VWAP_EMA_PULLBACK") == (True, "enabled")
    assert strategy_orchestrator.is_strategy_enabled("NIFTY", "ORB_BREAKOUT") == (False, "regime_rule")


def test_is_strategy_enabled_defaults_closed():
    from app.services.trading.strategy_orchestrator import StrategyOrchestrator
    assert StrategyOrchestrator().is_strategy_enabled("NIFTY", "ORB_BREAKOUT") == \
        (False, "regime flags not yet computed")


def test_detection_failure_disables_everything():
    flags = strategy_orchestrator.mark_regime_unavailable("NIFTY")
    assert set(flags) == CANONICAL and not any(flags.values())
    assert strategy_orchestrator.is_strategy_enabled("NIFTY", "ORB_BREAKOUT") == \
        (False, "regime_unavailable")


def test_should_allow_signal_fails_closed_without_a_regime():
    from app.services.trading.no_trade import ReasonCode
    from tests.helpers import make_signal
    ok, code, reason = strategy_orchestrator.should_allow_signal(make_signal(), None)
    assert (ok, code) == (False, ReasonCode.REGIME_STATE_UNAVAILABLE) and reason


def test_trend_high_vol_narrows_direction_by_price_vs_vwap():
    from app.strategies.base import Direction
    from tests.helpers import make_signal
    st = _state("TREND_HIGH_VOL")                 # price 22610 > vwap 22600
    assert strategy_orchestrator.should_allow_signal(
        make_signal(strategy="SUPERTREND_EMA", direction=Direction.BUY), st)[0] is True
    ok, _, reason = strategy_orchestrator.should_allow_signal(
        make_signal(strategy="SUPERTREND_EMA", direction=Direction.SELL), st)
    assert ok is False and "BUY only" in reason


# ── startup checks 2-5 (design §6.3) ─────────────────────────────────────────

def test_startup_checks_2_to_5_pass_on_the_shipped_map():
    from app.services.trading.startup_checks import check_regime_map
    assert check_regime_map() == []


def test_startup_regime_checks_catch_a_broken_map(monkeypatch):
    from app.services.trading import regime_rules
    from app.services.trading.startup_checks import check_regime_map

    broken = dict(regime_rules.REGIME_RULES)
    broken["RANGE_NORMAL_VOL"] = broken["RANGE_NORMAL_VOL"]._replace(
        enabled_strategies=frozenset({"PCR" + "_SIGNAL"}), position_multiplier=0.3)
    del broken["UNKNOWN"]
    for k in ("TREND_HIGH_VOL", "BULL_TREND_NORMAL_VOL", "BEAR_TREND_NORMAL_VOL"):
        broken[k] = broken[k]._replace(enabled_strategies=frozenset())
    monkeypatch.setattr(regime_rules, "REGIME_RULES", broken)
    v = " | ".join(check_regime_map())
    assert "unknown strategy" in v                 # check 2
    assert "never enabled" in v and "VWAP_EMA_PULLBACK" in v   # check 3
    assert "multiplier" in v                       # check 4
    assert "regime keys" in v                      # check 5


def test_run_startup_checks_passes_under_runtime_ctx(runtime_ctx, sqlite_db):
    from app.services.trading.startup_checks import run_startup_checks
    from tests.helpers import seed_config
    run(seed_config(sqlite_db))
    run(run_startup_checks(require_config_row=True))


def test_strategy_map_endpoint(runtime_ctx, monkeypatch):
    from app.api.routes.regime import get_strategy_map

    _merge(monkeypatch, "REGIME_TRANSITION", {"SUPERTREND_EMA": False})
    body = run(get_strategy_map())
    assert body["multiplier_set"] == [0.0, 0.5, 1.0] and body["never_enabled"] == []
    assert {r["regime"] for r in body["regimes"]} == THIRTEEN_REGIMES
    nifty = next(c for c in body["current"] if c["underlying"] == "NIFTY")
    reasons = {s["name"]: s["reason"] for s in nifty["strategies"]}
    assert reasons["SUPERTREND_EMA"] == "user_disabled"
    assert reasons["ORB_BREAKOUT"] == "transition_cooldown"
    bank = next(c for c in body["current"] if c["underlying"] == "BANKNIFTY")
    assert {s["reason"] for s in bank["strategies"]} == {"regime_unavailable"}


def test_no_module_emits_unknown_as_a_regime():
    from app.services.trading.startup_checks import scan_regime_unknown
    assert scan_regime_unknown() == []


@pytest.mark.parametrize("src", [
    'regime = "UNKNOWN"\n',
    'def f(d):\n    current_regime = d.get("r", "UNKNOWN")\n',
    'def f(x, regime="UNKNOWN"):\n    return x\n',
    'def f(s):\n    return g(regime="UNKNOWN")\n',
    'def f(s):\n    return s.regime == "UNKNOWN"\n',
])
def test_unknown_scan_fails_on_a_synthetic_module(tmp_path, src):
    from app.services.trading.startup_checks import scan_regime_unknown
    (tmp_path / "bad.py").write_text(src, encoding="utf-8")
    assert scan_regime_unknown(root=tmp_path, allowed=frozenset()) != []


def test_unknown_scan_ignores_non_regime_identifiers(tmp_path):
    from app.services.trading.startup_checks import scan_regime_unknown
    (tmp_path / "ok.py").write_text(
        'def f(p):\n    strategy_name = p.strategy_name or "UNKNOWN"\n    k = "UNKNOWN"\n',
        encoding="utf-8")
    assert scan_regime_unknown(root=tmp_path, allowed=frozenset()) == []


def test_no_phantom_strategy_literal_under_app():
    hits = []
    for path in APP_DIR.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PHANTOM.search(line):
                hits.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}: {line.strip()}")
    assert not hits, "phantom strategy names found:\n" + "\n".join(hits)
