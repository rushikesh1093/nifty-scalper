"""
The statistical validation report and the go/no-go recommendation (design §18).

``build_validation`` is pure: rows and numbers in, one JSON-safe dict out.
``render_markdown`` renders it. ``build_and_write`` is the ReplayEngine hook:
it gathers the run's rows, builds, stores the dict on the engine result (and so
on replay_runs.validation) and writes ``<run_id>.md`` / ``<run_id>.json`` to
the artefact directory — outside the worktree, never committed.

Rules the report enforces (D8, D10, §18):
  * six criteria, all required, each labelled with the trade set it reads
    ([full] for 1-4 and 6, [oos]/[in-sample] for 5) and the bootstrap that
    produced the interval (day-block gates; i.i.d. is printed as reference);
  * criterion 3 is the net-of-cost gate: a positive gross Expectancy_R with a
    non-positive net Expectancy_INR can never reach CONSIDER;
  * caps force NOT YET regardless of the six: LTP-only fills without a passing
    companion run (D8), an undeclared parameter search, a FITTED regime-gating
    change over an overlapping window, and synthetic data;
  * "consider" is the strongest verdict. It authorises nothing: R1 (paper only)
    remains absolute.
Nothing in this module changes a parameter or reads results back into the run.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from app.services.metrics import stats, trade_metrics as tm

SECTION_TITLES = {
    "1": "Run identity", "2": "Data coverage", "2a": "Regime histogram",
    "3": "Sample sizes", "4": "Headline metrics", "5": "Daily statistics",
    "6": "Breakdowns", "7": "Out-of-sample split", "8": "No-trade reason distribution",
    "9": "Fitted parameters", "10": "Honest statements", "11": "Go/no-go recommendation",
    "12": "Known fidelity limitations and cost assumptions",
}
NUMBERED_SECTIONS = tuple(str(i) for i in range(1, 13))

# D10 thresholds
MIN_TRADES = 100
MIN_DAYS = 20
MAX_DRAWDOWN_PCT = 10.0
# Keys whose change alters regime gating (§18 item 9, "named-remedy adoptions")
REGIME_GATING_KEYS = ("transition_cooldown_candles",)

SYNTHETIC_BANNER = "SYNTHETIC — not evidence of edge"

LIMITATIONS = [
    "Execution fidelity is modelled, not measured. Fills come from stored bid/ask or LTP plus a "
    "slippage assumption; no test establishes that these are the prices the exchange would have given.",
    "Intra-candle ordering is an assumption when option ticks are absent: pessimistic, stop-first. "
    "Real sequencing may have been favourable.",
    "Option history depth bounds everything. Replay is only as good as ticks and option_chain_snapshots; "
    "gaps are skipped, never interpolated. Whether the stored history is deep enough for a statistically "
    "meaningful run is unverified, which is why a coverage report is the first deliverable.",
    "Chain snapshots are 5-minutely (OPTION_CHAIN_INTERVAL = 300): the chain is context (IV, OI) and a "
    "bounded last-resort mark, not an exit-resolution source. Days without option tick coverage are skipped.",
    "The instrument master is current, not historical. Replay derives its universe from the option tokens "
    "that traded that day; a contract traded that day whose instruments row has since been deleted cannot "
    "be recovered. The coverage report counts tick tokens with no instruments row.",
    "The cost schedule is a flat-fee approximation (Rs 20/lot/leg plus published statutory rates), not a "
    "specific broker's contract note. IPFT is ignored. Rates change with the Union Budget and circulars.",
    "There is no holiday calendar. A holiday looks like a day with no data.",
    "Greeks are approximations. Delta is a Black-Scholes estimate from an IV that may be a 20% default; "
    "gamma, theta and vega are not used in selection.",
    "Sharpe-like and Sortino-like are not portfolio Sharpe ratios. Assumptions are printed next to the numbers.",
    "Statistical validation cannot detect an edge that does not exist. Positive in-sample expectancy on a "
    "thin sample is weak evidence. The gating intervals are day-block bootstraps because trades within a day "
    "are not independent; the effective sample is nearer the day count than the trade count.",
    "The paper-only guard is structural but not formally verified: deletion of order methods, an AST scan "
    "of app/ and scripts/, and startup checks outside any broad handler. Nothing in this repository places an order.",
    "Expectancy_R is gross of costs. Expectancy_INR and r_multiple_net are the net figures; D10 gates on both.",
    "Quote-path slippage (0.05% per leg) is an assumption, not calibrated against any fill record.",
]


@dataclass
class ReportInputs:
    run_id: str
    date_from: date
    date_to: date
    underlyings: tuple
    seed: int
    code_version: str
    params_record: dict
    params_hash: str
    params: Any                              # TradingParams
    capital: float
    trades: list                             # list[tm.TradeRow]
    coverage: dict                           # CoverageReport.as_dict()
    tradeable_days: list                     # sorted list[date]
    label: str = ""
    notes: str = ""
    stats_: dict = field(default_factory=dict)         # engine stats (steps, fallbacks, wall time)
    regime_hist: dict = field(default_factory=dict)
    no_trade: list = field(default_factory=list)       # [{stage, code, count}]
    cycle_totals: dict = field(default_factory=dict)   # underlying -> {candles, strategies, signals, oi_iv_missing}
    oi_iv_by_day: list = field(default_factory=list)   # [{underlying, day, missing}]
    signals_by_strategy: dict = field(default_factory=dict)
    prior_runs: list = field(default_factory=list)     # see _search_trail
    oos_split: float = 0.70
    walk_forward: int = 0
    iterations: int = 10_000
    companion_passing: bool = False
    synthetic: Optional[bool] = None


# ── small formatting helpers ─────────────────────────────────────────────────

def _f(x: Optional[float], nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _ci_text(label: str, ci: tuple, nd: int = 4) -> str:
    lo, hi = ci
    return f"{label} 95% CI: [{_f(lo, nd)}, {_f(hi, nd)}]"


def _ci(trades: list, statistic, iterations: int, seed: int) -> tuple:
    return stats.bootstrap_ci_by_day(trades, statistic, iterations, seed)


def _ci_iid(trades: list, statistic, iterations: int, seed: int) -> tuple:
    return stats.bootstrap_ci(trades, statistic, iterations, seed)


# ── OOS split ────────────────────────────────────────────────────────────────

def split_days(days: list, oos_split: float) -> tuple[list, list]:
    """Chronological split of the TRADEABLE days (not calendar days)."""
    n = len(days)
    if n <= 1:
        return list(days), []
    k = min(max(int(math.floor(n * oos_split)), 1), n - 1)
    return days[:k], days[k:]


def oos_verdict(oos_e: Optional[float], is_lo: Optional[float], is_hi: Optional[float]) -> str:
    """One-sided (§18 item 7): pass when OOS > 0 and OOS >= the in-sample day-block
    lower bound; OOS_DIVERGENCE below it (a failure); OOS_FAVOURABLE above the
    in-sample upper bound (informational, not a failure)."""
    if oos_e is None or is_lo is None:
        return "INSUFFICIENT"
    if oos_e > 0 and oos_e >= is_lo:
        return "OOS_FAVOURABLE" if (is_hi is not None and oos_e > is_hi) else "PASS"
    return "OOS_DIVERGENCE"


def _oos_section(inp: ReportInputs, in_days: list, oos_days: list) -> dict:
    in_set, oos_set = set(in_days), set(oos_days)
    tr_in = [t for t in inp.trades if t.trading_day in in_set]
    tr_oos = [t for t in inp.trades if t.trading_day in oos_set]
    it, seed = inp.iterations, inp.seed
    m_in, m_oos = tm.compute(tr_in, inp.capital, inp.params), tm.compute(tr_oos, inp.capital, inp.params)
    in_ci = _ci(tr_in, tm.expectancy_r, it, seed) if tr_in else (None, None)
    in_iid = _ci_iid(tr_in, tm.expectancy_r, it, seed) if tr_in else (None, None)
    oos_ci = _ci(tr_oos, tm.expectancy_r, it, seed) if tr_oos else (None, None)
    oos_iid = _ci_iid(tr_oos, tm.expectancy_r, it, seed) if tr_oos else (None, None)
    oos_e = m_oos.expectancy_r
    verdict = oos_verdict(oos_e, in_ci[0], in_ci[1])

    per_strategy = {}
    for name in sorted({t.strategy_name for t in inp.trades}):
        s_in = [t for t in tr_in if t.strategy_name == name]
        s_oos = [t for t in tr_oos if t.strategy_name == name]
        s_ci = _ci(s_in, tm.expectancy_r, it, seed) if s_in else (None, None)
        s_e = tm.compute(s_oos, inp.capital, inp.params).expectancy_r
        per_strategy[name] = {
            "in_sample_trades": len(s_in), "oos_trades": len(s_oos),
            "oos_expectancy_r": s_e, "in_sample_ci_day_block": list(s_ci),
            "verdict": oos_verdict(s_e, s_ci[0], s_ci[1]),
            "low_sample": len(s_in) < inp.params.min_display_sample
                          or len(s_oos) < inp.params.min_display_sample,
        }
    return {
        "split_fraction": inp.oos_split,
        "split_date": oos_days[0].isoformat() if oos_days else None,
        "in_sample_days": len(in_days), "oos_days": len(oos_days),
        "in_sample": tm.as_dict(m_in), "oos": tm.as_dict(m_oos),
        "in_sample_ci_day_block": list(in_ci), "in_sample_ci_iid": list(in_iid),
        "oos_ci_day_block": list(oos_ci), "oos_ci_iid": list(oos_iid),
        "oos_expectancy_r": oos_e, "verdict": verdict,
        "per_strategy": per_strategy,
    }


def _walk_forward(inp: ReportInputs, n: int) -> list[dict]:
    days = inp.tradeable_days
    if n < 1 or len(days) < n + 1:
        return []
    size = len(days) // (n + 1)
    folds = []
    for i in range(1, n + 1):
        train, test = set(days[: i * size]), set(days[i * size:(i + 1) * size if i < n else None])
        a = tm.compute([t for t in inp.trades if t.trading_day in train], inp.capital, inp.params)
        b = tm.compute([t for t in inp.trades if t.trading_day in test], inp.capital, inp.params)
        folds.append({"fold": i, "train_days": len(train), "test_days": len(test),
                      "train_expectancy_r": a.expectancy_r, "test_expectancy_r": b.expectancy_r,
                      "test_trades": b.trades})
    return folds


# ── parameter-search trail (§18 item 9) ──────────────────────────────────────

def _search_trail(inp: ReportInputs) -> dict:
    mine = set(inp.tradeable_days)
    rows, undeclared, fitted = [], False, []
    cur_params = (inp.params_record or {}).get("params", {})
    for pr in inp.prior_runs:
        theirs = {date.fromisoformat(d) if isinstance(d, str) else d
                  for d in pr.get("tradeable_days", [])}
        if theirs and mine:
            overlap = len(mine & theirs) / len(mine)
        else:                              # no per-day record: fall back to the calendar window
            lo = max(pr["date_from"], inp.date_from)
            hi = min(pr["date_to"], inp.date_to)
            span = (inp.date_to - inp.date_from).days + 1
            overlap = max((hi - lo).days + 1, 0) / span if span > 0 else 0.0
        prior_params = (pr.get("params") or {}).get("params", {})
        changed = sorted(k for k in set(cur_params) | set(prior_params)
                         if json.dumps(cur_params.get(k), sort_keys=True, default=str)
                         != json.dumps(prior_params.get(k), sort_keys=True, default=str))
        rows.append({"run_id": pr["run_id"], "date_from": str(pr["date_from"]),
                     "date_to": str(pr["date_to"]), "overlap_fraction": round(overlap, 4),
                     "changed_keys": changed, "expectancy_r": pr.get("expectancy_r")})
        if overlap > inp.params.search_trail_overlap_threshold and not inp.notes.strip():
            undeclared = True
        if any(k in REGIME_GATING_KEYS for k in changed) and overlap > 0:
            fitted.append(pr["run_id"])
    return {"prior_runs": rows, "undeclared_search": undeclared,
            "fitted_regime_gating_after": fitted,
            "threshold": inp.params.search_trail_overlap_threshold}


# ── the six criteria (D10) ───────────────────────────────────────────────────

def _criteria(inp: ReportInputs, full: tm.MetricSet, r_ci: tuple, inr_ci: tuple,
              r_line: str, oos: dict) -> list[dict]:
    days_with_trades = len({t.trading_day for t in inp.trades})
    c = []

    def add(n, name, scope, passed, actual, rule):
        c.append({"n": n, "name": name, "set": scope, "passed": bool(passed),
                  "actual": actual, "rule": rule})

    add(1, "Sample size", "[full]", full.trades >= MIN_TRADES and days_with_trades >= MIN_DAYS,
        f"{full.trades} closed trades over {days_with_trades} trading days",
        f">= {MIN_TRADES} closed trades across >= {MIN_DAYS} distinct trading days")
    add(2, "Expectancy_R lower bound (gross of costs)", "[full, day-block]",
        r_ci[0] is not None and r_ci[0] > 0, r_line,
        "lower bound of the 95% day-block bootstrap CI on Expectancy_R strictly > 0")
    add(3, "Expectancy_INR lower bound (net of costs)", "[full, day-block]",
        inr_ci[0] is not None and inr_ci[0] > 0,
        _ci_text("Expectancy_INR [full, day-block]", inr_ci, 2),
        "lower bound of the 95% day-block bootstrap CI on Expectancy_INR strictly > 0 "
        "(the net-of-cost gate)")
    dd = full.max_equity_drawdown_pct
    add(4, "Max equity drawdown", "[full]", dd is not None and dd <= MAX_DRAWDOWN_PCT,
        f"{_f(dd, 2)}% of capital (Rs {_f(full.max_equity_drawdown_inr, 2)})",
        f"<= {MAX_DRAWDOWN_PCT:g}% of capital")
    lo = oos["in_sample_ci_day_block"][0]
    add(5, "One-sided out-of-sample consistency", "[oos] vs [in-sample, day-block]",
        oos["verdict"] in ("PASS", "OOS_FAVOURABLE"),
        f"Expectancy_R [oos] = {_f(oos['oos_expectancy_r'])} vs in-sample CI lower bound "
        f"{_f(lo)} -> {oos['verdict']}",
        "Expectancy_R(OOS) > 0 and >= the lower bound of the in-sample day-block CI")
    wr_ok = full.win_rate is not None and (
        full.win_rate >= 0.5 or (full.expectancy_r or 0) > 0 and (full.expectancy_inr or 0) > 0)
    add(6, "Win rate reported honestly", "[full]", wr_ok,
        f"win rate {_f(None if full.win_rate is None else full.win_rate * 100, 1)}%",
        "not required to exceed 50% when expectancy is positive; below 50% needs positive "
        "gross and net expectancy")
    return c


def build_validation(inp: ReportInputs) -> dict:
    p, it, seed = inp.params, inp.iterations, inp.seed
    synthetic = inp.synthetic if inp.synthetic is not None else "synthetic" in inp.label.lower()
    trades = list(inp.trades)
    full = tm.compute(trades, inp.capital, p)
    daily = tm.compute_daily(trades, inp.capital)

    if trades:
        r_ci, r_iid = _ci(trades, tm.expectancy_r, it, seed), _ci_iid(trades, tm.expectancy_r, it, seed)
        inr_ci, inr_iid = _ci(trades, tm.expectancy_inr, it, seed), _ci_iid(trades, tm.expectancy_inr, it, seed)
        wl, wh = stats.wilson_interval(full.wins, full.trades)
    else:
        r_ci = r_iid = inr_ci = inr_iid = (None, None)
        wl = wh = None
    # ONE string, used by item 4 and by criterion 2 in item 11, so they cannot disagree
    r_line = _ci_text("Expectancy_R [full, day-block]", r_ci)
    inr_line = _ci_text("Expectancy_INR [full, day-block]", inr_ci, 2)

    in_days, oos_days = split_days(list(inp.tradeable_days), inp.oos_split)
    oos = _oos_section(inp, in_days, oos_days)
    criteria = _criteria(inp, full, r_ci, inr_ci, r_line, oos)
    trail = _search_trail(inp)

    ltp_fallbacks = int(inp.stats_.get("ltp_only_fallbacks", 0) or 0)
    caps = []
    if ltp_fallbacks > 0 and not inp.companion_passing:
        caps.append(f"{ltp_fallbacks} LTP-only fills (allow_ltp_only_fill) without a passing companion "
                    f"run at allow_ltp_only_fill=False over the same window (D8)")
    if trail["undeclared_search"]:
        caps.append("undeclared parameter search detected (item 9)")
    if trail["fitted_regime_gating_after"]:
        caps.append("FITTED: regime gating changed after run "
                    + ", ".join(trail["fitted_regime_gating_after"])
                    + " over an overlapping window")
    if synthetic:
        caps.append(SYNTHETIC_BANNER)

    failing = [c for c in criteria if not c["passed"]]
    if not failing and not caps:
        rec, line = "CONSIDER", "RECOMMENDATION: CONSIDER"
    else:
        parts = [f"criterion {c['n']} ({c['name']})" for c in failing] + [f"cap: {x}" for x in caps]
        rec, line = "NOT YET", "RECOMMENDATION: NOT YET (" + "; ".join(parts) + ")"

    cov_summary = (inp.coverage or {}).get("summary", {})
    ps = [t.actual_entry for t in trades]
    lots = [t.lots for t in trades]
    sizing = {}
    for t in trades:
        k = t.sizing_method or "UNRECORDED"
        sizing[k] = sizing.get(k, 0) + 1
    by_strategy = tm.group_by(trades, "strategy", inp.capital, p)
    by_regime = tm.group_by(trades, "regime", inp.capital, p)
    by_sr = tm.group_by(trades, "strategy_regime", inp.capital, p)
    by_und = tm.group_by(trades, "underlying", inp.capital, p)
    uncl = {u: h.get("finding") for u, h in (inp.regime_hist or {}).items() if h.get("finding")}

    report = {
        "run_id": inp.run_id, "label": inp.label, "synthetic": synthetic,
        "banner": SYNTHETIC_BANNER if synthetic else None,
        "recommendation": rec, "recommendation_line": line, "failing_criteria": [c["n"] for c in failing],
        "caps": caps,
        "sections": {
            "1": {"run_id": inp.run_id, "label": inp.label, "date_from": inp.date_from.isoformat(),
                  "date_to": inp.date_to.isoformat(), "underlyings": list(inp.underlyings),
                  "seed": inp.seed, "code_version": inp.code_version, "params_hash": inp.params_hash,
                  "params": inp.params_record, "intra_candle_order": (inp.params_record or {}).get(
                      "intra_candle_order"), "bootstrap_iterations": it},
            "2": {"summary": cov_summary, "days": (inp.coverage or {}).get("days", []),
                  "engine": {k: inp.stats_.get(k) for k in (
                      "steps_from_ticks", "steps_from_snapshots", "candles_skipped_no_data",
                      "ltp_only_fallbacks", "days_replayed", "wall_seconds_per_underlying_day")},
                  "signals_by_strategy": inp.signals_by_strategy,
                  "oi_iv_context_missing": inp.oi_iv_by_day},
            "2a": {"by_underlying": inp.regime_hist, "findings": uncl},
            "3": {"closed_trades": full.trades, "low_sample": full.low_sample,
                  "min_display_sample": p.min_display_sample,
                  "per_strategy": {k: {"trades": v.trades, "low_sample": v.low_sample}
                                   for k, v in by_strategy.items()},
                  "per_regime": {k: {"trades": v.trades, "low_sample": v.low_sample}
                                 for k, v in by_regime.items()},
                  "r_unavailable": full.r_unavailable, "decided_r": full.decided_r,
                  "r_sample_size": full.r_sample_size, "total_trades": full.trades,
                  "sizing_method": sizing,
                  "lots": {"min": min(lots) if lots else None, "max": max(lots) if lots else None,
                           "mean": stats.mean(lots)},
                  "premium": {"min": min(ps) if ps else None, "median": stats.median(ps),
                              "max": max(ps) if ps else None},
                  "kelly_note": "kelly_min_trades = 30 is per strategy; a 100-trade window across 12 "
                                "strategies sizes every trade at FIXED_FRACTION and Kelly becomes "
                                "observable only around ~375 closed trades."},
            "4": {"metrics": tm.as_dict(full),
                  "win_rate_wilson_95": [wl, wh],
                  "wilson_note": "Wilson interval assumes independent trades, which trades are not; it gates nothing.",
                  "expectancy_r_line": r_line,
                  "expectancy_r_iid_line": _ci_text("Expectancy_R [full, iid, reference only]", r_iid),
                  "expectancy_inr_line": inr_line,
                  "expectancy_inr_iid_line": _ci_text("Expectancy_INR [full, iid, reference only]", inr_iid, 2),
                  "expectancy_r_ci_day_block": list(r_ci), "expectancy_inr_ci_day_block": list(inr_ci),
                  "expectancy_r_note": "Expectancy_R is GROSS of brokerage and statutory charges; "
                                       "Expectancy_INR and r_multiple_net are the net figures."},
            "5": tm.as_dict(daily),
            "6": {"by_strategy": {k: tm.as_dict(v) for k, v in by_strategy.items()},
                  "by_regime": {k: tm.as_dict(v) for k, v in by_regime.items()},
                  "by_strategy_regime": {k: tm.as_dict(v) for k, v in by_sr.items()},
                  "by_underlying": {k: tm.as_dict(v) for k, v in by_und.items()}},
            "7": dict(oos, walk_forward=_walk_forward(inp, inp.walk_forward)),
            "8": {"decisions": inp.no_trade, "cycle_totals": inp.cycle_totals},
            "9": {"declared": inp.notes.strip() or "No parameters were fitted to these results.",
                  "trail": trail},
            "10": {"win_rate_pct": None if full.win_rate is None else full.win_rate * 100,
                   "expectancy_r": full.expectancy_r, "expectancy_inr": full.expectancy_inr},
            "11": {"criteria": criteria, "caps": caps, "recommendation": rec,
                   "line": line},
            "12": {"limitations": LIMITATIONS, "fill_basis": _fill_basis(p),
                   "degradations": {"ltp_only_fallbacks": ltp_fallbacks,
                                    "candles_skipped_no_data": inp.stats_.get("candles_skipped_no_data"),
                                    "ltp_gap_exits": sum(1 for t in trades if t.exit_fill_source == "LTP_GAP"),
                                    "breakeven_exits_r_flat": full.r_flat_trades,
                                    "breakeven_exits_r_loss": sum(
                                        1 for t in trades if t.exit_reason == "BREAKEVEN" and t.r_valid
                                        and (t.r_multiple or 0) < -p.r_flat_epsilon)}},
        },
    }
    return json.loads(json.dumps(report, default=str))


def _fill_basis(p) -> dict:
    return {
        "entry": f"ask x (1 + {p.quote_slippage_pct}%), rounded up to the Rs 0.05 tick",
        "exit": f"bid x (1 - {p.quote_slippage_pct}%), rounded down to the tick",
        "ltp_fallback": f"+/- {p.slippage_pct}% (entry only when allow_ltp_only_fill)",
        "target": "trigger x (1 - 0.001)",
        "sl_gap_through": "min(base, ltp), labelled LTP_GAP",
        "costs": f"Rs {p.brokerage_per_lot_per_leg:g}/lot/leg brokerage; STT {p.stt_sell_pct}% sell, "
                 f"exchange {p.exchange_txn_pct}%, SEBI Rs {p.sebi_per_crore:g}/crore, stamp "
                 f"{p.stamp_buy_pct}% buy, GST {p.gst_pct:g}% (dated rates, design §15)",
    }


# ── markdown ─────────────────────────────────────────────────────────────────

def _table(headers: list, rows: list) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out + [""]


def _metric_row(name: str, m: dict) -> list:
    return [name, m["trades"], _f(None if m["win_rate"] is None else m["win_rate"] * 100, 1),
            _f(m["expectancy_r"], 3), _f(m["expectancy_inr"], 2), _f(m["profit_factor"], 2),
            _f(m["max_equity_drawdown_inr"], 0), "yes" if m["low_sample"] else ""]


def render_markdown(rep: dict) -> str:
    s = rep["sections"]
    L: list[str] = []
    if rep.get("banner"):
        L += [f"> **{rep['banner']}**", ""]
    L += [f"# Validation report — {rep['run_id']}", "", rep["recommendation_line"], ""]

    def h(key):
        L.append(f"## {key}. {SECTION_TITLES[key]}")
        L.append("")

    one = s["1"]
    h("1")
    L += [f"- run_id: `{one['run_id']}`   label: {one['label'] or '—'}",
          f"- window: {one['date_from']} to {one['date_to']}   underlyings: {', '.join(one['underlyings'])}",
          f"- seed: {one['seed']}   code_version: {one['code_version']}   params_hash: `{one['params_hash']}`",
          f"- intra-candle ordering: {one['intra_candle_order']}   bootstrap iterations: {one['bootstrap_iterations']}",
          "", "```json", json.dumps(one["params"], indent=2, sort_keys=True), "```", ""]

    h("2")
    for und, u in s["2"]["summary"].items():
        L.append(f"- **{und}**: {u['session_days']} weekdays, {u['tradeable']} TRADEABLE; skipped: "
                 f"{u['skipped'] or 'none'}; mean option ticks per tradeable day: "
                 f"{_f(u['mean_option_ticks_per_tradeable_day'], 0)}")
    L.append("")
    rows = [[d["underlying"], d["day"], d["status"], d["cause"] or "", d["spot_candles"],
             d["option_tokens_with_ticks"], d["option_tick_minutes"], d["snapshot_cadence_s"],
             d["nearest_dte"], d["chosen_expiry"], d["unattributed_tokens"]]
            for d in s["2"]["days"]]
    L += _table(["und", "day", "status", "cause", "candles", "opt tokens", "tick min",
                 "snap cadence s", "nearest DTE", "expiry", "unattributed tokens"], rows)
    L += [f"- engine: {json.dumps(s['2']['engine'], default=str)}",
          f"- signals by strategy: {json.dumps(s['2']['signals_by_strategy'])}",
          f"- oi_iv_context_missing per underlying-day: {json.dumps(s['2']['oi_iv_context_missing'])}", ""]

    h("2a")
    for und, hist in s["2a"]["by_underlying"].items():
        L.append(f"- **{und}** ({hist['total']} candles; structurally ineligible "
                 f"{hist['structurally_ineligible']}): {json.dumps(hist['counts'], sort_keys=True)}")
        if hist.get("finding"):
            L.append(f"  - {hist['finding']}")
    L.append("")

    t3 = s["3"]
    h("3")
    L += [f"- closed trades: {t3['closed_trades']}   low_sample: {t3['low_sample']}   "
          f"r_unavailable: {t3['r_unavailable']}",
          f"- denominators: decided_r={t3['decided_r']}  r_sample_size={t3['r_sample_size']}  "
          f"total_trades={t3['total_trades']}",
          f"- sizing_method: {json.dumps(t3['sizing_method'])}",
          f"- lots: {json.dumps(t3['lots'])}   premium: {json.dumps(t3['premium'])}",
          f"- {t3['kelly_note']}", ""]
    L += _table(["strategy", "trades", "low_sample"],
                [[k, v["trades"], v["low_sample"]] for k, v in t3["per_strategy"].items()])

    t4 = s["4"]
    m = t4["metrics"]
    h("4")
    L += [f"- win rate: {_f(None if m['win_rate'] is None else m['win_rate'] * 100, 1)}%  "
          f"(Wilson 95%: {_f(t4['win_rate_wilson_95'][0])}-{_f(t4['win_rate_wilson_95'][1])}; "
          f"{t4['wilson_note']})",
          f"- {t4['expectancy_r_line']}", f"- {t4['expectancy_r_iid_line']}",
          f"- {t4['expectancy_inr_line']}", f"- {t4['expectancy_inr_iid_line']}",
          f"- Expectancy_R = {_f(m['expectancy_r'])} (gross); Expectancy_INR = {_f(m['expectancy_inr'], 2)} (net); "
          f"mean r_multiple_net = {_f(m['expectancy_r_net'])}",
          f"- profit factor {_f(m['profit_factor'], 2)}; max equity drawdown Rs {_f(m['max_equity_drawdown_inr'], 2)} "
          f"({_f(m['max_equity_drawdown_pct'], 2)}% of capital); longest losing streak "
          f"{m['longest_losing_streak']}; avg hold {_f(m['avg_hold_minutes'], 1)} min",
          f"- {t4['expectancy_r_note']}", ""]

    d5 = s["5"]
    h("5")
    L += [f"- trading days {d5['trading_days']} ({d5['positive_days']} positive); mean daily P&L "
          f"{_f(d5['mean_daily_pnl'], 2)}; stdev {_f(d5['stdev_daily_pnl'], 2)}",
          f"- Sharpe-like {_f(d5['sharpe_like'], 3)}. {d5['sharpe_assumption']}",
          f"- Sortino-like {_f(d5['sortino_like'], 3)}. {d5['sortino_assumption']}", ""]

    h("6")
    for title, key in (("By strategy", "by_strategy"), ("By regime", "by_regime"),
                       ("By strategy x regime", "by_strategy_regime"),
                       ("By underlying", "by_underlying")):
        L.append(f"**{title}**")
        L.append("")
        L += _table(["group", "trades", "win %", "Exp R", "Exp INR", "PF", "max DD", "low sample"],
                    [_metric_row(k, v) for k, v in s["6"][key].items()])

    o = s["7"]
    h("7")
    L += [f"- split at {_f(o['split_fraction'], 2)} of tradeable days; first OOS day {o['split_date']}; "
          f"{o['in_sample_days']} in-sample / {o['oos_days']} OOS days",
          f"- Expectancy_R [in-sample, day-block] {_ci_text('', tuple(o['in_sample_ci_day_block'])).strip()}"
          f"   [in-sample, iid, reference only] {_ci_text('', tuple(o['in_sample_ci_iid'])).strip()}",
          f"- Expectancy_R [oos] point estimate {_f(o['oos_expectancy_r'])}   "
          f"[oos, day-block] {_ci_text('', tuple(o['oos_ci_day_block'])).strip()}   "
          f"[oos, iid, reference only] {_ci_text('', tuple(o['oos_ci_iid'])).strip()}",
          f"- verdict (one-sided): **{o['verdict']}**", ""]
    L += _table(["strategy", "IS trades", "OOS trades", "OOS Exp R", "IS CI lower", "verdict", "low sample"],
                [[k, v["in_sample_trades"], v["oos_trades"], _f(v["oos_expectancy_r"], 3),
                  _f(v["in_sample_ci_day_block"][0], 3), v["verdict"], v["low_sample"]]
                 for k, v in o["per_strategy"].items()])
    if o.get("walk_forward"):
        L += _table(["fold", "train days", "test days", "train Exp R", "test Exp R", "test trades"],
                    [[f["fold"], f["train_days"], f["test_days"], _f(f["train_expectancy_r"], 3),
                      _f(f["test_expectancy_r"], 3), f["test_trades"]] for f in o["walk_forward"]])

    h("8")
    L += _table(["stage", "code", "count"], [[d["stage"], d["code"], d["count"]]
                                              for d in s["8"]["decisions"]])
    L += [f"- cycle totals: {json.dumps(s['8']['cycle_totals'])}", ""]

    n9 = s["9"]
    h("9")
    L += [f"- declared: {n9['declared']}",
          f"- Parameter-search trail: {len(n9['trail']['prior_runs'])} prior completed run(s) over an "
          f"overlapping window with different parameters"]
    L += _table(["run", "window", "overlap", "changed keys", "Exp R"],
                [[r["run_id"], f"{r['date_from']}..{r['date_to']}", r["overlap_fraction"],
                  ", ".join(r["changed_keys"]), _f(r["expectancy_r"], 3)]
                 for r in n9["trail"]["prior_runs"]])
    if n9["trail"]["undeclared_search"]:
        L.append("**WARNING: undeclared parameter search detected**")
    for rid in n9["trail"]["fitted_regime_gating_after"]:
        L.append(f"**FITTED: regime gating changed after run {rid} over this window**")
    L.append("")

    n10 = s["10"]
    h("10")
    wr = n10["win_rate_pct"]
    L += [f"- Win rate is {_f(wr, 1)}%" + (" — below 50%, stated plainly next to an Expectancy_R of "
                                           f"{_f(n10['expectancy_r'])} and Expectancy_INR of "
                                           f"{_f(n10['expectancy_inr'], 2)}." if wr is not None and wr < 50
                                           else "."),
          "- Expectancy_R is GROSS of brokerage and statutory charges. Expectancy_INR and r_multiple_net "
          "are the net figures.",
          "- Satisfying every criterion authorises nothing. R1 (paper trading only) remains absolute.", ""]

    n11 = s["11"]
    h("11")
    L += [n11["line"], ""]
    for c in n11["criteria"]:
        L.append(f"- criterion {c['n']} {c['set']} **{'PASS' if c['passed'] else 'FAIL'}** — {c['name']}: "
                 f"{c['actual']}  (rule: {c['rule']})")
    for cap in n11["caps"]:
        L.append(f"- CAP -> NOT YET: {cap}")
    L += ["", "\"Consider\" is the strongest verdict this report can produce.", ""]

    n12 = s["12"]
    h("12")
    for i, lim in enumerate(n12["limitations"], 1):
        L.append(f"{i}. {lim}")
    L += ["", "**Fill basis**", ""] + [f"- {k}: {v}" for k, v in n12["fill_basis"].items()]
    L += ["", f"- run-specific degradations: {json.dumps(n12['degradations'])}", ""]
    return "\n".join(L)


# ── engine hook ──────────────────────────────────────────────────────────────

async def gather_inputs(engine, args, result, p, cfg, record) -> ReportInputs:
    from sqlalchemy import func, select

    from app.db.models.paper_trade import StrategySignal
    from app.db.models.replay import ReplayRun
    from app.db.models.risk import CycleStats, NoTradeDecision
    from app.services.replay import coverage as coverage_mod

    run_id = result.run_id
    async with engine.sessions() as db:
        ntd = (await db.execute(
            select(NoTradeDecision.stage, NoTradeDecision.code, func.count(NoTradeDecision.id))
            .where(NoTradeDecision.run_id == run_id)
            .group_by(NoTradeDecision.stage, NoTradeDecision.code)
            .order_by(NoTradeDecision.stage, NoTradeDecision.code))).all()
        cs = (await db.execute(select(CycleStats).where(CycleStats.run_id == run_id)
                               .order_by(CycleStats.trading_day, CycleStats.underlying))).scalars().all()
        sigs = (await db.execute(
            select(StrategySignal.strategy_name, func.count(StrategySignal.id))
            .where(StrategySignal.run_id == run_id)
            .group_by(StrategySignal.strategy_name))).all()
        hist = await coverage_mod.regime_histogram(db, run_id)
        priors = (await db.execute(select(ReplayRun).where(
            ReplayRun.status == "COMPLETE", ReplayRun.params_hash != result.params_hash,
            ReplayRun.run_id != run_id, ReplayRun.date_from <= args.date_to,
            ReplayRun.date_to >= args.date_from).order_by(ReplayRun.created_at))).scalars().all()
    totals: dict = {}
    for r in cs:
        t = totals.setdefault(r.underlying, {"candles_evaluated": 0, "strategies_evaluated": 0,
                                             "signals_emitted": 0, "rejections_recorded": 0,
                                             "oi_iv_context_missing": 0})
        t["candles_evaluated"] += r.candles_evaluated
        t["strategies_evaluated"] += r.strategies_evaluated
        t["signals_emitted"] += r.signals_emitted
        t["rejections_recorded"] += r.rejections_recorded
        t["oi_iv_context_missing"] += r.oi_iv_context_missing
    cov = result.coverage
    prior_runs = []
    for r in priors:
        days = [d["day"] for d in ((r.coverage or {}).get("days") or []) if d.get("status") == "TRADEABLE"]
        prior_runs.append({
            "run_id": r.run_id, "date_from": r.date_from, "date_to": r.date_to,
            "params": r.params, "tradeable_days": sorted(set(days)),
            "expectancy_r": (r.metrics or {}).get("expectancy_r")})
    return ReportInputs(
        run_id=run_id, date_from=args.date_from, date_to=args.date_to,
        underlyings=tuple(args.underlyings), seed=args.seed, code_version=_code_version(),
        params_record=record, params_hash=result.params_hash, params=p, capital=cfg.capital,
        trades=list(result.trades), coverage=cov.as_dict() if cov else {},
        tradeable_days=cov.tradeable_days() if cov else [], label=args.label, notes=args.notes,
        stats_=dict(result.stats), regime_hist=hist,
        no_trade=[{"stage": s_, "code": c_, "count": int(n)} for s_, c_, n in ntd],
        cycle_totals=totals,
        oi_iv_by_day=[{"underlying": r.underlying, "day": r.trading_day.isoformat(),
                       "missing": r.oi_iv_context_missing} for r in cs],
        signals_by_strategy={n: int(c) for n, c in sigs},
        prior_runs=prior_runs, oos_split=args.oos_split, walk_forward=args.walk_forward,
        iterations=args.bootstrap_iterations, synthetic=args.synthetic,
    )


def _code_version() -> str:
    from app.services.replay.engine import code_version
    return code_version()


def write_artefacts(rep: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / f"{rep['run_id']}.md", out_dir / f"{rep['run_id']}.json"
    md.write_text(render_markdown(rep), encoding="utf-8")
    js.write_text(json.dumps(rep, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return [md, js]


async def build_and_write(engine, args, result, p, cfg, record) -> None:
    """ReplayEngine hook: build the report, attach it to the result and write the artefacts."""
    inp = await gather_inputs(engine, args, result, p, cfg, record)
    rep = build_validation(inp)
    result.validation = rep
    if args.out_dir is not None:
        result.artefacts = write_artefacts(rep, Path(args.out_dir))
