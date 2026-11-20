"""Backtest read derivations and series builders (backtest design §4.5, §4.6, §4.9, §10). Unit, no I/O
beyond reading the real report artefacts."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.trading_params import TradingParams
from app.services.backtest import read, series
from app.services.metrics import stats
from app.services.metrics import trade_metrics as tm
from app.services.metrics.trade_metrics import TradeRow

P = TradingParams()
DAY = date(2026, 9, 1)


def _real_runs() -> Path:
    for parent in Path(__file__).resolve().parents:
        d = parent / ".agents" / "tasks" / "paper-trading-hardening" / "runs"
        if d.is_dir():
            return d
    pytest.skip("real replay artefacts not available")


def _artefact(run_id: str) -> dict:
    p = _real_runs() / f"{run_id}.json"
    if not p.is_file():
        pytest.skip(f"{p.name} not available")
    return json.loads(p.read_text(encoding="utf-8"))


BASELINE = "RPL-01M4GCZX80SBAR1TXKC8KSP3WE"
ZERO_DAY = "RPL-01M4G383WHJ8TQACYBD9C05BMM"


def _trade(i, r, net, *, day=DAY, reason=None, r_net="auto", valid=True, cost=50.0):
    t = datetime(day.year, day.month, day.day, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=i)
    return TradeRow(
        position_id=i, run_id="RPL-T", trading_day=day, underlying="NIFTY",
        strategy_name="EMA_VWAP", entry_regime="BULL_TREND_NORMAL_VOL", direction="BUY",
        strike=22600.0, option_type="CE", expiry=day + timedelta(days=3), dte_at_entry=3,
        lots=1, quantity=65, actual_entry=100.0, initial_stop_loss=85.0,
        initial_risk=15.0 if valid else None, target=130.0, actual_exit=100.0 + 15.0 * r,
        exit_reason=reason or ("TARGET" if r > 0 else "SL"),
        exit_value_weighted=100.0 + 15.0 * r, gross_pnl=net + cost, brokerage=cost * 0.8,
        statutory_charges=cost * 0.2, net_pnl=net, r_multiple=r,
        r_multiple_net=(r - 0.05) if r_net == "auto" else r_net,
        r_source="ENTRY" if valid else None, entry_time=t - timedelta(minutes=5), exit_time=t,
        hold_minutes=5.0)


# ── run_params / run_capital ─────────────────────────────────────────────────

def test_run_params_rebuilds_the_real_frozen_record():
    record = _artefact(BASELINE)["sections"]["1"]["params"]
    p, warnings = read.run_params(record)
    # recorded before the strategy rewrite: only its holds for removed strategies go
    assert warnings in ([], ["legacy_strategy_keys_dropped"])
    assert p.dte_bands["NIFTY"] == ((2, 7), (8, 14))
    assert read.run_capital(record) == 200_000.0


def test_run_params_drops_only_holds_for_removed_strategies():
    record = {"params": {"min_rr": 2.5,
                         "max_hold_minutes": {"DEFAULT": 40, "RSI_VWAP": 25, "ORB_BREAKOUT": 30}}}
    p, warnings = read.run_params(record)
    assert warnings == ["legacy_strategy_keys_dropped"]
    assert p.min_rr == 2.5                                   # the run's real params, not defaults
    assert p.max_hold_minutes == {"DEFAULT": 40, "ORB_BREAKOUT": 30}


def test_run_params_with_an_invented_key_falls_back_loudly():
    p, warnings = read.run_params({"params": {"nope": 1}})
    assert p == TradingParams() and warnings == ["params_defaults_used"]


def test_run_capital_falls_through_three_sources():
    assert read.run_capital({"cfg": {"capital": 15_000.0}, "params": {"capital": 1.0}}) == 15_000.0
    assert read.run_capital({"cfg": {}, "params": {"capital": 50_000.0}}) == 50_000.0
    assert read.run_capital({}) == 0.0
    assert read.run_capital(None) == 0.0


# ── sessions / label / provenance ────────────────────────────────────────────

def test_sessions_are_distinct_tradeable_days():
    zero = _artefact(ZERO_DAY)["sections"]["2"]
    assert len(zero["days"]) == 58 and read.sessions_of(zero) == 0     # 0, not 0 + 0
    base = _artefact(BASELINE)["sections"]["2"]
    assert read.sessions_of(base) == 25
    two = {"days": [{"day": "2026-09-01", "status": "TRADEABLE", "underlying": u}
                    for u in ("NIFTY", "BANKNIFTY")]}
    assert read.sessions_of(two) == 1                                    # not summed per underlying
    assert read.sessions_of({"summary": {"NIFTY": {"tradeable": 3}, "BANKNIFTY": {"tradeable": 5}}}) == 5
    assert read.sessions_of(None) == 0
    assert read.trades_per_day(10, 0) is None and read.trades_per_day(None, 5) is None
    assert read.trades_per_day(118, 25) == pytest.approx(4.72)


def test_label_resolution_order():
    assert read.label_of({"label": "a"}, {"label": "b"}) == "a"
    assert read.label_of({"label": ""}, {"label": "b"}) == "b"
    assert read.label_of({}, None) is None


@pytest.mark.parametrize("validation, kind, label, expected", [
    ({"synthetic": True}, "synthetic", "x", ("SYNTHETIC", "validation")),   # first row wins
    ({"synthetic": False}, "synthetic", "x", ("SYNTHETIC", "dataset")),
    (None, "artifacts", "my Synthetic run", ("SYNTHETIC", "label")),
    ({"synthetic": False}, "artifacts", "capital-15000-A-proposed", ("UNVERIFIED", "none")),
    (None, "replay_db", None, ("UNVERIFIED", "none")),
])
def test_synthetic_state_table(validation, kind, label, expected):
    assert read.synthetic_state(validation, kind, label) == expected


def test_redact_paths_keeps_only_basenames():
    tb = ('File "C:\\Users\\x\\app\\services\\replay\\engine.py", line 3\n'
          'File "/home/u/app/x.py", line 9\nartefact: C:/tmp/runs/RPL-x.json')
    out = read.redact_paths(tb)
    assert "engine.py" in out and "x.py" in out and "RPL-x.json" in out
    assert "Users" not in out and "/home/" not in out and "C:" not in out


# ── equity / drawdown ────────────────────────────────────────────────────────

def test_series_drawdown_equals_the_metric():
    book = [_trade(i, r, n) for i, (r, n) in enumerate(
        [(1.0, 900.0), (-1.0, -1100.0), (-1.0, -1050.0), (2.0, 1900.0), (-1.0, -980.0)], start=1)]
    out = series.build(list(reversed(book)), 200_000.0, P.flat_epsilon)
    nets = [t.net_pnl for t in book]
    m = tm.compute(book, 200_000.0, P)
    assert out["trades"] == 5 and out["empty_reason"] is None
    assert max(p["drawdown_inr"] for p in out["equity"]) == stats.equity_drawdown(nets)
    assert out["max_drawdown_inr"] == m.max_equity_drawdown_inr
    assert out["max_drawdown_pct"] == pytest.approx(m.max_equity_drawdown_pct)
    assert [p["i"] for p in out["equity"]] == [1, 2, 3, 4, 5]            # ordered by exit
    assert out["equity"][-1]["cum_net_inr"] == pytest.approx(sum(nets))
    assert sum(d["net_pnl"] for d in out["daily"]) == pytest.approx(sum(nets))
    assert sum(b["count"] for b in out["exit_reasons"]) == 5


def test_percentages_are_null_without_capital():
    out = series.build([_trade(1, -1.0, -500.0)], 0.0, P.flat_epsilon)
    assert out["max_drawdown_pct"] is None
    assert out["equity"][0]["cum_net_pct"] is None and out["equity"][0]["drawdown_pct"] is None


def test_zero_trades_is_a_defined_payload():
    out = series.build([], 200_000.0, P.flat_epsilon)
    assert out["equity"] == [] and out["daily"] == [] and out["exit_reasons"] == []
    assert out["max_drawdown_inr"] is None and out["max_drawdown_pct"] is None
    assert out["r_histogram"] == {"bin_width": 0.25, "bins": [], "r_valid": 0,
                                  "r_unavailable": 0, "clipped_low": 0, "clipped_high": 0}
    assert out["trades"] == 0 and out["empty_reason"] == "no_data_for_filter"
    assert tm.compute([], 200_000.0, P).max_equity_drawdown_inr is None   # both null


# ── R histogram boundary rule ────────────────────────────────────────────────

@pytest.mark.parametrize("w", series.BIN_WIDTHS)
def test_histogram_boundaries(w):
    rs = [-3.5, -3.0, -1.0, -0.1, 0.0, 0.3, 1.0, 2.99, 3.0, 3.5]
    book = [_trade(i, r, 100.0 * r) for i, r in enumerate(rs, start=1)]
    book.append(_trade(99, 1.0, 100.0, valid=False))                     # r_unavailable
    h = series.r_histogram(book, w)
    assert h["r_valid"] == len(rs) and h["r_unavailable"] == 1
    assert (h["clipped_low"], h["clipped_high"]) == (1, 1)                # ±3.5 clipped, ±3.0 binned
    assert sum(b["count"] for b in h["bins"]) + h["clipped_low"] + h["clipped_high"] == h["r_valid"]
    assert h["bins"][0]["lo"] == -3.0 and h["bins"][-1]["hi"] == 3.0
    assert h["bins"][-1]["count"] >= 1                                    # last bin closed: 3.0 inside
    for a, b in zip(h["bins"], h["bins"][1:]):
        assert a["hi"] == b["lo"]


def test_histogram_half_open_bins_and_single_value():
    h = series.r_histogram([_trade(1, 1.0, 10.0), _trade(2, 0.5, 5.0)], 0.25)
    assert h["bins"][0]["lo"] == 0.5 and h["bins"][-1]["hi"] == 1.0
    assert h["bins"][0]["count"] == 1 and h["bins"][-1]["count"] == 1    # 1.0 in the closed last bin
    one = series.r_histogram([_trade(1, 1.0, 10.0)], 0.5)
    assert one["bins"] == [{"lo": 1.0, "hi": 1.5, "count": 1}]
    top = series.r_histogram([_trade(1, 3.0, 10.0)], 0.5)
    assert top["bins"] == [{"lo": 2.5, "hi": 3.0, "count": 1}]


# ── downsampling ─────────────────────────────────────────────────────────────

def test_downsampling_keeps_first_last_peak_trough():
    nets = [((-1) ** i) * (i % 7) * 10.0 for i in range(4501)]
    nets[1234] = 1_000_000.0                                              # global peak at i=1235
    nets[3333] = -5_000_000.0                                             # global trough
    book = [_trade(i + 1, 0.0, n) for i, n in enumerate(nets)]
    full, max_dd = series.equity(book, 200_000.0)
    out = series.build(book, 200_000.0, P.flat_epsilon)
    assert out["downsampled"] is True and len(out["equity"]) <= series.MAX_EQUITY_POINTS + 4
    kept = {p["i"] for p in out["equity"]}
    peak = max(full, key=lambda p: p["cum_net_inr"])["i"]
    trough = min(full, key=lambda p: p["cum_net_inr"])["i"]
    assert {1, len(full), peak, trough} <= kept
    assert out["max_drawdown_inr"] == max_dd                              # computed before sampling
    small = series.build(book[:50], 200_000.0, P.flat_epsilon)
    assert small["downsampled"] is False and len(small["equity"]) == 50


# ── cost figures and their denominators ──────────────────────────────────────

def test_mean_cost_r_identity_when_the_samples_coincide():
    book = [_trade(i, r, n) for i, (r, n) in enumerate(
        [(1.0, 900.0), (-1.0, -1100.0), (2.0, 1900.0)], start=1)]
    c = read._cost_figures(book)
    m = tm.compute(book, 200_000.0, P)
    assert c["cost_sample_inr"] == 3 and c["mean_cost_inr"] == pytest.approx(50.0)
    if c["cost_sample_r"] == m.r_sample_size:
        assert c["mean_cost_r"] == pytest.approx(m.expectancy_r - m.expectancy_r_net, abs=1e-9)


def test_mean_cost_r_denominators_differ_when_a_net_r_is_missing():
    book = [_trade(1, 1.0, 900.0), _trade(2, -1.0, -1100.0, r_net=None), _trade(3, 2.0, 1900.0)]
    c = read._cost_figures(book)
    m = tm.compute(book, 200_000.0, P)
    assert c["cost_sample_r"] == 2 and m.r_sample_size == 3               # different n:
    # the gross-minus-net identity is not asserted here — it would compare two samples
    assert c["mean_cost_r"] == pytest.approx(0.05)
