"""
Regime classifier validation (Part 6), independent of any strategy.

    python -m app.research.regime_validation --db <candles.db> --out <dir> --design-end YYYY-MM-DD

For every session, the causal label at each 5-min close (09:45-15:00), then:
  * stability: label changes per session hour, mean run length in bars;
  * does the label say anything about the NEXT 30 minutes? signed move
    (in ATR) for trend labels and absolute move (range) by volatility bucket,
    separately for the design window and the later months;
  * per-strategy expectancy by label from a research run (summary.json), design
    vs post-design, with sign agreement — the evidence a regime->strategy map
    would need.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from app.research import data, regime
from app.research.features import annotate
from app.strategies.indicators import resample


def label_series(ds: data.Dataset) -> list[dict]:
    out = []
    tail: list[dict] = []
    for s in ds.sessions:
        b5_hist = resample(tail, 5)
        today = resample(s.candles, 5)
        fut = {b["start"]: b["volume"] for b in resample(s.fut_candles, 5)} if s.fut_candles else {}
        allb = b5_hist + today
        n_hist = len(b5_hist)
        for j in range(6, len(today) - 6):
            bars = allb[:n_hist + j + 1][-150:]
            today_from = len(bars) - (j + 1)
            r = regime.classify(bars, today_from, s.ctx.get("prior_ranges", []), fut)
            if r.get("atr5") is None:
                continue
            c0 = today[j]["close"]
            nxt = today[j + 1:j + 7]
            out.append({"day": str(s.day), "t": today[j]["end"].strftime("%H:%M"), "label": r["label"],
                        "structure": r["structure"], "vol": r["vol"], "transition": r["transition"],
                        "fwd_move_atr": (nxt[-1]["close"] - c0) / r["atr5"],
                        "fwd_range_atr": (max(b["high"] for b in nxt) - min(b["low"] for b in nxt)) / r["atr5"],
                        "fwd_range_pts": max(b["high"] for b in nxt) - min(b["low"] for b in nxt)})
        tail = (tail + s.candles)[-500:]
    return out


def _stats(rows, key):
    """Mean of *key* and its standard error from per-SESSION means (the
    30-min windows overlap, so bar-level errors would be ~2.5x too small)."""
    by = defaultdict(list)
    for r in rows:
        by[r["day"]].append(r[key])
    vals = [v for r in rows for v in (r[key],)]
    dm = [statistics.mean(v) for v in by.values()]
    return {"n": len(vals), "sessions": len(dm),
            "mean": round(statistics.mean(vals), 4) if vals else None,
            "se_session": round(statistics.stdev(dm) / len(dm) ** 0.5, 4) if len(dm) > 1 else None}


def validate(rows: list[dict], design_end: str) -> dict:
    res = {"design_end": design_end, "periods": {}}
    for name, sel in (("design", lambda r: r["day"] <= design_end), ("post_design", lambda r: r["day"] > design_end)):
        rs = [r for r in rows if sel(r)]
        by_day = defaultdict(list)
        for r in rs:
            by_day[r["day"]].append(r["label"])
        changes = sum(sum(1 for a, b in zip(v, v[1:]) if a != b) for v in by_day.values())
        runs = [sum(1 for _ in g) for v in by_day.values() for g in _groups(v)]
        per = {"bars": len(rs), "sessions": len(by_day),
               "label_share": {k: round(v / len(rs), 4) for k, v in Counter(r["label"] for r in rs).most_common()},
               "label_changes_per_hour": round(changes / max(1, len(rs)) * 12, 3),
               "mean_run_bars": round(statistics.mean(runs), 2) if runs else None,
               "transition_share": round(sum(r["transition"] for r in rs) / max(1, len(rs)), 4),
               "fwd_move_atr_by_label": {k: _stats([r for r in rs if r["label"] == k], "fwd_move_atr")
                                         for k in regime.LABELS},
               "fwd_range_pts_by_vol": {k: _stats([r for r in rs if r["vol"] == k], "fwd_range_pts")
                                        for k in ("LOW", "NORMAL", "HIGH")},
               "fwd_range_atr_by_vol": {k: _stats([r for r in rs if r["vol"] == k], "fwd_range_atr")
                                        for k in ("LOW", "NORMAL", "HIGH")}}
        res["periods"][name] = per
    return res


def _groups(seq):
    from itertools import groupby
    return (list(g) for _, g in groupby(seq))


def strategy_by_regime(summary_path: Path, out_dir: Path) -> dict:
    """Expectancy by regime label per baseline, design vs post-design (MEASURED costs)."""
    import csv
    from app.research import metrics, walkforward as wf
    summ = json.loads(summary_path.read_text())
    design_end = summ["design_end"]
    res = {}
    for vid in ("FCB_BASE", "ORB_BASE", "ST_BASE", "ER_BASE", "PV_BASE"):
        p = out_dir / f"trades_{vid}.csv"
        rows = list(csv.DictReader(p.open(encoding="utf-8")))
        by = defaultdict(lambda: {"design": [], "post": []})
        for r in rows:
            x = wf.apply_costs({"r_gross": float(r["r_gross"]), "risk_pts": float(r["risk_pts"]),
                                "hold_min": float(r["hold_min"]), "expiry": r.get("f_expiry") == "True",
                                "day": r["day"]}, wf.COST_MODELS[wf.PRIMARY_COST])
            by[r.get("f_regime")]["design" if r["day"] <= design_end else "post"].append(x)
        res[vid] = {}
        for lab, g in sorted(by.items(), key=lambda kv: str(kv[0])):
            d = metrics.summarize(g["design"], boot=False) if g["design"] else {"n": 0}
            q = metrics.summarize(g["post"], boot=len(g["post"]) >= 20) if g["post"] else {"n": 0}
            res[vid][str(lab)] = {"design_n": d["n"], "design_exp": d.get("expectancy_r"),
                                  "post_n": q["n"], "post_exp": q.get("expectancy_r"),
                                  "post_ci95": q.get("expectancy_ci95"),
                                  "same_sign": (None if d.get("expectancy_r") is None or q.get("expectancy_r") is None
                                                else (d["expectancy_r"] > 0) == (q["expectancy_r"] > 0))}
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--design-end", required=True)
    a = ap.parse_args(argv)
    ds = data.load(a.db, hash_file=False)
    annotate(ds.sessions)
    rows = label_series(ds)
    out = Path(a.out)
    res = validate(rows, a.design_end)
    if (out / "summary.json").exists():
        res["strategy_by_regime"] = strategy_by_regime(out / "summary.json", out)
    (out / "regime_validation.json").write_text(json.dumps(res, indent=1, default=str))
    print(out / "regime_validation.json")


if __name__ == "__main__":
    main()
