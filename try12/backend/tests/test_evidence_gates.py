"""
Evidence gates in the risk gate (risk_gate._evidence_refusal): strategies the
research or their own paper trades show losing are refused, and EMA/RSI skips
entries in LOW volatility. Each gate is a TradingParams switch.
"""
from types import SimpleNamespace

from app.core.trading_params import TradingParams
from app.runtime.context import using
from app.services.trading import risk_gate
from app.services.trading.no_trade import ReasonCode
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.strategies.base import Direction
from tests.helpers import make_signal, run, with_params


def _refusal(sqlite_db, rc, strategy, **params):
    sig = make_signal(strategy=strategy, direction=Direction.BUY)

    async def go():
        async with sqlite_db() as db:
            with using(with_params(rc, **params)):
                from app.runtime.context import params as p
                return await risk_gate._evidence_refusal(db, sig, p())
    return run(go())


def test_defaults():
    p = TradingParams()
    assert p.evidence_blocked_strategies == ("SUPERTREND_EMA",)
    assert p.evidence_auto_block is True
    assert p.low_vol_skip_strategies == ("EMA_RSI_PULLBACK_15M",)
    assert any("evidence_blocked_strategies" in v
               for v in TradingParams(evidence_blocked_strategies=("NOPE",)).validate())


def test_supertrend_is_refused_by_research_evidence(runtime_ctx, sqlite_db):
    d = _refusal(sqlite_db, runtime_ctx, "SUPERTREND_EMA")
    assert d is not None and d.code == ReasonCode.EVIDENCE_BLOCKED
    assert _refusal(sqlite_db, runtime_ctx, "SUPERTREND_EMA", evidence_blocked_strategies=()) is None
    assert _refusal(sqlite_db, runtime_ctx, "ORB_BREAKOUT") is None


def test_auto_block_from_paper_evidence(runtime_ctx, sqlite_db, monkeypatch):
    from app.services.metrics import strategy_evidence
    monkeypatch.setattr(strategy_evidence, "evaluate", lambda log: {
        "PIVOT_BOUNCE": {"status": "DISABLED_FOR_RESEARCH"},
        "ORB_BREAKOUT": {"status": "INSUFFICIENT_EVIDENCE"}})
    d = _refusal(sqlite_db, runtime_ctx, "PIVOT_BOUNCE")
    assert d is not None and d.code == ReasonCode.EVIDENCE_BLOCKED and d.context["source"] == "paper"
    assert _refusal(sqlite_db, runtime_ctx, "ORB_BREAKOUT") is None
    risk_gate._evidence_cache.clear()
    assert _refusal(sqlite_db, runtime_ctx, "PIVOT_BOUNCE", evidence_auto_block=False) is None


def test_evidence_failure_never_blocks(runtime_ctx, sqlite_db, monkeypatch):
    from app.services.metrics import strategy_evidence

    def boom(log):
        raise RuntimeError("no stats")
    monkeypatch.setattr(strategy_evidence, "evaluate", boom)
    assert _refusal(sqlite_db, runtime_ctx, "PIVOT_BOUNCE") is None


def test_ema_rsi_skips_low_volatility_only(runtime_ctx, sqlite_db):
    strategy_orchestrator.set_regime_v2("NIFTY", SimpleNamespace(vol="LOW", atr_percentile=12.0))
    d = _refusal(sqlite_db, runtime_ctx, "EMA_RSI_PULLBACK_15M")
    assert d is not None and d.code == ReasonCode.LOW_VOL_SKIP
    assert _refusal(sqlite_db, runtime_ctx, "PIVOT_BOUNCE") is None          # not listed
    strategy_orchestrator.set_regime_v2("NIFTY", SimpleNamespace(vol="NORMAL", atr_percentile=50.0))
    assert _refusal(sqlite_db, runtime_ctx, "EMA_RSI_PULLBACK_15M") is None
    strategy_orchestrator.set_regime_v2("NIFTY", None)                         # no reading: no skip
    assert _refusal(sqlite_db, runtime_ctx, "EMA_RSI_PULLBACK_15M") is None
