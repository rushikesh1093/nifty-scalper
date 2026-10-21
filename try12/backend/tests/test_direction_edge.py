"""Direction-edge and entry-timing research reports over trend_decisions rows."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.metrics import direction_edge as de
from app.services.metrics import entry_timing as et
from tests.helpers import run


def _row(day, direction=1, spot=100.0, px=None, decision="BUY CE", atr=10.0, stop=5.0,
         underlying="NIFTY", score=4, regime="BULL_TREND_NORMAL_VOL", **kw):
    px = px or {}
    r = dict(trading_day=day, direction=direction, spot=spot, atr=atr, stop_points=stop,
             decision=decision, underlying=underlying, score=score, regime=regime,
             strength="STRONG", entry_trigger="pullback rejection",
             px_15=px.get(15), px_30=px.get(30), px_60=px.get(60))
    r.update(kw)
    return r


def test_signed_move_follows_direction():
    assert de.signed_move(_row("d", 1, 100.0, {30: 104.0}), 30) == 4.0
    assert de.signed_move(_row("d", -1, 100.0, {30: 104.0}), 30) == -4.0
    assert de.signed_move(_row("d", 0, 100.0, {30: 104.0}), 30) is None
    assert de.signed_move(_row("d", 1, 100.0, {}), 30) is None


def test_each_day_counts_once_however_many_decisions():
    rows = [_row("2026-10-01", px={30: 110.0}) for _ in range(50)]     # +1 ATR, 50 rows
    rows += [_row("2026-10-02", px={30: 95.0})]                          # −0.5 ATR, 1 row
    s = de.summarize(rows, 30, "atr")
    assert s["n_decisions"] == 51 and s["n_days"] == 2
    assert s["mean"] == pytest.approx(0.25)                             # (1 − 0.5) / 2, not ≈ 0.97
    assert s["verdict"].startswith("insufficient")


def test_verdicts_need_enough_days_and_an_interval_clear_of_zero():
    days = [f"2026-09-{d:02d}" for d in range(1, 13)]
    pos = [_row(d, px={30: 103.0 + i % 3}) for i, d in enumerate(days)]
    assert de.summarize(pos, 30)["verdict"] == "positive"
    neg = [_row(d, px={30: 97.0 - i % 3}) for i, d in enumerate(days)]
    assert de.summarize(neg, 30)["verdict"] == "negative"
    mixed = [_row(d, px={30: 104.0 if i % 2 else 96.0}) for i, d in enumerate(days)]
    assert de.summarize(mixed, 30)["verdict"] == "not distinguishable from zero"


def test_r_units_use_the_index_point_stop():
    s = de.summarize([_row("d", px={30: 110.0}, stop=5.0)], 30, "R")
    assert s["mean"] == pytest.approx(2.0)


def test_report_shape_and_groupings():
    rows = [_row("2026-10-01", px={15: 101.0, 30: 102.0, 60: 99.0}),
            _row("2026-10-01", decision="NO TRADE", direction=-1, px={30: 98.0},
                 underlying="BANKNIFTY", score=None),
            _row("2026-10-02", decision="NO TRADE", direction=0, px={30: 98.0})]
    rep = de.report(rows)
    assert rep["scope"]["trading_days"] == 2
    sig = rep["populations"]["signals"]
    assert sig["overall"]["30m"]["n_decisions"] == 1
    assert set(sig["by"]) == {"underlying", "regime", "score", "strength", "trigger"}
    tc = rep["populations"]["trend_calls"]["overall"]["30m"]
    assert tc["n_decisions"] == 2                                       # sideways excluded
    assert "n/a" in rep["populations"]["trend_calls"]["by"]["score"]["30m"]
    assert "not a probability" in rep["method"]


# ── entry timing ─────────────────────────────────────────────────────────────

def _sig(direction=1, spot=100.0, nxt=101.0, hi=103.0, lo=99.5, px30=105.0, day="2026-10-01"):
    return _row(day, direction, spot, {30: px30}, next_open=nxt, next2_high=hi, next2_low=lo)


def test_limit_needs_a_trade_through_not_a_touch():
    assert et.limit_fill(_sig(lo=100.0), 1.0) is None          # touched only
    assert et.limit_fill(_sig(lo=99.5), 1.0) is None           # 0.5 through < 1.0
    assert et.limit_fill(_sig(lo=98.9), 1.0) == 100.0          # filled at the limit
    assert et.limit_fill(_sig(direction=-1, hi=101.0), 1.0) == 100.0
    assert et.limit_fill(_sig(direction=-1, hi=100.5), 1.0) is None


def test_gap_in_our_favour_still_fills_at_the_limit_not_better():
    r = _sig(nxt=97.0, lo=96.0)                                   # opened 3 below the limit
    assert et.limit_fill(r, 1.0) == 100.0


def test_entry_timing_report_counts_missed_limits_as_no_trade():
    rows = [_sig(lo=98.0, px30=104.0),                            # filled: +4 from 100
            _sig(lo=100.5, px30=110.0, day="2026-10-02")]          # missed; ran away
    rep = et.report(rows, unit="points")
    assert rep["signals"] == 2 and rep["limit_fill_rate"] == 0.5
    h = rep["horizons"]["30m"]
    assert h["limit2_filled"]["mean"] == pytest.approx(4.0)
    assert h["next_open_of_missed_limits"]["mean"] == pytest.approx(9.0)
    assert h["limit2_per_signal_missed_as_zero"]["mean"] == pytest.approx(2.0)
    assert h["next_open_all"]["mean"] == pytest.approx(6.0)        # (3 + 9) / 2
    assert rep["entry_improvement_pts"]["mean"] == pytest.approx(1.0)


# ── endpoint over the table ──────────────────────────────────────────────────

def test_direction_edge_endpoint_reads_this_runs_rows(runtime_ctx, sqlite_db):
    from app.api.routes.research import direction_edge, entry_timing
    from app.db.models.research import TrendDecisionRecord

    async def _seed():
        async with sqlite_db() as db:
            for i, run_id in enumerate(("PAPER", "OTHER")):
                db.add(TrendDecisionRecord(
                    run_id=run_id, trading_day=date(2026, 9, 1), underlying="NIFTY",
                    candle_ts=datetime(2026, 9, 1, 4, 30 + i, tzinfo=timezone.utc),
                    evaluated_at=datetime(2026, 9, 1, 4, 31, tzinfo=timezone.utc),
                    decision="BUY CE", direction=1, spot=100.0, atr=10.0, stop_points=5.0,
                    px_30=103.0, next_open=100.5, next2_high=102.0, next2_low=98.0,
                    engine_evaluated=True, signal_emitted=True, fwd_complete=True))
            await db.commit()
    run(_seed())
    rep = run(direction_edge(from_day=None, to_day=None, underlying=None, unit="atr",
                             run_id=None))
    assert rep["scope"]["rows"] == 1
    assert rep["populations"]["signals"]["overall"]["30m"]["mean"] == pytest.approx(0.3)
    et_rep = run(entry_timing(from_day=None, to_day=None, underlying=None, unit="points",
                              run_id=None))
    assert et_rep["signals"] == 1 and et_rep["limit_fill_rate"] == 1.0
