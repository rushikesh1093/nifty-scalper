"""
Run the full research protocol and write machine-readable results.

    python -m app.research.cli --db <candles.db> [--options-db <live_options.db>]
                               --out <dir> [--workers 10] [--start YYYY-MM-DD] [--end ...]

Writes into --out only: manifest.json, data_quality.json, thresholds.json,
trades_<variant>.csv, exit_variants.csv, summary.json, walk_forward.json,
regime_validation.json, option_execution.json, option_replay.csv.
Deterministic: same inputs and commit → same files (bootstrap seed fixed).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import date
from pathlib import Path

from app.research import data, metrics, sim, walkforward as wf

DESIGN_MONTHS = 12
STRATEGIES = ("FIRST_CANDLE_BREAKOUT", "ORB_BREAKOUT", "SUPERTREND_EMA",
              "EMA_RSI_PULLBACK_15M", "PIVOT_BOUNCE")


# ── picklable strategy specs: (variant id, strategy, class name, kwargs) ─────

def baseline_specs() -> list[tuple]:
    return [("FCB_BASE", "FIRST_CANDLE_BREAKOUT", "FirstCandleBreakoutStrategy", {}),
            ("ORB_BASE", "ORB_BREAKOUT", "ORBBreakoutStrategy", {}),
            ("ST_BASE", "SUPERTREND_EMA", "SupertrendEMAStrategy", {}),
            ("ER_BASE", "EMA_RSI_PULLBACK_15M", "EMARSIPullback15mStrategy", {}),
            ("PV_BASE", "PIVOT_BOUNCE", "PivotBounceStrategy", {})]


def new_specs() -> list[tuple]:
    """The candidates of docs/NEW_STRATEGY_PREREGISTRATION.md (one rule each)."""
    return [("SQUEEZE", "VOL_SQUEEZE_BREAKOUT", "VolSqueezeBreakout", {}),
            ("TREND_DAY", "TREND_DAY_CONTINUATION", "TrendDayContinuation", {}),
            ("GAP_FADE", "GAP_FADE", "GapFade", {}),
            ("VWAP_REV", "VWAP_EXTREME_REVERSION", "VWAPExtremeReversion", {})]


def _cls(cls_name: str):
    from app.research import new_strategies, variants
    from app.strategies import strategies
    for mod in (variants, new_strategies, strategies):
        if hasattr(mod, cls_name):
            return getattr(mod, cls_name)
    raise KeyError(cls_name)


def variant_specs(th: dict) -> list[tuple]:
    return [
        ("FCB_STRONG", "FIRST_CANDLE_BREAKOUT", "FCBStrong",
         {"body_min": th["fcb_body_min"], "close_loc_min": th["fcb_close_loc_min"]}),
        ("FCB_RETEST", "FIRST_CANDLE_BREAKOUT", "FCBRetest", {}),
        ("ORB_DISPLACEMENT", "ORB_BREAKOUT", "ORBFilter",
         {"variant": "ORB_DISPLACEMENT", "threshold": th["orb_disp_min"]}),
        ("ORB_VOLUME", "ORB_BREAKOUT", "ORBFilter",
         {"variant": "ORB_VOLUME", "threshold": th["orb_relvol_min"]}),
        ("ORB_VWAP", "ORB_BREAKOUT", "ORBFilter", {"variant": "ORB_VWAP"}),
        ("ORB_RETEST", "ORB_BREAKOUT", "ORBRetest", {}),
        ("ST_EMA921", "SUPERTREND_EMA", "STVariant", {"variant": "ST_EMA921"}),
        ("ST_PULLBACK", "SUPERTREND_EMA", "STVariant", {"variant": "ST_PULLBACK"}),
        ("ST_STRUCTURE", "SUPERTREND_EMA", "STVariant", {"variant": "ST_STRUCTURE"}),
        ("ER_3M", "EMA_RSI_PULLBACK_15M", "ER3m", {"variant": "ER_3M"}),
        ("ER_3M_CONFIRM", "EMA_RSI_PULLBACK_15M", "ER3m", {"variant": "ER_3M_CONFIRM"}),
        ("PV_SWEEP_CLOSE", "PIVOT_BOUNCE", "PivotSweep",
         {"variant": "PV_SWEEP_CLOSE", "sweep_min": th["pv_sweep_min"]}),
        ("PV_SWEEP_CONFIRM", "PIVOT_BOUNCE", "PivotSweep",
         {"variant": "PV_SWEEP_CONFIRM", "sweep_min": th["pv_sweep_min"]}),
    ]


def _factory(cls_name: str, kwargs: dict):
    cls = _cls(cls_name)
    return lambda: cls(**kwargs)


def _task(args) -> dict:
    """Worker: one spec over one chunk of consecutive sessions."""
    spec, chunk, tail, prev, with_exits = args
    vid, _strategy, cls_name, kwargs = spec
    from app.research.features import make_feature_fn
    bar = getattr(_cls(cls_name), "BAR_MINUTES", 1)
    trades = sim.run_days(_factory(cls_name, kwargs), chunk, tail, prev, vid,
                          feature_fn=make_feature_fn(bar))
    by_day = {str(s.day): s for s in chunk}
    tails = {}
    t = list(tail)
    for s in chunk:
        tails[str(s.day)] = t[-sim.HISTORY_CANDLES:]
        t = (t + s.candles)[-sim.HISTORY_CANDLES:]
    rows, exits = [], []
    for tr in trades:
        sess = by_day[tr.day]
        tr.features.update(sim.excursions(sess, tr))
        rows.append(tr.row())
        if with_exits:
            for ev in sim.EXIT_VARIANTS[1:]:
                res = sim.walk_exit(sess, tr, ev, tails[tr.day])
                exits.append({"variant": vid, "exit_variant": ev, "day": tr.day,
                              "entry_time": tr.entry_time, "side": tr.side,
                              "risk_pts": tr.risk_pts, "expiry": tr.features.get("expiry"),
                              "regime": tr.features.get("regime"), "r_gross": res["r"],
                              "hold_min": res["hold_min"], "exit_reason": res["reason"],
                              "ambiguous": res["ambiguous"]})
    return {"vid": vid, "rows": rows, "exits": exits, "trades": trades if with_exits else []}


def run_specs(specs, ds: data.Dataset, workers: int, with_exits: bool) -> dict:
    n = len(ds.sessions)
    size = max(20, n // (workers * 2) + 1)
    jobs = []
    for spec in specs:
        for i in range(1, n, size):
            chunk = ds.sessions[i:i + size]
            prev = ds.sessions[i - 1]
            tail = [c for s in ds.sessions[max(0, i - 2):i] for c in s.candles]
            jobs.append((spec, chunk, tail, prev, with_exits))
    out = defaultdict(lambda: {"rows": [], "exits": [], "trades": []})
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_task, jobs, chunksize=1):
            o = out[res["vid"]]
            o["rows"].extend(res["rows"]); o["exits"].extend(res["exits"])
            o["trades"].extend(res["trades"])
    for o in out.values():
        o["rows"].sort(key=lambda r: (r["day"], r["entry_time"]))
    return dict(out)


def design_thresholds(rows_by_vid: dict, design_end: str) -> dict:
    """Medians of baseline signal features in the DESIGN window only."""
    def med(vid, key, default):
        v = [r[f"f_{key}"] for r in rows_by_vid[vid]["rows"]
             if r["day"] <= design_end and r.get(f"f_{key}") is not None]
        return round(statistics.median(v), 3) if v else default
    import re
    disp = []
    for r in rows_by_vid["ORB_BASE"]["rows"]:
        if r["day"] > design_end:
            continue
        m = re.search(r"width=([0-9.]+)", r["notes"])
        if m and r.get("f_bar_range_atr") is not None and r.get("f_atr5"):
            disp.append(r["f_bar_body_ratio"] * r["f_bar_range_atr"] * r["f_atr5"] / float(m.group(1)))
    return {"design_end": design_end,
            "fcb_body_min": med("FCB_BASE", "bar_body_ratio", 0.5),
            "fcb_close_loc_min": med("FCB_BASE", "bar_close_loc", 0.7),
            "orb_disp_min": round(statistics.median(disp), 3) if disp else 0.3,
            "orb_relvol_min": med("ORB_BASE", "rel_fut_vol", 1.5),
            "pv_sweep_min": max(1.0, med("PV_BASE", "sweep_depth", 5.0)),
            "method": "median of the baseline signals' trigger-bar feature, DESIGN window only"}


def git_commit() -> dict:
    def g(*a):
        try:
            return subprocess.run(["git", *a], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return ""
    return {"commit": g("rev-parse", "HEAD"), "dirty": bool(g("status", "--porcelain", "--", "."))}


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _costed(rows, model):
    out = []
    for r in rows:
        x = {"day": r["day"], "entry_time": r["entry_time"], "r_gross": r["r_gross"],
             "risk_pts": r["risk_pts"], "hold_min": r["hold_min"], "expiry": r.get("f_expiry"),
             "regime": r.get("f_regime"), "side": r["side"], "exit_reason": r["exit_reason"],
             "mfe_r": r["mfe_r"], "mae_r": r["mae_r"], "signal_time": r["signal_time"],
             "vol": r.get("f_vol"), "pivot_level": r.get("f_pivot_level"),
             "trend_align": r.get("f_trend_align"), "ambiguous": r["ambiguous"]}
        out.append(wf.apply_costs(x, model))
    return out


def signal_quality(rows: list[dict]) -> dict:
    """Experiment A statistics on the underlying (no costs)."""
    n = len(rows)
    if not n:
        return {"n": 0}
    first = defaultdict(int)
    for r in rows:
        first[r.get("f_first")] += 1
    d30 = [r["f_dir_30m"] for r in rows if r.get("f_dir_30m") is not None]
    t1 = [r["f_t_1r"] for r in rows if r.get("f_t_1r") is not None]
    ts = [r["f_t_stop"] for r in rows if r.get("f_t_stop") is not None]
    hit1 = sum(1 for r in rows if r.get("f_t_1r") is not None
               and (r.get("f_t_stop") is None or r["f_t_1r"] < r["f_t_stop"]))
    return {
        "n": n,
        "directional_hit_30m": round(sum(d30) / len(d30), 4) if d30 else None,
        "directional_hit_30m_ci95": metrics.wilson(sum(d30), len(d30)),
        "hit_1r_before_stop": round(hit1 / n, 4),
        "hit_1r_before_stop_ci95": metrics.wilson(hit1, n),
        "hit_2r_before_stop": round(first.get("2R", 0) / n, 4),
        "stop_first": round(first.get("STOP", 0) / n, 4),
        "median_mfe_eod_r": round(statistics.median(r["f_mfe_eod_r"] for r in rows), 3),
        "median_mae_eod_r": round(statistics.median(r["f_mae_eod_r"] for r in rows), 3),
        "median_min_to_1r": statistics.median(t1) if t1 else None,
        "median_min_to_stop": statistics.median(ts) if ts else None,
        "mean_r_gross_live_exits": round(statistics.mean(r["r_gross"] for r in rows), 4),
        "ambiguous_candles": sum(1 for r in rows if r["ambiguous"]),
    }


_INT = {"entry_idx", "exit_idx"}
_FLOAT = {"plan_entry", "entry", "stop0", "target", "risk_pts", "exit_px", "r_gross", "hold_min",
          "mfe_r", "mae_r"}
_BOOL = {"ambiguous", "f_transition", "f_expiry", "expiry"}


def _typed(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if v == "" or v is None:
            out[k] = None
        elif k in _BOOL:
            out[k] = v == "True"
        elif k in _INT:
            out[k] = int(v)
        elif k in _FLOAT or k.startswith("f_") or k in ("r_gross",):
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
        else:
            out[k] = v
    return out


def load_results(out: Path, base_specs: list, var_specs: list) -> tuple[dict, dict]:
    """Trade rows (typed) per variant, exit rows, and Trade objects for the
    baselines (option replay), read back from the CSVs."""
    fields = {f.name for f in sim.Trade.__dataclass_fields__.values()} - {"features"}

    def read(vid, with_exits):
        p = out / f"trades_{vid}.csv"
        rows = [_typed(r) for r in csv.DictReader(p.open(encoding="utf-8"))] if p.stat().st_size else []
        for r in rows:
            r["f_regime"] = None if r.get("f_regime") is None else str(r["f_regime"])
        trades = [sim.Trade(**{k: r[k] for k in fields if k in r}) for r in rows] if with_exits else []
        return {"rows": rows, "exits": [], "trades": trades}

    base = {s[0]: read(s[0], True) for s in base_specs}
    var = {s[0]: read(s[0], False) for s in var_specs}
    p = out / "exit_variants.csv"
    for e in (_typed(r) for r in csv.DictReader(p.open(encoding="utf-8"))):
        e["r_gross"] = float(e["r_gross"])
        base[e["variant"]]["exits"].append(e)
    return base, var


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--options-db")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--design-months", type=int, default=DESIGN_MONTHS)
    ap.add_argument("--suite", choices=("live", "new"), default="live",
                    help="live: the five live strategies and their variants; new: the "
                         "pre-registered candidates (docs/NEW_STRATEGY_PREREGISTRATION.md)")
    ap.add_argument("--analyse-only", action="store_true",
                    help="skip the simulation; re-analyse the CSVs already in --out")
    a = ap.parse_args(argv)
    t0 = time.time()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    ds = data.load(a.db, date.fromisoformat(a.start) if a.start else None,
                   date.fromisoformat(a.end) if a.end else None)
    from app.research.features import annotate
    annotate(ds.sessions)
    months = wf.months_between(str(ds.days[0]), str(ds.days[-1]))
    design_end_month = months[a.design_months - 1]
    design_end = max(str(d) for d in ds.days if str(d)[:7] <= design_end_month)
    (out / "data_quality.json").write_text(json.dumps(ds.quality, indent=1))
    print(f"[research] {len(ds.sessions)} sessions, design window ends {design_end}", flush=True)

    base_specs = baseline_specs() if a.suite == "live" else new_specs()
    if not a.analyse_only:
        base = run_specs(base_specs, ds, a.workers, with_exits=True)
        th = (design_thresholds(base, design_end) if a.suite == "live"
              else {"design_end": design_end, "suite": "new", "method": "pre-registered constants"})
        (out / "thresholds.json").write_text(json.dumps(th, indent=1))
        print(f"[research] baselines done {time.time() - t0:.0f}s thresholds {th}", flush=True)
        var = run_specs(variant_specs(th), ds, a.workers, with_exits=False) if a.suite == "live" else {}
        print(f"[research] variants done {time.time() - t0:.0f}s", flush=True)
        for vid, o in {**base, **var}.items():
            _write_csv(out / f"trades_{vid}.csv", o["rows"])
        _write_csv(out / "exit_variants.csv", [e for o in base.values() for e in o["exits"]])
    # The analysis always reads the written CSVs back, so --analyse-only (new
    # cost model, new statistics) reproduces it without re-simulating.
    th = json.loads((out / "thresholds.json").read_text())
    var_specs = variant_specs(th) if a.suite == "live" else []
    base, var = load_results(out, base_specs, var_specs)
    allv = {**base, **var}
    spec_strategy = {s[0]: s[1] for s in base_specs + var_specs}
    strategies = list(dict.fromkeys(s[1] for s in base_specs))

    summary = {"cost_models": {k: asdict(v) for k, v in wf.COST_MODELS.items()},
               "primary_cost_model": wf.PRIMARY_COST, "design_end": design_end, "suite": a.suite,
               "base_variants": [s[0] for s in base_specs], "variants": {}}
    for vid, o in sorted(allv.items()):
        rows = o["rows"]
        ent = {"strategy": spec_strategy[vid], "signal_quality": signal_quality(rows)}
        for cm in wf.COST_MODELS.values():
            c = _costed(rows, cm)
            ent[cm.name] = {"all": metrics.summarize(c),
                            "design": metrics.summarize([r for r in c if r["day"] <= design_end]),
                            "post_design": metrics.summarize([r for r in c if r["day"] > design_end])}
        c = _costed(rows, wf.COST_MODELS[wf.PRIMARY_COST])
        post = [r for r in c if r["day"] > design_end]
        ent["breakdowns_post_design"] = {
            "side": metrics.breakdown(post, lambda r: r["side"]),
            "regime": metrics.breakdown(post, lambda r: r["regime"]),
            "vol": metrics.breakdown(post, lambda r: r["vol"]),
            "trend_align": metrics.breakdown(post, lambda r: r["trend_align"]),
            "time_of_day": metrics.breakdown(post, lambda r: metrics.time_bucket(r["signal_time"])),
            "weekday": metrics.breakdown(post, lambda r: date.fromisoformat(r["day"]).strftime("%a")),
            "month": metrics.breakdown(post, lambda r: r["day"][:7]),
            "expiry": metrics.breakdown(post, lambda r: bool(r["expiry"])),
            "exit_reason": metrics.breakdown(post, lambda r: r["exit_reason"]),
            "pivot_level": metrics.breakdown(post, lambda r: r["pivot_level"]),
        }
        summary["variants"][vid] = ent

    # exit-engine variants on the baseline entries (identical entries)
    exits = {}
    for vid, o in base.items():
        by_ev = defaultdict(list)
        for e in o["exits"]:
            by_ev[e["exit_variant"]].append(e)
        by_ev["LIVE"] = [{"day": r["day"], "entry_time": r["entry_time"], "r_gross": r["r_gross"],
                          "risk_pts": r["risk_pts"], "hold_min": r["hold_min"],
                          "expiry": r.get("f_expiry")} for r in o["rows"]]
        exits[vid] = {}
        for ev, rows in by_ev.items():
            rows.sort(key=lambda r: (r["day"], r["entry_time"]))
            ent = {}
            for cm in wf.COST_MODELS.values():
                c = [wf.apply_costs(r, cm) for r in rows]
                ent[cm.name] = {"design": metrics.summarize([r for r in c if r["day"] <= design_end], boot=False),
                                "post_design": metrics.summarize([r for r in c if r["day"] > design_end])}
            exits[vid][ev] = ent
    summary["exit_variants"] = exits

    # walk-forward per strategy over its entry candidates, every cost model
    wfo = {"protocol": wf.__doc__, "criteria": wf.criteria_dict(), "design_end": design_end,
           "folds_definition": wf.folds(months, design_end), "strategies": {}}
    status = {}
    for strat in strategies:
        cands = {vid: o["rows"] for vid, o in allv.items() if spec_strategy[vid] == strat}
        wfo["strategies"][strat] = {}
        for cm in wf.COST_MODELS.values():
            costed = {vid: _costed(rows, cm) for vid, rows in cands.items()}
            res = wf.walk_forward(costed, months, design_end)
            base_vid = next(s[0] for s in base_specs if s[1] == strat)
            design_exp = summary["variants"][base_vid][cm.name]["design"].get("expectancy_r")
            st, why = wf.classify_status(res["oos"], design_exp, res["folds_traded"],
                                         res["folds_positive"])
            fixed = {vid: summary["variants"][vid][cm.name]["post_design"] for vid in cands}
            res.pop("oos_rows")
            wfo["strategies"][strat][cm.name] = {**res, "status": st, "status_reasons": why,
                                                 "fixed_candidates_post_design": fixed}
            if cm.name == wf.PRIMARY_COST:
                status[strat] = {"status": st, "reasons": why}
    (out / "walk_forward.json").write_text(json.dumps(wfo, indent=1, default=str))
    summary["status"] = status

    # option execution on recorded quotes
    if a.options_db:
        from app.research import options
        q = options.Quotes(a.options_db)
        study = options.execution_study(q)
        rep = []
        for vid, o in base.items():
            for steps in (0, 1):
                rep.extend(options.replay_trades(o["trades"], q, steps))
        _write_csv(out / "option_replay.csv", rep)
        (out / "option_execution.json").write_text(json.dumps(study, indent=1))
        summary["option_replay_counts"] = {
            "rows": len(rep), "filled": sum(1 for r in rep if r["status"] == "FILLED")}

    (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    cfg = {"thresholds": th, "cost_models": summary["cost_models"],
           "specs": base_specs + var_specs, "suite": a.suite, "exit_variants": sim.EXIT_VARIANTS,
           "criteria": wf.criteria_dict(), "design_months": a.design_months}
    manifest = {**git_commit(), "dataset": ds.identifier(),
                "options_dataset": ({"file": Path(a.options_db).name,
                                     "sha256": data.file_sha256(a.options_db)} if a.options_db else None),
                "date_range": [str(ds.days[0]), str(ds.days[-1])],
                "config_hash": hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest(),
                "config": cfg, "execution_model_version": sim.EXECUTION_MODEL_VERSION,
                "cost_model_version": "spot-points-v2 (ZERO/MEASURED/HARSH)",
                "bootstrap_seed": metrics.BOOTSTRAP_SEED, "python": sys.version.split()[0],
                "runtime_s": round(time.time() - t0, 1)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    print(f"[research] done in {time.time() - t0:.0f}s -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
