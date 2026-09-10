"""
diagnose_no_signals.py
======================
When no signals are firing, run this to get a definitive answer:

  SYSTEM PROBLEM  — something in the pipeline is broken
  MARKET CONDITION — entry conditions not met (expected behaviour)

Checks in order of the signal pipeline:
  1.  Infrastructure   — Redis up, DB reachable
  2.  Feed             — Live LTP ticks in Redis, tick freshness
  3.  Candle data      — How many today-candles does StrategyRunner see?
  4.  Indicators       — compute_all() output sanity check
  5.  Gates            — ATR, VIX, market-hours, expiry-day
  6.  Per-strategy dry-run — evaluate() each strategy with real candles
  7.  Signal aggregator — active cooldowns blocking any underlying?
  8.  Capital / config  — study_phase, daily loss, capital exhausted
  9.  Option availability — OptionSelector probe with current market data
  10. Verdict           — SYSTEM / MARKET / BOTH

Usage:
    cd backend
    python scripts/diagnose_no_signals.py
    python scripts/diagnose_no_signals.py --underlying BANKNIFTY
"""
import asyncio
import argparse
import sys
import os
from datetime import datetime, date, time as dtime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.cache.redis_client import redis_cache
from app.core.config import settings
from app.strategies.indicators import compute_all
from app.strategies.base import signal_aggregator, SignalBus, SignalEvent, Direction
from app.strategies.strategies import (
    build_strategies, OISpikeStrategy, IVCrushStrategy,
    PDHPDLStrategy, CPRVWAPStrategy, GapVWAPFillStrategy,
    _is_between, _market_time, _atr_ok,
)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Output helpers ────────────────────────────────────────────────────────────
OK   = "✅"
FAIL = "❌"
WARN = "⚠️ "
INFO = "ℹ️ "

def _ok(msg):   print(f"  {OK}   {msg}")
def _fail(msg): print(f"  {FAIL}  {msg}")
def _warn(msg): print(f"  {WARN}  {msg}")
def _info(msg): print(f"  {INFO}  {msg}")

def sep(title=""):
    w = 70
    if title:
        pad = (w - len(title) - 2) // 2
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * w)

def ist_now() -> datetime:
    return datetime.now(IST)

def market_open() -> bool:
    t = ist_now().time()
    return dtime(9, 15) <= t <= dtime(15, 30)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Infrastructure
# ══════════════════════════════════════════════════════════════════════════════
async def check_infrastructure() -> dict:
    sep("1. Infrastructure")
    result = {"redis": False, "db": False}

    try:
        await redis_cache.connect()
        if await redis_cache.health():
            _ok("Redis connected and healthy")
            result["redis"] = True
        else:
            _fail("Redis ping failed")
    except Exception as e:
        _fail(f"Redis connection error: {e}")

    try:
        from app.db.base import AsyncSessionLocal
        from sqlalchemy import text
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        _ok("PostgreSQL reachable")
        result["db"] = True
    except Exception as e:
        _fail(f"PostgreSQL error: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 2. Feed / Live Prices
# ══════════════════════════════════════════════════════════════════════════════
async def check_feed(underlying: str) -> dict:
    sep("2. WebSocket Feed & Live Prices")
    spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
    result = {"spot_ltp": None, "vix": None, "tick_fresh": False}

    # Spot LTP
    spot_ltp = await redis_cache.get_ltp(spot_token)
    if spot_ltp:
        _ok(f"{underlying} spot LTP = ₹{spot_ltp:,.2f}  (token {spot_token})")
        result["spot_ltp"] = spot_ltp
    else:
        if market_open():
            _fail(f"No LTP for {underlying} token {spot_token} — feed disconnected during market hours")
        else:
            _warn(f"No LTP for {underlying} — market is closed, expected")

    # VIX
    vix = await redis_cache.get_ltp(settings.VIX_TOKEN)
    if vix:
        _ok(f"India VIX = {vix:.2f}")
        result["vix"] = vix
    else:
        _warn("No VIX in Redis — VIX gate will be bypassed (no block, just missing data)")

    # Tick freshness
    tick = await redis_cache.get_tick(spot_token)
    if tick:
        ts_str = tick.get("ts", "")
        try:
            tick_ts = datetime.fromisoformat(ts_str)
            if tick_ts.tzinfo is None:
                tick_ts = tick_ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - tick_ts).total_seconds()
            if age < 300:
                _ok(f"Spot tick is {age:.0f}s old — feed is live")
                result["tick_fresh"] = True
            else:
                _warn(f"Spot tick is {age:.0f}s old — feed may be stale or reconnecting")
        except Exception:
            _warn("Could not parse tick timestamp")
    else:
        _warn("No full tick in Redis for spot (bid/ask/OI not available)")

    # Option chain cache (IV source)
    from app.db.base import AsyncSessionLocal
    from app.services.broker.instrument_sync import get_active_expiries
    try:
        async with AsyncSessionLocal() as db:
            expiries = await get_active_expiries(db, underlying)
        if expiries:
            chain = await redis_cache.get_option_chain(underlying, str(expiries[0]))
            if chain:
                n_strikes = len(chain.get("strikes", []))
                fetched   = chain.get("fetched_at", "?")
                _ok(f"Option chain cached: {n_strikes} strikes, fetched_at={fetched[:19]}")
            else:
                _warn("Option chain NOT in Redis — IV data unavailable")
                _info("IVCrushStrategy and OI scoring will use fallback values")
        else:
            _warn("No active expiry found — cannot check option chain cache")
    except Exception as e:
        _warn(f"Option chain cache check error: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 3. Candle Data
# ══════════════════════════════════════════════════════════════════════════════
async def check_candles(underlying: str) -> dict:
    sep("3. Candle Data (what StrategyRunner actually sees)")
    spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
    result = {"total": 0, "today": 0, "sufficient": False, "candles": []}

    all_candles = await redis_cache.get_candles(spot_token, "ONE_MINUTE", 500)
    today_str   = str(ist_now().date())

    today_candles = []
    for c in all_candles:
        ts = str(c.get("timestamp", ""))
        if not ts.startswith(today_str):
            continue
        try:
            time_part = ts[11:16]
            h, m = int(time_part[:2]), int(time_part[3:5])
            if h * 60 + m < 9 * 60 + 15:
                continue
        except Exception:
            pass
        today_candles.append(c)

    result["total"]   = len(all_candles)
    result["today"]   = len(today_candles)
    result["candles"] = today_candles

    _info(f"Total candles in Redis     : {len(all_candles)}")
    _info(f"Today-only candles (≥09:15): {len(today_candles)}")

    if len(today_candles) == 0:
        if market_open():
            _fail("ZERO today-candles during market hours — TickWriter or feed broken")
        else:
            _warn("Zero today-candles — market is closed, normal")
        result["sufficient"] = False
    elif len(today_candles) < 25:
        _warn(f"Only {len(today_candles)} candles — strategies need ≥25 (first 25 min after open)")
        result["sufficient"] = False
    else:
        _ok(f"{len(today_candles)} today-candles — sufficient for all strategies")
        result["sufficient"] = True
        first = today_candles[0].get("timestamp", "?")
        last  = today_candles[-1].get("timestamp", "?")
        close = float(today_candles[-1].get("close", 0))
        _info(f"First: {first}   Last: {last}   close=₹{close:,.2f}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 4. Indicators
# ══════════════════════════════════════════════════════════════════════════════
async def check_indicators(candles: list) -> dict:
    sep("4. Indicator Computation")
    result = {"ok": False, "indicators": {}}

    if len(candles) < 2:
        _warn("Not enough candles to compute indicators — skipping")
        return result

    try:
        ind = compute_all(candles)
    except Exception as e:
        _fail(f"compute_all() threw: {e}")
        return {"ok": False, "error": str(e), "indicators": {}}

    critical = ["ema9", "ema21", "atr", "vwap", "rsi"]
    optional  = ["ema50", "bb_upper", "bb_lower", "bb_width",
                 "supertrend", "supertrend_dir", "orb_high", "orb_low"]

    # Indicators that are only critical once we have enough candles to warm up.
    # With fewer than 25 candles these will be None due to warm-up, not a bug.
    warmup_required = {
        "ema21": 21,
        "ema9":  9,
        "rsi":   15,
        "atr":   15,
    }

    all_ok = True
    for key in critical:
        val = ind.get(key)
        min_candles_needed = warmup_required.get(key, 2)
        if val is not None:
            _ok(f"{key:20s} = {val:.4f}" if isinstance(val, float) else f"{key:20s} = {val}")
        elif len(candles) < min_candles_needed:
            # Not enough candles yet — warm-up in progress, not a system error
            _warn(f"{key:20s} = None  (warm-up: need {min_candles_needed} candles, have {len(candles)})")
        else:
            _fail(f"{key:20s} = None  ← critical with {len(candles)} candles — unexpected")
            all_ok = False

    for key in optional:
        val = ind.get(key)
        fmtd = f"{val:.4f}" if isinstance(val, float) else str(val)
        if val is not None:
            _info(f"{key:20s} = {fmtd}")
        else:
            _warn(f"{key:20s} = None  (warm-up still in progress)")

    result["ok"]         = all_ok
    result["indicators"] = ind
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 5. Strategy Gates
# ══════════════════════════════════════════════════════════════════════════════
async def check_gates(underlying: str, indicators: dict) -> dict:
    sep("5. Strategy Gate Checks")
    blocked = []
    passed  = []

    # Market hours
    now_ist  = ist_now()
    ist_mins = now_ist.hour * 60 + now_ist.minute
    in_hours = 9 * 60 + 15 <= ist_mins <= 15 * 60 + 25
    if in_hours:
        _ok(f"Market hours  : {now_ist.strftime('%H:%M')} IST — inside window")
        passed.append("market_hours")
    else:
        _warn(f"Market hours  : {now_ist.strftime('%H:%M')} IST — OUTSIDE 09:15-15:25")
        _info("StrategyRunner candle loop only runs during market hours")
        blocked.append("market_hours")

    # ATR — runner gate
    atr_val      = indicators.get("atr") or 0.0
    atr_min_run  = 1.5  if underlying == "NIFTY" else 5.0
    atr_min_strat= 3.0  if underlying == "NIFTY" else 10.0
    if atr_val < atr_min_run:
        _fail(f"ATR = {atr_val:.2f} < runner minimum {atr_min_run} — ALL strategies blocked by StrategyRunner")
        blocked.append("atr_runner")
    elif atr_val < atr_min_strat:
        _warn(f"ATR = {atr_val:.2f} — passes runner gate ({atr_min_run}) but below strategy gate ({atr_min_strat})")
        _info("Individual strategies with _atr_ok() check will skip. Market is quiet.")
        blocked.append("atr_strategy")
    else:
        _ok(f"ATR = {atr_val:.2f} — passes both runner ({atr_min_run}) and strategy ({atr_min_strat}) gates")
        passed.append("atr")

    # VIX gate
    vix = await redis_cache.get_ltp(settings.VIX_TOKEN)
    if vix is None:
        _warn("VIX not in Redis — gate bypassed (won't block strategies)")
        passed.append("vix_bypass")
    elif vix > 25.0:
        _fail(f"VIX = {vix:.1f} > 25.0 — ALL strategies blocked (extreme fear / erratic moves)")
        blocked.append("vix_high")
    elif vix < 10.0:
        _fail(f"VIX = {vix:.1f} < 10.0 — ALL strategies blocked (no movement, options worthless)")
        blocked.append("vix_low")
    else:
        _ok(f"VIX = {vix:.1f} — in tradeable range 10-25")
        passed.append("vix")

    # Expiry day
    try:
        from app.db.base import AsyncSessionLocal
        from app.services.broker.instrument_sync import get_active_expiries
        async with AsyncSessionLocal() as db:
            expiries = await get_active_expiries(db, underlying)
        is_expiry = bool(expiries and expiries[0] == ist_now().date())
        if is_expiry:
            _warn("Expiry day — strategies with sl_pct < 0.15 are individually skipped")
            blocked.append("expiry_partial")
        else:
            dte = (expiries[0] - ist_now().date()).days if expiries else "?"
            _ok(f"Not expiry day — DTE = {dte}")
            passed.append("expiry")
    except Exception as e:
        _warn(f"Expiry check error: {e}")

    # Pre-market data availability (PDH/PDL/CPR/prev_close)
    _check_premarket_data(underlying, passed, blocked)

    return {"blocked": blocked, "passed": passed}


def _check_premarket_data(underlying: str, passed: list, blocked: list):
    """
    Check whether yesterday's OHLC/snapshot data exists so PDH/PDL, CPR,
    and GapFill strategies will work.  Uses a synchronous Redis check since
    this is called from within async check_gates().
    """
    # We can't easily do a sync DB query here; just advise based on common state
    # The live runner uses _seed_prev_from_ohlc as fallback — note this in output
    _info("PDH/PDL/CPR/GapFill: set by live runner at 09:00 pre-market setup")
    _info("  If yesterday had OHLC data these ARE set in the live process")
    _info("  (dry-run instances in this script don't call pre-market setup)")


# ══════════════════════════════════════════════════════════════════════════════
# 6. Per-strategy dry-run
# ══════════════════════════════════════════════════════════════════════════════
async def check_strategies(underlying: str, candles: list, indicators: dict) -> dict:
    sep("6. Per-Strategy Dry-Run (real candles, cooldowns cleared)")
    result = {"fired": [], "skipped": [], "errors": []}

    if len(candles) < 5:
        _warn("Too few candles for strategy dry-run — skipping")
        return result

    strategies  = build_strategies(underlying)
    fired_sigs  = []

    async def _capture(sig: SignalEvent) -> bool:
        fired_sigs.append(sig)
        return True

    orig_handlers = list(SignalBus._handlers)
    SignalBus._handlers = [_capture]

    for strat in strategies:
        strat._last_signal_time = None   # remove strategy-level cooldown for diagnostic
        fired_sigs.clear()

        try:
            sig = await strat.evaluate(candles, indicators)
        except Exception as e:
            _fail(f"{strat.name:25s}  ERROR: {e}")
            result["errors"].append(strat.name)
            continue

        if sig is not None:
            _ok(f"{strat.name:25s}  🔥 SIGNAL: {sig.direction.value}  spot={sig.spot_price:.0f}  [{sig.notes[:55]}]")
            result["fired"].append(strat.name)
        else:
            reason = _explain(strat, candles, indicators)
            _info(f"{strat.name:25s}  no signal  [{reason}]")
            result["skipped"].append((strat.name, reason))

    SignalBus._handlers = orig_handlers
    return result


def _explain(strat, candles, indicators) -> str:
    """One-line human-readable reason why a strategy returned None."""
    # Time window
    windows = {
        "ORB_VWAP":          (dtime(9,30),  dtime(11,30)),
        "VWAP_PULLBACK":      (dtime(9,45),  dtime(14, 0)),
        "EMA_VWAP":           (dtime(9,30),  dtime(14,30)),
        "SUPERTREND_VWAP":    (dtime(9,30),  dtime(14, 0)),
        "PDH_PDL":            (dtime(9,30),  dtime(13, 0)),
        "VWAP_REVERSAL":      (dtime(10, 0), dtime(14,30)),
        "BOLLINGER_BREAKOUT": (dtime(9,45),  dtime(14, 0)),
        "RSI_VWAP":           (dtime(10, 0), dtime(14,30)),
        "OI_SPIKE":           (dtime(9,30),  dtime(12,30)),
        "IV_CRUSH":           (dtime(9,30),  dtime(13, 0)),
        "CPR_VWAP":           (dtime(9,30),  dtime(13,30)),
        "GAP_VWAP_FILL":      (dtime(9,15),  dtime(10,45)),
    }
    t = _market_time(candles)
    if strat.name in windows:
        s, e = windows[strat.name]
        if not _is_between(t, s, e):
            return f"outside window {s.strftime('%H:%M')}–{e.strftime('%H:%M')} (now {t.strftime('%H:%M')})"

    # ATR
    if not _atr_ok(indicators, strat.underlying):
        atr = indicators.get("atr") or 0
        return f"ATR={atr:.2f} below strategy minimum"

    # Strategy-specific missing state
    if isinstance(strat, OISpikeStrategy):
        if strat._update_count < 2:
            return "OI not populated yet — need ≥2 option-chain polls (wait ~10 min after startup)"
        if strat.is_expiry_day:
            return "blocked on expiry day"
    if isinstance(strat, IVCrushStrategy):
        if not strat.atm_iv or not strat.prev_atm_iv:
            return "IV not populated — wait for study_collector option-chain fetch (~5 min after startup)"
        if strat.is_expiry_day:
            return "blocked on expiry day"
    if isinstance(strat, PDHPDLStrategy) and not strat.pdh:
        return "PDH/PDL not set in dry-run instance — live runner sets these at 09:00 via pre-market setup"
    if isinstance(strat, CPRVWAPStrategy) and not strat.pivot:
        return "CPR not set in dry-run instance — live runner sets these at 09:00 via pre-market setup"
    if isinstance(strat, GapVWAPFillStrategy) and not strat.prev_close:
        return "prev_close not set in dry-run instance — live runner sets this at 09:00 via pre-market setup"

    # Generic: show current market state for context
    close  = indicators.get("close") or 0
    vwap_v = indicators.get("vwap")  or 0
    rsi_v  = indicators.get("rsi")   or 0
    atr_v  = indicators.get("atr")   or 0
    pct    = (close - vwap_v) / vwap_v * 100 if vwap_v else 0
    return (f"conditions not met — "
            f"close={close:.0f} vwap={vwap_v:.0f} ({pct:+.2f}%) "
            f"rsi={rsi_v:.1f} atr={atr_v:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# 7. Signal aggregator cooldowns
# ══════════════════════════════════════════════════════════════════════════════
async def check_aggregator() -> dict:
    sep("7. Signal Aggregator Cooldown State")
    last    = signal_aggregator._last_acted
    blocked = []

    if not last:
        _ok("No active cooldowns — aggregator clear")
        return {"blocked": []}

    now = datetime.now(timezone.utc)
    for (und, direction), ts in last.items():
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        elapsed   = (now - ts).total_seconds()
        remaining = max(0, signal_aggregator.SAME_DIR_COOLDOWN_SECS - elapsed)
        if remaining > 0:
            _warn(f"{und} {direction.value:4s} — cooldown {remaining:.0f}s remaining")
            blocked.append((und, direction, remaining))
        else:
            _ok(f"{und} {direction.value:4s} — expired ({elapsed:.0f}s ago)")

    if not blocked:
        _ok("All cooldowns expired — aggregator clear")

    return {"blocked": blocked}


# ══════════════════════════════════════════════════════════════════════════════
# 8. Capital & Config
# ══════════════════════════════════════════════════════════════════════════════
async def check_config() -> dict:
    sep("8. Trading Config & Capital")
    result = {"blockers": []}

    try:
        from app.services.trading.mode_manager import mode_manager
        cfg = await mode_manager.get_config()
    except Exception as e:
        _fail(f"Could not load TradingConfig: {e}")
        result["blockers"].append("no_config")
        return result

    if not cfg:
        _fail("TradingConfig row missing — app startup did not complete")
        result["blockers"].append("no_config")
        return result

    available = cfg.capital - (cfg.used_capital or 0.0)

    _info(f"Mode            : {cfg.mode}")
    _info(f"Capital         : ₹{cfg.capital:,.0f}")
    _info(f"Used capital    : ₹{cfg.used_capital or 0:,.0f}")
    _info(f"Available       : ₹{available:,.0f}")
    _info(f"Trade amount    : ₹{cfg.trade_amount:,.0f}")
    _info(f"Max open pos    : {cfg.max_open_positions}")
    _info(f"Trades today    : {cfg.daily_trades_count} / {cfg.max_trades_per_day}")
    _info(f"Study phase     : {cfg.study_phase}")

    if cfg.study_phase:
        _warn("study_phase = True — signals RECORDED but NO positions opened")
        _info("→ PUT /api/v1/trading/config  {\"study_phase\": false}  to start trading")
        result["blockers"].append("study_phase")
    else:
        _ok("study_phase = False — PaperEngine will open positions")

    if available < cfg.trade_amount:
        _fail(f"Available ₹{available:,.0f} < trade_amount ₹{cfg.trade_amount:,.0f} — capital exhausted")
        result["blockers"].append("no_capital")
    else:
        _ok(f"Capital available: ₹{available:,.0f}")

    if cfg.max_trades_per_day and cfg.daily_trades_count >= cfg.max_trades_per_day:
        _fail(f"Daily trade limit reached: {cfg.daily_trades_count}/{cfg.max_trades_per_day}")
        result["blockers"].append("daily_limit")
    else:
        _ok(f"Daily trade count: {cfg.daily_trades_count}/{cfg.max_trades_per_day}")

    # Daily loss circuit breaker
    try:
        stats     = await mode_manager.get_daily_stats()
        daily_pnl = stats.get("net_pnl", 0) or 0
        max_loss  = cfg.max_daily_loss or 0
        if max_loss and daily_pnl <= -abs(max_loss):
            _fail(f"Circuit breaker tripped: P&L=₹{daily_pnl:,.0f}  limit=₹{-abs(max_loss):,.0f}")
            result["blockers"].append("circuit_breaker")
        elif daily_pnl < 0:
            _warn(f"Daily P&L: ₹{daily_pnl:,.0f}  (negative but within limit)")
        else:
            _ok(f"Daily P&L: ₹{daily_pnl:,.0f}")
    except Exception:
        pass

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 9. Option availability
# ══════════════════════════════════════════════════════════════════════════════
async def check_options(underlying: str, spot_ltp: Optional[float]) -> dict:
    sep("9. Option Availability (OptionSelector live probe)")
    result = {"available": False}

    if not spot_ltp:
        spot_ltp = 24500.0 if underlying == "NIFTY" else 52000.0
        _warn(f"No live spot — using ₹{spot_ltp:,.0f} as proxy")

    try:
        from app.db.base import AsyncSessionLocal
        from app.services.trading.option_selector import option_selector

        # Probe BUY side
        async with AsyncSessionLocal() as db:
            top3 = await option_selector.select_top_n(
                db=db, direction=Direction.BUY,
                underlying=underlying, spot_price=spot_ltp, n=3,
            )

        if top3:
            _ok(f"CE candidates found ({len(top3)}) — option selector is working:")
            for i, c in enumerate(top3):
                _info(f"  #{i+1}  {underlying} {c.strike:.0f}{c.option_type}  "
                      f"ltp=₹{c.ltp:.2f}  score={c.score:.1f}/100  "
                      f"oi={c.oi:,}  spread={c.spread_pct:.1f}%  δ={c.delta_est:.3f}")
            result["available"] = True
        else:
            _fail("OptionSelector returned 0 CE candidates")
            _info("Possible reasons:")
            _info("  • Market closed — no live bid/ask/OI in Redis ticks")
            _info("  • Instruments not in DB — run instrument sync")
            _info("  • All strikes fail hard filters (OI<50k, spread>3%, LTP out of range)")

        # Probe SELL side
        async with AsyncSessionLocal() as db:
            top_pe = await option_selector.select_top_n(
                db=db, direction=Direction.SELL,
                underlying=underlying, spot_price=spot_ltp, n=1,
            )
        if top_pe:
            _ok(f"PE candidates also available — SELL signals will work")
        else:
            _warn("No PE candidates — SELL signals would fail option selection")

    except Exception as e:
        _fail(f"OptionSelector probe error: {e}")
        result["error"] = str(e)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# 10. Verdict
# ══════════════════════════════════════════════════════════════════════════════
def print_verdict(infra, feed, candle, gates, strats, config, options):
    sep("VERDICT — Is this a SYSTEM problem or MARKET CONDITION?")

    system  = []   # things that are broken
    market  = []   # expected behaviour given current conditions
    tips    = []   # actionable suggestions

    # ── Infra ──────────────────────────────────────────────────────────────
    if not infra.get("redis"):
        system.append("Redis is DOWN — entire pipeline blocked")
    if not infra.get("db"):
        system.append("PostgreSQL is DOWN — DB operations failing")

    # ── Feed ───────────────────────────────────────────────────────────────
    if not feed.get("spot_ltp") and market_open():
        system.append("No spot LTP during market hours — WebSocket feed disconnected")
        tips.append("POST /api/v1/auth/refresh  then  POST /api/v1/market/feed/subscribe")

    # ── Candles ────────────────────────────────────────────────────────────
    if not candle.get("sufficient"):
        if market_open() and candle.get("total", 0) == 0:
            system.append("Zero candles in Redis — TickWriter not writing or feed not ticking")
        elif market_open() and candle.get("today", 0) < 25:
            market.append(f"Only {candle.get('today',0)} today-candles — first 25 min after open (normal)")
        elif not market_open():
            market.append("Market closed — no today-candles (expected)")

    # ── Strategy errors ────────────────────────────────────────────────────
    if strats.get("errors"):
        system.append(f"Strategy evaluate() exceptions: {', '.join(strats['errors'])}")

    # ── Config blockers ────────────────────────────────────────────────────
    for b in config.get("blockers", []):
        if b == "no_config":
            system.append("TradingConfig missing — app startup failed")
        elif b == "circuit_breaker":
            system.append("Daily loss circuit breaker tripped — trading halted for today")
            tips.append("POST /api/v1/trading/capital/reset-daily  (after reviewing losses)")
        elif b == "no_capital":
            system.append("Capital exhausted — no room for new positions")
            tips.append("Wait for open positions to close, or increase capital via PUT /trading/config")
        elif b == "daily_limit":
            system.append(f"Daily trade limit reached")
            tips.append("POST /api/v1/trading/capital/reset-daily  to reset counter")
        elif b == "study_phase":
            market.append("study_phase=True — signals logged but no positions opened (intentional)")
            tips.append("PUT /api/v1/trading/config  {\"study_phase\": false}  to enable live paper trading")

    # ── Gate blockers ───────────────────────────────────────────────────────
    for b in gates.get("blocked", []):
        if b == "market_hours":
            market.append("Outside market hours 09:15–15:25 IST — runner is sleeping")
        elif b == "atr_runner":
            market.append("ATR below runner minimum — market too dead/choppy for any strategy")
        elif b == "atr_strategy":
            market.append("ATR passes runner but below strategy gate — market is quiet")
        elif b == "vix_high":
            market.append("VIX > 25 — extreme fear environment, runner blocks all strategies")
        elif b == "vix_low":
            market.append("VIX < 10 — options too cheap / no directional movement expected")
        elif b == "expiry_partial":
            market.append("Expiry day — low-sl strategies skipped (high gamma risk)")

    # ── Strategies with missing pre-market data ─────────────────────────────
    # Only flag OI/IV as missing — PDH/CPR/GapFill dry-run instances never have
    # pre-market setup called so they always appear unset here. That's an
    # artefact of the diagnostic, not a live-runner issue.
    oi_iv_missing = [
        name for name, reason in strats.get("skipped", [])
        if any(kw in reason for kw in ["OI not populated", "IV not populated"])
    ]
    if oi_iv_missing:
        market.append(
            f"OI/IV not yet populated ({', '.join(oi_iv_missing)}) — "
            f"wait ~10 min for study_collector option-chain fetch"
        )

    # ── Options ────────────────────────────────────────────────────────────
    if not options.get("available") and not market_open():
        market.append("No live option candidates — market closed, expected")
    elif not options.get("available") and market_open():
        system.append("OptionSelector found 0 candidates during market hours — check instrument sync and feed ticks")

    # ── Aggregator ─────────────────────────────────────────────────────────
    agg_blocked = gates.get("agg_blocked", [])  # populated by caller if needed

    # ── Print ───────────────────────────────────────────────────────────────
    fired = strats.get("fired", [])

    if system:
        print(f"\n  {FAIL}  SYSTEM PROBLEMS ({len(system)}) — fix these first:")
        for s in system:
            print(f"       • {s}")

    if market:
        print(f"\n  {WARN}  MARKET / CONDITION REASONS ({len(market)}) — expected behaviour:")
        for m in market:
            print(f"       • {m}")

    if fired:
        print(f"\n  {OK}  Dry-run fired {len(fired)} signal(s) with today's candles:")
        for f in fired:
            print(f"       • {f}")
        if not system:
            print(f"\n       System is working. If live signals aren't appearing,")
            print(f"       check that study_phase=false and capital is available.")

    if tips:
        print(f"\n  💡  Suggested actions:")
        for t in tips:
            print(f"       → {t}")

    # ── Final line ──────────────────────────────────────────────────────────
    print()
    if system:
        print(f"  {FAIL}  CONCLUSION: SYSTEM ISSUE — pipeline needs fixing before signals can fire.")
    elif not market_open() and not fired:
        print(f"  {INFO}  CONCLUSION: MARKET CLOSED — come back at 09:15 IST.")
    elif market and not fired:
        print(f"  {INFO}  CONCLUSION: MARKET CONDITION — system healthy, waiting for entry setup.")
    elif fired and not system:
        print(f"  {OK}  CONCLUSION: SYSTEM HEALTHY — dry-run produced signals. Live signals should appear when conditions align.")
    else:
        print(f"  {INFO}  CONCLUSION: Mixed — review items above.")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════
async def main(underlying: str):
    print("\n" + "=" * 70)
    print(f"  NO-SIGNAL DIAGNOSTIC  |  {underlying}  |  {ist_now().strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 70)

    infra   = await check_infrastructure()
    feed    = await check_feed(underlying)
    candle  = await check_candles(underlying)

    today_c = candle.get("candles", [])

    # If no real candles available, build synthetic ones so gate/strategy checks still run
    using_mock = False
    if len(today_c) < 5:
        spot = feed.get("spot_ltp") or (24500.0 if underlying == "NIFTY" else 52000.0)
        from datetime import time as _dtime
        base_ts = ist_now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(minutes=60)
        today_c = []
        price = spot
        for i in range(60):
            ts = base_ts + timedelta(minutes=i)
            today_c.append({
                "timestamp": ts.isoformat(),
                "open": round(price, 2), "high": round(price + 5, 2),
                "low":  round(price - 5, 2), "close": round(price, 2),
                "volume": 80_000,
            })
            price += 0.3
        using_mock = True
        _warn("Using synthetic candles for checks 4-6 (no real data in Redis)")

    ind_res    = await check_indicators(today_c)
    indicators = ind_res.get("indicators", {})

    gates  = await check_gates(underlying, indicators)
    strats = await check_strategies(underlying, today_c, indicators)
    agg    = await check_aggregator()
    cfg    = await check_config()
    opts   = await check_options(underlying, feed.get("spot_ltp"))

    print_verdict(infra, feed, candle if not using_mock else {**candle, "sufficient": False},
                  gates, strats, cfg, opts)

    print("=" * 70)
    await redis_cache.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose why no signals are firing")
    parser.add_argument(
        "--underlying", choices=["NIFTY", "BANKNIFTY"], default="NIFTY",
        help="Underlying to diagnose (default: NIFTY)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.underlying.upper()))
