"""
Trading API routes:
  GET  /trading/mode                    — current mode
  POST /trading/mode/paper              — switch to paper
  POST /trading/mode/live               — switch to live (requires confirm)
  GET  /trading/config                  — get full config
  PUT  /trading/config                  — update config
  PUT  /trading/config/strategy-flag    — enable/disable a strategy
  GET  /trading/signals                 — recent signals
  GET  /trading/signals/diagnostics     — why signals are (not) firing / converting
  GET  /trading/positions               — open + closed positions
  POST /trading/positions/{id}/close    — manual close a position
  GET  /trading/pnl                     — daily P&L summary
  GET  /trading/pnl/history             — multi-day P&L
  GET  /trading/capital                 — capital breakdown
  PUT  /trading/capital/trade-amount    — set ₹ per trade
  PUT  /trading/capital/set             — set total capital
  POST /trading/capital/reset-daily     — reset daily trade counter
  GET  /trading/stats                   — daily stats
  POST /trading/option-selector         — best option for a direction
  GET  /trading/market-study/intraday   — intraday features
  GET  /trading/market-study/daily      — daily snapshots
  GET  /trading/market-study/vix        — VIX history
  POST /trading/kill-switch             — halt new entries + square off all (paper)
  GET  /trading/kill-switch             — kill switch status
  POST /trading/kill-switch/release     — lift the halt
  WS   /trading/ws/signals              — live signal stream

NOTE: no route in this module is authenticated yet (see live-fixes-decisions.md §3).
"""
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models.paper_trade import (
    StrategySignal, PaperPosition, DailyPnLSummary, TradingConfig,
    TradingMode, SignalStatus,
)
from app.db.models.market_study import IntradayMarketFeature, MarketDailySnapshot, VIXData
from app.schemas.diagnostics import SignalDiagnosticsResponse
from app.services.trading.mode_manager import mode_manager
from app.services.trading.paper_engine import paper_engine
from app.services.trading.option_selector import option_selector
from app.strategies.base import Direction
from app.strategies.names import ALL_STRATEGY_NAMES
from app.core.config import settings

router = APIRouter(prefix="/trading", tags=["trading"])
logger = logging.getLogger(__name__)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    max_daily_loss:       Optional[float]   = None
    max_daily_loss_pct:   Optional[float]   = None
    max_open_positions:   Optional[int]     = None
    max_lots_per_trade:   Optional[int]     = None
    capital:              Optional[float]   = None
    slippage_pct:         Optional[float]   = None
    square_off_time:      Optional[str]     = None
    # ── new capital/trade-sizing fields ──
    trade_amount:         Optional[float]   = None   # ₹ per trade
    max_trades_per_day:   Optional[int]     = None
    study_phase:          Optional[bool]    = None   # flip to False to start paper trading

class StrategyFlagUpdate(BaseModel):
    strategy_name: str
    enabled:       bool

class OptionSelectorRequest(BaseModel):
    direction:   str          # BUY or SELL
    underlying:  str          # NIFTY or BANKNIFTY
    spot_price:  float
    expiry:      Optional[str] = None   # YYYY-MM-DD


# ── Mode ──────────────────────────────────────────────────────────────────────

@router.get("/mode")
async def get_mode():
    cfg = await mode_manager.get_config()
    return {
        "mode":                cfg.mode if cfg else "PAPER",
        "live_mode_confirmed": cfg.live_mode_confirmed if cfg else False,
    }

@router.post("/mode/paper")
async def switch_paper():
    return await mode_manager.switch_to_paper()

@router.post("/mode/live")
async def switch_live(confirm: bool = Query(False)):
    result = await mode_manager.switch_to_live(confirm=confirm)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config():
    cfg = await mode_manager.get_config()
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")
    return {
        "mode":                cfg.mode,
        "max_daily_loss":      cfg.max_daily_loss,
        "max_daily_loss_pct":  cfg.max_daily_loss_pct,
        "max_open_positions":  cfg.max_open_positions,
        "max_lots_per_trade":  cfg.max_lots_per_trade,
        "capital":             cfg.capital,
        "trade_amount":        cfg.trade_amount,
        "used_capital":        round(cfg.used_capital or 0, 2),
            "available_capital":   round(cfg.capital - (cfg.used_capital or 0.0), 2),
        "slippage_pct":        cfg.slippage_pct,
        "square_off_time":     cfg.square_off_time,
        "strategy_flags":      cfg.strategy_flags,
        "live_mode_confirmed": cfg.live_mode_confirmed,
        "study_phase":         cfg.study_phase,
        "max_trades_per_day":  cfg.max_trades_per_day,
        "daily_trades_count":  cfg.daily_trades_count,
        "trading_halted":      cfg.trading_halted,
    }

@router.put("/config")
async def update_config(body: ConfigUpdate):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    try:
        cfg = await mode_manager.update_config(**updates)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "updated", "mode": cfg.mode}

@router.put("/config/strategy-flag")
async def update_strategy_flag(body: StrategyFlagUpdate):
    await mode_manager.update_strategy_flag(body.strategy_name, body.enabled)
    return {"status": "updated", "strategy": body.strategy_name, "enabled": body.enabled}


# ── Capital endpoints ─────────────────────────────────────────────────────────

@router.get("/capital")
async def get_capital():
    """
    Full capital breakdown:
      capital, used_capital, available_capital,
      trade_amount, realised_pnl, utilisation_pct,
      circuit_breaker status, daily trade count.
    """
    return await mode_manager.get_capital_summary()


class TradeAmountUpdate(BaseModel):
    trade_amount: float       # ₹ per trade, e.g. 10000
    capital:      Optional[float] = None   # total funds (optional, update if provided)


@router.put("/capital/trade-amount")
async def set_trade_amount(body: TradeAmountUpdate):
    """
    Set the ₹ amount to deploy per trade.
    Optionally update total capital at the same time.

    Example: trade_amount=10000 with Nifty option at ₹150 (lot 50):
      cost_per_lot = 150 × 50 = ₹7,500
      lots = floor(10000 / 7500) = 1
      capital_deployed = ₹7,500
    """
    if body.trade_amount <= 0:
        raise HTTPException(status_code=400, detail="trade_amount must be > 0")

    updates: dict = {"trade_amount": body.trade_amount}
    if body.capital is not None:
        if body.capital <= 0:
            raise HTTPException(status_code=400, detail="capital must be > 0")
        updates["capital"] = body.capital

    cfg = await mode_manager.update_config(**updates)

    # Preview: calculate lots for current ATM option price (approximate)
    preview = _trade_amount_preview(body.trade_amount, cfg.max_lots_per_trade)

    return {
        "status":           "updated",
        "trade_amount":     cfg.trade_amount,
        "capital":          cfg.capital,
        "available_capital": round(cfg.capital - (cfg.used_capital or 0), 2),
        "preview":          preview,
    }


@router.put("/capital/set")
async def set_capital(capital: float = Query(..., gt=0, description="Total paper trading capital in ₹")):
    """Update total capital (funds available for trading)."""
    try:
        cfg = await mode_manager.update_config(capital=capital)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status":          "updated",
        "capital":         cfg.capital,
        "used_capital":    round(cfg.used_capital or 0.0, 2),
        "available":       cfg.capital,
        "trade_amount":    cfg.trade_amount,
    }


@router.post("/capital/reset-daily")
async def reset_daily_counter():
    """Reset the daily trades counter (normally happens automatically at midnight)."""
    await mode_manager.reset_daily_counter()
    return {"status": "reset", "daily_trades_count": 0}


def _trade_amount_preview(trade_amount: float, max_lots: int) -> list[dict]:
    """
    Preview how many lots ₹trade_amount buys at various typical option prices.
    Useful for the frontend to show the user what they'd get.
    """
    from app.services.trading.paper_engine import _calc_lots
    result = []
    for ltp in [50, 80, 100, 150, 200, 300, 500]:
        for lot_size in [50, 15]:   # Nifty=50, BankNifty=15 (revised Nov 2024)
            cost_per_lot = ltp * lot_size
            # 0 lots = unaffordable → paper engine would SKIP such a signal
            lots = min(max_lots, _calc_lots(trade_amount, ltp, lot_size)[0])
            result.append({
                "ltp":            ltp,
                "lot_size":       lot_size,
                "lots":           lots,
                "capital_used":   round(lots * cost_per_lot, 2),
                "capital_needed": cost_per_lot,
            })
    return result


# ── Daily stats ───────────────────────────────────────────────────────────────

@router.get("/stats")
async def daily_stats():
    return await mode_manager.get_daily_stats()


# ── Signals ───────────────────────────────────────────────────────────────────

@router.get("/signals")
async def get_signals(
    strategy:    Optional[str]  = None,
    underlying:  Optional[str]  = None,
    status:      Optional[str]  = None,
    today_only:  bool           = Query(True,  description="Only return today's signals (default true)"),
    limit:       int            = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    q = select(StrategySignal).order_by(desc(StrategySignal.signal_time))
    if strategy:
        q = q.where(StrategySignal.strategy_name == strategy)
    if underlying:
        q = q.where(StrategySignal.underlying == underlying.upper())
    if status:
        try:
            status_enum = SignalStatus(status.upper())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of: {', '.join(s.value for s in SignalStatus)}",
            )
        q = q.where(StrategySignal.status == status_enum)
    if today_only:
        # Filter to today's IST date using timezone-aware bounds.
        # signal_time is stored as UTC with timezone — convert today IST to UTC bounds.
        from datetime import timezone as _tz, timedelta as _td
        IST = _tz(_td(hours=5, minutes=30))
        today_ist = datetime.now(IST).date()
        day_start_utc = datetime.combine(today_ist, datetime.min.time()).replace(
            tzinfo=IST
        ).astimezone(_tz.utc)
        q = q.where(StrategySignal.signal_time >= day_start_utc)
    q = q.limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [
        {
            "id":               r.id,
            "strategy":         r.strategy_name,
            "underlying":       r.underlying,
            "direction":        r.direction,
            "status":           r.status,
            "spot":             r.spot_price,
            "vwap":             r.vwap,
            "strike":           r.suggested_strike,
            "symbol":           r.suggested_symbol,
            "suggested_token":  r.suggested_token,
            "entry_ref":        r.entry_price_ref,
            "sl_ref":           r.stop_loss_ref,
            "target_ref":       r.target_ref,
            "rr":               r.risk_reward,
            "mode":             r.mode,
            "time":             r.signal_time.isoformat() if r.signal_time else None,
            "indicators":       r.indicators,
            "skip_reason":      r.skip_reason,
        }
        for r in rows
    ]


# ── Signal pipeline diagnostics ───────────────────────────────────────────────

class _SkipProbe(Exception):
    """Internal: a probe skipped because its dependency already failed the preflight."""


@router.get("/signals/diagnostics", response_model=SignalDiagnosticsResponse)
async def get_signal_diagnostics():
    """
    Read-only explanation of why signals are (not) firing / converting.
    Total by construction: every probe is individually wrapped; a failing probe
    becomes a warning blocker and the endpoint still returns 200.
    """
    # Function-local imports: gates/constants are read from the runner itself so
    # the reported thresholds can never drift from the ones the loop applies.
    from app.services.trading import strategy_runner as _sr
    from app.services.trading.signal_diagnostics import (
        SESSION_WINDOW_NOTE, derive_blockers, format_session_window, session_window_open, vix_gate,
    )
    from app.services.trading.regime_rules import get_regime_rule
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    from app.services.cache.redis_client import redis_cache
    from app.services.market_data.levels import IST, parse_candle_ts
    from app.db.base import AsyncSessionLocal
    from app.db.models.instrument import Instrument, InstrumentType, OptionType
    from app.services.broker.instrument_sync import get_active_expiries
    from app.strategies.indicators import compute_all
    from sqlalchemy import func as _func

    now_ist = datetime.now(IST)
    start, end = settings.SESSION_START_IST, settings.SESSION_END_IST
    probe_errors: list[dict] = []

    def _probe_failed(code: str, exc: Exception) -> None:
        logger.warning("diagnostics probe %s failed: %s", code, exc)
        probe_errors.append({"code": code, "detail": str(exc)})

    # Redis preflight: with Redis down every call costs seconds (connect retries), so
    # one bounded ping decides whether the Redis-backed probes run at all.
    redis_ok = True
    try:
        await asyncio.wait_for(redis_cache._r().ping(), timeout=1.5)
    except Exception as exc:
        redis_ok = False
        _probe_failed("REDIS_UNAVAILABLE", exc if str(exc) else TimeoutError("redis ping timed out after 1.5 s"))

    # Runner
    runner_running = None
    try:
        runner_running = _sr.strategy_runner.is_running
    except Exception as exc:
        _probe_failed("RUNNER_PROBE", exc)

    # Feed + broker session
    feed = {"connected": None, "subscriptions": None, "session_valid": None}
    try:
        from app.services.market_data.feed import market_feed
        feed["connected"] = bool(market_feed.is_connected)
    except Exception as exc:
        _probe_failed("FEED_PROBE", exc)
    try:
        from app.services.market_data.feed import market_feed
        feed["subscriptions"] = int(market_feed.subscription_count)
    except Exception as exc:
        _probe_failed("FEED_SUBSCRIPTIONS_PROBE", exc)
    try:
        from app.services.broker.angel_client import _session
        feed["session_valid"] = bool(_session.is_valid())
    except Exception as exc:
        _probe_failed("SESSION_PROBE", exc)

    # Config (mode, study phase, strategy flags)
    mode = study_phase = None
    strategy_flags: dict = {}
    try:
        cfg = await mode_manager.get_config()
        if cfg:
            mode = cfg.mode.value if hasattr(cfg.mode, "value") else str(cfg.mode)
            study_phase = bool(cfg.study_phase)
            strategy_flags = cfg.strategy_flags or {}
    except Exception as exc:
        _probe_failed("CONFIG_PROBE", exc)

    # DB: signals + positions (DB_UNAVAILABLE if unreachable — counts stay null)
    db_available = True
    signals = {"today_count": None, "by_status": {}, "last_signal_at": None, "last_signal_age_secs": None}
    positions = {"open": None, "today_total": None}
    day_start_utc = datetime.combine(now_ist.date(), datetime.min.time()).replace(tzinfo=IST).astimezone(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(StrategySignal.status, _func.count())
                .where(StrategySignal.signal_time >= day_start_utc)
                .group_by(StrategySignal.status)
            )).all()
            by_status = {(s.value if hasattr(s, "value") else str(s)): int(n) for s, n in rows}
            signals["by_status"] = by_status
            signals["today_count"] = sum(by_status.values())
            last = (await db.execute(select(_func.max(StrategySignal.signal_time)))).scalar()
            if last is not None:
                signals["last_signal_at"] = last.astimezone(IST).isoformat()
                signals["last_signal_age_secs"] = max(0, int((now_ist - last).total_seconds()))
            positions["open"] = int((await db.execute(
                select(_func.count()).select_from(PaperPosition).where(PaperPosition.is_open == True)  # noqa: E712
            )).scalar() or 0)
            positions["today_total"] = int((await db.execute(
                select(_func.count()).select_from(PaperPosition).where(PaperPosition.entry_time >= day_start_utc)
            )).scalar() or 0)
    except Exception as exc:
        logger.error("diagnostics: database unreachable: %s", exc)
        db_available = False

    # Capital
    capital = {k: None for k in ("capital", "available", "trade_amount", "daily_trades", "max_trades_per_day",
                                 "open_positions", "max_open_positions", "circuit_breaker", "circuit_reason")}
    if db_available:
        try:
            cs = await mode_manager.get_capital_summary()
            if cs:
                capital.update(
                    capital=cs.get("capital"), available=cs.get("available_capital"),
                    trade_amount=cs.get("trade_amount"), daily_trades=cs.get("daily_trades"),
                    max_trades_per_day=cs.get("max_trades_per_day"), open_positions=cs.get("open_positions"),
                    max_open_positions=cs.get("max_open_positions"),
                    circuit_breaker=bool(cs.get("circuit_breaker")), circuit_reason=cs.get("circuit_reason") or "",
                )
        except Exception as exc:
            _probe_failed("CAPITAL_PROBE", exc)

    # VIX
    vix_ltp = None
    if redis_ok:
        try:
            vix_ltp = await redis_cache.get_ltp(settings.VIX_TOKEN)
        except Exception as exc:
            _probe_failed("VIX_PROBE", exc)
    vix = {"ltp": vix_ltp, "min": _sr.VIX_MIN, "max": _sr.VIX_MAX,
           "gate": vix_gate(vix_ltp, _sr.VIX_MIN, _sr.VIX_MAX)}

    # Per underlying
    underlyings: list[dict] = []
    session_start_mins = start.hour * 60 + start.minute
    for u in ("NIFTY", "BANKNIFTY"):
        token = settings.NIFTY_TOKEN if u == "NIFTY" else settings.BANKNIFTY_TOKEN
        info: dict = {
            "underlying": u, "spot": None, "candles_today": None,
            "candles_required": _sr.MIN_TODAY_CANDLES,
            "atr": None, "atr_min": _sr.ATR_MIN_NIFTY if u == "NIFTY" else _sr.ATR_MIN_BANKNIFTY,
            "last_candle_at": None, "regime": None, "regime_allows_trading": None,
        }
        extra: dict = {}
        try:
            if not redis_ok:
                raise _SkipProbe
            info["spot"] = await redis_cache.get_ltp(token)
            all_c = await redis_cache.get_candles(token, "ONE_MINUTE", 500)
            today_c = []
            for c in all_c:
                try:
                    ts = parse_candle_ts(c.get("timestamp")).astimezone(IST)
                except Exception:
                    continue
                if ts.date() == now_ist.date() and ts.hour * 60 + ts.minute >= session_start_mins:
                    today_c.append(c)
            info["candles_today"] = len(today_c)
            if all_c:
                info["last_candle_at"] = str(all_c[-1].get("timestamp"))
            if len(today_c) >= _sr.MIN_TODAY_CANDLES:
                atr = compute_all(today_c).get("atr")
                info["atr"] = round(float(atr), 4) if atr is not None else None
        except _SkipProbe:
            pass
        except Exception as exc:
            _probe_failed(f"CANDLES_PROBE_{u}", exc)
        try:
            if redis_ok:
                raw = await redis_cache._r().get(f"regime:{u}:current")
                if raw:
                    info["regime"] = json.loads(raw).get("regime")
                    info["regime_allows_trading"] = bool(get_regime_rule(info["regime"]).enabled_strategies)
            extra["in_cooldown"] = bool(strategy_orchestrator.is_in_cooldown(u))
        except Exception as exc:
            _probe_failed(f"REGIME_PROBE_{u}", exc)
        if db_available:
            try:
                async with AsyncSessionLocal() as db:
                    snap_date = (await db.execute(
                        select(MarketDailySnapshot.date)
                        .where(MarketDailySnapshot.underlying == u, MarketDailySnapshot.date < now_ist.date())
                        .order_by(desc(MarketDailySnapshot.date)).limit(1)
                    )).scalar()
                    extra["prev_day_snapshot"] = snap_date.isoformat() if snap_date else None
                    info["prev_day_snapshot"] = extra["prev_day_snapshot"]

                    # CAPITAL_BELOW_LOT_COST: nearest-expiry ATM CE/PE premium x lot size
                    if info["spot"]:
                        step = 50 if u == "NIFTY" else 100
                        atm = round(info["spot"] / step) * step
                        expiries = await get_active_expiries(db, u, as_of=now_ist.date())
                        insts = []
                        if expiries:
                            insts = (await db.execute(
                                select(Instrument).where(
                                    Instrument.name == u,
                                    Instrument.instrument_type == InstrumentType.OPTIDX,
                                    Instrument.expiry == expiries[0],
                                    Instrument.strike == atm,
                                    Instrument.option_type.in_([OptionType.CE, OptionType.PE]),
                                    Instrument.is_active == True,  # noqa: E712
                                )
                            )).scalars().all()
                        chain_ltp: dict = {}
                        if expiries:
                            chain = await redis_cache.get_option_chain(u, str(expiries[0]))
                            for s in (chain or {}).get("strikes", []):
                                if s.get("token") and s.get("ltp"):
                                    chain_ltp[str(s["token"])] = float(s["ltp"])
                        best = None
                        for inst in insts:
                            prem = chain_ltp.get(inst.token) or await redis_cache.get_ltp(inst.token)
                            if prem and prem > 0:
                                cost = prem * inst.lot_size
                                if best is None or cost < best[0]:
                                    best = (cost, prem, inst.lot_size)
                        if best:
                            extra.update(lot_cost=round(best[0], 2), atm_premium=best[1], lot_size=best[2])
                        else:
                            extra["lot_cost_unavailable"] = True
                    else:
                        extra["lot_cost_unavailable"] = True
            except Exception as exc:
                _probe_failed(f"LOT_COST_PROBE_{u}", exc)
        underlyings.append(info | {"_extra": extra})

    # Strategies
    strategies = {"total": None, "enabled": None, "disabled": []}
    try:
        names = list(ALL_STRATEGY_NAMES)    # canonical universe, never strategy_flags.keys()
        if names:
            disabled = [n for n in names if strategy_flags.get(n, True) is False]
            strategies = {"total": len(names), "enabled": len(names) - len(disabled), "disabled": disabled}
    except Exception as exc:
        _probe_failed("STRATEGIES_PROBE", exc)

    blockers = derive_blockers({
        "now_ist": now_ist, "session_start": start, "session_end": end,
        "db_available": db_available, "runner_running": runner_running, "study_phase": study_phase,
        "feed": feed, "vix": vix, "capital": capital, "strategies": strategies,
        "underlyings": [{**{k: v for k, v in i.items() if k != "_extra"}, **i["_extra"]} for i in underlyings],
        "probe_errors": probe_errors,
    })

    return SignalDiagnosticsResponse(
        as_of=now_ist.isoformat(),
        session_window_open=session_window_open(now_ist, start, end),
        session_window=format_session_window(start, end),
        session_window_note=SESSION_WINDOW_NOTE,
        runner_running=runner_running, mode=mode, study_phase=study_phase,
        feed=feed, signals=signals, positions=positions,
        underlyings=[{k: v for k, v in i.items() if k != "_extra"} for i in underlyings],
        vix=vix, capital=capital, strategies=strategies, blockers=blockers,
    )


# ── Positions ─────────────────────────────────────────────────────────────────

@router.get("/positions")
async def get_positions(
    is_open:    Optional[bool] = None,
    strategy:   Optional[str] = None,
    underlying: Optional[str] = None,
    limit:      int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    q = select(PaperPosition).order_by(desc(PaperPosition.entry_time))
    if is_open is not None:
        q = q.where(PaperPosition.is_open == is_open)
    if strategy:
        q = q.where(PaperPosition.strategy_name == strategy)
    if underlying:
        q = q.where(PaperPosition.underlying == underlying.upper())
    q = q.limit(limit)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [
        {
            "id":               r.id,
            "strategy":         r.strategy_name,
            "token":            r.token,
            "symbol":           r.symbol,
            "underlying":       r.underlying,
            "strike":           r.strike,
            "option_type":      r.option_type,
            "expiry":           r.expiry.isoformat() if r.expiry else None,
            "direction":        r.direction,
            "lots":             r.lots,
            "lot_size":         r.lot_size,
            "qty":              r.quantity,
            "entry":            r.actual_entry,
            "sl":               r.stop_loss,
            "initial_sl":       r.initial_stop_loss,
            "target":           r.target,
            "trailing_sl":      r.trailing_sl,
            "exit":             r.actual_exit,
            "exit_reason":      r.exit_reason,
            "capital_used":     r.trade_amount_used,
            "gross_pnl":    r.gross_pnl,
            "charges":      r.statutory_charges,
            "net_pnl":      r.net_pnl,
            "is_open":      r.is_open,
            "mode":         r.mode,
            "partial_closed":     r.partial_closed,
            "partial_exit_price": r.partial_exit_price,
            "partial_exit_qty":   r.partial_exit_qty,
            "partial_pnl":        r.partial_pnl,
            "remaining_qty":      r.remaining_quantity,
            "entry_time":   r.entry_time.isoformat() if r.entry_time else None,
            "exit_time":    r.exit_time.isoformat() if r.exit_time else None,
        }
        for r in rows
    ]

@router.post("/positions/{pos_id}/close")
async def close_position(pos_id: int):
    ok = await paper_engine.manual_close(pos_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    return {"status": "closed", "id": pos_id}


# ── Kill switch (paper only; unauthenticated — see module docstring) ──────────

class KillSwitchRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/kill-switch")
async def kill_switch(body: Optional[KillSwitchRequest] = None):
    """
    Halt all new entries (persisted, survives restart) and square off every
    open PAPER position at the best available price. Fails safe.
    No authentication yet.
    """
    reason = (body.reason if body and body.reason else None) or "manual kill switch"
    persisted = await mode_manager.set_trading_halted(True, reason)
    result = await paper_engine.square_off_all("KILL_SWITCH")
    return {"halted": True, "reason": reason, "persisted": persisted, **result}


@router.get("/kill-switch")
async def kill_switch_status():
    """Current kill switch state. No authentication yet."""
    return await mode_manager.get_halt_status()


@router.post("/kill-switch/release")
async def kill_switch_release():
    """Lift the halt so paper entries can resume. No authentication yet."""
    persisted = await mode_manager.set_trading_halted(False, None)
    if not persisted:
        raise HTTPException(status_code=500, detail="Failed to persist kill switch release")
    return {"halted": False}


@router.post("/clear-test-trades")
async def clear_test_trades(db: AsyncSession = Depends(get_db)):
    """Clear all test/paper trades, positions, signals, and reset daily stats."""
    from sqlalchemy import delete, update
    from app.db.models.order import Order
    from app.db.models.trade import Trade
    from app.services.cache.redis_client import redis_cache

    r_pos = await db.execute(delete(PaperPosition))
    r_sig = await db.execute(delete(StrategySignal))
    r_pnl = await db.execute(delete(DailyPnLSummary))
    r_orders = await db.execute(delete(Order).where(Order.is_paper == True))
    r_trades = await db.execute(delete(Trade).where(Trade.is_paper == True))

    await db.execute(
        update(TradingConfig)
        .where(TradingConfig.id == 1)
        .values(
            daily_trades_count=0,
            used_capital=0.0,
        )
    )
    await db.commit()

    # Clear redis keys
    try:
        r = redis_cache._r()   # uses the guarded accessor — raises if not connected
        pos_keys = await r.keys("pos:*")
        if pos_keys:
            await r.delete(*pos_keys)
        await redis_cache.delete("positions")
        await redis_cache.delete("order_book")
    except Exception as e:
        logger.warning(f"Error clearing redis keys: {e}")

    return {
        "status": "cleared",
        "positions_deleted": r_pos.rowcount,
        "signals_deleted": r_sig.rowcount,
        "pnl_summaries_deleted": r_pnl.rowcount,
        "orders_deleted": r_orders.rowcount,
        "trades_deleted": r_trades.rowcount,
    }



# ── P&L ───────────────────────────────────────────────────────────────────────

@router.get("/pnl")
async def get_pnl_today(db: AsyncSession = Depends(get_db)):
    today = date.today()
    result = await db.execute(
        select(DailyPnLSummary).where(DailyPnLSummary.date == today)
        .order_by(desc(DailyPnLSummary.net_pnl))
    )
    rows = result.scalars().all()
    total_pnl = sum(r.net_pnl for r in rows)
    return {
        "date":       str(today),
        "total_pnl":  round(total_pnl, 2),
        "strategies": [
            {
                "strategy":      r.strategy_name,
                "mode":          r.mode,
                "trades":        r.total_trades,
                "wins":          r.winning_trades,
                "losses":        r.losing_trades,
                "win_rate":      r.win_rate,
                "gross_pnl":     r.gross_pnl,
                "net_pnl":       r.net_pnl,
                "brokerage":     r.brokerage,
                "max_drawdown":  r.max_drawdown,
                "avg_rr":        r.avg_rr,
            }
            for r in rows
        ],
    }

@router.get("/pnl/history")
async def get_pnl_history(
    days: int = Query(22, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(DailyPnLSummary)
        .where(DailyPnLSummary.date >= since)
        .order_by(DailyPnLSummary.date)
    )
    rows = result.scalars().all()

    # Aggregate by date
    by_date: dict[str, dict] = {}
    for r in rows:
        d = str(r.date)
        if d not in by_date:
            by_date[d] = {"date": d, "net_pnl": 0, "trades": 0, "wins": 0}
        by_date[d]["net_pnl"]  = round(by_date[d]["net_pnl"] + r.net_pnl, 2)
        by_date[d]["trades"]  += r.total_trades
        by_date[d]["wins"]    += r.winning_trades

    # Cumulative P&L
    cum = 0.0
    result_list = []
    for d in sorted(by_date.keys()):
        cum += by_date[d]["net_pnl"]
        by_date[d]["cumulative_pnl"] = round(cum, 2)
        result_list.append(by_date[d])
    return result_list


# ── Option Selector ───────────────────────────────────────────────────────────

@router.post("/option-selector")
async def select_option(body: OptionSelectorRequest, db: AsyncSession = Depends(get_db)):
    try:
        direction = Direction(body.direction.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="direction must be BUY or SELL")

    expiry = date.fromisoformat(body.expiry) if body.expiry else None

    top3 = await option_selector.select_top_n(
        db=db, direction=direction,
        underlying=body.underlying.upper(),
        spot_price=body.spot_price, n=3,
    )
    if not top3:
        raise HTTPException(status_code=404, detail="No suitable options found")

    return [
        {
            "rank":        i + 1,
            "token":       o.token,
            "symbol":      o.symbol,
            "strike":      o.strike,
            "option_type": o.option_type,
            "expiry":      str(o.expiry),
            "ltp":         o.ltp,
            "bid":         o.bid,
            "ask":         o.ask,
            "spread_pct":  o.spread_pct,
            "oi":          o.oi,
            "volume":      o.volume,
            "delta":       o.delta_est,
            "iv":          o.iv,
            "score":       o.score,
            "lot_size":    o.lot_size,
        }
        for i, o in enumerate(top3)
    ]


# ── Market Study ──────────────────────────────────────────────────────────────

@router.get("/market-study/intraday")
async def get_intraday_features(
    underlying: str = "NIFTY",
    date_str:   Optional[str] = Query(None, alias="date"),
    limit:      int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    target_date = date.fromisoformat(date_str) if date_str else date.today()
    # Use IST-aware bounds so the query matches snapshot_time (timezone-aware UTC column)
    IST = timezone(timedelta(hours=5, minutes=30))
    day_start = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=IST)
    day_end   = datetime.combine(target_date + timedelta(days=1), datetime.min.time()).replace(tzinfo=IST)
    result = await db.execute(
        select(IntradayMarketFeature)
        .where(
            IntradayMarketFeature.underlying == underlying.upper(),
            IntradayMarketFeature.snapshot_time >= day_start,
            IntradayMarketFeature.snapshot_time < day_end,
        )
        .order_by(IntradayMarketFeature.snapshot_time)
        .limit(limit)
    )
    rows = result.scalars().all()
    return [
        {
            "time":            r.snapshot_time.isoformat(),
            "spot":            r.spot_ltp,
            "vwap":            r.vwap,
            "spot_vs_vwap":    r.spot_vs_vwap_pct,
            "atm":             r.atm_strike,
            "ce_ltp":          r.atm_ce_ltp,
            "pe_ltp":          r.atm_pe_ltp,
            "ce_iv":           r.atm_ce_iv,
            "pe_iv":           r.atm_pe_iv,
            "straddle":        r.atm_straddle_val,
            "pcr":             r.pcr_oi,
            "ce_oi_chg":       r.ce_oi_change,
            "pe_oi_chg":       r.pe_oi_change,
            "ema9":            r.ema9,
            "ema21":           r.ema21,
            "rsi":             r.rsi14,
            "supertrend":      r.supertrend,
            "st_dir":          r.supertrend_dir,
            "bb_upper":        r.bb_upper,
            "bb_lower":        r.bb_lower,
            "orb_high":        r.orb_high,
            "orb_low":         r.orb_low,
            "orb_broken":      r.orb_broken,
            "vix":             r.vix_snapshot,
            "5m_ret":          r.spot_5m_return,
            "15m_ret":         r.spot_15m_return,
        }
        for r in rows
    ]

@router.get("/market-study/daily")
async def get_daily_snapshots(
    underlying: str = "NIFTY",
    days:       int = Query(30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(MarketDailySnapshot)
        .where(
            MarketDailySnapshot.underlying == underlying.upper(),
            MarketDailySnapshot.date >= since,
        )
        .order_by(MarketDailySnapshot.date)
    )
    rows = result.scalars().all()
    return [
        {
            "date":        str(r.date),
            "underlying":  r.underlying,
            "open":        r.open,
            "high":        r.high,
            "low":         r.low,
            "close":       r.close,
            "change_pct":  r.change_pct,
            "vix":         r.india_vix,
            "atm_iv":      r.atm_iv,
            "iv_pct":      r.iv_percentile,
            "pcr_oi":      r.pcr_oi,
            "max_pain":    r.max_pain,
            "max_ce_oi":   r.max_oi_ce_strike,
            "max_pe_oi":   r.max_oi_pe_strike,
            "trend":       r.trend,
            "orb_high":    r.orb_high,
            "orb_low":     r.orb_low,
            "vwap_close":  r.vwap_close,
            "pdh":         r.pdh,
            "pdl":         r.pdl,
            "dte":         r.days_to_expiry,
        }
        for r in rows
    ]

@router.get("/market-study/vix")
async def get_vix_history(days: int = Query(30), db: AsyncSession = Depends(get_db)):
    since = date.today() - timedelta(days=days)
    result = await db.execute(
        select(VIXData).where(VIXData.date >= since).order_by(VIXData.date)
    )
    rows = result.scalars().all()
    return [{"date": str(r.date), "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "change_pct": r.change_pct}
            for r in rows]


# ── WebSocket — live signal stream ────────────────────────────────────────────

@router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket):
    """Streams new signals and position events to the frontend in real-time."""
    await websocket.accept()
    import redis.asyncio as aioredis

    pubsub_client = aioredis.from_url(
        settings.REDIS_URL, encoding="utf-8", decode_responses=True
    )
    pubsub = pubsub_client.pubsub()
    await pubsub.subscribe("signals:paper_trade", "signals:live_trade")

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    await websocket.send_text(message["data"])
                except WebSocketDisconnect:
                    break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await pubsub.unsubscribe("signals:paper_trade", "signals:live_trade")
        await pubsub_client.aclose()
