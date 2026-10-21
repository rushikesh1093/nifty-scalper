"""
Research endpoints — READ-ONLY. GET /research/direction-edge and
GET /research/entry-timing report on the trend_decisions telemetry; the rest is a
market-history download for offline strategy
evaluation. Nothing here trades, writes to the trading database, or touches
positions: it calls AngelOne's historical-candle API and saves the candles as
JSON files under ``backend/research_data/``.

  POST /research/fetch-history?days=25
      For NIFTY and BANKNIFTY: 1-minute SPOT candles (NSE index) and 1-minute
      candles of the current near-month index FUTURE (NFO; its volume is the
      VWAP / volume-surge source).
      source=angel (default): AngelOne historical API; on failure the error
      carries AngelOne's own response text.
      source=db: the candles this system already recorded in its ohlc table
      (no AngelOne call). One file per underlying:
        research_data/{UNDERLYING}_1m.json
          {"underlying", "spot_token", "futures_token", "futures_symbol",
           "fetched_at", "spot": [[ts, o, h, l, c, v], ...], "futures": [...]}
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.services.broker.angel_client import angel_client

router = APIRouter(prefix="/research", tags=["research"])
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[3] / "research_data"
CHUNK_DAYS = 8            # AngelOne caps ONE_MINUTE requests at ~30 days; stay well under
REQUEST_GAP_S = 0.5       # historical API rate limit is ~3 requests/second


async def _fetch_range(exchange: str, token: str, start: date, end: date) -> list[list]:
    out: list[list] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        rows = await angel_client.get_candle_data(
            exchange=exchange, symbol_token=token, interval="ONE_MINUTE",
            from_date=f"{cur} 09:15", to_date=f"{chunk_end} 15:30",
        )
        out.extend(rows or [])
        cur = chunk_end + timedelta(days=1)
        await asyncio.sleep(REQUEST_GAP_S)
    # de-duplicate on timestamp, keep order
    seen, uniq = set(), []
    for r in out:
        if r and r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    uniq.sort(key=lambda r: r[0])
    return uniq


async def _db_range(token: str, start: date) -> list[list]:
    from app.db.models.ohlc import OHLC
    from datetime import timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(OHLC).where(OHLC.token == token, OHLC.interval == "ONE_MINUTE",
                               OHLC.timestamp >= datetime.combine(start, datetime.min.time(), ist))
            .order_by(OHLC.timestamp)
        )).scalars().all()
    return [[r.timestamp.astimezone(ist).isoformat(), r.open, r.high, r.low, r.close,
             r.volume or 0] for r in rows]


def _angel_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        body = (exc.response.text or "")[:400]
        return f"AngelOne HTTP {exc.response.status_code}: {body or '(empty body)'}"
    return f"{type(exc).__name__}: {exc}"


@router.post("/fetch-history")
async def fetch_history(days: int = Query(25, ge=2, le=60),
                        source: str = Query("angel", pattern="^(angel|db)$")):
    from app.services.market_data.market_manager import resolve_index_future

    end = date.today()
    start = end - timedelta(days=days)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = {}
    for und, spot_token in (("NIFTY", settings.NIFTY_TOKEN),
                            ("BANKNIFTY", settings.BANKNIFTY_TOKEN)):
        try:
            async with AsyncSessionLocal() as db:
                fut = await resolve_index_future(db, und, end)
            # Spot and futures are fetched independently: AngelOne refuses NSE
            # index history (403) for this account while NFO futures history
            # works, and the futures alone are enough for a backtest.
            spot_error = None
            if source == "db":
                spot = await _db_range(spot_token, start)
                futs = await _db_range(fut.token, start) if fut else []
            else:
                try:
                    spot = await _fetch_range("NSE", spot_token, start, end)
                except Exception as exc:
                    spot, spot_error = [], _angel_error(exc)
                futs = await _fetch_range("NFO", fut.token, start, end) if fut else []
            payload = {
                "underlying": und, "spot_token": spot_token,
                "futures_token": fut.token if fut else None,
                "futures_symbol": fut.symbol if fut else None,
                "fetched_at": datetime.now().isoformat(), "source": source,
                "spot": spot, "futures": futs,
            }
            path = DATA_DIR / f"{und}_1m.json"
            path.write_text(json.dumps(payload))
            days_seen = sorted({str(r[0])[:10] for r in spot})
            fut_days = sorted({str(r[0])[:10] for r in futs})
            summary[und] = {"file": str(path), "spot_candles": len(spot),
                            "spot_error": spot_error,
                            "futures_days": len(fut_days),
                            "futures_candles": len(futs), "days": len(days_seen),
                            "first_day": days_seen[0] if days_seen else None,
                            "last_day": days_seen[-1] if days_seen else None,
                            "futures_symbol": payload["futures_symbol"]}
        except Exception as exc:
            logger.warning("[research] history fetch failed for %s: %s", und, exc, exc_info=True)
            summary[und] = {"error": _angel_error(exc)}
    if all("error" in v for v in summary.values()):
        raise HTTPException(status_code=502, detail=summary)
    return {"status": "ok", "summary": summary}


# ── Trend-engine research telemetry (read-only) ──────────────────────────────

async def _decision_rows(from_day: Optional[date], to_day: Optional[date],
                         underlying: Optional[str], run_id: Optional[str]) -> list:
    from app.db.models.research import TrendDecisionRecord
    from app.runtime.context import ctx, sessions

    q = select(TrendDecisionRecord).where(
        TrendDecisionRecord.run_id == (run_id or ctx().run_id))
    if from_day:
        q = q.where(TrendDecisionRecord.trading_day >= from_day)
    if to_day:
        q = q.where(TrendDecisionRecord.trading_day <= to_day)
    if underlying:
        q = q.where(TrendDecisionRecord.underlying == underlying.upper())
    try:
        async with sessions()() as db:
            return list((await db.execute(q.order_by(TrendDecisionRecord.candle_ts))).scalars().all())
    except Exception as exc:          # table not created yet (no decision recorded)
        logger.info("[research] trend_decisions unavailable: %s", exc)
        return []


@router.get("/direction-edge")
async def direction_edge(from_day: Optional[date] = None, to_day: Optional[date] = None,
                         underlying: Optional[str] = Query(None, pattern="^(NIFTY|BANKNIFTY)$"),
                         unit: str = Query("atr", pattern="^(atr|points)$"),
                         run_id: Optional[str] = None):
    """Does the engine's direction call predict the index's next 15/30/60 min?
    Per whole trading day, by index / regime / score / strength / trigger, with
    day-block bootstrap intervals (app.services.metrics.direction_edge)."""
    from app.services.metrics import direction_edge as de

    return de.report(await _decision_rows(from_day, to_day, underlying, run_id), unit=unit)


@router.get("/entry-timing")
async def entry_timing(from_day: Optional[date] = None, to_day: Optional[date] = None,
                       underlying: Optional[str] = Query(None, pattern="^(NIFTY|BANKNIFTY)$"),
                       unit: str = Query("atr", pattern="^(atr|points)$"),
                       run_id: Optional[str] = None):
    """Next-open entry vs a two-candle limit at the decision close, conservative
    fills, same fixed-horizon exit (app.services.metrics.entry_timing)."""
    from app.services.metrics import entry_timing as et

    return et.report(await _decision_rows(from_day, to_day, underlying, run_id), unit=unit)
