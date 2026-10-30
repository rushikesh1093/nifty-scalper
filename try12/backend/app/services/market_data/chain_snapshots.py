"""
Option-chain snapshot persistence (option_chain_snapshots).

The study collector fetches the AngelOne REST option chain every 300 s; this
module normalises it, classifies fetch failures and writes one snapshot set
per (underlying, expiry, 300 s UTC bucket). Readers: replay/data_loader.load_day
(groups rows by (underlying, expiry, snapshot_time)), replay/coverage.py
(snapshot cadence) and api/routes/market.py.

Idempotency without a unique constraint: snapshot_time is floored to the 300 s
bucket and a set is inserted only if no row exists yet for
(underlying, expiry, snapshot_time) — check-then-insert in one transaction.
That is race-free because a single collector instance writes, and it
serialises its own refreshes (StudyCollector._chain_lock).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.db.models.option_chain import OptionChainSnapshot, OptionSide

logger = logging.getLogger(__name__)

SNAPSHOT_BUCKET_S = 300

# Outcome statuses
OK = "OK"
EMPTY_BODY = "EMPTY_BODY"
HTTP_ERROR = "HTTP_ERROR"
NETWORK_ERROR = "NETWORK_ERROR"
ERROR = "ERROR"
UNPARSEABLE = "UNPARSEABLE"
NO_EXPIRY = "NO_EXPIRY"
NO_SPOT = "NO_SPOT"
NO_TICKS = "NO_TICKS"
DB_ERROR = "DB_ERROR"
OUT_OF_SESSION = "OUT_OF_SESSION"

SUCCESS_STATUSES = frozenset({OK, OUT_OF_SESSION})


@dataclass
class ChainFetchOutcome:
    underlying: str
    status: str
    detail: str = ""
    strikes: int = 0
    rows_written: int = 0

    @property
    def failed(self) -> bool:
        return self.status not in SUCCESS_STATUSES


def snapshot_bucket(now: datetime, interval_s: int = SNAPSHOT_BUCKET_S) -> datetime:
    """*now* floored to the *interval_s* UTC bucket, as aware UTC. Naive = UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % interval_s, tz=timezone.utc)


def classify_fetch_error(exc: BaseException) -> str:
    """Map a chain-fetch exception to an outcome status. An empty HTTP body
    makes resp.json() raise json.JSONDecodeError (a ValueError):
    'Expecting value: line 1 column 1 (char 0)'."""
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTP_ERROR
    if isinstance(exc, (httpx.TransportError, OSError)):
        return NETWORK_ERROR
    if isinstance(exc, ValueError):
        return EMPTY_BODY
    return ERROR


def normalise_chain(raw_chain: Any) -> list[dict]:
    """
    AngelOne option chain response → list of strike dicts with keys
    token/strike/option_type/ltp/bid/ask/oi/volume/iv (IV in percent form).

    The response structure varies by API version: a list directly, a dict with
    a list under a wrapper key, or {CE: {strike: {...}}, PE: {strike: {...}}}.
    Returns [] when nothing is parseable.
    """
    if not raw_chain:
        return []

    raw_strikes = []
    if isinstance(raw_chain, list):
        raw_strikes = raw_chain
    elif isinstance(raw_chain, dict):
        # Check common wrapper keys
        for key in ("data", "fetched", "optionChainData", "strikes"):
            if key in raw_chain and isinstance(raw_chain[key], list):
                raw_strikes = raw_chain[key]
                break
        if not raw_strikes and "CE" in raw_chain:
            # Some versions: {CE: {strike: {...}}, PE: {strike: {...}}}
            for opt_type, strikes_dict in raw_chain.items():
                if opt_type not in ("CE", "PE"):
                    continue
                if isinstance(strikes_dict, dict):
                    for strike_key, strike_data in strikes_dict.items():
                        if isinstance(strike_data, dict):
                            strike_data["option_type"] = opt_type
                            raw_strikes.append(strike_data)

    strikes_out = []
    for s in raw_strikes:
        if not isinstance(s, dict):
            continue

        # Extract fields — AngelOne uses different key names across API versions
        token       = str(s.get("token") or s.get("symboltoken") or "")
        strike_val  = float(s.get("strikePrice") or s.get("strike_price") or s.get("strike") or 0)
        opt_type    = str(s.get("optionType") or s.get("option_type") or s.get("instrumentType") or "")
        opt_type    = "CE" if "CE" in opt_type.upper() else ("PE" if "PE" in opt_type.upper() else opt_type)
        ltp         = float(s.get("lastPrice") or s.get("ltp") or s.get("close") or 0)
        bid         = float(s.get("bidprice") or s.get("bid") or s.get("best_bid") or 0)
        ask         = float(s.get("askprice") or s.get("ask") or s.get("best_ask") or 0)
        oi          = int(s.get("openInterest") or s.get("oi") or 0)
        volume      = int(s.get("tradedVolume") or s.get("volume") or 0)
        iv_raw      = float(s.get("impliedVolatility") or s.get("iv") or 0)

        # Normalise IV to percentage form (e.g. 0.185 → 18.5, 18.5 → 18.5)
        iv = iv_raw * 100.0 if (0 < iv_raw < 1.0) else iv_raw

        if not token:
            continue

        strikes_out.append({
            "token":       token,
            "strike":      strike_val,
            "option_type": opt_type,
            "ltp":         ltp,
            "bid":         bid,
            "ask":         ask,
            "oi":          oi,
            "volume":      volume,
            "iv":          iv,
        })
    return strikes_out


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, years: float, vol: float, call: bool) -> float:
    """Black-Scholes, zero rate (a week's interest is far below the bid-ask)."""
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if call:
        return spot * _ncdf(d1) - strike * _ncdf(d2)
    return strike * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float,
                call: bool) -> Optional[float]:
    """Implied volatility in percent (18.5 = 18.5 %) by bisection, or None when
    the price carries no time value or the inputs are unusable."""
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if call else max(0.0, strike - spot)
    if price <= intrinsic + 0.05:
        return None
    lo, hi = 0.01, 3.0
    if _bs_price(spot, strike, years, hi, call) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if _bs_price(spot, strike, years, mid, call) < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2 * 100, 2)


def chain_from_ticks(instruments, ticks: dict, spot: float, expiry_close: datetime,
                     now: datetime, max_age_s: float = 120.0) -> list[dict]:
    """The option chain from the live feed's ticks, in normalise_chain's format.

    *instruments*: option rows (token, strike, option_type); *ticks*: token ->
    Redis tick. A strike is kept when its tick is fresh; IV is implied from the
    bid-ask mid (the last trade when there is no two-sided quote) with calendar
    time to *expiry_close*, the convention India VIX uses."""
    from app.services.trading.fills import tick_age_seconds
    years = max(0.0, (expiry_close - now).total_seconds()) / (365.0 * 86400.0)
    out = []
    for inst in instruments:
        tick = ticks.get(str(inst.token))
        age = tick_age_seconds(tick, now) if tick else None
        if age is None or age > max_age_s:
            continue
        opt = getattr(inst.option_type, "value", inst.option_type)
        ltp = float(tick.get("ltp") or 0)
        bid, ask = float(tick.get("best_bid") or 0), float(tick.get("best_ask") or 0)
        price = (bid + ask) / 2 if bid > 0 and ask >= bid else ltp
        iv = implied_vol(price, spot, float(inst.strike), years, opt == "CE") if years > 0 else None
        out.append({
            "token": str(inst.token), "strike": float(inst.strike), "option_type": opt,
            "ltp": ltp, "bid": bid, "ask": ask,
            "oi": int(tick.get("oi") or 0), "volume": int(tick.get("volume") or 0),
            "iv": iv or 0.0,
        })
    return out


def _pos(x: Any) -> Optional[float]:
    return float(x) if x is not None and float(x) > 0 else None


def _nz(x: Any) -> Optional[int]:
    return int(x) if x else None


async def persist_chain_snapshot(session_factory, *, underlying: str, expiry: date,
                                 spot: float, strikes: list[dict],
                                 snapshot_time: datetime) -> int:
    """Insert one snapshot set; returns rows written. 0 when the bucket already
    has rows for (underlying, expiry, snapshot_time), when spot <= 0, or when no
    strike is CE/PE. Exceptions propagate (the caller reports DB_ERROR)."""
    if not spot or spot <= 0:
        return 0
    rows = [dict(
        underlying=underlying, underlying_ltp=float(spot), expiry=expiry,
        strike=float(s["strike"]), option_type=OptionSide[s["option_type"]],
        token=str(s["token"]), ltp=_pos(s.get("ltp")), bid=_pos(s.get("bid")),
        ask=_pos(s.get("ask")), iv=_pos(s.get("iv")), oi=_nz(s.get("oi")),
        volume=_nz(s.get("volume")), snapshot_time=snapshot_time,
    ) for s in strikes if s.get("option_type") in ("CE", "PE")]
    if not rows:
        return 0

    async with session_factory() as db:
        try:
            exists = (await db.execute(select(OptionChainSnapshot.id).where(
                OptionChainSnapshot.underlying == underlying,
                OptionChainSnapshot.expiry == expiry,
                OptionChainSnapshot.snapshot_time == snapshot_time,
            ).limit(1))).scalar_one_or_none()
            if exists is not None:
                return 0
            db.add_all([OptionChainSnapshot(**r) for r in rows])
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return len(rows)
