"""
Experiment B: option execution on RECORDED quotes (Part 4B, 7, 9).

Source: a SQLite file of per-minute option snapshots recorded from the live
feed (``opt_min``: minute, token, expiry, strike, opt, ltp, bid, ask) and
spot (``spot_min``). It holds no volume, open interest, delta or IV, so none
of those is used, and nothing here is Black-Scholes: a contract without a
valid quote in the needed minute is reported UNAVAILABLE, never priced.

1. ``execution_study`` — for every in-session minute and hold h, a round
   trip in the ATM and the one-strike-OTM contract of the nearest expiry:
   buy at the ask, sell at the bid h minutes later. Regressing that P&L on
   the spot move gives P&L ~= a + b × move: b is the effective delta and
   -a / b the round-trip cost (spread + decay) in SPOT points, which is what
   a signal must out-run. Charges are added separately.
2. ``replay_trades`` — the exact Experiment A trades on dates with quotes:
   contract chosen at the signal minute, ask at entry, bid at exit, charges
   from app.services.trading.fills with the lot size of the trade date.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Optional

import numpy as np

from app.core.trading_params import TradingParams
from app.research.data import lot_size_on
from app.services.trading import fills

STRIKE_STEP = 50
SESSION = (dtime(9, 15), dtime(15, 30))
HOLDS = (5, 15, 30, 60, 120)
COST_MODEL_VERSION = "recorded-quotes-regression-v1"


class Quotes:
    def __init__(self, path: str):
        con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        self.q: dict = {}                     # (minute, expiry, strike, opt) -> (bid, ask)
        self.expiries: dict = defaultdict(set)
        for minute, expiry, k, opt, bid, ask in con.execute(
                "select minute, expiry, strike, opt, bid, ask from opt_min"):
            t = datetime.fromisoformat(minute).time()
            if not (SESSION[0] <= t < SESSION[1]):
                continue                       # out-of-hours snapshots are stale quotes
            self.q[(minute, expiry, float(k), opt)] = (bid, ask)
            self.expiries[minute[:10]].add(expiry)
        self.spot = {m: v for m, v in con.execute("select minute, ltp from spot_min")
                     if SESSION[0] <= datetime.fromisoformat(m).time() < SESSION[1]}
        con.close()

    @property
    def days(self) -> list[str]:
        return sorted(self.expiries)

    def nearest_expiry(self, day: str) -> Optional[str]:
        ex = sorted(e for e in self.expiries.get(day, ()) if e >= day)
        return ex[0] if ex else None

    def quote(self, minute: str, expiry: str, strike: float, opt: str):
        """(bid, ask) when valid (both > 0, uncrossed), else None."""
        v = self.q.get((minute, expiry, float(strike), opt))
        if v is None:
            return None
        bid, ask = v
        if not bid or not ask or bid <= 0 or ask < bid:
            return None
        return float(bid), float(ask)


def contract(spot: float, side: str, otm_steps: int) -> tuple[float, str]:
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    if side == "BUY":
        return float(atm + otm_steps * STRIKE_STEP), "CE"
    return float(atm - otm_steps * STRIKE_STEP), "PE"


def _plus(minute: str, m: int) -> str:
    from datetime import timedelta
    return (datetime.fromisoformat(minute) + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M")


def round_trip_charges(entry: float, exit_: float, qty: int, params: TradingParams) -> float:
    brok = 2 * fills.brokerage(1, params)
    return round(brok + fills.statutory_charges(entry * qty, exit_ * qty, brok, params), 2)


def execution_study(q: Quotes, params: Optional[TradingParams] = None) -> dict:
    p = params or TradingParams()
    samples = defaultdict(list)                 # (moneyness, expiry_day, h) -> [(move, pnl)]
    spreads = defaultdict(list)
    for minute, spot in sorted(q.spot.items()):
        day = minute[:10]
        ex = q.nearest_expiry(day)
        if ex is None:
            continue
        for side in ("BUY", "SELL"):
            sgn = 1 if side == "BUY" else -1
            for steps, label in ((0, "ATM"), (1, "OTM1")):
                k, opt = contract(spot, side, steps)
                a = q.quote(minute, ex, k, opt)
                if a is None:
                    continue
                spreads[(label, ex == day)].append(a[1] - a[0])
                for h in HOLDS:
                    m2 = _plus(minute, h)
                    b = q.quote(m2, ex, k, opt)
                    s2 = q.spot.get(m2)
                    if b is None or s2 is None:
                        continue
                    samples[(label, ex == day, h)].append((sgn * (s2 - spot), b[0] - a[1]))
    out = {"model_version": COST_MODEL_VERSION, "days": q.days, "spread": {}, "round_trip": {}}
    for (label, exp), v in sorted(spreads.items()):
        out["spread"][f"{label}|expiry={exp}"] = {
            "n": len(v), "median_rs": round(float(np.median(v)), 3),
            "p90_rs": round(float(np.percentile(v, 90)), 3)}
    for (label, exp, h), v in sorted(samples.items()):
        if len(v) < 30:
            continue
        x = np.array([m for m, _ in v]); y = np.array([pnl for _, pnl in v])
        b, a = np.polyfit(x, y, 1)
        out["round_trip"][f"{label}|expiry={exp}|{h}m"] = {
            "n": len(v), "eff_delta": round(float(b), 3), "intercept_rs": round(float(a), 3),
            "cost_spot_pts": round(float(-a / b), 2) if b > 0.05 else None,
            "mean_pnl_rs": round(float(y.mean()), 3)}
    lot = lot_size_on(date.fromisoformat(q.days[-1])) if q.days else 65
    out["charges_rs_per_lot_at_100"] = round_trip_charges(100.0, 100.0, lot, p)
    out["lot_size_used"] = lot
    return out


def replay_trades(trades: list, q: Quotes, otm_steps: int = 0,
                  params: Optional[TradingParams] = None) -> list[dict]:
    """Experiment A trades replayed on recorded quotes (entry ask at the fill
    minute, exit bid at the exit minute). Rows for every trade on a quoted
    day, with status FILLED or UNAVAILABLE and the reason."""
    p = params or TradingParams()
    days = set(q.days)
    out = []
    for t in trades:
        if t.day not in days:
            continue
        row = {"strategy": t.strategy, "variant": t.variant, "day": t.day, "side": t.side,
               "signal_time": t.signal_time, "entry_time": t.entry_time, "exit_time": t.exit_time,
               "spot_r_gross": t.r_gross, "otm_steps": otm_steps, "status": "UNAVAILABLE"}
        m_sig = f"{t.day}T{t.signal_time}"
        m_in, m_out = f"{t.day}T{t.entry_time}", f"{t.day}T{t.exit_time}"
        ex = q.nearest_expiry(t.day)
        spot = q.spot.get(m_sig) or t.plan_entry
        if ex is None:
            row["reason"] = "no expiry recorded"
            out.append(row)
            continue
        k, opt = contract(spot, t.side, otm_steps)
        a, b = q.quote(m_in, ex, k, opt), q.quote(m_out, ex, k, opt)
        row.update(expiry=ex, strike=k, opt=opt)
        if a is None or b is None:
            row["reason"] = "no valid quote at entry" if a is None else "no valid quote at exit"
            out.append(row)
            continue
        lot = lot_size_on(date.fromisoformat(t.day))
        entry = fills.ceil_tick(a[1] * (1 + p.quote_slippage_pct / 100))
        exit_ = fills.floor_tick(b[0] * (1 - p.quote_slippage_pct / 100))
        gross = round((exit_ - entry) * lot, 2)
        charges = round_trip_charges(entry, exit_, lot, p)
        mid_in, mid_out = (a[0] + a[1]) / 2, (b[0] + b[1]) / 2
        row.update(status="FILLED", lot=lot, entry_ask=a[1], entry_fill=entry, exit_bid=b[0],
                   exit_fill=exit_, spread_cost_rs=round(((a[1] - a[0]) / 2 + (b[1] - b[0]) / 2) * lot, 2),
                   mid_pnl_rs=round((mid_out - mid_in) * lot, 2), gross_rs=gross,
                   charges_rs=charges, net_rs=round(gross - charges, 2))
        out.append(row)
    return out
