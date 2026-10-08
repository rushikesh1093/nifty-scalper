"""
Bug 2, name half (design §6.1, R3.3, R3.9).

The orchestrator's flag universe must be exactly the 12 strategies that
build_strategies() instantiates. Before this fix it listed two phantom names
(PCR_SIGNAL, GAP_FILL) and omitted CPR_VWAP / GAP_VWAP_FILL, so those two
strategies were never gated by a regime rule.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

from app.services.trading.regime_indicators import RegimeIndicators
from app.services.trading.regime_persistence import RegimeState
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.strategies.strategies import build_strategies
from tests.helpers import run

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# The expected set is written out here on purpose rather than imported, so the
# test cannot pass by both sides reading the same wrong source.
CANONICAL_12 = {
    "ORB_VWAP", "VWAP_PULLBACK", "EMA_VWAP", "SUPERTREND_VWAP", "PDH_PDL",
    "VWAP_REVERSAL", "BOLLINGER_BREAKOUT", "RSI_VWAP", "OI_SPIKE", "IV_CRUSH",
    "CPR_VWAP", "GAP_VWAP_FILL",
}
PHANTOM = re.compile(r"\b(PCR_SIGNAL|GAP_FILL)\b")


def _state(regime: str = "RANGE_NORMAL_VOL") -> RegimeState:
    ind = RegimeIndicators(
        adx=15.0, atr=5.0, atr_percentile=50.0, vwap=22600.0, price=22610.0,
        vwap_cross_count=2, volume=0.0, avg_volume=0.0,
        orb_high=None, orb_low=None, pdh=None, pdl=None,
    )
    return RegimeState(regime=regime, underlying="NIFTY",
                       timestamp=datetime.now(timezone.utc), indicators=ind)


def test_built_strategies_are_the_canonical_12():
    assert {s.name for s in build_strategies("NIFTY")} == CANONICAL_12


def test_orchestrator_universe_equals_the_12_canonical_names():
    flags = run(strategy_orchestrator.on_regime_detected(_state()))
    assert set(flags) == CANONICAL_12


def test_strategy_name_enum_matches_canonical_12():
    from app.strategies.names import ALL_STRATEGY_NAMES, StrategyName
    assert set(ALL_STRATEGY_NAMES) == CANONICAL_12
    assert len(ALL_STRATEGY_NAMES) == 12
    assert {m.value for m in StrategyName} == CANONICAL_12


def test_strategies_module_reexports_the_canonical_tuple():
    from app.strategies.names import ALL_STRATEGY_NAMES
    from app.strategies.strategies import STRATEGY_NAMES
    assert tuple(STRATEGY_NAMES) == ALL_STRATEGY_NAMES


def test_regime_rules_only_name_canonical_strategies():
    from app.services.trading.regime_rules import REGIME_RULES
    for regime, rule in REGIME_RULES.items():
        unknown = set(rule.enabled_strategies) - CANONICAL_12
        assert not unknown, f"{regime} names unknown strategies {unknown}"


def test_no_phantom_strategy_literal_under_app():
    hits = []
    for path in APP_DIR.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PHANTOM.search(line):
                hits.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}: {line.strip()}")
    assert not hits, "phantom strategy names found:\n" + "\n".join(hits)
