"""
Permanent per-minute option archive, built from the live recorder's ticks.

    python -m app.research.option_archive --out <options_archive.db>
        [--backups <dir of ticks_YYYY-MM-DD.csv.gz>] [--days 2026-10-27,...] [--force]

Why: Postgres keeps ticks for TICK_RETENTION_DAYS (30), then the retention job
moves them to gzip files. Nothing turned them into a research-ready history, so
real option evidence could never grow beyond a month. This builds one SQLite
file that only grows, one row per contract per in-session minute (09:15-15:29
IST): the minute's last LTP, last valid-looking bid and ask (both > 0, as
recorded: crossed quotes are kept and rejected later by the replay), last OI,
and the day's cumulative volume. NIFTY and BANKNIFTY options, plus the spot
indices' minute LTP.

Read-only on its sources: Postgres is opened with SET TRANSACTION READ ONLY,
backups are only read. A day already archived is skipped unless --force.
Run it after each session (or at least every few weeks, inside the 30-day
window) to keep the archive complete.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

IST = timezone(timedelta(hours=5, minutes=30))
SESSION = (dtime(9, 15), dtime(15, 30))
SYMBOL = re.compile(r"^(NIFTY|BANKNIFTY)(\d{2})([A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
# Live feed spot tokens (settings.NIFTY_TOKEN / BANKNIFTY_TOKEN) and the history-API ones
SPOT = {"26000": "NIFTY", "26009": "BANKNIFTY", "99926000": "NIFTY", "99926009": "BANKNIFTY"}

SCHEMA = """
create table if not exists opt_min (
    minute text not null,            -- IST 'YYYY-MM-DDTHH:MM' (the minute's start)
    token text not null, underlying text not null, symbol text not null,
    expiry text not null, strike real not null, opt text not null,
    ltp real, bid real, ask real, oi integer, volume_cum integer,
    primary key (token, minute));
create index if not exists ix_opt_min_minute on opt_min (minute, underlying, expiry);
create table if not exists spot_min (minute text not null, underlying text not null, ltp real,
    primary key (underlying, minute));
create table if not exists days (day text primary key, source text, opt_rows integer,
    spot_rows integer, exported_at text);
"""


def parse_symbol(symbol: str) -> Optional[tuple[str, str, float, str]]:
    """'NIFTY03NOV2622050CE' -> ('NIFTY', '2026-11-03', 22050.0, 'CE')."""
    m = SYMBOL.match(symbol or "")
    if not m:
        return None
    und, dd, mon, yy, strike, opt = m.groups()
    if mon not in MONTHS:
        return None
    return und, f"20{yy}-{MONTHS[mon]:02d}-{int(dd):02d}", float(strike), opt


def _minute(ts: datetime) -> Optional[str]:
    t = ts.astimezone(IST)
    if not (SESSION[0] <= t.time() < SESSION[1]):
        return None
    return t.strftime("%Y-%m-%dT%H:%M")


class _Agg:
    """Last-value aggregation of ticks into minutes, fed in timestamp order."""

    def __init__(self):
        self.opt: dict = {}
        self.spot: dict = {}

    def add(self, token, symbol, exchange, ts: datetime, ltp, bid, ask, oi, volume):
        minute = _minute(ts)
        if minute is None:
            return
        if exchange == "NSE":
            und = SPOT.get(str(token))
            if und and ltp:
                self.spot[(und, minute)] = float(ltp)
            return
        meta = parse_symbol(symbol)
        if meta is None:
            return
        key = (str(token), minute)
        row = self.opt.get(key) or {"token": str(token), "symbol": symbol, "minute": minute,
                                    "underlying": meta[0], "expiry": meta[1], "strike": meta[2],
                                    "opt": meta[3], "ltp": None, "bid": None, "ask": None,
                                    "oi": None, "volume_cum": None}
        if ltp:
            row["ltp"] = float(ltp)
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            row["bid"], row["ask"] = float(bid), float(ask)
        if oi and int(float(oi)) > 0:
            row["oi"] = int(float(oi))
        if volume not in (None, ""):
            v = int(float(volume))
            row["volume_cum"] = max(v, row["volume_cum"] or 0)
        self.opt[key] = row


def _num(x):
    return None if x in (None, "") else x


def from_backup(path: Path) -> _Agg:
    agg = _Agg()
    rows = []
    with gzip.open(path, "rt", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    rows.sort(key=lambda r: r["timestamp"])
    for r in rows:
        agg.add(r["token"], r["symbol"], r["exchange"], datetime.fromisoformat(r["timestamp"]),
                _num(r["ltp"]), _num(r["best_bid"]), _num(r["best_ask"]), _num(r["oi"]), _num(r["volume"]))
    return agg


async def from_postgres(day: date) -> _Agg:
    from sqlalchemy import text
    from app.db.base import AsyncSessionLocal
    start = datetime.combine(day, SESSION[0], IST)
    end = datetime.combine(day, SESSION[1], IST)
    agg = _Agg()
    async with AsyncSessionLocal() as db:
        await db.execute(text("set transaction read only"))
        res = await db.stream(text(
            "select token, symbol, exchange, timestamp, ltp, best_bid, best_ask, oi, volume "
            "from ticks where timestamp >= :s and timestamp < :e order by timestamp"),
            {"s": start, "e": end})
        async for r in res:
            agg.add(r.token, r.symbol, r.exchange, r.timestamp, r.ltp, r.best_bid, r.best_ask,
                    r.oi, r.volume)
    return agg


async def postgres_days() -> list[date]:
    from sqlalchemy import text
    from app.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(text("set transaction read only"))
        rows = (await db.execute(text(
            "select distinct (timestamp at time zone 'Asia/Kolkata')::date d from ticks "
            "where exchange = 'NFO' order by d"))).all()
    return [r.d for r in rows]


def write_day(con: sqlite3.Connection, day: str, agg: _Agg, source: str) -> tuple[int, int]:
    con.execute("delete from opt_min where minute like ?", (f"{day}T%",))
    con.execute("delete from spot_min where minute like ?", (f"{day}T%",))
    con.executemany(
        "insert into opt_min values (:minute, :token, :underlying, :symbol, :expiry, :strike, :opt, "
        ":ltp, :bid, :ask, :oi, :volume_cum)", list(agg.opt.values()))
    con.executemany("insert into spot_min values (?, ?, ?)",
                    [(m, u, v) for (u, m), v in agg.spot.items()])
    con.execute("insert or replace into days values (?, ?, ?, ?, ?)",
                (day, source, len(agg.opt), len(agg.spot), datetime.now(IST).isoformat(timespec="seconds")))
    con.commit()
    return len(agg.opt), len(agg.spot)


def backup_files(folder: Optional[Path]) -> dict[str, Path]:
    if folder is None or not folder.exists():
        return {}
    out = {}
    for p in sorted(folder.glob("ticks_*.csv.gz")):
        m = re.match(r"ticks_(\d{4}-\d{2}-\d{2})\.csv\.gz$", p.name)
        if m:
            out[m.group(1)] = p
    return out


async def run(out: Path, backups: Optional[Path], only: Optional[Iterable[str]], force: bool) -> list:
    con = sqlite3.connect(out)
    con.executescript(SCHEMA)
    done = {r[0] for r in con.execute("select day from days")}
    plan: dict[str, str] = {d: "backup" for d in backup_files(backups)}
    try:
        for d in await postgres_days():
            plan[str(d)] = "postgres"                 # the database wins where both exist
    except Exception as exc:                          # no database: backups only
        print(f"[archive] postgres unavailable ({type(exc).__name__}: {exc}); backups only")
    if only:
        plan = {d: s for d, s in plan.items() if d in set(only)}
    report = []
    files = backup_files(backups)
    for day in sorted(plan):
        if day in done and not force:
            report.append((day, "skipped (archived)"))
            continue
        agg = await from_postgres(date.fromisoformat(day)) if plan[day] == "postgres" else from_backup(files[day])
        if not agg.opt:
            report.append((day, f"{plan[day]}: no in-session option ticks"))
            continue
        n_opt, n_spot = write_day(con, day, agg, plan[day])
        report.append((day, f"{plan[day]}: {n_opt} option minutes, {n_spot} spot minutes"))
    con.close()
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--backups")
    ap.add_argument("--days", help="comma-separated YYYY-MM-DD; default every day available")
    ap.add_argument("--force", action="store_true", help="rebuild days already archived")
    a = ap.parse_args(argv)
    rep = asyncio.run(run(Path(a.out), Path(a.backups) if a.backups else None,
                          a.days.split(",") if a.days else None, a.force))
    for day, what in rep:
        print(day, what)


if __name__ == "__main__":
    main()
