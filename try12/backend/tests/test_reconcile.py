"""reconcile_performance (design §7, §9.5): an idempotent rebuild of
strategy_perf, regime_performance and daily_pnl_summary from closed
paper_positions, with net-P&L outcome classes and R-sign buckets."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models.paper_trade import DailyPnLSummary, StrategyPerf, TradingMode
from app.db.models.regime import RegimePerformanceModel
from tests.helpers import add_position, run, seed_config

T0 = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)      # 09:30 IST

_SKIP = {"id", "updated_at", "created_at"}


def _closed(Session, *, minute, net, gross=None, brok=40.0, charges=0.0, r=None,
            r_source="ENTRY", risk=15.0, strategy="ORB", regime="BULL_TREND_NORMAL_VOL",
            run_id="PAPER", lots=1):
    gross = net + brok + charges if gross is None else gross
    return run(add_position(
        Session, strategy_name=strategy, is_open=False, entry_regime=regime, run_id=run_id,
        entry_time=T0 + timedelta(minutes=minute), exit_time=T0 + timedelta(minutes=minute + 5),
        net_pnl=net, gross_pnl=gross, brokerage=brok, statutory_charges=charges,
        r_multiple=r, r_source=r_source, initial_risk=risk, lots=lots, original_lots=lots,
    ))


async def _snapshot(Session):
    out = {}
    async with Session() as db:
        for model in (StrategyPerf, RegimePerformanceModel, DailyPnLSummary):
            rows = (await db.execute(select(model))).scalars().all()
            out[model.__tablename__] = sorted(
                (tuple(sorted((c.key, _plain(getattr(r, c.key))) for c in model.__table__.columns
                              if c.key not in _SKIP)) for r in rows),
                key=repr,
            )
    return out


def _plain(v):
    return getattr(v, "value", v)


async def _one(Session, model, **where):
    async with Session() as db:
        q = select(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return (await db.execute(q)).scalar_one()


@pytest.fixture
def dataset(runtime_ctx, sqlite_db):
    run(seed_config(sqlite_db))
    _closed(sqlite_db, minute=0, net=300.0, r=2.0)                     # win, R_WIN
    _closed(sqlite_db, minute=10, net=-200.0, r=-1.0)                  # loss, R_LOSS
    _closed(sqlite_db, minute=20, net=-100.0, r=0.0)                   # breakeven: loss, R_FLAT
    _closed(sqlite_db, minute=30, net=0.5, r=0.004)                    # P&L flat, R_FLAT
    _closed(sqlite_db, minute=40, net=500.0, r=3.0, r_source="BACKFILL_UNVERIFIED")  # no R
    _closed(sqlite_db, minute=50, net=-150.0, r=None, risk=None)       # no R
    _closed(sqlite_db, minute=60, net=250.0, r=1.5, charges=10.0, strategy="VWAP_EMA_PULLBACK",
            regime="RANGE_LOW_VOL")
    _closed(sqlite_db, minute=0, net=999.0, r=5.0, run_id="REPLAY1")   # other run, untouched
    run(add_position(sqlite_db, strategy_name="ORB", is_open=True, run_id="PAPER"))  # open: ignored
    return sqlite_db


def test_reconcile_twice_is_identical(dataset):
    from app.services.trading.reconcile import reconcile_performance

    first = run(reconcile_performance("PAPER"))
    snap1 = run(_snapshot(dataset))
    second = run(reconcile_performance("PAPER"))
    snap2 = run(_snapshot(dataset))
    assert snap1 == snap2
    assert second.changed_keys == []
    assert first.after == second.after == second.before
    assert first.after["strategy_perf"] == 2


def test_reconcile_strategy_perf_buckets(dataset):
    from app.services.trading.reconcile import reconcile_performance

    run(reconcile_performance("PAPER"))
    sp = run(_one(dataset, StrategyPerf, strategy_name="ORB", run_id="PAPER"))
    assert sp.total_trades == 6
    # net-P&L classes (FLAT_EPSILON = ₹1): breakeven is a loss, +0.5 is flat
    assert (sp.winning_trades, sp.losing_trades, sp.flat_trades) == (2, 3, 1)
    assert sp.win_rate == pytest.approx(2 / 6 * 100)
    # R-sign buckets over R-valid rows only (r_source ENTRY, initial_risk > 0, r set)
    assert sp.r_sample_size == 4
    assert (sp.winning_trades_r, sp.losing_trades_r) == (1, 1)
    assert sp.sum_r == pytest.approx(2.0 - 1.0 + 0.0 + 0.004)
    assert sp.sum_r_win == pytest.approx(2.0) and sp.sum_r_loss == pytest.approx(-1.0)
    assert sp.avg_win_r == pytest.approx(2.0) and sp.avg_loss_r == pytest.approx(1.0)
    assert sp.expectancy_r == pytest.approx(1.004 / 4)
    assert sp.avg_rr == pytest.approx(1.004 / 4)
    assert sp.expectancy_inr == pytest.approx((300 - 200 - 100 + 0.5 + 500 - 150) / 6)
    assert sp.gross_profit == pytest.approx(800.5) and sp.gross_loss == pytest.approx(450.0)

    other = run(_one(dataset, StrategyPerf, strategy_name="VWAP_EMA_PULLBACK", run_id="PAPER"))
    assert (other.total_trades, other.winning_trades_r) == (1, 1)


def test_reconcile_regime_and_daily(dataset):
    from app.services.trading.reconcile import reconcile_performance

    run(reconcile_performance("PAPER"))
    rp = run(_one(dataset, RegimePerformanceModel, regime="BULL_TREND_NORMAL_VOL",
                  strategy_name="ORB", run_id="PAPER"))
    assert (rp.total_trades, rp.winning_trades, rp.losing_trades, rp.flat_trades) == (6, 2, 3, 1)
    assert float(rp.total_pnl) == pytest.approx(350.5)
    assert rp.avg_r_multiple == pytest.approx(1.004 / 4)
    assert float(rp.avg_rr) == pytest.approx(1.004 / 4, abs=1e-3)   # Numeric(6,3) legacy column
    assert rp.first_trade_at is not None and rp.last_trade_at is not None

    dp = run(_one(dataset, DailyPnLSummary, strategy_name="ORB", run_id="PAPER"))
    assert dp.mode == TradingMode.PAPER
    assert dp.total_trades == 6 and dp.flat_trades == 1
    assert dp.worst_trade_loss == pytest.approx(200.0)
    # cumulative net: 300, 100, 0, 0.5, 500.5, 350.5 → deepest fall from a peak is 300
    assert dp.max_equity_drawdown == pytest.approx(300.0)
    assert dp.gross_pnl - dp.brokerage - dp.statutory_charges == pytest.approx(dp.net_pnl)

    vw = run(_one(dataset, DailyPnLSummary, strategy_name="VWAP_EMA_PULLBACK", run_id="PAPER"))
    assert vw.statutory_charges == pytest.approx(10.0)
    assert vw.worst_trade_loss is None and vw.max_equity_drawdown == pytest.approx(0.0)


def test_reconcile_leaves_other_runs_alone(dataset):
    from app.services.trading.reconcile import reconcile_performance

    run(reconcile_performance("REPLAY1"))
    before = run(_snapshot(dataset))
    run(reconcile_performance("PAPER"))
    after = run(_snapshot(dataset))
    replay_rows = [r for r in after["strategy_perf"] if ("run_id", "REPLAY1") in r]
    assert replay_rows == [r for r in before["strategy_perf"] if ("run_id", "REPLAY1") in r]
    assert len(replay_rows) == 1


def test_reconcile_reports_and_repairs_drift(dataset):
    from app.services.trading.reconcile import reconcile_performance

    run(reconcile_performance("PAPER"))

    async def _corrupt():
        async with dataset() as db:
            sp = (await db.execute(select(StrategyPerf).where(
                StrategyPerf.strategy_name == "ORB"))).scalar_one()
            sp.total_trades = 99
            await db.commit()

    run(_corrupt())
    report = run(reconcile_performance("PAPER"))
    assert ("strategy_perf", ("ORB", "PAPER")) in report.changed_keys
    assert run(_one(dataset, StrategyPerf, strategy_name="ORB", run_id="PAPER")).total_trades == 6


def test_incremental_totals_equal_recomputed(runtime_ctx, sqlite_db):
    """The counters the live close path maintains match a full rebuild."""
    from app.services.trading.paper_engine import paper_engine
    from app.services.trading.reconcile import reconcile_performance

    run(seed_config(sqlite_db, used_capital=19500.0))
    pids = [run(add_position(sqlite_db, entry_regime="BULL_TREND_NORMAL_VOL",
                             entry_time=T0 + timedelta(minutes=i))) for i in range(3)]
    for pid, px in zip(pids, (130.0, 84.0, 120.0)):
        run(paper_engine._close_position(pid, px, "MANUAL"))

    incremental = run(_snapshot(sqlite_db))
    run(reconcile_performance("PAPER"))
    rebuilt = run(_snapshot(sqlite_db))

    def _pick(snap, table, keys):
        return [{k: v for k, v in row if k in keys} for row in snap[table]]

    daily_keys = {"date", "strategy_name", "run_id", "total_trades", "winning_trades",
                  "losing_trades", "net_pnl", "gross_pnl", "brokerage"}
    assert _pick(incremental, "daily_pnl_summary", daily_keys) == \
        _pick(rebuilt, "daily_pnl_summary", daily_keys)
    perf_keys = {"strategy_name", "run_id", "total_trades", "winning_trades"}
    assert _pick(incremental, "strategy_perf", perf_keys) == _pick(rebuilt, "strategy_perf", perf_keys)
    regime_keys = {"regime", "underlying", "strategy_name", "run_id", "total_trades",
                   "winning_trades", "losing_trades"}
    assert _pick(incremental, "regime_performance", regime_keys) == \
        _pick(rebuilt, "regime_performance", regime_keys)
