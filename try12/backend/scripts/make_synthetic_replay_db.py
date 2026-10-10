"""
Build a SYNTHETIC replay database (plan item 6.6a) — NOT evidence of edge.

    python scripts/make_synthetic_replay_db.py [--out PATH] [--days 25] [--seed 20260901]

Writes ~25 synthetic NIFTY trading days to ``%TEMP%\\pth-replay.db`` (SQLite):
spot ONE_MINUTE candles, option ticks with bid/ask/OI/volume, option-chain
snapshots (5-minutely), previous-day snapshots, VIX and instruments. Every
instrument row is is_active = False with weekly expiries, so the replay must
derive its universe from the tokens that traded (design §10.4). It only
touches the SQLite file it is told to write; it never opens the settings
database. The prices are a seeded random walk and Black-Scholes premiums:
they exercise the pipeline and say nothing about any market.

``populate`` is reusable: tests/test_replay_engine.py feeds it a hand-built
day with scripted option prices (R13.11).
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
SESSION_MINUTES = 375                     # 09:15 .. 15:29 IST
OPEN_IST = dtime(9, 15)
STRIKE_STEP = 50
BASE_SPOT = 22600.0
LOT_SIZE = 65
DEFAULT_SEED = 20260901


@dataclass
class SyntheticDay:
    day: date
    candles: list            # [(open, high, low, close)] x 375, spot index points


def default_db_path() -> Path:
    return Path(tempfile.gettempdir()) / "pth-replay.db"


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def weekly_expiries(first: date, last: date, weekday: int = 3) -> list[date]:
    """Thursdays (weekday 3) from the first on-or-after *first* to one past *last*."""
    d = first
    while d.weekday() != weekday:
        d += timedelta(days=1)
    out = []
    while d <= last + timedelta(days=14):
        out.append(d)
        d += timedelta(days=7)
    return out


def expiry_for(day: date, expiries: list[date]) -> date:
    """The expiry the D9 bands pick for a weekly cadence: the first with DTE >= 2."""
    return min(e for e in expiries if (e - day).days >= 2)


def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, iv: float, call: bool) -> float:
    intrinsic = max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    if t_years <= 0 or iv <= 0:
        return intrinsic
    sd = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / sd
    d2 = d1 - sd
    if call:
        return spot * _ncdf(d1) - strike * _ncdf(d2)
    return strike * _ncdf(-d2) - spot * _ncdf(-d1)


def default_quote(day: date, inst: dict, k: int, spot: float) -> dict:
    """Black-Scholes premium with a 14% IV, a ~0.25% half-spread and liquid OI/volume."""
    dte = (inst["expiry"] - day).days + (SESSION_MINUTES - k) / SESSION_MINUTES
    ltp = max(_tick(bs_price(spot, inst["strike"], dte / 365.0, 0.14, inst["kind"] == "CE")), 0.5)
    half = max(0.05, ltp * 0.0025)
    dist = abs(inst["strike"] - spot) / STRIKE_STEP
    return {"ltp": ltp, "bid": _tick(ltp - half), "ask": _tick(ltp + half),
            "oi": int(220_000 / (1.0 + 0.25 * dist)), "volume": int(4_000 + 60 * k),
            "iv": 14.0}


def make_days(days: list[date], seed: int) -> list[SyntheticDay]:
    """Seeded random-walk sessions: trend-up, trend-down and range days."""
    rng = random.Random(seed)
    out, prev_close = [], BASE_SPOT
    for day in days:
        kind = rng.choice(("UP", "DOWN", "RANGE", "UP", "DOWN"))
        drift = {"UP": 0.55, "DOWN": -0.55, "RANGE": 0.0}[kind]
        px = prev_close + rng.gauss(0, 12) + 0.15 * (BASE_SPOT - prev_close)
        open_px, candles = px, []
        for _ in range(SESSION_MINUTES):
            o = px
            pull = -0.06 * (px - open_px) if kind == "RANGE" else 0.0
            # body and wick noise sized to an ordinary NIFTY 1-minute bar
            # (true range ~10 points); a fixture property, not a strategy parameter
            c = o + drift + pull + rng.gauss(0, 5.5)
            h = max(o, c) + abs(rng.gauss(0, 3.0))
            low = min(o, c) - abs(rng.gauss(0, 3.0))
            candles.append((round(o, 2), round(h, 2), round(low, 2), round(c, 2)))
            px = c
        prev_close = px
        out.append(SyntheticDay(day, candles))
    return out


def _utc(day: date, k: int, seconds: int = 0) -> datetime:
    ist = datetime.combine(day, OPEN_IST, IST) + timedelta(minutes=k, seconds=seconds)
    return ist.astimezone(UTC)


def populate(engine, days: list[SyntheticDay], *, expiries: list[date],
             quote_fn: Callable = default_quote, strike_range: int = 8,
             lot_size: int = LOT_SIZE, vix_open: float = 14.5,
             prev_day_snapshot: bool = True, grid: int = 20) -> dict:
    """Insert instruments, then per day: spot candles, option ticks, chain
    snapshots, a daily snapshot and VIX. *engine* is a SYNC SQLAlchemy engine
    over a schema created with Base.metadata.create_all."""
    from sqlalchemy import insert

    from app.core.config import settings
    from app.db.models.instrument import Instrument, InstrumentType, OptionType
    from app.db.models.market_study import MarketDailySnapshot, VIXData
    from app.db.models.ohlc import OHLC
    from app.db.models.option_chain import OptionChainSnapshot, OptionSide
    from app.db.models.tick import Tick

    insts: dict[tuple, dict] = {}
    inst_rows = []

    def add_inst(ei: int, exp: date, s: int, kind: str) -> None:
        tok = f"9{ei:02d}{s // STRIKE_STEP:04d}{0 if kind == 'CE' else 1}"
        sym = f"NIFTY{exp:%d%b%y}{s}{kind}".upper()
        insts[(exp, float(s), kind)] = {"token": tok, "symbol": sym, "expiry": exp,
                                        "strike": float(s), "kind": kind}
        inst_rows.append(dict(
            token=tok, symbol=sym, name="NIFTY", expiry=exp, strike=float(s),
            option_type=OptionType[kind], instrument_type=InstrumentType.OPTIDX,
            lot_size=lot_size, tick_size=0.05, exchange="NFO", is_active=False))

    for ei, exp in enumerate(expiries):
        for s in range(int(BASE_SPOT) - grid * STRIKE_STEP, int(BASE_SPOT) + grid * STRIKE_STEP + 1,
                       STRIKE_STEP):
            for kind in ("CE", "PE"):
                add_inst(ei, exp, s, kind)
    # A long or drifting walk can open outside the fixed grid; add the strikes those days quote
    # (after the grid rows, so a dataset whose walk stays inside the grid is unchanged).
    for sd in days:
        exp = expiry_for(sd.day, expiries)
        atm = round(sd.candles[0][0] / STRIKE_STEP) * STRIKE_STEP
        for i in range(-strike_range, strike_range + 1):
            for kind in ("CE", "PE"):
                if (exp, float(atm + i * STRIKE_STEP), kind) not in insts:
                    add_inst(expiries.index(exp), exp, atm + i * STRIKE_STEP, kind)
    summary = {"instruments": len(inst_rows), "ticks": 0, "candles": 0, "snapshots": 0,
               "days": [d.day.isoformat() for d in days]}
    spot_tok = settings.NIFTY_TOKEN

    def daily_row(sd: SyntheticDay, prev_close: Optional[float]) -> dict:
        hi, lo = max(c[1] for c in sd.candles), min(c[2] for c in sd.candles)
        cl, op = sd.candles[-1][3], sd.candles[0][0]
        return dict(date=sd.day, underlying="NIFTY", open=op, high=hi, low=lo, close=cl,
                    prev_close=prev_close, india_vix=vix_open,
                    change_pct=((cl - prev_close) / prev_close * 100) if prev_close else None)

    with engine.begin() as conn:
        conn.execute(insert(Instrument), inst_rows)
        if prev_day_snapshot and days:
            d0 = days[0]
            o = d0.candles[0][0]
            conn.execute(insert(MarketDailySnapshot), [dict(
                date=d0.day - timedelta(days=1), underlying="NIFTY", open=o - 20,
                high=o + 45, low=o - 60, close=o, prev_close=o - 15, india_vix=vix_open)])
            conn.execute(insert(VIXData), [dict(date=d0.day - timedelta(days=1), open=vix_open,
                                                high=vix_open + .3, low=vix_open - .3,
                                                close=vix_open, change_pct=0.0)])
        prev_close = None
        for sd in days:
            exp = expiry_for(sd.day, expiries)
            atm = round(sd.candles[0][0] / STRIKE_STEP) * STRIKE_STEP
            strikes = [float(atm + i * STRIKE_STEP) for i in range(-strike_range, strike_range + 1)]
            day_insts = [insts[(exp, s, kd)] for s in strikes for kd in ("CE", "PE")]

            conn.execute(insert(OHLC), [dict(
                token=spot_tok, symbol="NIFTY", exchange="NSE", interval="ONE_MINUTE",
                open=c[0], high=c[1], low=c[2], close=c[3], volume=0, oi=0,
                timestamp=_utc(sd.day, k)) for k, c in enumerate(sd.candles)])
            summary["candles"] += len(sd.candles)

            ticks, snaps, first_ltp = [], [], {}
            for k, c in enumerate(sd.candles):
                spot = c[3]
                for inst in day_insts:
                    q = quote_fn(sd.day, inst, k, spot)
                    first_ltp.setdefault(inst["token"], q["ltp"])
                    ticks.append(dict(
                        token=inst["token"], symbol=inst["symbol"], exchange="NFO",
                        ltp=q["ltp"], open=first_ltp[inst["token"]], high=None, low=None,
                        close=None, volume=q["volume"], oi=q["oi"], best_bid=q["bid"],
                        best_ask=q["ask"], timestamp=_utc(sd.day, k, 59)))
                    if k % 5 == 4:                                   # 300 s cadence
                        snaps.append(dict(
                            underlying="NIFTY", underlying_ltp=spot, expiry=inst["expiry"],
                            strike=inst["strike"], option_type=OptionSide[inst["kind"]],
                            token=inst["token"], ltp=q["ltp"], bid=q["bid"], ask=q["ask"],
                            iv=q.get("iv"), oi=q["oi"], volume=q["volume"],
                            snapshot_time=_utc(sd.day, k, 59)))
            for i in range(0, len(ticks), 20_000):
                conn.execute(insert(Tick), ticks[i:i + 20_000])
            for i in range(0, len(snaps), 20_000):
                conn.execute(insert(OptionChainSnapshot), snaps[i:i + 20_000])
            summary["ticks"] += len(ticks)
            summary["snapshots"] += len(snaps)

            conn.execute(insert(MarketDailySnapshot), [daily_row(sd, prev_close)])
            conn.execute(insert(VIXData), [dict(date=sd.day, open=vix_open, high=vix_open + .4,
                                                low=vix_open - .4, close=vix_open, change_pct=0.0)])
            prev_close = sd.candles[-1][3]
    return summary


def build(path: Path, *, n_days: int = 25, seed: int = DEFAULT_SEED,
          start: date = date(2026, 9, 1)) -> dict:
    """Create *path* from scratch (any existing file is replaced) and populate it."""
    from sqlalchemy import create_engine

    from app.services.replay.cli import create_sqlite_schema

    path = Path(path)
    if path.exists():
        path.unlink()
    create_sqlite_schema(f"sqlite+aiosqlite:///{path.as_posix()}")
    days = weekdays(start, n_days)
    expiries = weekly_expiries(days[0], days[-1])
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        summary = populate(engine, make_days(days, seed), expiries=expiries)
    finally:
        engine.dispose()
    summary.update(path=str(path), first=days[0].isoformat(), last=days[-1].isoformat(),
                   seed=seed, synthetic=True)
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=default_db_path())
    ap.add_argument("--days", type=int, default=25)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 1))
    a = ap.parse_args(argv)
    s = build(a.out, n_days=a.days, seed=a.seed, start=a.start)
    print(f"SYNTHETIC replay database written: {s['path']}")
    print(f"  {len(s['days'])} trading days {s['first']} .. {s['last']}, "
          f"{s['instruments']} instruments, {s['candles']} candles, {s['ticks']} ticks, "
          f"{s['snapshots']} chain rows")
    print("  NOT evidence of edge: seeded random walk + Black-Scholes premiums.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
