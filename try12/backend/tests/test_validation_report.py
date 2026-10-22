"""The statistical validation report and the D10 go/no-go (design §18, §26, D8, D10)."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.trading_params import TradingParams
from app.services.metrics.trade_metrics import TradeRow
from app.services.replay import report
from tests.replay_fixtures import run

P = TradingParams()
START = date(2026, 9, 1)


def _days(n):
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _trade(i, day, r, net, strategy="EMA_VWAP"):
    t = datetime(day.year, day.month, day.day, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=i)
    return TradeRow(
        position_id=i, run_id="RPL-T", trading_day=day, underlying="NIFTY",
        strategy_name=strategy, entry_regime="BULL_TREND_NORMAL_VOL", direction="BUY",
        strike=22600.0, option_type="CE", expiry=day + timedelta(days=3), dte_at_entry=3,
        lots=1, quantity=65, actual_entry=100.0, initial_stop_loss=85.0, initial_risk=15.0,
        target=130.0, actual_exit=100.0 + 15.0 * r, exit_reason="TARGET" if r > 0 else "SL",
        exit_value_weighted=100.0 + 15.0 * r, gross_pnl=15.0 * r * 65, brokerage=40.0,
        statutory_charges=10.0, net_pnl=net, r_multiple=r, r_multiple_net=r - 0.05,
        r_source="ENTRY", entry_time=t - timedelta(minutes=5), exit_time=t, hold_minutes=5.0,
        entry_fill_source="ASK", exit_fill_source="BID", sizing_method="FIXED_FRACTION")


def _book(days, rs, nets):
    """The same per-day pattern of (r, net) on every day."""
    out, i = [], 0
    for d in days:
        for r, n in zip(rs, nets):
            i += 1
            out.append(_trade(i, d, r, n))
    return out


def _inputs(trades, days, **kw):
    base = dict(run_id="RPL-T", date_from=days[0], date_to=days[-1], underlyings=("NIFTY",),
                seed=20260901, code_version="test", params_record={"params": {}, "cfg": {}},
                params_hash="h", params=P, capital=200_000.0, trades=trades,
                coverage={"summary": {}, "days": []}, tradeable_days=list(days),
                label="unit", iterations=500)
    base.update(kw)
    return report.ReportInputs(**base)


R_PATTERN = [2.0, 2.0, -1.0, 2.0, -1.0]            # Expectancy_R 0.8, win rate 60%
GOOD_NETS = [1950.0, 1950.0, -1050.0, 1950.0, -1050.0]
COSTLY_NETS = [100.0, 100.0, -400.0, 100.0, -400.0]  # positive gross R, negative net ₹
DAYS24 = _days(24)


def test_all_six_criteria_pass_and_the_verdict_is_consider():
    rep = report.build_validation(_inputs(_book(DAYS24, R_PATTERN, GOOD_NETS), DAYS24))
    assert [c["passed"] for c in rep["sections"]["11"]["criteria"]] == [True] * 6
    assert rep["recommendation"] == "CONSIDER" and rep["caps"] == []
    assert rep["recommendation_line"] == "RECOMMENDATION: CONSIDER"


def test_positive_gross_negative_net_is_not_yet_naming_criterion_3():
    rep = report.build_validation(_inputs(_book(DAYS24, R_PATTERN, COSTLY_NETS), DAYS24))
    crit = {c["n"]: c for c in rep["sections"]["11"]["criteria"]}
    assert crit[2]["passed"] and not crit[3]["passed"]          # gross R passes, net ₹ fails
    assert rep["failing_criteria"] == [3]
    assert rep["recommendation"] == "NOT YET"
    assert "criterion 3" in rep["recommendation_line"]
    assert "RECOMMENDATION: NOT YET" in report.render_markdown(rep)


def test_criterion_sets_and_bootstrap_labels():
    rep = report.build_validation(_inputs(_book(DAYS24, R_PATTERN, GOOD_NETS), DAYS24))
    sets = {c["n"]: c["set"] for c in rep["sections"]["11"]["criteria"]}
    assert sets == {1: "[full]", 2: "[full, day-block]", 3: "[full, day-block]", 4: "[full]",
                    5: "[oos] vs [in-sample, day-block]", 6: "[full]"}
    s4 = rep["sections"]["4"]
    assert s4["expectancy_r_line"].startswith("Expectancy_R [full, day-block] 95% CI")
    assert s4["expectancy_r_iid_line"].startswith("Expectancy_R [full, iid, reference only]")


def test_item_4_and_item_11_print_the_same_criterion_2_line():
    rep = report.build_validation(_inputs(_book(DAYS24, R_PATTERN, GOOD_NETS), DAYS24))
    line4 = rep["sections"]["4"]["expectancy_r_line"]
    crit2 = next(c for c in rep["sections"]["11"]["criteria"] if c["n"] == 2)
    assert crit2["actual"] == line4                               # byte-identical
    md = report.render_markdown(rep)
    sec4 = md.split("## 4.")[1].split("## 5.")[0]
    sec11 = md.split("## 11.")[1].split("## 12.")[0]
    assert line4 in sec4 and line4 in sec11


# ── the one-sided OOS test (§18 item 7) ──────────────────────────────────────

@pytest.mark.parametrize("oos, lo, hi, verdict", [
    (0.5, 0.2, 0.9, "PASS"),
    (0.1, 0.2, 0.9, "OOS_DIVERGENCE"),
    (-0.1, -0.5, 0.9, "OOS_DIVERGENCE"),            # >= the bound but not > 0
    (1.5, 0.2, 0.9, "OOS_FAVOURABLE"),              # informational, not a failure
    (None, 0.2, 0.9, "INSUFFICIENT"),
])
def test_oos_verdict_is_one_sided(oos, lo, hi, verdict):
    assert report.oos_verdict(oos, lo, hi) == verdict


def _split_book(in_rs, oos_rs, days=DAYS24, split=0.70):
    k = int(len(days) * split)
    out, i = [], 0
    for j, d in enumerate(days):
        for r in (in_rs if j < k else oos_rs):
            i += 1
            out.append(_trade(i, d, r, 1000.0 * r - 50.0))
    return out


def test_a_better_out_of_sample_is_favourable_and_passes_criterion_5():
    rep = report.build_validation(_inputs(_split_book([2.0, -1.0, 1.0], [2.0, 2.0, 2.0]), DAYS24))
    assert rep["sections"]["7"]["verdict"] == "OOS_FAVOURABLE"
    assert next(c for c in rep["sections"]["11"]["criteria"] if c["n"] == 5)["passed"]
    assert rep["sections"]["7"]["split_date"] == DAYS24[16].isoformat()   # 70% of 24 days


def test_a_worse_out_of_sample_diverges_and_fails_criterion_5():
    rep = report.build_validation(_inputs(_split_book([2.0, -1.0, 1.0], [-1.0, -1.0, 2.0]),
                                          DAYS24))
    assert rep["sections"]["7"]["verdict"] == "OOS_DIVERGENCE"
    assert 5 in rep["failing_criteria"] and rep["recommendation"] == "NOT YET"

# ── caps that force NOT YET regardless of the six (D8, §18 item 9) ───────────

def test_ltp_fallback_fills_cap_the_verdict_unless_a_companion_run_passed():
    book = _book(DAYS24, R_PATTERN, GOOD_NETS)
    capped = report.build_validation(_inputs(book, DAYS24, stats_={"ltp_only_fallbacks": 3}))
    assert capped["recommendation"] == "NOT YET" and capped["failing_criteria"] == []
    assert any("LTP-only" in c for c in capped["caps"])
    ok = report.build_validation(_inputs(book, DAYS24, stats_={"ltp_only_fallbacks": 3},
                                         companion_passing=True))
    assert ok["recommendation"] == "CONSIDER"


def _prior(overlap_days, params, run_id="RPL-PRIOR"):
    return {"run_id": run_id, "date_from": overlap_days[0], "date_to": overlap_days[-1],
            "params": {"params": params}, "tradeable_days": [d.isoformat() for d in overlap_days],
            "expectancy_r": 0.1}


def test_an_undeclared_parameter_search_is_detected_and_caps():
    book = _book(DAYS24, R_PATTERN, GOOD_NETS)
    prior = _prior(DAYS24[:20], {"min_score": 35.0})                # 20/24 overlap > 0.5
    rep = report.build_validation(_inputs(book, DAYS24, prior_runs=[prior],
                                          params_record={"params": {"min_score": 40.0}}))
    trail = rep["sections"]["9"]["trail"]
    assert trail["undeclared_search"] and trail["prior_runs"][0]["changed_keys"] == ["min_score"]
    assert trail["prior_runs"][0]["overlap_fraction"] == pytest.approx(20 / 24, abs=1e-4)
    assert rep["recommendation"] == "NOT YET"
    assert "WARNING: undeclared parameter search detected" in report.render_markdown(rep)
    declared = report.build_validation(_inputs(book, DAYS24, prior_runs=[prior],
                                               notes="min_score 35 -> 40 after run RPL-PRIOR",
                                               params_record={"params": {"min_score": 40.0}}))
    assert not declared["sections"]["9"]["trail"]["undeclared_search"]
    small = report.build_validation(_inputs(book, DAYS24, prior_runs=[_prior(DAYS24[:6], {})]))
    assert not small["sections"]["9"]["trail"]["undeclared_search"]      # 6/24 <= 0.5


def test_a_regime_gating_change_over_an_overlapping_window_is_fitted():
    book = _book(DAYS24, R_PATTERN, GOOD_NETS)
    prior = _prior(DAYS24[:3], {"transition_cooldown_candles": 2})
    rep = report.build_validation(_inputs(book, DAYS24, prior_runs=[prior], notes="declared",
                                          params_record={"params": {"transition_cooldown_candles": 3}}))
    assert rep["sections"]["9"]["trail"]["fitted_regime_gating_after"] == ["RPL-PRIOR"]
    assert rep["recommendation"] == "NOT YET"
    assert "FITTED: regime gating changed after run RPL-PRIOR" in report.render_markdown(rep)


def test_a_synthetic_run_is_labelled_and_never_considered():
    rep = report.build_validation(_inputs(_book(DAYS24, R_PATTERN, GOOD_NETS), DAYS24,
                                          label="synthetic-e2e"))
    assert rep["synthetic"] and rep["banner"] == report.SYNTHETIC_BANNER
    assert rep["recommendation"] == "NOT YET"
    md = report.render_markdown(rep)
    assert md.startswith(f"> **{report.SYNTHETIC_BANNER}**")
    assert "SYNTHETIC" in md and "not evidence of edge" in md


def _gathered(four_trade, **args_kw):
    """ReportInputs from the real gather_inputs hook for four_trade's run1."""
    from app.services.replay.engine import (
        ReplayArgs, params_record, resolve_params, run_config_for,
    )
    r = four_trade.run1
    p, extra, _ = resolve_params({"user_strategy_flags": {"OI_SPIKE": False, "IV_CRUSH": False}})
    cfg = run_config_for(p, extra, ("NIFTY",))
    record, _ = params_record(p, cfg)
    args = ReplayArgs(date_from=r.coverage.days[0].day, date_to=r.coverage.days[0].day,
                      bootstrap_iterations=200, **args_kw)
    result = SimpleNamespace(**{**vars(r), "artefacts": []})
    return run(report.gather_inputs(SimpleNamespace(sessions=four_trade.SessA), args, result,
                                    p, cfg, record))


def test_the_synthetic_flag_caps_a_run_whose_label_lacks_the_word(four_trade):
    from app.services.replay.cli import parse_args
    base = ["--from", "2026-09-01", "--to", "2026-09-01"]
    assert parse_args(base + ["--synthetic"]).synthetic is True
    assert parse_args(base + ["--no-synthetic"]).synthetic is False
    inp = _gathered(four_trade, label="plain-run", synthetic=True)
    assert inp.synthetic is True
    rep = report.build_validation(inp)
    assert rep["synthetic"] and rep["banner"] == report.SYNTHETIC_BANNER
    assert report.SYNTHETIC_BANNER in rep["caps"] and rep["recommendation"] == "NOT YET"


def test_omitting_the_synthetic_flag_keeps_the_label_heuristic(four_trade):
    from app.services.replay.cli import parse_args
    assert parse_args(["--from", "2026-09-01", "--to", "2026-09-01"]).synthetic is None
    plain = _gathered(four_trade, label="plain-run")
    assert plain.synthetic is None
    rep = report.build_validation(plain)
    assert rep["synthetic"] is False and report.SYNTHETIC_BANNER not in rep["caps"]
    labelled = report.build_validation(_gathered(four_trade, label="synthetic-four-trade"))
    assert labelled["synthetic"] is True and report.SYNTHETIC_BANNER in labelled["caps"]


def test_win_rate_below_50_is_stated_plainly():
    rs, nets = [2.5, -1.0, -1.0], [2400.0, -1050.0, -1050.0]       # 33% wins, positive edge
    rep = report.build_validation(_inputs(_book(DAYS24, rs, nets), DAYS24))
    md = report.render_markdown(rep)
    assert "below 50%, stated plainly" in md.split("## 10.")[1].split("## 11.")[0]
    assert next(c for c in rep["sections"]["11"]["criteria"] if c["n"] == 6)["passed"]


# ── the run's report end to end: 12 sections, §26 limitations, fill basis ───

def test_the_replayed_run_renders_all_twelve_sections(four_trade, tmp_path):
    from app.services.replay.engine import (
        ReplayArgs, params_record, resolve_params, run_config_for,
    )
    r = four_trade.run1
    p, extra, _ = resolve_params({"user_strategy_flags": {"OI_SPIKE": False, "IV_CRUSH": False}})
    cfg = run_config_for(p, extra, ("NIFTY",))
    record, _ = params_record(p, cfg)
    args = ReplayArgs(date_from=r.coverage.days[0].day, date_to=r.coverage.days[0].day,
                      label="synthetic-four-trade", bootstrap_iterations=300, out_dir=tmp_path)
    result = SimpleNamespace(**{**vars(r), "artefacts": []})
    run(report.build_and_write(SimpleNamespace(sessions=four_trade.SessA), args, result,
                               p, cfg, record))
    rep = result.validation
    md, js = result.artefacts
    text = md.read_text(encoding="utf-8")
    for key in report.NUMBERED_SECTIONS + ("2a",):
        assert f"## {key}. {report.SECTION_TITLES[key]}" in text
    assert set(rep["sections"]) == set(report.SECTION_TITLES)
    assert js.exists() and js.name == f"{r.run_id}.json"
    lims = rep["sections"]["12"]["limitations"]
    assert len(lims) == 13 and any("gross of costs" in x for x in lims)
    basis = rep["sections"]["12"]["fill_basis"]
    assert "ask x (1 + 0.05%)" in basis["entry"] and "bid x (1 - 0.05%)" in basis["exit"]
    assert rep["sections"]["3"]["sizing_method"] == {"FIXED_FRACTION": 4}
    assert rep["sections"]["2a"]["by_underlying"]["NIFTY"]["counts"] == \
        {"BULL_TREND_NORMAL_VOL": N_EVALUATED_REGIME_ROWS}
    assert rep["sections"]["8"]["cycle_totals"]["NIFTY"]["candles_evaluated"] == 345


# the first 19 candles stop at INSUFFICIENT_CANDLES (MIN_TODAY_CANDLES = 20)
N_EVALUATED_REGIME_ROWS = 345 - 19
