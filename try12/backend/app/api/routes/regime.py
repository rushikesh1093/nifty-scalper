"""
Regime API routes:
  GET  /api/regime/performance  — regime performance statistics per (regime, underlying, strategy)
  GET  /api/regime/current      — current regime status for an underlying
  GET  /api/regime/history      — regime transition history
"""
import json
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
from app.services.cache.redis_client import redis_cache

router = APIRouter(tags=["regime"])
logger = logging.getLogger(__name__)


@router.get("/performance")
async def get_regime_performance(
    underlying: Optional[str] = Query(None, description="Filter by underlying (e.g. NIFTY, BANKNIFTY)"),
    date_from:  Optional[str] = Query(None, description="ISO date string — include rows with last_trade_at >= this date"),
    date_to:    Optional[str] = Query(None, description="ISO date string — include rows with last_trade_at <= this date"),
    db: AsyncSession = Depends(get_db),
):
    """
    Return regime performance statistics.

    Computes win_rate dynamically from winning_trades / total_trades so the
    value is always current even when the stored column is stale or NULL.
    Results are ordered by total_pnl DESC.
    """
    try:
        q = select(RegimePerformanceModel).order_by(desc(RegimePerformanceModel.total_pnl))

        if underlying:
            q = q.where(RegimePerformanceModel.underlying == underlying.upper())

        if date_from:
            dt_from = datetime.combine(date.fromisoformat(date_from), datetime.min.time()).replace(
                tzinfo=timezone.utc
            )
            q = q.where(RegimePerformanceModel.last_trade_at >= dt_from)

        if date_to:
            dt_to = datetime.combine(date.fromisoformat(date_to), datetime.max.time()).replace(
                tzinfo=timezone.utc
            )
            q = q.where(RegimePerformanceModel.last_trade_at <= dt_to)

        result = await db.execute(q)
        rows = result.scalars().all()

        return [
            {
                "regime":         r.regime,
                "underlying":     r.underlying,
                "strategy_name":  r.strategy_name,
                "total_trades":   r.total_trades,
                "win_rate":       round(r.winning_trades / r.total_trades * 100, 2)
                                  if r.total_trades else 0.0,
                "total_pnl":      float(r.total_pnl) if r.total_pnl is not None else 0.0,
                "avg_pnl":        float(r.avg_pnl)   if r.avg_pnl   is not None else None,
                "avg_rr":         float(r.avg_rr)    if r.avg_rr    is not None else None,
                "last_trade_at":  r.last_trade_at.isoformat() if r.last_trade_at else None,
            }
            for r in rows
        ]

    except Exception as exc:
        logger.error("Error fetching regime performance: %s", exc, exc_info=True)
        return []


@router.get("/current")
async def get_regime_current(
    underlying: str = Query("NIFTY", description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
):
    """
    Return current regime status for the specified underlying.
    Reads from Redis key regime:{underlying}:current.
    Returns {regime: str, since: ISO timestamp, seconds_in_regime: int}.
    """
    try:
        key = f"regime:{underlying.upper()}:current"
        raw = await redis_cache._r().get(key)
        
        if not raw:
            return {
                "regime": "UNKNOWN",
                "since": None,
                "seconds_in_regime": 0,
            }
        
        data = json.loads(raw)
        # Calculate seconds_in_regime from 'timestamp' field (stored by RegimeState)
        since_dt = datetime.fromisoformat(data.get("timestamp", "")) if data.get("timestamp") else None
        seconds_in_regime = 0
        if since_dt:
            now = datetime.now(timezone.utc)
            seconds_in_regime = int((now - since_dt).total_seconds())
        
        return {
            "regime": data.get("regime", "UNKNOWN"),
            "since": data.get("timestamp"),
            "seconds_in_regime": seconds_in_regime,
        }
    
    except Exception as exc:
        logger.error("Error fetching current regime: %s", exc, exc_info=True)
        return {
            "regime": "UNKNOWN",
            "since": None,
            "seconds_in_regime": 0,
        }


@router.get("/history")
async def get_regime_history(
    underlying: str = Query("NIFTY", description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
    hours: int = Query(24, description="Number of hours to look back"),
    db: AsyncSession = Depends(get_db),
):
    """
    Return regime transition history for the specified underlying.
    Queries regime_states table for rows within the specified time window.
    Returns list of {regime: str, timestamp: ISO, adx: float, atr: float, previous_regime: str | null}.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        q = (
            select(RegimeStateModel)
            .where(
                RegimeStateModel.underlying == underlying.upper(),
                RegimeStateModel.timestamp >= cutoff,
            )
            .order_by(desc(RegimeStateModel.timestamp))
        )
        
        result = await db.execute(q)
        rows = result.scalars().all()
        
        return [
            {
                "regime": r.regime,
                "timestamp": r.timestamp.isoformat(),
                "adx": float(r.adx) if r.adx is not None else None,
                "atr": float(r.atr) if r.atr is not None else None,
                "previous_regime": r.previous_regime,
            }
            for r in rows
        ]
    
    except Exception as exc:
        logger.error("Error fetching regime history: %s", exc, exc_info=True)
        return []
