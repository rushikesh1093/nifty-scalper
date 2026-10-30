"""
Expiry-day NIFTY iron fly, paper only (research/FINDINGS.md section 6).

On a NIFTY weekly expiry day, between 09:30 and 09:45 IST: sell the ATM CE and
PE, buy the CE and PE WING_PTS away (one lot each). Close all four legs at
15:00. No stop: the wings cap the loss at (wing - credit) x quantity.

Fills use the live quotes: a leg we sell fills at the bid, a leg we buy at the
ask. Nothing opens unless all four legs have a fresh two-sided quote. If the
15:00 close cannot be done (no quotes, backend down) the spread is settled at
the options' intrinsic value against spot once the market has closed, as NSE
settles expiring index options. A spread found open on a later day (the
backend was down through expiry) cannot be valued honestly: it is marked
UNSETTLED with no P&L.

A separate paper book (paper_spreads): it never touches paper_positions, the
paper capital, the risk gate, Kelly sizing or the strategy metrics. The pure
functions below carry the arithmetic; IronFlyPaper does the I/O.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime
from typing import Optional

from sqlalchemy import select

from app.core.config import settings
from app.runtime.context import ctx, now_ist, now_utc, params, trading_day
from app.services.trading import fills

logger = logging.getLogger(__name__)

STRATEGY = "EXPIRY_IRON_FLY"
UNDERLYING = "NIFTY"
STRIKE_STEP = 50
WING_PTS = 200
LOTS = 1
ENTRY_FROM, ENTRY_UNTIL = dtime(9, 30), dtime(9, 45)
EXIT_AT = dtime(15, 0)
MARKET_CLOSE = dtime(15, 30)
MAX_QUOTE_AGE_S = 60.0
LOOP_SECONDS = 15.0

OPEN, CLOSED, UNSETTLED = "OPEN", "CLOSED", "UNSETTLED"
TIME_EXIT, EXPIRY_SETTLE = "TIME_EXIT", "EXPIRY_SETTLE"


# ── pure arithmetic ──────────────────────────────────────────────────────────

def atm_strike(spot: float, step: int = STRIKE_STEP) -> float:
    return float(round(spot / step) * step)


def pick_legs(rows, expiry: date, atm: float, wing: float = WING_PTS) -> Optional[list[dict]]:
    """The four legs (short ATM CE + PE, long CE + PE *wing* away) from the
    instrument rows of *expiry*, or None if any strike is missing."""
    by_key = {(float(r.strike), getattr(r.option_type, "value", r.option_type)): r
              for r in rows if r.expiry == expiry and r.strike is not None}
    wanted = [(atm, "CE", "SELL"), (atm, "PE", "SELL"),
              (atm + wing, "CE", "BUY"), (atm - wing, "PE", "BUY")]
    legs = []
    for strike, opt, side in wanted:
        r = by_key.get((strike, opt))
        if r is None:
            return None
        legs.append({"token": str(r.token), "symbol": r.symbol, "strike": strike,
                     "option_type": opt, "side": side, "lot_size": int(r.lot_size or 0)})
    return legs


def two_sided(tick: Optional[dict], now: datetime, max_age_s: float = MAX_QUOTE_AGE_S):
    """(bid, ask) from a fresh tick with a real two-sided quote, else None."""
    if not tick:
        return None
    bid, ask = float(tick.get("best_bid") or 0), float(tick.get("best_ask") or 0)
    age = fills.tick_age_seconds(tick, now)
    if bid <= 0 or ask < bid or age is None or age > max_age_s:
        return None
    return bid, ask


def fill(side: str, bid: float, ask: float) -> float:
    """Selling fills at the bid, buying at the ask."""
    return bid if side == "SELL" else ask


def intrinsic(leg: dict, spot: float) -> float:
    k = leg["strike"]
    return max(0.0, spot - k) if leg["option_type"] == "CE" else max(0.0, k - spot)


def credit(legs: list[dict]) -> float:
    """Net premium received per unit at entry."""
    return round(sum(l["entry_price"] if l["side"] == "SELL" else -l["entry_price"] for l in legs), 2)


def settle(legs: list[dict], quantity: int, p) -> dict:
    """Gross, brokerage, charges, net for legs that all have entry_price and exit_price.
    Every leg is two orders (open + close); a short leg's sell is its entry."""
    gross = sum((l["entry_price"] - l["exit_price"]) if l["side"] == "SELL"
                else (l["exit_price"] - l["entry_price"]) for l in legs) * quantity
    sold = sum((l["entry_price"] if l["side"] == "SELL" else l["exit_price"]) for l in legs) * quantity
    bought = sum((l["exit_price"] if l["side"] == "SELL" else l["entry_price"]) for l in legs) * quantity
    brk = fills.brokerage(LOTS, p) * 2 * len(legs)
    charges = fills.statutory_charges(bought, sold, brk, p)
    return {"gross_pnl": round(gross, 2), "brokerage": round(brk, 2),
            "statutory_charges": charges, "net_pnl": fills.net_pnl(gross, brk, charges)}


def mark_to_market(legs: list[dict], quotes: dict, quantity: int) -> Optional[float]:
    """Gross P&L if closed now at the quotes (buy back at ask, sell at bid), or None."""
    total = 0.0
    for l in legs:
        q = quotes.get(l["token"])
        if q is None:
            return None
        bid, ask = q
        total += (l["entry_price"] - ask) if l["side"] == "SELL" else (bid - l["entry_price"])
    return round(total * quantity, 2)


# ── service ──────────────────────────────────────────────────────────────────

class IronFlyPaper:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._skip_logged: Optional[tuple] = None

    async def start(self) -> None:
        if not settings.IRON_FLY_PAPER_ENABLED:
            logger.info("[%s] disabled (IRON_FLY_PAPER_ENABLED=false)", STRATEGY)
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[%s] cycle failed: %s", STRATEGY, exc, exc_info=True)
            await asyncio.sleep(LOOP_SECONDS)

    def _skip(self, reason: str) -> None:
        """Log why today's entry is not happening, once per reason per day."""
        key = (trading_day(), reason)
        if self._skip_logged != key:
            self._skip_logged = key
            logger.info("[%s] no entry: %s", STRATEGY, reason)

    async def _quotes(self, legs: list[dict]) -> dict:
        now = now_utc()
        out = {}
        for l in legs:
            q = two_sided(await ctx().market.get_tick(l["token"]), now)
            if q is not None:
                out[l["token"]] = q
        return out

    async def _spot(self, max_age_s: Optional[float] = MAX_QUOTE_AGE_S) -> Optional[float]:
        tick = await ctx().market.get_tick(settings.NIFTY_TOKEN)
        ltp = float((tick or {}).get("ltp") or 0)
        if ltp <= 0:
            return None
        age = fills.tick_age_seconds(tick, now_utc())
        if max_age_s is not None and (age is None or age > max_age_s):
            return None
        return ltp

    async def cycle(self) -> None:
        from app.db.models.paper_spread import PaperSpread
        today, t = trading_day(), now_ist().time().replace(tzinfo=None)
        async with ctx().sessions() as db:
            open_rows = (await db.execute(select(PaperSpread).where(
                PaperSpread.strategy_name == STRATEGY, PaperSpread.status == OPEN))).scalars().all()
            for row in open_rows:
                if row.trading_day < today:
                    self._mark_unsettled(row)
                elif t >= EXIT_AT:
                    await self._close(row, t)
            if open_rows:
                await db.commit()
                return
            if not (ENTRY_FROM <= t < ENTRY_UNTIL):
                return
            done = (await db.execute(select(PaperSpread.id).where(
                PaperSpread.strategy_name == STRATEGY, PaperSpread.trading_day == today))).first()
            if done:
                return
            row = await self._open(db, today)
            if row is not None:
                db.add(row)
                await db.commit()
                logger.info("[%s] opened: credit %.2f pts, max loss Rs%.0f, legs %s",
                            STRATEGY, row.credit_pts, row.max_loss,
                            ", ".join(f"{l['side']} {l['symbol']}@{l['entry_price']}" for l in row.legs))

    async def _open(self, db, today: date):
        from app.db.models.paper_spread import PaperSpread
        rows = [r for r in await ctx().instruments.instruments_for(db, UNDERLYING)
                if r.expiry is not None and r.strike is not None and r.option_type is not None]
        expiries = sorted({r.expiry for r in rows if r.expiry >= today})
        if not expiries or expiries[0] != today:
            return self._skip("not a NIFTY expiry day")
        spot = await self._spot()
        if spot is None:
            return self._skip("no fresh NIFTY spot price")
        atm = atm_strike(spot)
        legs = pick_legs(rows, today, atm)
        if legs is None:
            return self._skip(f"strikes {atm - WING_PTS:.0f}-{atm + WING_PTS:.0f} not all in the instrument master")
        quotes = await self._quotes(legs)
        missing = [l["symbol"] for l in legs if l["token"] not in quotes]
        if missing:
            return self._skip(f"no fresh two-sided quote for {', '.join(missing)}")
        for l in legs:
            bid, ask = quotes[l["token"]]
            l.update(entry_bid=bid, entry_ask=ask, entry_price=fill(l["side"], bid, ask))
        cr = credit(legs)
        lot_size = legs[0]["lot_size"]
        if cr <= 0 or cr >= WING_PTS or lot_size <= 0:
            return self._skip(f"credit {cr:.2f} pts is outside (0, {WING_PTS})")
        qty = LOTS * lot_size
        return PaperSpread(
            strategy_name=STRATEGY, trading_day=today, underlying=UNDERLYING, expiry=today,
            status=OPEN, lots=LOTS, lot_size=lot_size, legs=legs, spot_entry=spot,
            credit_pts=cr, max_loss=round((WING_PTS - cr) * qty, 2), entry_time=now_utc())

    async def _close(self, row, t: dtime) -> None:
        legs = [dict(l) for l in row.legs]
        quotes = await self._quotes(legs)
        if all(l["token"] in quotes for l in legs):
            for l in legs:
                bid, ask = quotes[l["token"]]
                # closing: a short leg is bought back at the ask, a long one sold at the bid
                l.update(exit_bid=bid, exit_ask=ask, exit_price=fill("BUY" if l["side"] == "SELL" else "SELL", bid, ask))
            reason, spot = TIME_EXIT, await self._spot(max_age_s=None)
        elif t >= MARKET_CLOSE and row.expiry == trading_day():
            spot = await self._spot(max_age_s=None)
            if spot is None:
                return
            for l in legs:
                l["exit_price"] = intrinsic(l, spot)
            reason = EXPIRY_SETTLE
        else:
            return                                    # wait for quotes until the close
        res = settle(legs, row.lots * row.lot_size, params())
        row.legs, row.status, row.exit_reason = legs, CLOSED, reason
        row.exit_time, row.spot_exit = now_utc(), spot
        for k, v in res.items():
            setattr(row, k, v)
        row.r_multiple = round(res["net_pnl"] / row.max_loss, 4) if row.max_loss else None
        logger.info("[%s] closed (%s): net Rs%.2f, %.3fR", STRATEGY, reason, row.net_pnl, row.r_multiple or 0)

    def _mark_unsettled(self, row) -> None:
        from app.core.alerts import alert
        row.status, row.exit_reason, row.exit_time = UNSETTLED, UNSETTLED, now_utc()
        alert("iron_fly_unsettled", f"{STRATEGY} spread of {row.trading_day} was still open on a "
              f"later day; marked UNSETTLED with no P&L")

    async def snapshot(self, limit: int = 50) -> dict:
        """Today's spread with live mark-to-market, the recent history and a summary."""
        from app.db.models.paper_spread import PaperSpread
        async with ctx().sessions() as db:
            rows = (await db.execute(select(PaperSpread).where(PaperSpread.strategy_name == STRATEGY)
                                     .order_by(PaperSpread.trading_day.desc()).limit(limit))).scalars().all()
        out = []
        for r in rows:
            d = {c.name: getattr(r, c.name) for c in PaperSpread.__table__.columns}
            if r.status == OPEN:
                mtm = mark_to_market(r.legs, await self._quotes(r.legs), r.lots * r.lot_size)
                d["mtm_gross_pnl"] = mtm
            out.append(d)
        closed = [r for r in rows if r.status == CLOSED and r.net_pnl is not None]
        return {
            "strategy": STRATEGY,
            "enabled": settings.IRON_FLY_PAPER_ENABLED,
            "rules": {"entry": f"{ENTRY_FROM:%H:%M}-{ENTRY_UNTIL:%H:%M} on expiry day",
                      "exit": f"{EXIT_AT:%H:%M}", "wing_pts": WING_PTS, "lots": LOTS},
            "summary": {
                "trades": len(closed),
                "wins": sum(1 for r in closed if r.net_pnl > 0),
                "net_pnl": round(sum(r.net_pnl for r in closed), 2),
                "avg_r": round(sum(r.r_multiple or 0 for r in closed) / len(closed), 4) if closed else None,
            },
            "spreads": out,
        }


iron_fly_paper = IronFlyPaper()
