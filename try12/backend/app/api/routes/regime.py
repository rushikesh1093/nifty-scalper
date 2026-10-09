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

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guards import LIVE_RUN_ID, run_id_query
from app.db.base import get_db
from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
from app.services.cache.redis_client import redis_cache
from app.services.trading.regime_persistence import regime_persistence

router = APIRouter(tags=["regime"])
logger = logging.getLogger(__name__)


@router.get("/performance")
async def get_regime_performance(
    underlying: Optional[str] = Query(None, description="Filter by underlying (e.g. NIFTY, BANKNIFTY)"),
    date_from:  Optional[str] = Query(None, description="ISO date string — include rows with last_trade_at >= this date"),
    date_to:    Optional[str] = Query(None, description="ISO date string — include rows with last_trade_at <= this date"),
    run_id:     str = Depends(run_id_query),
    db: AsyncSession = Depends(get_db),
):
    """
    Return regime performance statistics for one run (default PAPER).

    Computes win_rate dynamically from winning_trades / total_trades so the
    value is always current even when the stored column is stale or NULL.
    Results are ordered by total_pnl DESC.
    """
    try:
        q = select(RegimePerformanceModel).where(RegimePerformanceModel.run_id == run_id) \
            .order_by(desc(RegimePerformanceModel.total_pnl))

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
        raise HTTPException(status_code=503, detail=f"regime performance unavailable: {exc}")


@router.get("/current")
async def get_regime_current(
    underlying: str = Query("NIFTY", description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
):
    """
    Return current regime status for the specified underlying.
    Reads from Redis key regime:{underlying}:current.
    Returns {regime: str, since: ISO timestamp, seconds_in_regime: int, source: "redis" | "absent"}.
    "absent" means the key genuinely does not exist; a cache failure is a 503, never UNKNOWN.
    """
    try:
        key = f"regime:{underlying.upper()}:current"
        raw = await redis_cache._r().get(key)
        
        if not raw:
            return {
                "regime": "UNKNOWN",
                "since": None,
                "seconds_in_regime": 0,
                "source": "absent",
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
            "source": "redis",
        }
    
    except Exception as exc:
        logger.error("Error fetching current regime: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail=f"regime cache unavailable: {exc}")


@router.get("/strategy-map")
async def get_strategy_map():
    """
    The regime → strategy map (design §6.2) plus live per-underlying state
    (§6.5). Reasons are a closed set: enabled, regime_rule, user_disabled,
    daily_strategy_halt, transition_cooldown, regime_unavailable.
    """
    from app.runtime.context import ctx
    from app.services.trading.regime_rules import (
        NEVER_ENABLED, POSITION_MULTIPLIERS, REGIME_RULES,
    )
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    from app.strategies.names import ALL_STRATEGY_NAMES

    regimes = [
        {
            "regime": name,
            "strategies": sorted(rule.enabled_strategies),
            "directions": sorted(rule.allowed_directions),
            "multiplier": rule.position_multiplier,
            "notes": rule.notes,
        }
        for name, rule in REGIME_RULES.items()
    ]
    user_flags = ctx().user_flags.current()
    current = []
    for u in ctx().cfg.underlyings:
        strategies = []
        for name in ALL_STRATEGY_NAMES:
            enabled, reason = strategy_orchestrator.is_strategy_enabled(u, name)
            if not user_flags.get(name, True):
                enabled, reason = False, "user_disabled"
            elif reason == "regime flags not yet computed":
                reason = "regime_unavailable"
            strategies.append({"name": name, "enabled": enabled, "reason": reason})
        current.append({
            "underlying": u,
            "regime": strategy_orchestrator.current_regime_name(u),
            "cooldown_remaining": strategy_orchestrator.cooldown_remaining(u),
            "strategies": strategies,
        })
    return {
        "multiplier_set": sorted(POSITION_MULTIPLIERS),
        "never_enabled": sorted(NEVER_ENABLED),
        "regimes": regimes,
        "current": current,
    }


_IST = timezone(timedelta(hours=5, minutes=30))


def _collapse_spans(rows_asc: list[dict], source: str, now: datetime) -> list[dict]:
    """
    Merge consecutive rows with the same regime into spans (ascending).
    A span runs until the next span starts; the newest span runs until `now`.
    adx/atr come from the span's last row, previous_regime from its first row.
    """
    groups: list[list[dict]] = []
    for row in rows_asc:
        if groups and groups[-1][-1]["regime"] == row["regime"]:
            groups[-1].append(row)
        else:
            groups.append([row])
    spans = []
    for i, g in enumerate(groups):
        start = g[0]["timestamp"]
        end = groups[i + 1][0]["timestamp"] if i + 1 < len(groups) else now
        spans.append({
            "regime":          g[0]["regime"],
            "from":            start.astimezone(_IST).isoformat(),
            "to":              end.astimezone(_IST).isoformat(),
            "duration_secs":   max(0, int((end - start).total_seconds())),
            "adx":             g[-1]["adx"],
            "atr":             g[-1]["atr"],
            "previous_regime": g[0]["previous_regime"],
            "samples":         len(g),
            "source":          source,
        })
    return spans


@router.get("/history")
async def get_regime_history(
    underlying: str = Query("NIFTY", description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
    hours: int = Query(24, ge=1, le=168, description="Number of hours to look back"),
    limit: int = Query(500, ge=1, le=2000, description="Max rows scanned before collapsing (most recent kept)"),
    collapse: bool = Query(True, description="Merge consecutive equal regimes into spans"),
    run_id: str = Depends(run_id_query),
    db: AsyncSession = Depends(get_db),
):
    """
    Return regime history for the specified underlying.

    collapse=true (default): ascending spans
      {regime, from, to, duration_secs, adx, atr, previous_regime, samples, source}.
    collapse=false: the per-row shape {regime, timestamp, adx, atr, previous_regime},
      newest first (descending by timestamp) exactly as before.

    Source is the regime_states table; when it has no rows (or is unreachable) the
    Redis regime:{underlying}:history key (last 5 states) is used with source "redis".
    DB and Redis both failing → 503.
    """
    u = underlying.upper()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    db_rows: list[dict] = []
    db_error: Optional[Exception] = None
    try:
        q = (
            select(RegimeStateModel)
            .where(
                RegimeStateModel.run_id == run_id,
                RegimeStateModel.underlying == u,
                RegimeStateModel.timestamp >= cutoff,
            )
            .order_by(desc(RegimeStateModel.timestamp))
            .limit(limit)
        )
        result = await db.execute(q)
        db_rows = [
            {
                "regime": r.regime,
                "timestamp": r.timestamp,
                "adx": float(r.adx) if r.adx is not None else None,
                "atr": float(r.atr) if r.atr is not None else None,
                "previous_regime": r.previous_regime,
            }
            for r in result.scalars().all()
        ]
    except Exception as exc:
        logger.error("Error fetching regime history from DB: %s", exc, exc_info=True)
        db_error = exc

    if db_rows:
        if not collapse:
            return [{**r, "timestamp": r["timestamp"].isoformat()} for r in db_rows]   # descending, as before
        return _collapse_spans(list(reversed(db_rows)), "db", now)
    if run_id != LIVE_RUN_ID and db_error is None:
        return []          # the Redis keys hold the LIVE regime; never mix runs (D12)

    # Redis fallback (regime_states empty or unreachable)
    try:
        await redis_cache._r().exists(f"regime:{u}:history")   # raises when Redis is down
        states = await regime_persistence.load_regime_history(u)
    except Exception as exc:
        if db_error is not None:
            logger.error("Regime history: DB and Redis both failed: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=503,
                detail=f"regime history unavailable: db: {db_error}; redis: {exc}",
            )
        logger.warning("Regime history: regime_states empty and Redis fallback failed: %s", exc)
        return []

    logger.debug("Regime history %s: regime_states empty — using Redis fallback (%d states)", u, len(states))
    redis_rows = sorted(
        (
            {
                "regime": s.regime,
                "timestamp": s.timestamp,
                "adx": float(s.indicators.adx) if s.indicators.adx is not None else None,
                "atr": float(s.indicators.atr) if s.indicators.atr is not None else None,
                "previous_regime": s.previous_regime,
            }
            for s in states
            if s.underlying.upper() == u and s.timestamp >= cutoff
        ),
        key=lambda r: r["timestamp"],
    )
    if not collapse:
        return [
            {**r, "timestamp": r["timestamp"].isoformat(), "source": "redis"}
            for r in reversed(redis_rows)
        ]
    return _collapse_spans(redis_rows, "redis", now)
