"""
Up to 40% of capital per trade.

``TradingParams.validate()`` bounds ``fixed_fraction_pct`` in (0, 40] in one
place (``MAX_FIXED_FRACTION_PCT``). Kelly is capped at
``min(kelly_fraction_cap_pct, fixed_fraction_pct)`` so it can only size DOWN from
the fixed fraction, and the old ``kelly_fraction_cap_pct >= fixed_fraction_pct``
rule (which made the live row's Kelly cap of 15 the effective ceiling) is gone.
Every other limit is unchanged: the 40% open-exposure cap means one ₹60-95
NIFTY position at a time on ₹15,500, and the pre-trade guards still apply.
"""
from datetime import time as dtime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import trading_params
from app.core.config_reconcile import overlay_from_row
from app.core.trading_params import TradingParams, validate_overrides
from app.db.models.paper_trade import PaperPosition, StrategyPerf
from app.db.models.risk import NoTradeDecision
from app.runtime.context import using
from app.runtime.live import params_from_row
from app.services.trading import fills, startup_checks
from app.services.trading.capital_check import capital_sufficiency_warnings, premium_ceiling
from app.services.trading.mode_manager import mode_manager
from app.services.trading.option_selector import option_selector
from app.services.trading.paper_engine import paper_engine
from app.services.trading.risk_gate import (
    FractionDecision, fraction_decision, round_to_500, sizing_decision,
)
from app.strategies.base import Direction
from tests.helpers import (
    allow_regime, fake_candidate, make_signal, option_row, pin_ist_time, run, seed_config,
    stub_select, with_cfg, with_instruments, with_params,
)

# The live trading_config row (read-only, 2026-10-11) with only the fraction raised.
LIVE_ROW_40 = dict(
    id=1, mode="PAPER", live_mode_confirmed=False,
    capital=15500.0, fixed_fraction_pct=40.0, trade_amount=None,
    max_lots_per_trade=3, max_trades_per_day=6, max_open_positions=2,
    max_daily_loss=500.0, max_daily_loss_pct=3.0, open_exposure_cap_pct=40.0,
    kelly_fraction_cap_pct=15.0, kelly_multiplier=0.25, kelly_min_trades=30,
    losing_streak_halt_n=3, strategy_streak_halt_n=2, post_loss_cooldown_minutes=15,
    min_rr=2.0, stale_tick_seconds=60, entry_cutoff_buffer_min=10, slippage_pct=0.2,
    square_off_time="15:15",
)
LIVE_ROW_BEFORE = dict(LIVE_ROW_40, fixed_fraction_pct=6.666667, trade_amount=1033.33)
LOT_NIFTY, LOT_BANKNIFTY = 65, 30



@pytest.fixture
def runtime_ctx(runtime_ctx):
    """These tests exercise Kelly sizing, which is opt-in (kelly_enabled=False
    by default since sizing became fixed-fraction only): turn it on here."""
    from app.runtime.context import using
    from tests.helpers import with_params as _wp
    rc = _wp(runtime_ctx, kelly_enabled=True)
    with using(rc):
        yield rc

def _ctx40(rc, **param_over):
    """*rc* with the live RunConfig and the post-update live TradingParams."""
    cfg = with_cfg(rc, capital=15500.0, max_open_positions=2, max_lots_per_trade=3,
                   max_trades_per_day=6, max_daily_loss=500.0, max_daily_loss_pct=3.0,
                   open_exposure_cap_pct=40.0)
    # hand-computed before the per-trade risk cap existed: pin it off here
    over = dict(overlay_from_row(LIVE_ROW_40), max_risk_per_trade_pct=0.0)
    return with_params(cfg, **dict(over, **param_over))


def _seed_live_row(Session):
    db_row = {k: v for k, v in LIVE_ROW_40.items() if k != "mode"}
    run(seed_config(Session, **db_row))


def _rows(Session, model):
    async def _all():
        async with Session() as db:
            return (await db.execute(select(model))).scalars().all()
    return run(_all())


def _decide(Session, strategy="VWAP_PULLBACK") -> FractionDecision:
    async def _go():
        async with Session() as db:
            return await fraction_decision(db, strategy)
    return run(_go())


# ── the bound ────────────────────────────────────────────────────────────────

BOUND_TEXT = "(0, 100]"     # validate()'s fixed_fraction_pct message names the bound


def test_the_bound_is_100_and_lives_in_one_place():
    # raised from 40% to 100%: the operator may size a trade on all capital
    assert getattr(trading_params, "MAX_FIXED_FRACTION_PCT", None) == 100.0
    assert TradingParams(fixed_fraction_pct=40.0).validate() == []
    assert TradingParams(fixed_fraction_pct=100.0).validate() == []
    for bad in (100.01, 0.0, -1.0):
        v = TradingParams(fixed_fraction_pct=bad).validate()
        assert v and all("fixed_fraction_pct" in x for x in v), v
        assert BOUND_TEXT in v[0]


@pytest.mark.parametrize("cap", [15.0, 25.0])
def test_40_validates_with_any_legal_kelly_cap(cap):
    assert TradingParams(fixed_fraction_pct=40.0, kelly_fraction_cap_pct=cap).validate() == []
    # the Kelly cap keeps its own bound
    assert any("kelly_fraction_cap_pct" in v for v in
               TradingParams(fixed_fraction_pct=40.0, kelly_fraction_cap_pct=25.01).validate())


def test_every_other_rule_still_applies_at_40():
    def _v(**kw):
        return TradingParams(**dict(overlay_from_row(LIVE_ROW_40), **kw)).validate()
    from app.services.trading.trailing import TrailTier
    assert any("max_lots_per_trade" in v for v in _v(max_lots_per_trade=1))   # partial exit
    assert any("min_rr" in v for v in _v(min_rr=1.9))                         # R:R floor
    unreachable = (TrailTier(1.0, 0.6, "T0"), TrailTier(0.5, 0.0, "T1"))
    assert any("trail_tiers" in v for v in _v(trail_tiers=unreachable))      # R:R ladder
    assert any("trade_amount" in v for v in _v(trade_amount=1033.33))        # stale cache
    assert any("max_daily_loss" in v for v in _v(max_daily_loss=20_000.0))


# ── the exact post-update live row ───────────────────────────────────────────

@pytest.mark.parametrize("trade_amount", [None, 6200.0])
def test_post_update_live_row_validates(trade_amount, runtime_ctx):
    row = dict(LIVE_ROW_40, trade_amount=trade_amount)
    assert validate_overrides(overlay_from_row(row)) == []
    tp = params_from_row(SimpleNamespace(**row))
    assert tp.validate() == []
    assert (tp.capital, tp.fixed_fraction_pct, tp.kelly_fraction_cap_pct) == (15500.0, 40.0, 15.0)
    assert tp.kelly_cap_effective_pct == 15.0
    # startup check 6 over the same params
    with using(with_params(runtime_ctx, **overlay_from_row(row))):
        assert startup_checks._check_params() == []


def test_pre_update_live_row_still_validates():
    assert validate_overrides(overlay_from_row(LIVE_ROW_BEFORE)) == []


def test_fraction_above_100_on_the_live_row_names_the_field():
    v = validate_overrides(overlay_from_row(dict(LIVE_ROW_40, fixed_fraction_pct=100.01)))
    assert v and all("fixed_fraction_pct" in x for x in v)
    # the bound itself, not just any message mentioning the field
    assert any("fixed_fraction_pct" in x and BOUND_TEXT in x for x in v), v


# ── base, ceiling ────────────────────────────────────────────────────────────

def test_base_is_round_to_500_of_40pct_of_15500(runtime_ctx, sqlite_db):
    with using(_ctx40(runtime_ctx)):
        d = _decide(sqlite_db)                     # no strategy_perf row
    assert d.method == "FIXED_FRACTION" and d.applied_fraction == pytest.approx(0.40)
    assert d.base_amount == round_to_500(0.40 * 15500) == 6000.0
    assert round(d.base_amount / LOT_NIFTY, 2) == 92.31
    assert d.base_amount / LOT_BANKNIFTY == 200.0
    tp = TradingParams(**overlay_from_row(LIVE_ROW_40))
    assert round(premium_ceiling(15500, tp, LOT_NIFTY), 2) == 92.31
    assert round(premium_ceiling(15500, tp, LOT_NIFTY, regime_multiplier=0.5), 2) == 46.15


def _chain(rc, strikes):
    return [option_row(k, rc.clock.trading_day() + timedelta(days=7)) for k in strikes]


def _tick(redis, token, ltp):
    redis.put_tick(token, ltp, bid=round(ltp - 0.05, 2), ask=ltp, age_s=1.0,
                   oi=300_000, volume=20_000)


@pytest.mark.parametrize("mult, below, above", [(1.0, 92.30, 92.35), (0.5, 46.15, 46.20)])
def test_the_selector_ceiling_is_base_times_multiplier_over_the_lot(runtime_ctx, fake_redis,
                                                                    mult, below, above):
    rows = _chain(runtime_ctx, (22600, 22650))
    _tick(fake_redis, rows[0].token, above)
    _tick(fake_redis, rows[1].token, below)
    tp = TradingParams(**overlay_from_row(LIVE_ROW_40))
    with using(with_instruments(_ctx40(runtime_ctx), rows)):
        res = run(option_selector.select(
            None, Direction.BUY, "NIFTY", 22600.0, sl_pct=0.15, target_pct=0.30, params=tp,
            base_amount=6000.0, regime_multiplier=mult))
    assert res.rejections == {"PREMIUM_ABOVE_AFFORDABLE": 1}
    assert res.candidate is not None and res.candidate.token == rows[1].token


def test_the_old_live_fraction_affords_none_of_them(runtime_ctx, fake_redis):
    """Before: 6.666667% of ₹15,500 → base ₹1,000 → ceiling ₹15.38, so ₹60-92 contracts
    were all PREMIUM_ABOVE_AFFORDABLE."""
    rows = _chain(runtime_ctx, (22600, 22650, 22700))
    for r, px in zip(rows, (92.0, 75.0, 60.0)):
        _tick(fake_redis, r.token, px)
    before = round_to_500(6.666667 / 100 * 15500)
    assert before == 1000.0
    with using(with_instruments(runtime_ctx, rows)):
        old = run(option_selector.select(
            None, Direction.BUY, "NIFTY", 22600.0, sl_pct=0.15, target_pct=0.30,
            params=TradingParams(), base_amount=before, regime_multiplier=1.0))
        new = run(option_selector.select(
            None, Direction.BUY, "NIFTY", 22600.0, sl_pct=0.15, target_pct=0.30,
            params=TradingParams(), base_amount=6000.0, regime_multiplier=1.0))
    assert old.candidate is None and old.rejections == {"PREMIUM_ABOVE_AFFORDABLE": 3}
    assert new.candidate is not None and new.rejections == {}


def test_startup_summary_logs_the_ceiling_sizing_applies(runtime_ctx, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="app.services.trading.option_selector")
    with using(with_instruments(_ctx40(runtime_ctx), _chain(runtime_ctx, (22600,)))):
        run(option_selector.log_startup_summary(None))
    (msg,) = [r.getMessage() for r in caplog.records if "NIFTY: expiries" in r.getMessage()
              and r.getMessage().startswith("[OptionSelector] NIFTY")]
    assert "premium ceiling (1.0x) ₹92.31" in msg          # not the unrounded 95.38


def test_capital_warning_reads_correctly_at_40pct():
    tp = TradingParams(**overlay_from_row(LIVE_ROW_40))
    (nifty,) = [w for w in capital_sufficiency_warnings(15500, tp, {"NIFTY": 65}) if "NIFTY" in w]
    assert "fixed_fraction_pct 40% (max 100%)" in nifty
    assert "base ₹6,000" in nifty and "92.31" in nifty
    assert "1 ATM lot needs ≈ ₹16,500" in nifty


# ── Kelly never sizes above the fixed fraction ───────────────────────────────

STRONG_EDGE = dict(winning_trades_r=25, losing_trades_r=5, sum_r_win=200.0, sum_r_loss=-5.0)
WEAK_EDGE = dict(winning_trades_r=16, losing_trades_r=14, sum_r_win=32.0, sum_r_loss=-14.0)


@pytest.mark.parametrize("edge", [STRONG_EDGE, WEAK_EDGE], ids=["strong", "weak"])
@pytest.mark.parametrize("fixed, cap", [(10.0, 15.0), (40.0, 15.0), (40.0, 25.0),
                                        (6.666667, 15.0), (20.0, 20.0)])
def test_kelly_never_exceeds_the_fixed_fraction(runtime_ctx, sqlite_db, fixed, cap, edge):
    async def _seed():
        async with sqlite_db() as db:
            db.add(StrategyPerf(strategy_name="VWAP_PULLBACK", run_id="PAPER", **edge))
            await db.commit()
    run(_seed())
    with using(_ctx40(runtime_ctx, fixed_fraction_pct=fixed, kelly_fraction_cap_pct=cap)):
        d = _decide(sqlite_db)
        cached = mode_manager._cached_kelly_fraction(SimpleNamespace(**edge))
    assert d.method == "KELLY" and d.reject is None
    assert d.applied_fraction <= min(fixed, cap) / 100.0 + 1e-12
    assert d.base_amount <= round_to_500(fixed / 100.0 * 15500.0)
    assert cached == round(d.applied_fraction, 6)
    if edge is STRONG_EDGE:                       # quarter Kelly ≈ 0.203: the cap binds
        assert d.applied_fraction == pytest.approx(min(fixed, cap, 20.3125) / 100.0)
        assert ("fraction_cap" in d.clamps) == (min(fixed, cap) < 20.3125)


# ── exposure: one position at a time ─────────────────────────────────────────

def test_second_concurrent_entry_is_refused_by_the_exposure_cap(runtime_ctx):
    """₹6,200 cap (40% of ₹15,500) − ₹5,850 open (one ₹90 lot) = ₹350 headroom."""
    with using(_ctx40(runtime_ctx)):
        first = sizing_decision(
            fraction=FractionDecision("FIXED_FRACTION", 0.40, 6000.0), regime_multiplier=1.0,
            expected_entry_fill=90.0, lot_size=65, available_capital=15500.0, open_exposure=0.0)
        second = sizing_decision(
            fraction=FractionDecision("FIXED_FRACTION", 0.40, 6000.0), regime_multiplier=1.0,
            expected_entry_fill=60.0, lot_size=65, available_capital=15500.0 - 5850.0,
            open_exposure=5850.0)
    assert (first.lots, first.capital_used, first.reject) == (1, 5850.0, None)
    assert second.lots == 0 and second.reject.value == "INSUFFICIENT_CAPITAL"
    assert second.binding_clamp == "open_exposure_cap"


@pytest.fixture
def live40(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    _seed_live_row(sqlite_db)
    pin_ist_time(monkeypatch, dtime(10, 30))
    allow_regime(monkeypatch)
    with using(_ctx40(runtime_ctx)):
        yield sqlite_db


def test_two_signals_end_to_end_open_one_position(live40, monkeypatch):
    stub_select(monkeypatch, fake_candidate(ltp=90.0, bid=89.90, ask=89.95, tick_age_s=1.0))
    assert run(paper_engine.handle_signal(make_signal(atr=7.6))) is True
    stub_select(monkeypatch, fake_candidate(ltp=60.0, bid=59.90, ask=59.95, tick_age_s=1.0,
                                            token="OPT2"))
    assert run(paper_engine.handle_signal(make_signal(atr=7.6))) is False

    (pos,) = _rows(live40, PaperPosition)
    assert pos.lots == 1 and pos.actual_entry == pytest.approx(90.0)        # ASK fill
    assert pos.trade_amount_used == pytest.approx(5850.0)
    assert pos.sizing_detail["trade_amount"] == 6000.0
    (d,) = [d for d in _rows(live40, NoTradeDecision) if d.stage != "DEGRADATION"]
    assert (d.stage, d.code) == ("SIZING", "INSUFFICIENT_CAPITAL")
    assert d.context["binding_clamp"] == "open_exposure_cap"


# ── the pre-trade guards do not refuse every realistic 40% entry ─────────────

@pytest.mark.parametrize("fill", [60.0, 92.0])
def test_costs_are_well_inside_the_edge_at_one_lot(fill):
    p = TradingParams()
    _, tgt, _, _ = fills.entry_levels(fill, 0.15, 0.30, 2.0)
    cost = fills.expected_round_trip_cost(fill, 65, 1, p)
    assert cost <= p.max_cost_fraction_of_edge * (tgt - fill) * 65


@pytest.mark.parametrize("ltp, ask", [(60.0, 59.95), (92.0, 91.95)])
def test_atr_guard_needs_spot_atr_7_5_and_is_unchanged(live40, monkeypatch, ltp, ask):
    """The NIFTY threshold is min(fill × sl_pct / δ / 2, 7.5) = 7.5 for these fills
    (δ 0.5); it depends on the market, so it does not refuse every entry."""
    stub_select(monkeypatch, fake_candidate(ltp=ltp, bid=ask - 0.05, ask=ask, tick_age_s=1.0))
    assert run(paper_engine.handle_signal(make_signal(atr=7.4))) is False
    (d,) = [d for d in _rows(live40, NoTradeDecision) if d.stage != "DEGRADATION"]
    assert d.code == "ATR_VS_SL_DISTANCE" and "7.5 needed" in d.reason
    assert run(paper_engine.handle_signal(make_signal(atr=7.6))) is True
    (pos,) = _rows(live40, PaperPosition)
    assert pos.lots == 1 and pos.trade_amount_used <= 6000.0
