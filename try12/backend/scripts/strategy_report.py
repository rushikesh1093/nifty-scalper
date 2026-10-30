"""Print the per-strategy report (app.services.metrics.strategy_report) for the live
paper run, and optionally write the trade log as CSV.

    cd try12/backend
    python scripts/strategy_report.py [--from 2026-10-01] [--to 2026-10-30] [--csv trades.csv]
"""
import argparse
import asyncio
import csv
import sys
from datetime import date

sys.path.insert(0, ".")


def _fmt(v):
    return "-" if v is None else f"{v:,.2f}" if isinstance(v, float) else str(v)


async def main(d_from, d_to, csv_path):
    from app.db.base import AsyncSessionLocal
    from app.services.metrics import strategy_report as sr

    async with AsyncSessionLocal() as db:
        log = await sr.load_closed(db, "PAPER", d_from, d_to)
    if not log:
        print("No closed paper trades in that range.")
        return
    rep = sr.build_report(log)
    print(f"{len(log)} closed trades, {log[0]['trading_day']} .. {log[-1]['trading_day']}\n")
    cols = ("trades", "win_rate", "avg_win", "avg_loss", "expectancy_gross", "expectancy_net",
            "net_pnl", "charges", "slippage")
    print(f"{'strategy':24}" + "".join(f"{c:>17}" for c in cols))
    for name, r in rep.items():
        print(f"{name:24}" + "".join(f"{_fmt(r.get(c)):>17}" for c in cols))
    for name, r in rep.items():
        print(f"\n{name}")
        print("  exits:      " + ", ".join(f"{k} {v}" for k, v in r["exit_reasons"].items()))
        for k in ("expiry_day", "other_days"):
            b = r[k]
            print(f"  {k:11} " + (f"{b['trades']} trades, net {_fmt(b['net_pnl'])}, "
                                  f"expectancy {_fmt(b['expectancy_net'])}" if b["trades"] else "no trades"))
        print("  by hour:    " + ", ".join(f"{h}:00 {v['trades']}/{_fmt(v['net_pnl'])}"
                                          for h, v in r["by_entry_hour"].items()))
        for f in r["flags"]:
            print(f"  ! {f}")
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(log[0].keys()))
            w.writeheader()
            w.writerows(log)
        print(f"\ntrade log written to {csv_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", type=date.fromisoformat)
    ap.add_argument("--to", type=date.fromisoformat)
    ap.add_argument("--csv")
    a = ap.parse_args()
    asyncio.run(main(a.frm, a.to, a.csv))
