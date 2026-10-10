"""Bug 3 (design §7, R4): a closed position writes regime_performance,
strategy_perf and daily_pnl_summary on the close's own session, as one
TradeOutcome; a failed stats write still closes the position, leaves one
PERF_WRITE_FAILED row, and reconcile restores the aggregates."""
from datetime import time as dtime

import pytest
from sqlalchemy import select

from app.db.models.paper_trade import DailyPnLSummary, PaperPosition, StrategyPerf
from app.db.models.regime import RegimePerformanceModel
from app.db.models.risk import NoTradeDecision
from app.services.trading import paper_engine as pe_mod
from app.services.trading.mode_manager import mode_manager
from app.services.trading.option_selector import option_selector
from app.services.trading.paper_engine import paper_engine
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.runtime.context import using
from tests.helpers import (
    add_position, allow_regime, fake_candidate, get_position, make_signal, pin_ist_time, run,
    seed_config, stub_select, with_cfg,
)

REGIME = "BULL_TREND_NORMAL_VOL"


async def _all(Session, model):
    async with Session() as db:
        return (await db.execute(select(model))).scalars().all()


def _rows(Session, model):
    return run(_all(Session, model))


def _open_two_lots(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    """Signal → selection → sizing → a 2-lot position, through handle_signal."""
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 30))
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))
    allow_regime(monkeypatch, REGIME)
    # ₹60,000 capital × 10% fixed fraction = ₹6,000: 2 lots at 40.08 × 65 = ₹2,605.2
    with using(with_cfg(runtime_ctx, capital=60_000.0)):
        assert run(paper_engine.handle_signal(make_signal())) is True
    (pos,) = _rows(sqlite_db, PaperPosition)
    assert pos.lots == 2 and pos.entry_regime == REGIME
    return pos


def _walk(sqlite_db, fake_redis, pid, prices):
    for px in prices:
        fake_redis.put_tick("OPT1", px, age_s=1)
        pos = run(get_position(sqlite_db, pid))
        if pos.is_open:
            run(paper_engine._check_position(pos, dtime(11, 0), dtime(15, 15)))
    return run(get_position(sqlite_db, pid))


def test_signal_open_trail_partial_close_writes_every_aggregate(runtime_ctx, sqlite_db,
                                                                fake_redis, monkeypatch):
    pos = _open_two_lots(runtime_ctx, sqlite_db, fake_redis, monkeypatch)
    entry, tgt = pos.actual_entry, pos.target
    reward = tgt - entry
    trail_px = round(entry + 0.55 * reward, 2)          # BREAKEVEN
    partial_px = round(entry + 0.80 * reward, 2)        # partial + LOCK_40
    closed = _walk(sqlite_db, fake_redis, pos.id, [trail_px, partial_px, tgt + 0.1])
    assert not closed.is_open and closed.exit_reason == "TARGET" and closed.partial_closed
    r = closed.r_multiple
    assert r is not None and r > 0.01

    (rp,) = _rows(sqlite_db, RegimePerformanceModel)
    assert (rp.regime, rp.underlying, rp.strategy_name, rp.run_id) == \
        (REGIME, "NIFTY", "VWAP_PULLBACK", "PAPER")
    assert (rp.total_trades, rp.winning_trades, rp.losing_trades, rp.flat_trades) == (1, 1, 0, 0)
    assert float(rp.total_pnl) == pytest.approx(closed.net_pnl, abs=0.01)
    assert rp.r_sample_size == 1 and rp.sum_r == pytest.approx(r) and rp.sum_r_win == pytest.approx(r)
    assert rp.sum_r_loss == 0.0 and rp.avg_r_multiple == pytest.approx(r)
    assert rp.expectancy_inr == pytest.approx(closed.net_pnl)
    assert rp.gross_profit == pytest.approx(closed.net_pnl) and rp.gross_loss == 0.0

    (sp,) = _rows(sqlite_db, StrategyPerf)
    assert (sp.strategy_name, sp.total_trades, sp.winning_trades, sp.losing_trades) == \
        ("VWAP_PULLBACK", 1, 1, 0)
    assert (sp.winning_trades_r, sp.losing_trades_r, sp.r_sample_size) == (1, 0, 1)
    assert sp.avg_win_r == pytest.approx(r) and sp.avg_loss_r is None
    assert sp.expectancy_r == pytest.approx(r) and sp.avg_rr == pytest.approx(r)

    (dp,) = _rows(sqlite_db, DailyPnLSummary)
    assert dp.total_trades == 1 and dp.winning_trades == 1      # partial + final = ONE result
    assert dp.statutory_charges == pytest.approx(closed.statutory_charges)
    assert dp.total_lots == 2                                   # original lots, not the remainder
    assert dp.gross_pnl - dp.brokerage - dp.statutory_charges == pytest.approx(dp.net_pnl, abs=0.01)
    assert dp.max_equity_drawdown == pytest.approx(0.0)


def test_incremental_aggregates_equal_a_full_rebuild(runtime_ctx, sqlite_db, fake_redis):
    """Every column the close path maintains matches reconcile (R15.6)."""
    from app.services.trading.reconcile import reconcile_performance

    run(seed_config(sqlite_db, used_capital=26000.0))
    common = dict(entry_regime=REGIME, initial_risk=15.0, r_source="ENTRY",
                  original_quantity=65, original_lots=1)
    plan = [("VWAP_PULLBACK", 130.0, "TARGET"), ("VWAP_PULLBACK", 84.0, "SL"),
            ("ORB_VWAP", 100.4, "MANUAL"), ("VWAP_PULLBACK", 112.0, "MANUAL")]
    for strategy, px, reason in plan:
        pid = run(add_position(sqlite_db, strategy_name=strategy, **common))
        fake_redis.put_tick("OPT1", px, age_s=1)
        run(paper_engine._close_position(pid, px, reason))
        runtime_ctx.clock.advance(60)
    report = run(reconcile_performance("PAPER"))
    assert report.changed_keys == []
    assert report.before == report.after == {"strategy_perf": 2, "regime_performance": 2,
                                             "daily_pnl_summary": 2}


def test_stats_write_failure_still_closes_and_reconcile_restores(runtime_ctx, sqlite_db,
                                                                 fake_redis, monkeypatch):
    from app.services.trading.reconcile import reconcile_performance

    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, entry_regime=REGIME, initial_risk=15.0, r_source="ENTRY",
                           original_quantity=65, original_lots=1))

    async def _boom(*a, **k):
        raise RuntimeError("strategy_perf write failed")

    original = mode_manager.update_strategy_perf
    monkeypatch.setattr(mode_manager, "update_strategy_perf", _boom)
    fake_redis.put_tick("OPT1", 130.0, age_s=1)
    run(paper_engine._close_position(pid, 130.0, "TARGET"))

    pos = run(get_position(sqlite_db, pid))
    assert pos.is_open is False and pos.exit_reason == "TARGET"
    assert pos.r_multiple is not None and pos.net_pnl is not None
    assert len(fake_redis.events("POSITION_CLOSED")) == 1
    failed = [d for d in _rows(sqlite_db, NoTradeDecision) if d.code == "PERF_WRITE_FAILED"]
    assert len(failed) == 1 and failed[0].position_id == pid and failed[0].stage == "DEGRADATION"
    # the stats transaction rolled back as a whole: nothing half-written
    assert _rows(sqlite_db, RegimePerformanceModel) == []
    assert _rows(sqlite_db, StrategyPerf) == []
    assert _rows(sqlite_db, DailyPnLSummary) == []

    monkeypatch.setattr(mode_manager, "update_strategy_perf", original)
    run(reconcile_performance("PAPER"))
    (sp,) = _rows(sqlite_db, StrategyPerf)
    (rp,) = _rows(sqlite_db, RegimePerformanceModel)
    assert sp.total_trades == 1 and rp.total_trades == 1 and sp.winning_trades_r == 1


def test_record_trade_result_requires_a_session():
    from app.services.trading.trade_outcome import TradeOutcome
    with pytest.raises(TypeError):
        run(strategy_orchestrator.record_trade_result(
            None, regime=REGIME, underlying="NIFTY", strategy_name="ORB_VWAP",
            outcome=None))
    assert TradeOutcome.__dataclass_params__.frozen


def test_daily_summary_net_identity_across_closes(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=19500.0))
    for px, reason in ((130.0, "TARGET"), (84.0, "SL"), (107.3, "MANUAL")):
        pid = run(add_position(sqlite_db, initial_risk=15.0, r_source="ENTRY",
                               original_quantity=65, original_lots=1))
        fake_redis.put_tick("OPT1", px, bid=round(px - 0.2, 2), ask=round(px + 0.2, 2), age_s=1)
        run(paper_engine._close_position(pid, px, reason))
    (dp,) = _rows(sqlite_db, DailyPnLSummary)
    positions = _rows(sqlite_db, PaperPosition)
    assert dp.total_trades == 3
    assert dp.gross_pnl - dp.brokerage - dp.statutory_charges == pytest.approx(dp.net_pnl, abs=0.01)
    for field in ("gross_pnl", "brokerage", "statutory_charges", "net_pnl"):
        assert getattr(dp, field) == pytest.approx(sum(getattr(p, field) for p in positions),
                                                   abs=0.01)
    assert dp.worst_trade_loss == pytest.approx(-min(p.net_pnl for p in positions))
