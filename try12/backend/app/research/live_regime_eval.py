"""
Evaluate the LIVE regime classifier and regime -> strategy map on history.

    python -m app.research.live_regime_eval --db <candles.db> --trades <out_dir> --out <json>

For every research trade (trades_<vid>.csv of a cli run) it rebuilds, at the
signal minute, exactly what strategy_runner hands the detector with the live
defaults (transition_cooldown_candles = 0, stabiliser off, trade_on_expiry_day
= True): compute_all on today's candles, the ATR tail fallback, the index
future's volume / avg_volume (_regime_indicators), build_regime_indicators on
the 500-candle history, classify_regime with the hardcoded RegimeConfig. Then
it asks the live map (regime_rules.get_regime_rule) whether that strategy and
direction would have been allowed, and reports net expectancy for ALLOWED vs
BLOCKED trades and per live label, by year.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from app.research import data, walkforward as wf

STRATEGY_OF = {"FCB_BASE": "FIRST_CANDLE_BREAKOUT", "ORB_BASE": "ORB_BREAKOUT",
               "ST_BASE": "SUPERTREND_EMA", "ER_BASE": "EMA_RSI_PULLBACK_15M",
               "PV_BASE": "PIVOT_BOUNCE"}
VOLUME_BARS = 20                              # StrategyRunner.REGIME_VOLUME_BARS


def year_of(day: str) -> str:
    return "Y1" if day <= "2024-09-30" else ("Y2" if day <= "2025-09-30" else "Y3")


def live_label(today: list[dict], history: list[dict], fut: list[dict], config) -> tuple[str, float]:
    """The live detector's label after the last candle of *today* (cooldown 0)."""
    from app.services.trading.regime_indicators import build_regime_indicators
    from app.services.trading.regime_rules import classify_regime
    from app.strategies.indicators import atr, compute_all
    ind = compute_all(today)
    if ind.get("atr") is None and len(history) >= 15:
        tail = history[-40:]
        a = atr(np.array([c["high"] for c in tail]), np.array([c["low"] for c in tail]),
                np.array([c["close"] for c in tail]), 14)
        if len(a) and np.isfinite(a[-1]):
            ind["atr"] = float(a[-1])
    last = today[-1]["timestamp"]
    rows = [(c["timestamp"], c["volume"]) for c in fut if c["timestamp"] <= last]
    if rows and rows[-1][0] == last and rows[-1][1] > 0:
        prior = [v for _, v in rows[:-1]][-VOLUME_BARS:]
        if len(prior) >= 5:
            ind = dict(ind, volume=rows[-1][1], avg_volume=sum(prior) / len(prior))
    ri = build_regime_indicators(today, ind, config, history=history, atr_pct_lookback=20)
    return classify_regime(ri, "NIFTY", False, None, config), ri.atr_percentile


def allowed(regime: str, atr_pct: float, strategy: str, side: str) -> bool:
    from app.services.trading.regime_rules import get_regime_rule
    rule = get_regime_rule(regime, atr_pct)
    return strategy in rule.enabled_strategies and side in rule.allowed_directions


def label_trades(ds: data.Dataset, trades_dir: Path) -> list[dict]:
    from app.services.trading.regime_config import load_config
    config = load_config("NIFTY", None)
    by_day = {str(s.day): (i, s) for i, s in enumerate(ds.sessions)}
    out = []
    for vid, strategy in STRATEGY_OF.items():
        for r in csv.DictReader((trades_dir / f"trades_{vid}.csv").open(encoding="utf-8")):
            if r["day"] not in by_day:
                continue
            i, s = by_day[r["day"]]
            k = int(r["entry_idx"])                      # candles closed at the signal
            today = s.candles[:k]
            prev = [c for p in ds.sessions[max(0, i - 2):i] for c in p.candles]
            history = (prev + today)[-500:]
            fut = s.fut_candles
            regime, atr_pct = live_label(today, history, fut, config)
            cost = wf.COST_MODELS[wf.PRIMARY_COST].pts(float(r["hold_min"]), False)
            out.append({"vid": vid, "strategy": strategy, "day": r["day"], "side": r["side"],
                        "regime": regime, "atr_pct": round(float(atr_pct), 1),
                        "allowed": allowed(regime, atr_pct, strategy, r["side"]),
                        "r_net": float(r["r_gross"]) - cost / float(r["risk_pts"])})
    return out


def _m(xs):
    return (len(xs), round(statistics.mean(xs), 4) if xs else None)


def summarize(rows: list[dict]) -> dict:
    res = {}
    for vid in STRATEGY_OF:
        rs = [r for r in rows if r["vid"] == vid]
        by = defaultdict(list)
        lab = defaultdict(lambda: defaultdict(list))
        for r in rs:
            by[("ALLOWED" if r["allowed"] else "BLOCKED", year_of(r["day"]))].append(r["r_net"])
            lab[r["regime"]][year_of(r["day"])].append(r["r_net"])
        res[vid] = {
            "allowed_vs_blocked": {f"{a}|{y}": _m(v) for (a, y), v in sorted(by.items())},
            "allowed_share": round(sum(r["allowed"] for r in rs) / max(1, len(rs)), 3),
            "by_live_label": {k: {y: _m(v[y]) for y in ("Y1", "Y2", "Y3")} for k, v in sorted(lab.items())},
        }
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    ds = data.load(a.db, hash_file=False)
    rows = label_trades(ds, Path(a.trades))
    res = {"generated": datetime.now().isoformat(timespec="seconds"), "trades": len(rows),
           "label_share": {k: round(v / len(rows), 4) for k, v in
                           sorted(((k, sum(1 for r in rows if r["regime"] == k)) for k in
                                   {r["regime"] for r in rows}), key=lambda kv: -kv[1])},
           "strategies": summarize(rows)}
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(a.out)


if __name__ == "__main__":
    main()


# ── Does the live label predict anything? (volatility and direction) ─────────

def label_grid(ds: data.Dataset, lookbacks=(20, 375, 486), step: int = 15) -> list[dict]:
    """Every *step* minutes from 09:45 to 14:45: the live ATR percentile for each
    lookback, the live label (default lookback 20), and the next 30 minutes'
    range and signed move in points."""
    from app.services.trading.regime_config import load_config
    from app.services.trading.regime_indicators import compute_atr_percentile
    from app.strategies.indicators import compute_all
    config = load_config("NIFTY", None)
    out = []
    for i, s in enumerate(ds.sessions[2:], start=2):
        prev = [c for p in ds.sessions[i - 2:i] for c in p.candles]
        for k in range(30, len(s.candles) - 30, step):
            today = s.candles[:k]
            if today[-1]["timestamp"].time() > datetime.strptime("14:45", "%H:%M").time():
                break
            history = (prev + today)[-500:]
            label, _ = live_label(today, history, s.fut_candles, config)
            atr_now = compute_all(today).get("atr") or 0.0
            nxt = s.candles[k:k + 30]
            row = {"day": str(s.day), "label": label,
                   "fwd_range": max(c["high"] for c in nxt) - min(c["low"] for c in nxt),
                   "fwd_move": nxt[-1]["close"] - today[-1]["close"]}
            for lb in lookbacks:
                row[f"pct{lb}"] = compute_atr_percentile(atr_now, history, period=lb)
            out.append(row)
    return out


def grid_summary(rows: list[dict], lookbacks=(20, 375, 486)) -> dict:
    from scipy.stats import spearmanr          # noqa: F401  (optional dependency)
    res = {"vol": {}, "direction": {}}
    for lb in lookbacks:
        for y in ("Y1", "Y2", "Y3"):
            rs = [r for r in rows if year_of(r["day"]) == y]
            b = defaultdict(list)
            for r in rs:
                p = r[f"pct{lb}"]
                b["LOW<30" if p < 30 else "HIGH>70" if p > 70 else "MID"].append(r["fwd_range"])
            rho = spearmanr([r[f"pct{lb}"] for r in rs], [r["fwd_range"] for r in rs]).correlation
            res["vol"][f"lookback{lb}|{y}"] = {"rho": round(float(rho), 3),
                                              **{k: _m(v) for k, v in sorted(b.items())}}
    for y in ("Y1", "Y2", "Y3"):
        b = defaultdict(list)
        for r in rows:
            if year_of(r["day"]) == y:
                b[r["label"]].append(r["fwd_move"])
        res["direction"][y] = {k: _m(v) for k, v in sorted(b.items())}
    return res


# ── Regime v2 by strategy and year ───────────────────────────────────────────

def v2_by_strategy(ds: data.Dataset, trades_dir: Path) -> dict:
    """Net R (MEASURED) per v2 volatility bucket and OR side, per strategy and
    year, for the baseline trades: the evidence a v2 gating rule would need."""
    from app.services.trading.regime_v2 import classify_v2
    by_day = {str(s.day): (i, s) for i, s in enumerate(ds.sessions)}
    res: dict = {}
    for vid in STRATEGY_OF:
        g = defaultdict(list)
        for r in csv.DictReader((trades_dir / f"trades_{vid}.csv").open(encoding="utf-8")):
            if r["day"] not in by_day:
                continue
            i, s = by_day[r["day"]]
            today = s.candles[:int(r["entry_idx"])]
            prev = [c for p in ds.sessions[max(0, i - 2):i] for c in p.candles]
            v2 = classify_v2(today, (prev + today)[-500:])
            net = float(r["r_gross"]) - wf.COST_MODELS[wf.PRIMARY_COST].pts(float(r["hold_min"]), False) / float(r["risk_pts"])
            y = year_of(r["day"])
            g[("vol", v2.vol, y)].append(net)
            g[("or", f"{v2.or_side}|{'with' if (v2.or_side == 'ABOVE') == (r['side'] == 'BUY') else 'against'}", y)].append(net)
        res[vid] = {f"{a}:{b}": {y: _m(g[(a, b, y)]) for y in ("Y1", "Y2", "Y3")}
                    for (a, b) in sorted({(k[0], k[1]) for k in g})}
    return res
