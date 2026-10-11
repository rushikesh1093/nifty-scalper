"""Bug 6 (design §9.5, R7): Kelly b = avg_win_r / avg_loss_r over the R-valid,
non-R-flat population; quarter Kelly, capped at min(kelly_fraction_cap_pct 15%,
fixed_fraction_pct) so Kelly only ever sizes down; no floor, no edge → skip.

Tests whose Kelly fraction is above the 10% default fixed fraction run with
fixed_fraction_pct=15 so the arithmetic they pin (b, quarter Kelly, the 15% cap
clamp) is unchanged; tests/test_sizing_40pct.py pins the cap at the fraction."""
import math
from datetime import datetime, time as dtime, timezone
from types import SimpleNamespace

import pytest

from app.core.trading_params import TradingParams
from app.db.models.paper_trade import StrategyPerf, TradingMode
from app.runtime.context import trading_day, using
from app.services.trading import risk_gate
from app.services.trading.mode_manager import mode_manager
from app.services.trading.risk_gate import (
    FractionDecision, fraction_decision, kelly_from_stats, round_to_500, sizing_decision,
)
from app.services.trading.trade_outcome import build_outcome
from app.strategies.names import ALL_STRATEGY_NAMES
from tests.helpers import (
    allow_regime, fake_candidate, make_signal, pin_ist_time, regime_state, run, seed_config,
    stub_select, with_params,
)


def _seed_perf(Session, strategy="VWAP_PULLBACK", **cols):
    async def _go():
        async with Session() as db:
            db.add(StrategyPerf(strategy_name=strategy, run_id="PAPER", **cols))
            await db.commit()
    run(_go())


def _decide(Session, strategy="VWAP_PULLBACK") -> FractionDecision:
    async def _go():
        async with Session() as db:
            return await fraction_decision(db, strategy)
    return run(_go())


# ── the corrected b, against the legacy mean-R formula (R7.9) ────────────────

def _legacy_fraction(p: float, avg_rr: float) -> float:
    """The pre-FEAT-005 formula: b was the mean achieved R (avg_rr)."""
    return max(0.0, (p * avg_rr - (1 - p)) / avg_rr)


def test_b_is_avg_win_over_avg_loss_and_the_legacy_fraction_was_larger(runtime_ctx, sqlite_db):
    # 21 winners averaging +4R, 9 losers averaging −3R: 30 decided R-valid trades
    _seed_perf(sqlite_db, winning_trades_r=21, losing_trades_r=9, sum_r_win=84.0,
               sum_r_loss=-27.0, total_trades=30, winning_trades=21, win_rate=70.0,
               avg_rr=(84.0 - 27.0) / 30)
    with using(with_params(runtime_ctx, fixed_fraction_pct=15.0)):     # 11.875% < 15%
        d = _decide(sqlite_db)
    assert d.method == "KELLY"
    assert d.p == pytest.approx(0.7)
    assert d.avg_win == pytest.approx(4.0) and d.avg_loss == pytest.approx(3.0)
    assert d.b == pytest.approx(4.0 / 3.0)
    assert d.kelly_full == pytest.approx((0.7 * (4 / 3) - 0.3) / (4 / 3))      # 0.475
    assert d.applied_fraction == pytest.approx(0.25 * d.kelly_full)            # quarter Kelly
    assert d.base_amount == round_to_500(d.applied_fraction * 200_000.0) == 23_500.0
    legacy_b = (84.0 - 27.0) / 30                                              # mean R = 1.9
    assert _legacy_fraction(0.7, legacy_b) > d.kelly_full                      # mean-R overbet


def test_fraction_cap_binds_and_is_recorded(runtime_ctx, sqlite_db):
    _seed_perf(sqlite_db, winning_trades_r=25, losing_trades_r=5, sum_r_win=200.0,
               sum_r_loss=-5.0)                                    # b = 8, kelly ≈ 0.81
    with using(with_params(runtime_ctx, fixed_fraction_pct=15.0)):
        d = _decide(sqlite_db)
    assert d.applied_fraction == pytest.approx(0.15) and "fraction_cap" in d.clamps
    assert d.base_amount == 30_000.0


def test_kelly_multiplier_is_data_not_a_label(runtime_ctx, sqlite_db):
    _seed_perf(sqlite_db, winning_trades_r=20, losing_trades_r=20, sum_r_win=40.0,
               sum_r_loss=-20.0)                                   # p=.5, b=2, kelly=.25
    with using(with_params(runtime_ctx, kelly_multiplier=0.5, fixed_fraction_pct=15.0)):
        d = _decide(sqlite_db)
    assert d.method == "KELLY" and d.kelly_multiplier == 0.5
    assert d.applied_fraction == pytest.approx(0.125)


# ── guard parametrisations, evaluated before any division ────────────────────

@pytest.mark.parametrize("cols, expected", [
    # (i) fewer than kelly_min_trades decided R-valid trades
    (dict(winning_trades_r=10, losing_trades_r=10, sum_r_win=30.0, sum_r_loss=-10.0),
     "FIXED_FRACTION"),
    # (ii) losing_trades_r == 0: avg_loss is undefined. 30 R-valid winners and four
    #      losers that are all BACKFILL_UNVERIFIED (losing_trades=4 but losing_trades_r=0)
    (dict(winning_trades_r=30, losing_trades_r=0, sum_r_win=60.0, sum_r_loss=0.0,
          losing_trades=4, total_trades=34),
     "FIXED_FRACTION"),
    # (iii) winning_trades_r == 0 with a full sample: p == 0
    (dict(winning_trades_r=0, losing_trades_r=30, sum_r_win=0.0, sum_r_loss=-30.0),
     "KELLY_NO_EDGE"),
])
def test_guards_run_before_any_division(runtime_ctx, sqlite_db, cols, expected):
    _seed_perf(sqlite_db, **cols)
    d = _decide(sqlite_db)                        # must not raise ZeroDivisionError
    assert (d.reject.value if d.reject else d.method) == expected


def test_no_perf_row_is_the_fixed_fraction(runtime_ctx, sqlite_db):
    d = _decide(sqlite_db)
    assert d.method == "FIXED_FRACTION" and d.applied_fraction == pytest.approx(0.10)
    assert d.base_amount == 20_000.0 and d.reject is None


def test_negative_edge_is_skipped_not_floored(runtime_ctx, sqlite_db):
    # p=.5, b=.5 → kelly = −0.5: the old code sized this UP to a 10% floor
    _seed_perf(sqlite_db, winning_trades_r=15, losing_trades_r=15, sum_r_win=15.0,
               sum_r_loss=-30.0)
    d = _decide(sqlite_db)
    assert d.reject is not None and d.reject.value == "KELLY_NO_EDGE"
    assert d.base_amount == 0.0 and d.kelly_full < 0


def test_no_edge_skips_the_trade_end_to_end(runtime_ctx, sqlite_db, monkeypatch):
    from sqlalchemy import select
    from app.db.models.paper_trade import PaperPosition
    from app.db.models.risk import NoTradeDecision
    from app.services.trading.paper_engine import paper_engine

    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))
    _seed_perf(sqlite_db, winning_trades_r=15, losing_trades_r=15, sum_r_win=15.0,
               sum_r_loss=-30.0)
    assert run(paper_engine.handle_signal(make_signal())) is False

    async def _rows():
        async with sqlite_db() as db:
            return ((await db.execute(select(PaperPosition))).scalars().all(),
                    (await db.execute(select(NoTradeDecision))).scalars().all())
    positions, decisions = run(_rows())
    assert positions == []
    (d,) = decisions
    assert (d.stage, d.code) == ("SIZING", "KELLY_NO_EDGE")


# ── which trades feed the Kelly sample ───────────────────────────────────────

def _outcome(net, r, source="ENTRY", risk=30.0, pid=1, strategy="VWAP_PULLBACK"):
    p = TradingParams()
    pos = SimpleNamespace(
        id=pid, strategy_name=strategy, underlying="NIFTY", entry_regime=None,
        mode=TradingMode.PAPER, net_pnl=net, gross_pnl=net, brokerage=0.0,
        statutory_charges=0.0, r_source=source, initial_risk=risk, r_multiple=r,
        exit_value_weighted=100.0, hold_minutes=10.0, original_lots=1, lots=1,
        exit_time=datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc))
    return build_outcome(pos, run_id="PAPER", trading_day=trading_day(), params=p)


def test_flats_backfill_and_breakeven_exits_are_excluded_from_decided_r(runtime_ctx, sqlite_db):
    outcomes = [
        _outcome(+900.0, 2.0, pid=1),                          # a real win
        _outcome(+0.4, 0.004, pid=2),                          # P&L-flat AND R-flat
        _outcome(+500.0, 1.5, source="BACKFILL_UNVERIFIED", pid=3),   # not R-valid at all
        _outcome(-60.0, 0.0, pid=4),                           # breakeven exit: gross R 0, net −60
    ]

    async def _apply():
        async with sqlite_db() as db:
            for o in outcomes:
                await mode_manager.update_strategy_perf(db, "VWAP_PULLBACK", o)
            await db.commit()
            from sqlalchemy import select
            return (await db.execute(select(StrategyPerf))).scalar_one()
    perf = run(_apply())

    assert (perf.winning_trades_r, perf.losing_trades_r) == (1, 0)     # decided_r = 1
    assert perf.sum_r_win == pytest.approx(2.0) and perf.sum_r_loss == 0.0
    assert perf.r_sample_size == 3        # win + R-flat + breakeven are R-valid; backfill is not
    assert perf.total_trades == 4
    # …while win rate and the streak (money statements) see the breakeven exit as a LOSS
    assert (perf.winning_trades, perf.losing_trades, perf.flat_trades) == (2, 1, 1)


def test_one_hundred_trades_across_twelve_strategies_stay_on_the_fixed_fraction(runtime_ctx,
                                                                                sqlite_db):
    """~8 trades per strategy is far below kelly_min_trades=30: Kelly is inert for the
    whole first validation window, and the counts say so."""
    counts = {"FIXED_FRACTION": 0, "KELLY": 0}

    async def _run_100():
        async with sqlite_db() as db:
            for i in range(100):
                name = ALL_STRATEGY_NAMES[i % 12]
                counts[(await fraction_decision(db, name)).method] += 1
                await mode_manager.update_strategy_perf(
                    db, name, _outcome(+300.0 if i % 3 else -300.0, 1.0 if i % 3 else -1.0,
                                       pid=i + 1, strategy=name))
                await db.commit()
    run(_run_100())
    assert counts == {"FIXED_FRACTION": 100, "KELLY": 0}


# ── sizing arithmetic ────────────────────────────────────────────────────────

def test_regime_multiplier_is_applied_exactly_once(runtime_ctx):
    fraction = FractionDecision(method="FIXED_FRACTION", applied_fraction=0.10,
                                base_amount=20_000.0)
    full = sizing_decision(fraction=fraction, regime_multiplier=1.0, expected_entry_fill=40.0,
                           lot_size=65, available_capital=200_000.0, open_exposure=0.0)
    half = sizing_decision(fraction=fraction, regime_multiplier=0.5, expected_entry_fill=40.0,
                           lot_size=65, available_capital=200_000.0, open_exposure=0.0)
    assert full.trade_amount == 20_000.0 and half.trade_amount == 10_000.0   # not 5,000
    assert half.fraction.base_amount == 20_000.0          # the stored base stays unadjusted
    assert "regime_multiplier" in half.clamps


@pytest.mark.parametrize("x", [0.0, 1.0, 499.99, 500.0, 12_345.67, 23_750.0, 29_999.99])
def test_round_to_500_never_rounds_up(x):
    assert round_to_500(x) <= x and round_to_500(x) % 500 == 0
    assert x - round_to_500(x) < 500
    assert round_to_500(-5.0) == 0.0


def test_kelly_params_validate_bounds():
    assert TradingParams(kelly_multiplier=0.5).validate() == []
    assert any("kelly_multiplier" in v for v in TradingParams(kelly_multiplier=0.6).validate())
    assert any("kelly_multiplier" in v for v in TradingParams(kelly_multiplier=0.0).validate())
    assert any("kelly_fraction_cap_pct" in v
               for v in TradingParams(kelly_fraction_cap_pct=30.0).validate())
    with pytest.raises(Exception):
        TradingParams.from_mapping({"fraction_floor": 0.1})     # a floor sizes UP a no-edge strategy


def test_kelly_from_stats_is_pure_and_guarded(runtime_ctx):
    assert kelly_from_stats(10, 10, 20.0, -10.0) == "FIXED_FRACTION"
    assert kelly_from_stats(30, 0, 60.0, 0.0) == "FIXED_FRACTION"
    assert kelly_from_stats(0, 30, 0.0, -30.0) == "NO_EDGE"
    p, aw, al, b, k = kelly_from_stats(20, 20, 40.0, -20.0)
    assert (p, aw, al, b) == (0.5, 2.0, 1.0, 2.0) and math.isclose(k, 0.25)
