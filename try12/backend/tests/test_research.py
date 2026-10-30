"""
The offline research harness (app.research): intrabar ambiguity and gaps,
trailing-stop monotonicity, causality (no future candle changes a past
signal), determinism, the walk-forward's out-of-sample isolation, status
rules, statistics, expiry and lot-size calendars, and recorded-quote option
replay (ask in, bid out, missing quote = UNAVAILABLE).
"""
import random
import sqlite3
from datetime import date, datetime, time as dtime, timedelta

import pytest

from app.research import data, metrics, options, regime, sim, walkforward as wf
from app.research.data import Session
from app.services.market_data.timestamps import IST
from app.strategies.strategies import FirstCandleBreakoutStrategy, SupertrendEMAStrategy


def _day(d: date, bars: list[tuple], fill_to: int = 375, drift: float = 0.0) -> Session:
    """1-min session from (o, h, l, c) tuples, padded with flat candles."""
    out, t = [], datetime.combine(d, dtime(9, 15), IST)
    last = bars[-1][3]
    for i in range(fill_to):
        o, h, l, c = bars[i] if i < len(bars) else (last, last + 1, last - 1, last + drift)
        if i >= len(bars):
            last = c
        out.append({"timestamp": t + timedelta(minutes=i), "open": o, "high": h, "low": l,
                    "close": c, "volume": 0.0})
    return Session(day=d, candles=out)


def _trade(**kw) -> sim.Trade:
    base = dict(strategy="X", variant="V", day="2024-01-02", side="BUY", signal_time="09:30",
                entry_time="09:30", plan_entry=100.0, entry=100.0, stop0=90.0, target=None,
                risk_pts=10.0, entry_idx=1)
    base.update(kw)
    return sim.Trade(**base)


# ── intrabar: same-candle stop and target is a stop; gaps fill at the open ──

def test_walk_exit_same_candle_stop_and_target_is_a_stop_and_flagged():
    s = _day(date(2024, 1, 2), [(100, 100, 100, 100), (100, 125, 85, 110)])
    res = sim.walk_exit(s, _trade(), "A_FIXED_2R", [])
    assert res["reason"] == "STOP" and res["r"] == -1.0 and res["ambiguous"]


def test_walk_exit_gap_through_the_stop_fills_at_the_worse_open():
    s = _day(date(2024, 1, 2), [(100, 100, 100, 100), (100, 101, 99, 100), (80, 82, 78, 81)])
    res = sim.walk_exit(s, _trade(), "A_FIXED_2R", [])
    assert res["reason"] == "STOP" and res["r"] == -2.0


def test_walk_exit_squares_off_at_1515():
    s = _day(date(2024, 1, 2), [(100, 100, 100, 100)])
    res = sim.walk_exit(s, _trade(), "A_FIXED_2R", [])
    assert res["reason"] == "EOD"
    assert s.candles[res["exit_idx"]]["timestamp"].time() == dtime(15, 15)


@pytest.mark.parametrize("variant", sim.EXIT_VARIANTS[1:])
def test_trailing_never_loosens_and_never_beats_the_path(variant):
    """Random walks: no exit worse than the initial stop unless a candle gaps
    through it, and the result never exceeds the best price reached."""
    rng = random.Random(7)
    for k in range(25):
        p, bars = 100.0, [(100, 100, 100, 100)]
        for _ in range(200):
            o = p
            p += rng.gauss(0, 1.5)
            bars.append((o, max(o, p) + abs(rng.gauss(0, 0.5)), min(o, p) - abs(rng.gauss(0, 0.5)), p))
        s = _day(date(2024, 1, 2), bars, fill_to=376)
        hist = _day(date(2024, 1, 1), [(100, 101, 99, 100)] * 375).candles
        side = "BUY" if k % 2 else "SELL"
        t = _trade(side=side, stop0=90.0 if side == "BUY" else 110.0)
        res = sim.walk_exit(s, t, variant, hist)
        sign = 1 if side == "BUY" else -1
        held = s.candles[1:res["exit_idx"] + 1]
        best_r = max(sign * ((c["high"] if sign > 0 else c["low"]) - 100.0) / 10.0 for c in held)
        worst_open_r = min(sign * (c["open"] - 100.0) / 10.0 for c in held)
        assert res["r"] >= min(-1.0, worst_open_r) - 1e-9          # beyond -1R only by a gap
        assert res["r"] <= best_r + 1e-9                           # never better than the path


def test_live_plan_step_flags_ambiguity_and_takes_the_stop():
    """_step_candle on a live ExitPlan: a candle through both stop and target."""
    from app.services.trading.exit_plan import ExitPlan
    plan = ExitPlan(strategy="ORB_BREAKOUT", side="BUY", spot_entry=100.0, spot_stop=90.0,
                    spot_target=120.0)
    t = _trade(target=120.0)
    pos = sim._OpenPosition(t, plan, None, datetime(2024, 1, 2, 9, 30, tzinfo=IST), None)
    c = {"timestamp": datetime(2024, 1, 2, 9, 31, tzinfo=IST), "open": 100, "high": 125, "low": 85,
         "close": 110}
    assert sim._step_candle(pos, c, 2)
    assert t.exit_reason == "SPOT_SL" and t.exit_px == 90.0 and t.ambiguous


# ── causality and determinism of the live-strategy harness ──────────────────

def _breakout_day(d):
    bars = [(22000.0, 22040.0, 21980.0, 22010.0)] + [(22010.0, 22020.0, 22000.0, 22010.0)] * 4
    bars += [(22010.0, 22062.0, 22005.0, 22060.0)] + [(22060.0, 22061.0, 22059.0, 22060.0)] * 4
    return _day(d, bars, drift=0.3)


def test_harness_runs_the_live_class_and_fills_at_the_next_open():
    prev, s = _breakout_day(date(2024, 1, 1)), _breakout_day(date(2024, 1, 2))
    trades = sim.run_days(FirstCandleBreakoutStrategy, [s], prev.candles, prev, "BASE")
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "BUY" and t.signal_time == "09:25" and t.entry_time == "09:25"
    assert t.entry == s.candles[t.entry_idx]["open"] and t.plan_entry == 22060.0
    assert t.stop0 == 22060.0 - 50.0                              # live 50-pt cap


def test_no_future_candle_changes_an_earlier_signal():
    rng = random.Random(3)
    days = []
    for k, d in enumerate((date(2024, 1, 1), date(2024, 1, 2))):
        p, bars = 22000.0, []
        for _ in range(375):
            o = p
            p += rng.gauss(0, 6)
            bars.append((o, max(o, p) + 2, min(o, p) - 2, p))
        days.append(_day(d, bars))
    full = sim.run_days(SupertrendEMAStrategy, [days[1]], days[0].candles, days[0], "BASE")
    cut = Session(day=days[1].day, candles=days[1].candles[:200])
    part = sim.run_days(SupertrendEMAStrategy, [cut], days[0].candles, days[0], "BASE")
    early = lambda ts: [(t.signal_time, t.side, t.entry, t.stop0) for t in ts if t.entry_idx < 190]  # noqa: E731
    assert early(full) == early(part) and early(full)


def test_harness_is_deterministic():
    prev, s = _breakout_day(date(2024, 1, 1)), _breakout_day(date(2024, 1, 2))
    a = [t.row() for t in sim.run_days(FirstCandleBreakoutStrategy, [s], prev.candles, prev, "B")]
    b = [t.row() for t in sim.run_days(FirstCandleBreakoutStrategy, [s], prev.candles, prev, "B")]
    assert a == b


def test_regime_label_is_causal_and_needs_warmup():
    rng = random.Random(1)
    t0, p, bars = datetime(2024, 1, 2, 9, 15, tzinfo=IST), 100.0, []
    for i in range(80):
        o = p
        p += rng.gauss(0.3, 1)
        bars.append({"start": t0 + timedelta(minutes=5 * i), "end": t0 + timedelta(minutes=5 * i + 5),
                     "open": o, "high": max(o, p) + .5, "low": min(o, p) - .5, "close": p})
    ranges = [2.0] * 20
    assert regime.classify(bars[:20], 0, ranges)["label"] == "INSUFFICIENT_DATA"
    assert regime.classify(bars[:60], 0, ranges[:3])["label"] == "INSUFFICIENT_DATA"
    at50 = regime.classify(bars[:50], 0, ranges)
    assert regime.classify(bars[:50], 0, ranges) == at50                  # pure
    assert at50["label"] in regime.LABELS


# ── statistics ──────────────────────────────────────────────────────────────

def test_metrics_drawdown_streak_and_cluster_bootstrap():
    assert metrics.max_drawdown([1, -2, -1, 3, -4]) == -4
    assert metrics.max_consecutive_losses([1, -1, -1, 2, -1, -1, -1]) == 3
    lo, hi = metrics.wilson(50, 100)
    assert lo < 0.5 < hi
    vals, days = [1.0, -1.0, 1.0, -1.0] * 10, [f"d{i // 2}" for i in range(40)]
    assert metrics.cluster_bootstrap_mean(vals, days) == metrics.cluster_bootstrap_mean(vals, days)
    rows = [{"r_net": v, "day": d} for v, d in zip(vals, days)]
    s = metrics.summarize(rows)
    assert s["n"] == 40 and s["profit_factor"] == 1.0 and s["sessions"] == 20


def test_costs_scale_with_hold_and_expiry():
    m = wf.COST_MODELS["MEASURED"]
    assert m.pts(60, True) == m.pts(60, False) > m.pts(0, False) == m.fixed_pts
    r = wf.apply_costs({"r_gross": 1.0, "risk_pts": 20.0, "hold_min": 60, "expiry": False}, m)
    assert r["r_net"] == pytest.approx(1.0 - (2.6 + 1.5) / 20.0)


# ── walk-forward isolation and status rules ─────────────────────────────────

def _rows(month_exp: dict, per_month: int = 25) -> list[dict]:
    out = []
    for m, e in month_exp.items():
        for i in range(per_month):
            out.append({"day": f"{m}-{(i % 20) + 1:02d}", "entry_time": "10:00", "r_net": e,
                        "regime": "RANGE"})
    return out


def test_walk_forward_never_scores_design_months_or_uses_the_test_month():
    months = [f"2024-{m:02d}" for m in range(1, 13)]
    # A wins every train/val month but loses its test month; B is the reverse.
    a = _rows({m: (0.5 if m != "2024-08" else -1.0) for m in months})
    b = _rows({m: (-0.5 if m != "2024-08" else 5.0) for m in months})
    res = wf.walk_forward({"A": a, "B": b}, months, "2024-06-30")
    tests = [f["test"] for f in res["folds"]]
    assert min(tests) > "2024-06"                                        # design window not scored
    aug = next(f for f in res["folds"] if f["test"] == "2024-08")
    assert aug["candidate"] == "A" and aug["test_exp"] == -1.0           # chosen without peeking


def test_status_rules():
    neg = {"n": 300, "expectancy_r": -0.1, "expectancy_ci95": (-0.2, -0.01), "sessions": 200}
    assert wf.classify_status(neg, 0.2, 10, 3)[0] == "OVERFIT"
    assert wf.classify_status(neg, -0.1, 10, 3)[0] == "DISABLED_FOR_RESEARCH"
    small = {"n": 30, "expectancy_r": 0.3, "expectancy_ci95": (-0.1, 0.6), "sessions": 25,
             "profit_factor": 1.5, "expectancy_without_best_session": 0.2, "max_drawdown_r": -3}
    assert wf.classify_status(small, 0.3, 5, 4)[0] == "INSUFFICIENT_EVIDENCE"
    good = {"n": 250, "expectancy_r": 0.2, "expectancy_ci95": (0.05, 0.35), "sessions": 150,
            "profit_factor": 1.4, "expectancy_without_best_session": 0.18, "max_drawdown_r": -8}
    assert wf.classify_status(good, 0.2, 20, 14)[0] == "ELIGIBLE_FOR_PAPER"
    assert wf.classify_status({"n": 0}, None, 0, 0)[0] == "INSUFFICIENT_EVIDENCE"


# ── calendars ───────────────────────────────────────────────────────────────

def test_lot_size_schedule():
    assert data.lot_size_on(date(2024, 6, 1)) == 25
    assert data.lot_size_on(date(2025, 6, 1)) == 75
    assert data.lot_size_on(date(2026, 6, 1)) == 65


def test_expiry_moves_to_the_previous_session_on_a_holiday():
    # Thursday expiries before Sep 2025; 2024-03-28 (Thu) present, 2024-04-11 (Thu) a holiday
    days = [date(2024, 3, 25) + timedelta(days=i) for i in range(21)
            if (date(2024, 3, 25) + timedelta(days=i)).weekday() < 5
            and date(2024, 3, 25) + timedelta(days=i) != date(2024, 4, 11)]
    ex = data.expiry_days(days)
    assert date(2024, 3, 28) in ex and date(2024, 4, 10) in ex and date(2024, 4, 11) not in ex
    assert date(2025, 9, 2) in data.expiry_days([date(2025, 9, 1), date(2025, 9, 2), date(2025, 9, 3)])


# ── recorded-quote option replay ────────────────────────────────────────────

@pytest.fixture
def quotes_db(tmp_path):
    p = tmp_path / "q.db"
    con = sqlite3.connect(p)
    con.execute("create table opt_min(minute text, token text, expiry text, strike real, opt text, "
                "ltp real, bid real, ask real)")
    con.execute("create table spot_min(minute text, ltp real)")
    con.executemany("insert into opt_min values (?,?,?,?,?,?,?,?)", [
        ("2026-10-20T10:00", "1", "2026-10-20", 23000, "CE", 100, 99.8, 100.2),
        ("2026-10-20T10:30", "1", "2026-10-20", 23000, "CE", 110, 109.7, 110.1),
        ("2026-10-20T16:00", "1", "2026-10-20", 23000, "CE", 999, 998, 999),   # out of hours
    ])
    con.executemany("insert into spot_min values (?,?)", [("2026-10-20T10:00", 23010.0)])
    con.commit(); con.close()
    return str(p)


def test_option_replay_buys_the_ask_sells_the_bid_and_reports_missing(quotes_db):
    q = options.Quotes(quotes_db)
    assert ("2026-10-20T16:00", "2026-10-20", 23000.0, "CE") not in q.q
    ok = _trade(day="2026-10-20", signal_time="10:00", entry_time="10:00", exit_time="10:30")
    miss = _trade(day="2026-10-20", signal_time="10:00", entry_time="10:00", exit_time="10:31")
    rows = options.replay_trades([ok, miss], q)
    assert rows[0]["status"] == "FILLED" and rows[0]["entry_fill"] >= 100.2
    assert rows[0]["exit_fill"] <= 109.7 and rows[0]["lot"] == 65
    assert rows[0]["net_rs"] == pytest.approx(rows[0]["gross_rs"] - rows[0]["charges_rs"])
    assert rows[1]["status"] == "UNAVAILABLE" and "exit" in rows[1]["reason"]
