"""
ReplayEngine over the production code path (bug 11; design §14, R13).

The four-trade dataset and its runs are built once per session by the
``four_trade`` fixture (tests/replay_fixtures.py, where the price path is
documented). Every expected number below is computed by hand from that path:
fills from §15 (entry ceil_tick(ask x 1.0005), valid-quote exit
floor_tick(bid x 0.9995), TARGET trigger x 0.999), R against the initial risk
(§8.4), charges from the dated §15 schedule.
"""
import math

import pytest
from sqlalchemy import func, select

from app.db.models.instrument import Instrument
from app.db.models.paper_trade import TradingConfig, TradingMode
from app.db.models.replay import ReplayRun
from app.db.models.risk import CycleStats, NoTradeDecision, PositionEvent
from tests.replay_fixtures import (
    DAY1, DAY2, N_CANDLES, add_rows, assert_complete, fill_db, isolate_singletons, make_db,
    minute_index, pin_regime, positions, replay, run, scripted_factory, scripted_quote,
)

QTY, LOTS, ENTRY, STOP, TARGET, RISK = 195, 3, 100.00, 85.00, 130.00, 15.00
BROKERAGE = 120.0                 # ₹20/lot/leg x 3 lots x 2 legs (partial: 60 + 20 + 40)

def charges(buy_value: float, sell_value: float, brokerage: float) -> float:
    """§15 statutory schedule, written out: STT 0.15% sell, exchange 0.03553%
    turnover, SEBI ₹10/crore, stamp 0.003% buy, GST 18% on brokerage+exch+SEBI."""
    turnover = buy_value + sell_value
    stt = sell_value * 0.0015
    exch = turnover * 0.0003553
    sebi = turnover * 10 / 1e7
    stamp = buy_value * 0.00003
    gst = (brokerage + exch + sebi) * 0.18
    return round(stt + exch + sebi + stamp + gst, 2)


# (strategy, exit_reason, actual_exit, gross, charges, r_multiple)
EXPECTED = [
    ("VWAP_EMA_PULLBACK", "SL", 84.80, (84.80 - ENTRY) * QTY,
     charges(ENTRY * QTY, 84.80 * QTY, 120.0), (84.80 - ENTRY) / RISK),
    ("SUPERTREND_EMA", "SL", 117.75, (122.85 - ENTRY) * 65 + (117.75 - ENTRY) * 130,
     round(charges(ENTRY * 65, 122.85 * 65, 40.0) + charges(ENTRY * 130, 117.75 * 130, 80.0), 2),
     ((65 * 122.85 + 130 * 117.75) / QTY - ENTRY) / RISK),
    ("VWAP_EMA_PULLBACK", "TARGET", 129.87, (129.87 - ENTRY) * QTY,
     charges(ENTRY * QTY, 129.87 * QTY, 120.0), (129.87 - ENTRY) / RISK),
    ("SUPERTREND_EMA", "SL", 79.90, (79.90 - ENTRY) * QTY,
     charges(ENTRY * QTY, 79.90 * QTY, 120.0), (79.90 - ENTRY) / RISK),
]


def _net(e):
    return round(e[3] - BROKERAGE - e[4], 2)


def _q(Session, stmt):
    async def _go():
        async with Session() as db:
            return (await db.execute(stmt)).all()
    return run(_go())

# ── R13.11: the hand-computed four-trade day ─────────────────────────────────

def test_four_trade_day_produces_exactly_four_hand_computed_positions(four_trade):
    r = four_trade.run1
    assert_complete(r)
    pos = positions(four_trade.SessA, r.run_id)
    assert len(pos) == 4
    for p, (strategy, reason, exit_px, gross, chg, rm) in zip(pos, EXPECTED):
        assert p.strategy_name == strategy and p.exit_reason == reason
        assert not p.is_open and p.entry_fill_source == "ASK" and p.target_stretched is False
        assert (p.actual_entry, p.initial_stop_loss, p.target) == (ENTRY, STOP, TARGET)
        assert p.initial_risk == pytest.approx(RISK)
        assert (p.original_lots or p.lots, p.original_quantity or p.quantity) == (LOTS, QTY)
        assert p.actual_exit == pytest.approx(exit_px)
        assert p.gross_pnl == pytest.approx(round(gross, 2))
        assert p.brokerage == pytest.approx(BROKERAGE)
        assert p.statutory_charges == pytest.approx(chg)
        assert p.net_pnl == pytest.approx(round(gross - BROKERAGE - chg, 2))
        assert p.r_multiple == pytest.approx(rm)


def test_the_trailed_winner_took_the_partial_and_walked_the_ladder(four_trade):
    r = four_trade.run1
    t2 = positions(four_trade.SessA, r.run_id)[1]
    assert t2.partial_closed and t2.partial_exit_price == pytest.approx(122.85)
    assert t2.partial_exit_qty == 65 and t2.quantity == 130 and t2.lots == 2
    assert t2.stop_loss == pytest.approx(118.00)                       # LOCK_60
    assert t2.exit_value_weighted == pytest.approx(119.45)
    events = _q(four_trade.SessA, select(PositionEvent.event_type, PositionEvent.tier_label,
                                         PositionEvent.to_value)
                .where(PositionEvent.run_id == r.run_id, PositionEvent.position_id == t2.id)
                .order_by(PositionEvent.id))
    trail = [(lbl, v) for kind, lbl, v in events if kind == "TRAIL_ADVANCE"]
    assert trail == [("BREAKEVEN", 100.0), ("LOCK_40", 112.0), ("LOCK_60", 118.0)]
    assert [kind for kind, _, _ in events].count("PARTIAL_EXIT") == 1

def test_four_trade_metrics_match_hand_computation(four_trade):
    m = four_trade.run1.metrics
    nets = [_net(e) for e in EXPECTED]
    rs = [e[5] for e in EXPECTED]
    assert (m.trades, m.wins, m.losses, m.flats) == (4, 2, 2, 0)
    assert m.win_rate == pytest.approx(0.5)
    assert m.expectancy_r == pytest.approx(sum(rs) / 4)
    assert m.expectancy_inr == pytest.approx(sum(nets) / 4)
    assert m.profit_factor == pytest.approx((nets[1] + nets[2]) / -(nets[0] + nets[3]))
    # equity 0 -> n0 -> n0+n1 -> n0+n1+n2 (the peak) -> +n3: the worst fall is n3
    curve, peak, dd = 0.0, 0.0, 0.0
    for n in nets:
        curve += n
        peak = max(peak, curve)
        dd = max(dd, peak - curve)
    assert dd == pytest.approx(-nets[3])
    assert m.max_equity_drawdown_inr == pytest.approx(dd)
    assert m.max_equity_drawdown_pct == pytest.approx(dd / 200_000 * 100)
    assert m.longest_losing_streak == 1

    from app.services.metrics import stats
    z, n, p = 1.96, 4, 0.5
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    lo, hi = stats.wilson_interval(m.wins, m.trades)
    assert (lo, hi) == (pytest.approx(centre - half), pytest.approx(centre + half))


# ── determinism (R13.8) and run isolation ────────────────────────────────────

def test_same_params_same_hash_even_with_the_live_row_mutated(four_trade):
    r1, r2 = four_trade.run1, four_trade.run2
    assert_complete(r2)
    assert r1.params_hash == r2.params_hash
    assert r1.determinism_hash == r2.determinism_hash
    assert r1.run_id != r2.run_id
    # the replay neither read nor wrote the live row
    a, b = four_trade.row_after_run1, four_trade.row_after_run2
    assert (a.capital, a.used_capital) == (200_000.0, 0.0)
    assert (b.capital, b.used_capital) == (50_000.0, 150_000.0)


def test_a_shifted_window_changes_the_hash(four_trade):
    r1, r3 = four_trade.run1, four_trade.run3
    assert_complete(r3)
    assert r3.metrics.trades == 4                     # the same path on another day
    assert r3.determinism_hash != r1.determinism_hash

def test_the_replay_row_records_the_run(four_trade):
    rows = _q(four_trade.SessA, select(ReplayRun.run_id, ReplayRun.status, ReplayRun.params_hash,
                                       ReplayRun.seed).order_by(ReplayRun.created_at))
    assert [r.status for r in rows] == ["COMPLETE"] * 3
    assert rows[0].run_id == four_trade.run1.run_id and rows[0].run_id.startswith("RPL-")
    assert rows[0].params_hash == four_trade.run1.params_hash and rows[0].seed == 20260901


# ── §14.3 count-in / count-out, instruments, OI/IV ───────────────────────────

def test_count_in_count_out_every_loaded_candle_is_evaluated(four_trade):
    from app.services.replay.data_loader import load_day

    async def _loaded():
        async with four_trade.SessA() as db:
            return await load_day(db, DAY1, ("NIFTY",))
    data = run(_loaded())
    n_in = sum(len(v) for v in data.candles.values())
    [(n_out,)] = _q(four_trade.SessA, select(CycleStats.candles_evaluated).where(
        CycleStats.run_id == four_trade.run1.run_id))
    assert n_in == n_out == N_CANDLES


def test_a_deactivated_instrument_master_is_tradeable(four_trade):
    flags = [a for (a,) in _q(four_trade.SessA, select(Instrument.is_active))]
    assert flags and not any(flags)                   # every row is_active = False
    [day] = four_trade.run1.coverage.days
    assert day.status == "TRADEABLE" and day.cause is None
    assert day.universe_size >= 2 * 8 and day.chosen_expiry == "2026-09-03"


def test_only_the_scripted_strategies_trade_under_replay(four_trade):
    assert {p.strategy_name for p in positions(four_trade.SessA, four_trade.run1.run_id)} ==         {"VWAP_EMA_PULLBACK", "SUPERTREND_EMA"}

# ── per-day caps (R13.9) ─────────────────────────────────────────────────────

def test_the_daily_trade_cap_applies_per_replayed_day(tmp_path, monkeypatch):
    pin_regime(monkeypatch)
    isolate_singletons(monkeypatch)
    times = ["10:00", "10:06", "10:12", "10:18", "10:24", "10:30"]
    path = {minute_index(t): 131.0 for t in times}       # every entry hits target next minute
    schedule = {"VWAP_EMA_PULLBACK": {t: "BUY" for t in times[0::2]},
                "SUPERTREND_EMA": {t: "BUY" for t in times[1::2]}}
    Session, engine, url = make_db(tmp_path / "caps.db")
    fill_db(url, [DAY1, DAY2], scripted_quote(path))
    try:
        r = replay(Session, DAY1, DAY2, scripted_factory(schedule),
                   overrides={"max_trades_per_day": 2})
        assert_complete(r)
        per_day: dict = {}
        for p in positions(Session, r.run_id):
            per_day.setdefault(p.entry_time.date(), []).append(p.exit_reason)
        assert per_day == {DAY1: ["TARGET", "TARGET"], DAY2: ["TARGET", "TARGET"]}
        capped = _q(Session, select(NoTradeDecision.trading_day, func.count(NoTradeDecision.id))
                    .where(NoTradeDecision.run_id == r.run_id,
                           NoTradeDecision.code == "DAILY_TRADE_CAP")
                    .group_by(NoTradeDecision.trading_day).order_by(NoTradeDecision.trading_day))
        assert [(d, n) for d, n in capped] == [(DAY1, 4), (DAY2, 4)]
    finally:
        run(engine.dispose())


# ── a LIVE row is a startup assertion: exit 3, no replay_runs row ────────────

def test_a_live_row_exits_3_and_writes_no_run_row(tmp_path, monkeypatch):
    isolate_singletons(monkeypatch)
    Session, engine, url = make_db(tmp_path / "live.db")
    add_rows(Session, TradingConfig(id=1, mode=TradingMode.LIVE, live_mode_confirmed=True,
                                    capital=200_000.0))
    try:
        r = replay(Session, DAY1, DAY1, scripted_factory({}))
        assert r.exit_code == 3 and r.status == "STARTUP_ASSERTION" and r.run_id is None
        assert any("LIVE" in v or "mode" in v for v in r.violations)
        assert _q(Session, select(func.count(ReplayRun.run_id))) == [(0,)]
    finally:
        run(engine.dispose())


def test_an_invalid_params_file_is_a_startup_assertion_too(tmp_path, monkeypatch):
    isolate_singletons(monkeypatch)
    Session, engine, url = make_db(tmp_path / "bad.db")
    try:
        r = replay(Session, DAY1, DAY1, scripted_factory({}), overrides={"min_rr": 1.0})
        assert r.exit_code == 3 and any("min_rr" in v for v in r.violations)
        r = replay(Session, DAY1, DAY1, scripted_factory({}), overrides={"fraction_floor": 0.1})
        assert r.exit_code == 3 and any("fraction_floor" in v for v in r.violations)
        assert _q(Session, select(func.count(ReplayRun.run_id))) == [(0,)]
    finally:
        run(engine.dispose())


def test_no_tradeable_day_exits_2_with_coverage(tmp_path, monkeypatch):
    isolate_singletons(monkeypatch)
    Session, engine, url = make_db(tmp_path / "empty.db")
    try:
        r = replay(Session, DAY1, DAY2, scripted_factory({}))
        assert r.exit_code == 2 and r.status == "NO_DATA" and r.metrics is None
        assert [d.cause for d in r.coverage.days] == ["NO_SPOT_CANDLES", "NO_SPOT_CANDLES"]
        assert _q(Session, select(ReplayRun.status)) == [("NO_DATA",)]
    finally:
        run(engine.dispose())
