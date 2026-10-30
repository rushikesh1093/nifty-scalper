"""
Compare replay runs window by window, with the production metric code
(app.services.metrics.trade_metrics.compute).

    cd try12/backend
    python scripts/compare_replay_runs.py \\
        --run current=<store-a.db> --run enabled=<store-b.db> \\
        --window "tune=2026-09-07:2026-10-02" --window "held-out=2026-10-05:2026-10-22" \\
        --out-json cmp.json --out-md cmp.md

Each store is a SQLite replay store holding (at least) one finished run; the
newest replay_runs row is used, or pass NAME=PATH#RUN_ID. Read-only
(the file is opened with mode=ro). Days are IST exit days. Trades/day divides
by the TRADEABLE days of the run's coverage inside the window.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.trading_params import TradingParams  # noqa: E402
from app.services.metrics.trade_metrics import compute, row_from_position  # noqa: E402

KEYS = ("trades", "win_rate", "expectancy_r", "expectancy_r_net", "expectancy_inr",
        "profit_factor", "max_equity_drawdown_inr", "gross_profit", "gross_loss")


def _dt(v):
    if v is None:
        return None
    t = datetime.fromisoformat(str(v))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def load(path: str, run_id: str | None):
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    if run_id is None:
        run_id = con.execute("select run_id from replay_runs order by created_at desc "
                             "limit 1").fetchone()[0]
    run = dict(con.execute("select * from replay_runs where run_id=?", (run_id,)).fetchone())
    cov = json.loads(run["coverage"]) if run["coverage"] else {}
    tradeable = sorted(date.fromisoformat(d["day"]) for d in cov.get("days", [])
                       if d.get("status") == "TRADEABLE")
    params = json.loads(run["params"]) if run["params"] else {}
    rows = []
    for r in con.execute("select * from paper_positions where run_id=? and is_open=0 "
                         "order by exit_time, id", (run_id,)):
        d = dict(r)
        d["exit_time"], d["entry_time"] = _dt(d["exit_time"]), _dt(d["entry_time"])
        d["sizing_detail"] = json.loads(d["sizing_detail"]) if d.get("sizing_detail") else None
        rows.append(row_from_position(SimpleNamespace(**d)))
    signals = Counter()
    try:
        for name, n in con.execute("select strategy_name, count(*) from strategy_signals "
                                   "where run_id=? group by strategy_name", (run_id,)):
            signals[name] = n
    except sqlite3.OperationalError:
        pass
    con.close()
    return SimpleNamespace(run_id=run_id, trades=rows, tradeable=tradeable, signals=signals,
                           capital=float(params.get("capital") or 0.0) or 21000.0)


def metrics(trades, capital, days):
    m = asdict(compute(trades, capital, TradingParams()))
    out = {k: m.get(k) for k in KEYS}
    out["tradeable_days"] = days
    out["trades_per_day"] = (len(trades) / days) if days else None
    out["net_total"] = sum(t.net_pnl for t in trades)
    out["exits"] = dict(Counter(t.exit_reason for t in trades))
    return out


def fmt(v, kind=""):
    if v is None:
        return "–"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "inr":
        return f"{v:,.0f}"
    if kind == "r":
        return f"{v:+.3f}"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def row_md(name, m):
    return (f"| {name} | {m['tradeable_days']} | {m['trades']} | {fmt(m['trades_per_day'])} | "
            f"{fmt(m['win_rate'], 'pct')} | {fmt(m['expectancy_r'], 'r')} / "
            f"{fmt(m['expectancy_r_net'], 'r')} | {fmt(m['expectancy_inr'], 'inr')} | "
            f"{fmt(m['net_total'], 'inr')} | {fmt(m['max_equity_drawdown_inr'], 'inr')} | "
            f"{fmt(m['profit_factor'])} |")


HEAD = ("| {0} | Tradeable days | Trades | Trades/day | Win rate | Exp R gross / net | "
        "Exp ₹ net/trade | Net ₹ | Max DD ₹ | PF |\n|---|---|---|---|---|---|---|---|---|---|")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", required=True, help="NAME=PATH[#RUN_ID]")
    ap.add_argument("--window", action="append", default=[], help="LABEL=YYYY-MM-DD:YYYY-MM-DD")
    ap.add_argument("--out-json", type=Path)
    ap.add_argument("--out-md", type=Path)
    a = ap.parse_args()

    runs = {}
    for spec in a.run:
        name, rest = spec.split("=", 1)
        path, _, rid = rest.partition("#")
        runs[name] = load(path, rid or None)
    windows = []
    for spec in a.window:
        label, rng = spec.split("=", 1)
        lo, hi = (date.fromisoformat(x) for x in rng.split(":"))
        windows.append((label, lo, hi))
    if not windows:
        all_days = sorted({d for r in runs.values() for d in r.tradeable})
        windows = [("all", all_days[0], all_days[-1])]

    out = {"runs": {n: r.run_id for n, r in runs.items()}, "windows": {}, "per_day": {},
           "signals": {n: dict(r.signals) for n, r in runs.items()}}
    md = []
    for label, lo, hi in windows:
        w = out["windows"].setdefault(label, {"from": lo.isoformat(), "to": hi.isoformat(),
                                              "runs": {}})
        md.append(f"### {label} ({lo} → {hi})\n\n" + HEAD.format("Variant"))
        strat_md = []
        for name, r in runs.items():
            ts = [t for t in r.trades if lo <= t.trading_day <= hi]
            days = sum(1 for d in r.tradeable if lo <= d <= hi)
            m = metrics(ts, r.capital, days)
            by = defaultdict(list)
            for t in ts:
                by[t.strategy_name].append(t)
            m["by_strategy"] = {s: metrics(v, r.capital, days) for s, v in sorted(by.items())}
            w["runs"][name] = m
            md.append(row_md(name, m))
            for s, sm in m["by_strategy"].items():
                strat_md.append(row_md(f"{name} · {s}", sm))
        md.append("\nPer strategy:\n\n" + HEAD.format("Variant · strategy"))
        md.extend(strat_md)
        md.append("")

    md.append("### Per day\n\n| Day | " + " | ".join(
        f"{n} trades | {n} wins | {n} net ₹ | {n} R gross" for n in runs) + " |")
    md.append("|---|" + "---|" * (4 * len(runs)))
    all_days = sorted({d for r in runs.values() for d in r.tradeable})
    for d in all_days:
        cells = []
        for name, r in runs.items():
            ts = [t for t in r.trades if t.trading_day == d]
            rs = [t.r_multiple for t in ts if t.r_valid]
            entry = {"trades": len(ts), "wins": sum(1 for t in ts if t.net_pnl > 1.0),
                     "net": sum(t.net_pnl for t in ts), "r": sum(rs),
                     "strategies": dict(Counter(t.strategy_name for t in ts))}
            out["per_day"].setdefault(d.isoformat(), {})[name] = entry
            cells.append(f"{entry['trades']} | {entry['wins']} | {entry['net']:,.0f} | "
                         f"{entry['r']:+.2f}")
        md.append(f"| {d} | " + " | ".join(cells) + " |")

    if a.out_json:
        a.out_json.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    text = "\n".join(md) + "\n"
    if a.out_md:
        a.out_md.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
