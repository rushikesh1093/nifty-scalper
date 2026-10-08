"""
Paper Trading Engine (PAPER ONLY — nothing here talks to the broker).
Simulates order fills, tracks open positions, monitors SL/target every second,
handles EOD square-off at the configured square_off_time (default 15:15 IST).

Lot sizing logic:
  budget       = Kelly/regime-adjusted trade amount (₹)
  cost_per_lot = fill_price × lot_size     (e.g. 40 × 65 = ₹2,600)
  lots = floor(budget / cost_per_lot)  — may be 0 → signal SKIPPED
         ("Lot unaffordable"); a lot is never forced above the budget.
  actual_capital_used = lots × cost_per_lot

Fills:
  entry at best ask, market-style exits at best bid, when the quote is valid
  (bid > 0, ask ≥ bid, spread ≤ 5% of mid, tick ≤ 60 s old); otherwise
  LTP × (1 + slippage_pct) for entry and price × (1 − 0.1%) for exit.

Capital tracking:
  used_capital  += actual_capital_used   on position OPEN
  used_capital  -= actual_capital_used   on position CLOSE
  available     = capital - used_capital
"""
import asyncio
import logging
import math
import time as _time
from datetime import datetime, date, time as dtime, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import (
    PaperPosition, StrategySignal, DailyPnLSummary,
    TradingMode, SignalDirection, SignalStatus,
)
from app.services.cache.redis_client import redis_cache
from app.services.trading.option_selector import SelectedOption, option_selector
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.services.trading.session_times import (
    DEFAULT_SQUARE_OFF, entry_cutoff, is_after_entry_cutoff,
    is_market_hours, parse_square_off,
)
from app.strategies.base import SignalEvent, Direction

logger = logging.getLogger(__name__)

BROKERAGE_PER_LOT = 20.0   # ₹20 per lot per leg (booked on entry and again on exit)

# ── Statutory charges: NSE index options (buyer) ──────────────────────────────
# Sources (checked 2026-10-06; verify periodically — rates change with the
# Union Budget and exchange circulars):
#   https://zerodha.com/charges/                       (F&O - Options column)
#   https://support.fyers.in/portal/en/kb/articles/what-are-the-statutory-charges-on-trades-at-fyers-stt-gst-sebi-turnover-fee-ipft-etc
# IPFT (₹0.01/crore) is ignored as negligible. These are approximations for
# paper P&L; the broker contract note is the source of truth for real trades.
STT_SELL_RATE  = 0.0015      # STT 0.15% of premium, sell side only
EXCH_TXN_RATE  = 0.0003553   # NSE exchange transaction charge 0.03553% of premium, both legs
SEBI_FEE_RATE  = 1e-6        # SEBI turnover fee ₹10 / crore, both legs
STAMP_BUY_RATE = 0.00003     # stamp duty 0.003% of premium, buy side only
GST_RATE       = 0.18        # GST 18% on (brokerage + exchange txn + SEBI fee)

# ── Fill / quote quality ──────────────────────────────────────────────────────
QUOTE_MAX_SPREAD_PCT   = 5.0     # bid/ask only trusted when spread ≤ 5% of mid
EXIT_FALLBACK_SLIPPAGE = 0.001   # 0.1% off the trigger price when no valid bid

# ── Price staleness ───────────────────────────────────────────────────────────
STALE_TICK_SECONDS       = 60    # tick older than this = stale price
LAST_PRICE_PERSIST_SECS  = 15    # throttle for writing last_ltp into pos:{id}
STALE_WARN_INTERVAL_SECS = 60    # rate-limit stale/missing-price warnings per position

# ── Trailing stop ladder ──────────────────────────────────────────────────────
# Expressed as progress towards target: progress = (ltp − entry) / R, with
# R = target − entry. Every tier is reachable before target, and both the
# trigger and the locked SL increase monotonically.
#   (progress trigger, SL lock as fraction of R above entry)
TRAIL_TIERS = ((0.90, 0.60), (0.75, 0.40), (0.50, 0.0))
PARTIAL_CLOSE_PROGRESS  = 0.75   # close 50% of lots (≥2-lot positions only)
DEFAULT_TARGET_FRACTION = 0.30   # R fallback (entry × 30%) when target is missing

# Strong references to detached observability tasks (SIGNAL_RECORDED publishes) so
# a pending task cannot be garbage-collected mid-await.
_bg_tasks: set[asyncio.Task] = set()


def _calc_lots(trade_amount: float, price: float, lot_size: int) -> tuple[int, float]:
    """
    Calculate lots and actual capital deployed.

    Returns:
        (lots, capital_used)
        lots         — floor(trade_amount / cost_per_lot); 0 when one lot
                       costs more than trade_amount (caller skips the signal)
        capital_used — lots × price × lot_size  (premium cost)
    """
    if not price or price <= 0 or not lot_size or lot_size <= 0:
        return 0, 0.0

    cost_per_lot = price * lot_size
    lots = math.floor(trade_amount / cost_per_lot)
    capital_used = round(lots * cost_per_lot, 2)
    return lots, capital_used


def _trailing_stop(
    entry: float, target: Optional[float], ltp: float, current_sl: Optional[float]
) -> Optional[float]:
    """Return a raised SL from TRAIL_TIERS, or None when SL should not move."""
    if not entry or entry <= 0:
        return None
    r = (target - entry) if (target and target > entry) else entry * DEFAULT_TARGET_FRACTION
    if r <= 0:
        return None
    progress = (ltp - entry) / r
    for trigger, lock in TRAIL_TIERS:
        if progress >= trigger:
            new_sl = round(entry + lock * r, 2)
            return new_sl if new_sl > (current_sl or 0) else None
    return None


def _statutory_charges(buy_value: float, sell_value: float, brokerage: float) -> float:
    """STT + exchange txn + SEBI + stamp + GST for one round trip (brokerage itself excluded)."""
    buy_value = max(buy_value or 0.0, 0.0)
    sell_value = max(sell_value or 0.0, 0.0)
    turnover = buy_value + sell_value
    stt   = sell_value * STT_SELL_RATE
    exch  = turnover * EXCH_TXN_RATE
    sebi  = turnover * SEBI_FEE_RATE
    stamp = buy_value * STAMP_BUY_RATE
    gst   = ((brokerage or 0.0) + exch + sebi) * GST_RATE
    return round(stt + exch + sebi + stamp + gst, 2)


def _tick_age_seconds(tick: Optional[dict], now: Optional[datetime] = None) -> Optional[float]:
    """Age of a Redis tick in seconds from its ``ts`` field (naive ISO = UTC). None if unknown."""
    if not tick:
        return None
    ts = tick.get("ts")
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - t).total_seconds())


def _quote_valid(bid, ask, age_s: Optional[float]) -> bool:
    """A quote is usable for fills only if fresh, uncrossed and reasonably tight."""
    try:
        bid = float(bid or 0)
        ask = float(ask or 0)
    except (TypeError, ValueError):
        return False
    if bid <= 0 or ask <= 0 or ask < bid:
        return False
    if age_s is None or age_s > STALE_TICK_SECONDS:
        return False
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 <= QUOTE_MAX_SPREAD_PCT


def _entry_fill(ltp: float, bid, ask, age_s: Optional[float], slippage_pct: float) -> float:
    """Buy at best ask when the quote is valid, else LTP + configured slippage."""
    if _quote_valid(bid, ask, age_s):
        return round(float(ask), 2)
    return round(ltp * (1 + (slippage_pct or 0) / 100), 2)


def _exit_fill(price: float, bid, ask, age_s: Optional[float], reason: str) -> float:
    """
    TARGET is a resting limit order: price × (1 − 0.1%).
    Every other reason (SL, EOD, TIMEOUT, MANUAL, KILL_SWITCH, *_STALE) is a
    market-style exit: best bid when valid, else price × (1 − 0.1%).
    """
    if reason != "TARGET" and _quote_valid(bid, ask, age_s):
        return round(float(bid), 2)
    return round(price * (1 - EXIT_FALLBACK_SLIPPAGE), 2)


class PaperEngine:
    """Full lifecycle: signal → lot sizing → fill → SL/target monitor → exit."""

    def __init__(self):
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False
        # pos_id → (last fresh LTP, UTC time seen) — EOD/kill-switch fallback price
        self._last_known: dict[int, tuple[float, datetime]] = {}
        self._last_persist: dict[int, float] = {}    # pos_id → monotonic ts of last Redis write
        self._stale_warned: dict[int, float] = {}    # pos_id → monotonic ts of last warning
        self._square_off_cache: Optional[tuple[dtime, float]] = None

    async def start(self):
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("PaperEngine started.")

    async def stop(self):
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()

    # ── Signal handler ────────────────────────────────────────────────────────

    async def handle_signal(self, signal: SignalEvent) -> bool:
        """
        Called by SignalBus for every fired strategy signal.
        Returns True only when a position is actually opened — SignalBus uses
        this to decide whether to consume the aggregator cooldown.
        """
        from app.services.trading.mode_manager import mode_manager

        cfg = await mode_manager.get_config()
        if not cfg:
            return False

        # Strategy enabled?
        flags = cfg.strategy_flags or {}
        if not flags.get(signal.strategy_name, True):
            logger.debug("[%s] disabled — skipping", signal.strategy_name)
            return False

        # ── Entry cutoff: no new entries in the last minutes before square-off ─
        now_t = _ist_time()
        if is_after_entry_cutoff(now_t, cfg.square_off_time):
            cutoff = entry_cutoff(cfg.square_off_time)
            sq = parse_square_off(cfg.square_off_time)
            reason = f"Entry cutoff {cutoff:%H:%M} IST reached (square-off {sq:%H:%M})"
            await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
            logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
            return False

        # Study phase: record signal but do not open any position.
        if cfg.study_phase:
            await self._save_signal(signal, SignalStatus.SKIPPED, reason="study_phase=True — observing only")
            logger.info(
                "[PaperEngine] STUDY PHASE — signal recorded, no trade: %s %s %s",
                signal.strategy_name, signal.underlying, signal.direction,
            )
            return False

        # ── Regime signal filter ──────────────────────────────────────────────────
        try:
            regime_state = await strategy_orchestrator.get_current_regime(signal.underlying)
            allowed, regime_reason = strategy_orchestrator.should_allow_signal(signal, regime_state)
            if not allowed:
                await self._save_signal(signal, SignalStatus.SKIPPED, reason=f"[Regime] {regime_reason}")
                logger.info("[PaperEngine] REGIME BLOCKED %s: %s", signal.strategy_name, regime_reason)
                return False
        except Exception as _re:
            logger.debug("[PaperEngine] Regime filter error (non-fatal): %s", _re)
            regime_state = None

        # All risk/capital checks
        ok, reason = await mode_manager.pre_trade_check(signal.underlying)
        if not ok:
            await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
            logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
            return False

        # ── Select best option first (before touching capital) ────────────────
        # Option selector runs DB + Redis queries — do this BEFORE reserving
        # capital so a "no option available" result never leaks capital.
        async with AsyncSessionLocal() as db:
            selected = await option_selector.select(
                db=db,
                direction=signal.direction,
                underlying=signal.underlying,
                spot_price=signal.spot_price,
                sl_pct=signal.sl_pct,
                target_pct=signal.target_pct,
            )

        if not selected:
            await self._save_signal(signal, SignalStatus.SKIPPED, reason="No option available")
            return False

        ltp      = selected.ltp
        lot_size = selected.lot_size

        # ── Option momentum confirmation ──────────────────────────────────────
        # Require the option's LTP to be moving in our favour before entering.
        # If the option is falling at entry we're buying into weakness — that is
        # the single biggest cause of immediate SL hits after entry.
        # Skip this check if tick history is unavailable (don't block the trade).
        tick_now = None
        try:
            tick_now = await redis_cache.get_tick(selected.token)
            if tick_now:
                opt_open  = float(tick_now.get("open", 0) or 0)
                opt_close = float(tick_now.get("ltp",  0) or 0)
                if opt_open > 0 and opt_close > 0:
                    falling_pct = (opt_open - opt_close) / opt_open * 100
                    if falling_pct > 2.0:
                        reason = (
                            f"Option momentum check FAILED — LTP falling {falling_pct:.1f}% "
                            f"this candle (open={opt_open:.2f} now={opt_close:.2f}). "
                            f"Skipping to avoid buying into weakness."
                        )
                        await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
                        logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
                        return False
        except Exception as _e:
            logger.debug("Option momentum check error (non-fatal): %s", _e)

        # ── Dynamic trade sizing via half-Kelly criterion ─────────────────────
        # Uses each strategy's trailing win_rate + avg_rr to scale position size.
        # Falls back to cfg.trade_amount when insufficient history (<20 trades).
        trade_amount = await mode_manager.kelly_trade_amount(
            signal.strategy_name, cfg
        )

        # ── Regime position size multiplier ──────────────────────────────────
        try:
            regime_multiplier = strategy_orchestrator.get_position_multiplier(signal.underlying)
            if regime_multiplier == 0.0:
                current_regime = getattr(regime_state, 'regime', 'UNKNOWN') if regime_state else 'UNKNOWN'
                reason = f"[Regime] Position_Size_Multiplier=0.0 for regime {current_regime}"
                await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
                logger.info("[PaperEngine] REGIME SKIPPED %s: %s", signal.strategy_name, reason)
                return False
            regime_adjusted_amount = round(trade_amount * regime_multiplier, 2)
            if regime_multiplier != 1.0:
                logger.info(
                    "[PaperEngine] Regime multiplier=%.1f applied: trade_amount ₹%.0f → ₹%.0f  strategy=%s",
                    regime_multiplier, trade_amount, regime_adjusted_amount, signal.strategy_name
                )
        except Exception as _me:
            logger.debug("[PaperEngine] Regime multiplier error (non-fatal): %s", _me)
            regime_adjusted_amount = trade_amount
            regime_multiplier = 1.0

        # ── Expected fill price (ask when quote valid, else LTP + slippage) ──
        actual_entry = _entry_fill(
            ltp,
            getattr(selected, "bid", 0),
            getattr(selected, "ask", 0),
            _tick_age_seconds(tick_now),
            cfg.slippage_pct,
        )
        slippage = round(actual_entry - ltp, 2)

        # ── Lot sizing on the fill price — MUST happen before reserve_capital ─
        lots, capital_used = _calc_lots(regime_adjusted_amount, actual_entry, lot_size)

        # Hard cap from config
        lots = min(lots, cfg.max_lots_per_trade)
        capital_used = round(lots * actual_entry * lot_size, 2)

        # One lot must fit the budget AND the free capital — never force a lot.
        available_now = cfg.capital - (cfg.used_capital or 0.0)
        cost_per_lot  = round(actual_entry * lot_size, 2)
        if lots < 1 or cost_per_lot > available_now:
            reason = (
                f"Lot unaffordable: 1 lot {selected.symbol} = ₹{cost_per_lot:.0f} "
                f"({actual_entry:.2f} × {lot_size}) > budget ₹{regime_adjusted_amount:.0f} "
                f"or available ₹{available_now:.0f}"
            )
            await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
            logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
            return False

        # ── Atomic capital reservation (serialises concurrent signals) ────────
        # reserve_capital atomically increments used_capital + daily_trades_count
        # in the DB and returns False if the budget is already exhausted.
        # This must happen AFTER lot sizing (capital_used is now known) and AFTER
        # option selection (so a missing option never silently consumes capital).
        reserved = await mode_manager.reserve_capital(capital_used)
        if not reserved:
            reason = "Capital was reserved by another trade or is no longer available"
            await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
            logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
            return False

        # All remaining checks that could still abort — must release reserved
        # capital on any early return below this point.
        try:
            qty = lots * lot_size

            # Entry fill (actual_entry/slippage) was computed before lot sizing
            brokerage    = round(BROKERAGE_PER_LOT * lots, 2)

            # ── Spot ATR vs option SL distance check ─────────────────────────────
            try:
                spot_atr = signal.indicators.get("atr") or 0.0
                if spot_atr > 0:
                    sl_distance_pts  = actual_entry * signal.sl_pct
                    approx_delta     = getattr(selected, "delta_est", None)
                    approx_delta     = abs(approx_delta) if approx_delta else 0.0
                    if approx_delta < 0.15:
                        approx_delta = 0.35
                    spot_move_needed = sl_distance_pts / approx_delta
                    min_atr_needed   = spot_move_needed / 2.0
                    if signal.underlying == "BANKNIFTY":
                        min_atr_needed = min(min_atr_needed, 25.0)
                    else:
                        min_atr_needed = min(min_atr_needed, 7.5)
                    if spot_atr < min_atr_needed:
                        reason = (
                            f"Spot ATR={spot_atr:.1f} < {min_atr_needed:.1f} needed "
                            f"(sl_dist=₹{sl_distance_pts:.1f}, δ≈{approx_delta:.2f}). "
                            f"Market too quiet to reach option target."
                        )
                        await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
                        logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
                        await mode_manager.release_reserved_capital(capital_used)
                        return False
            except Exception as _e:
                logger.debug("ATR/SL check error (non-fatal): %s", _e)

            # ── SL / target based on ACTUAL entry (post-slippage) ────────────────
            sl_pct     = signal.sl_pct
            target_pct = signal.target_pct

            sl  = round(actual_entry * (1.0 - sl_pct),    1)
            tgt = round(actual_entry * (1.0 + target_pct), 1)
            rr  = round((tgt - actual_entry) / max(actual_entry - sl, 0.01), 2)

            # ── Enforce minimum R:R from strategy (default 2.0) ──────────────────
            MIN_RR = signal.min_rr
            if rr < MIN_RR:
                tgt = round(actual_entry + (actual_entry - sl) * MIN_RR, 1)
                rr  = round((tgt - actual_entry) / max(actual_entry - sl, 0.01), 2)
                logger.debug(
                    "[PaperEngine] Target stretched to %.2f to meet min R:R=%.1f for %s",
                    tgt, MIN_RR, signal.strategy_name,
                )

            if rr < MIN_RR:
                reason = (
                    f"R:R {rr:.2f} below strategy minimum {MIN_RR} even after adjustment. "
                    f"actual_entry={actual_entry} sl={sl} tgt={tgt}"
                )
                await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
                logger.info("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
                await mode_manager.release_reserved_capital(capital_used)
                return False

            # Kill switch may have fired while this signal was being processed
            if mode_manager.is_halted():
                reason = "Kill switch activated during entry"
                await self._save_signal(signal, SignalStatus.SKIPPED, reason=reason)
                logger.warning("[PaperEngine] SKIPPED %s: %s", signal.strategy_name, reason)
                await mode_manager.release_reserved_capital(capital_used)
                return False

            async with AsyncSessionLocal() as db:
                sig_id = await self._save_signal(
                    signal, SignalStatus.SELECTED,
                    selected=selected, sl=sl, tgt=tgt, rr=rr, db=db
                )
                pos = PaperPosition(
                    strategy_name   = signal.strategy_name,
                    entry_regime    = getattr(regime_state, 'regime', None) if regime_state else None,
                    signal_id       = sig_id,
                    token           = selected.token,
                    symbol          = selected.symbol,
                    underlying      = signal.underlying,
                    strike          = selected.strike,
                    option_type     = selected.option_type,
                    expiry          = selected.expiry,
                    direction       = SignalDirection.BUY,
                    lots            = lots,
                    lot_size        = lot_size,
                    quantity        = qty,
                    entry_price     = ltp,
                    entry_slippage  = slippage,
                    actual_entry    = actual_entry,
                    stop_loss       = sl,
                    initial_stop_loss = sl,
                    target          = tgt,
                    max_loss_per_lot= round((actual_entry - sl) * lot_size, 2),
                    brokerage       = brokerage,
                    statutory_charges = 0.0,
                    trade_amount_used = capital_used,
                    is_open         = True,
                    mode            = TradingMode(cfg.mode),
                    entry_time      = datetime.now(timezone.utc),
                )
                db.add(pos)
                await db.commit()
                await db.refresh(pos)

            logger.info(
                "[PaperEngine] ✅ OPENED %s | %s %s %.0f%s | "
                "entry=%.2f SL=%.2f TGT=%.2f R:R=%.2f | "
                "lots=%d cost=₹%.0f (kelly_amount=₹%.0f)",
                signal.strategy_name, signal.underlying, selected.symbol,
                selected.strike, selected.option_type,
                actual_entry, sl, tgt, rr,
                lots, capital_used, trade_amount
            )

            # Cache for fast monitoring
            await redis_cache.set(
                f"pos:{pos.id}",
                {
                    "id":       pos.id,
                    "token":    selected.token,
                    "entry":    actual_entry,
                    "sl":       sl,
                    "tgt":      tgt,
                    "qty":      qty,
                    "strategy": signal.strategy_name,
                    "capital":  capital_used,
                },
                ttl=86400,
            )

            available = cfg.capital - (cfg.used_capital or 0.0)
            # Notify frontend
            await redis_cache.publish_signal("paper_trade", {
                "event":         "POSITION_OPENED",
                "pos_id":        pos.id,
                "symbol":        selected.symbol,
                "strategy":      signal.strategy_name,
                "direction":     signal.direction.value,
                "lots":          lots,
                "entry":         actual_entry,
                "sl":            sl,
                "target":        tgt,
                "capital_used":  capital_used,
                "available":     round(available - capital_used, 2),
            })

            # Race guard: kill switch fired after the pre-insert check but
            # before this position was visible to square_off_all → close now.
            if mode_manager.is_halted():
                try:
                    price, stale = await self._resolve_exit_price(pos)
                    await self._close_position(
                        pos.id, price or actual_entry,
                        "KILL_SWITCH_STALE" if stale else "KILL_SWITCH",
                    )
                except Exception as _ke:
                    logger.error("[PaperEngine] Kill-switch close of new pos %d failed: %s", pos.id, _ke)
            return True  # position opened successfully

        except Exception as exc:
            # Unexpected error after capital was reserved — release it to avoid leak
            logger.error(
                "[PaperEngine] Unexpected error after capital reservation for %s: %s — releasing capital",
                signal.strategy_name, exc, exc_info=True,
            )
            await mode_manager.release_reserved_capital(capital_used)
            return False

    # ── Position monitor loop ─────────────────────────────────────────────────

    async def _monitor_loop(self):
        while self._running:
            try:
                await asyncio.sleep(1)
                now_ist = _ist_time()
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(PaperPosition).where(PaperPosition.is_open == True)
                    )
                    open_positions = result.scalars().all()
                if not open_positions:
                    continue
                square_off = await self._get_square_off()
                for pos in open_positions:
                    await self._check_position(pos, now_ist, square_off)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor loop error: %s", e, exc_info=True)

    async def _get_square_off(self) -> dtime:
        """Configured square-off time, cached for 30 s (fallback 15:15)."""
        now_m = _time.monotonic()
        if self._square_off_cache and now_m - self._square_off_cache[1] < 30:
            return self._square_off_cache[0]
        try:
            from app.services.trading.mode_manager import mode_manager
            cfg = await mode_manager.get_config()
            sq = parse_square_off(cfg.square_off_time if cfg else None)
        except Exception as exc:
            logger.debug("square_off_time lookup failed (%s) — using default", exc)
            sq = DEFAULT_SQUARE_OFF
        self._square_off_cache = (sq, now_m)
        return sq

    def _warn_price(self, pos_id: int, msg: str, *args) -> None:
        """Rate-limited (1 / 60 s per position) WARNING for missing/stale prices."""
        now_m = _time.monotonic()
        last = self._stale_warned.get(pos_id)
        if last is None or now_m - last >= STALE_WARN_INTERVAL_SECS:
            self._stale_warned[pos_id] = now_m
            logger.warning(msg, *args)

    def _forget(self, pos_id: int) -> None:
        self._last_known.pop(pos_id, None)
        self._last_persist.pop(pos_id, None)
        self._stale_warned.pop(pos_id, None)

    async def _remember_price(self, pos_id: int, ltp: float) -> None:
        """Track the last fresh LTP in memory and (throttled) in Redis pos:{id}."""
        now_utc = datetime.now(timezone.utc)
        self._last_known[pos_id] = (float(ltp), now_utc)
        now_m = _time.monotonic()
        last = self._last_persist.get(pos_id)
        if last is not None and now_m - last < LAST_PRICE_PERSIST_SECS:
            return
        self._last_persist[pos_id] = now_m
        try:
            key = f"pos:{pos_id}"
            cached = await redis_cache.get(key)
            cached = cached if isinstance(cached, dict) else {"id": pos_id}
            cached["last_ltp"] = float(ltp)
            cached["last_ltp_ts"] = now_utc.isoformat()
            await redis_cache.set(key, cached, ttl=86400)
        except Exception as exc:
            logger.debug("last_ltp persist failed for pos %d: %s", pos_id, exc)

    async def _resolve_exit_price(self, pos: PaperPosition) -> tuple[Optional[float], bool]:
        """
        Best available exit price → (price, is_stale).
        Order: fresh Redis LTP → stale Redis LTP → in-memory last known →
        Redis pos:{id}.last_ltp → entry price (logged at ERROR) → (None, True).
        """
        ltp = None
        try:
            ltp = await redis_cache.get_ltp(pos.token)
        except Exception as exc:
            logger.debug("get_ltp failed for pos %d: %s", pos.id, exc)
        if ltp:
            tick = None
            try:
                tick = await redis_cache.get_tick(pos.token)
            except Exception:
                tick = None
            age = _tick_age_seconds(tick)
            return float(ltp), (age is not None and age > STALE_TICK_SECONDS)

        known = self._last_known.get(pos.id)
        if known:
            return known[0], True

        try:
            cached = await redis_cache.get(f"pos:{pos.id}")
            if isinstance(cached, dict) and cached.get("last_ltp"):
                return float(cached["last_ltp"]), True
        except Exception as exc:
            logger.debug("pos:%d cache read failed: %s", pos.id, exc)

        fallback = pos.actual_entry or pos.entry_price
        if fallback:
            logger.error(
                "[PaperEngine] No price at all for pos %d token %s — closing at ENTRY "
                "price %.2f, P&L unknown",
                pos.id, pos.token, fallback,
            )
            return float(fallback), True
        return None, True

    async def square_off_all(self, reason: str) -> dict:
        """
        Close every open position at the best available price (EOD and kill switch).
        Positions closed on a stale/fallback price get reason "<reason>_STALE".
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PaperPosition).where(PaperPosition.is_open == True)
            )
            open_positions = result.scalars().all()

        closed, stale_ids, failed_ids = 0, [], []
        for pos in open_positions:
            try:
                price, is_stale = await self._resolve_exit_price(pos)
                if price is None:
                    logger.error("[PaperEngine] %s: cannot close pos %d — no price", reason, pos.id)
                    failed_ids.append(pos.id)
                    continue
                exit_reason = f"{reason}_STALE" if is_stale else reason
                await self._close_position(pos.id, price, exit_reason)
                closed += 1
                if is_stale:
                    stale_ids.append(pos.id)
                self._forget(pos.id)
            except Exception as exc:
                logger.error("[PaperEngine] %s close error for pos %d: %s", reason, pos.id, exc)
                failed_ids.append(pos.id)

        logger.info(
            "[PaperEngine] square_off_all(%s): closed=%d stale=%s failed=%s",
            reason, closed, stale_ids, failed_ids,
        )
        return {"closed": closed, "stale": stale_ids, "failed": failed_ids}

    async def _check_position(
        self, pos: PaperPosition, now_ist: dtime, square_off: dtime = DEFAULT_SQUARE_OFF,
    ):
        ltp = await redis_cache.get_ltp(pos.token)
        tick = None
        try:
            tick = await redis_cache.get_tick(pos.token)
        except Exception:
            tick = None
        age = _tick_age_seconds(tick)
        stale = not ltp or (age is not None and age > STALE_TICK_SECONDS)
        in_session = is_market_hours(now_ist)

        if not ltp:
            # Never skip silently: warn, and still square off at EOD using the
            # last known price instead of leaving the position open.
            known = self._last_known.get(pos.id)
            if in_session:
                self._warn_price(
                    pos.id, "[PaperEngine] No LTP for pos %d token %s; last known %s",
                    pos.id, pos.token, f"{known[0]:.2f}" if known else "none",
                )
            if now_ist >= square_off:
                price, _ = await self._resolve_exit_price(pos)
                if price is not None:
                    await self._close_position(pos.id, price, "EOD_STALE")
                    self._forget(pos.id)
            return

        if not stale:
            await self._remember_price(pos.id, ltp)
        elif in_session and now_ist < square_off:
            self._warn_price(
                pos.id,
                "[PaperEngine] Stale price for pos %d token %s: tick %.0fs old "
                "(> %ds) — only SL/EOD exits evaluated",
                pos.id, pos.token, age or 0, STALE_TICK_SECONDS,
            )

        # Re-read the live SL and actual_entry from DB so trailing-SL updates
        # and any partial-close modifications written in a previous tick are
        # actually seen here (the pos object came from a snapshot query in
        # _monitor_loop and may be stale).
        live_sl: Optional[float] = pos.stop_loss
        live_entry: float = pos.actual_entry or 0.0
        async with AsyncSessionLocal() as _db:
            _r = await _db.execute(
                select(
                    PaperPosition.stop_loss,
                    PaperPosition.trailing_sl,
                    PaperPosition.actual_entry,
                    PaperPosition.target,
                )
                .where(PaperPosition.id == pos.id)
            )
            _row = _r.one_or_none()
            if _row:
                # trailing_sl takes priority when it's been set and is tighter
                live_sl   = _row.trailing_sl or _row.stop_loss or pos.stop_loss
                live_entry = _row.actual_entry or pos.actual_entry or 0.0
                # Also refresh target from DB (partial close may have updated it)
                if _row.target:
                    pos.target = _row.target

        exit_reason: Optional[str] = None
        exit_price:  Optional[float] = None
        sfx = "_STALE" if stale else ""

        if now_ist >= square_off:
            exit_reason, exit_price = "EOD" + sfx, ltp

        elif live_sl and ltp <= live_sl:
            # Gap-down protection: if LTP has already gone below SL (gapped),
            # use LTP as exit — can't fill at a price the market skipped past.
            # Risk-reducing exits run even on a stale price (tagged _STALE).
            exit_reason = "SL" + sfx
            exit_price  = ltp if ltp < live_sl else live_sl

        elif stale:
            # Never book profits (TARGET), raise trailing SL or TIMEOUT on an
            # old quote — wait for a fresh tick.
            return

        elif pos.target and ltp >= pos.target:
            exit_reason, exit_price = "TARGET", pos.target

        else:
            # ── Trailing SL ladder (TRAIL_TIERS, progress towards target) ─────
            # progress ≥ 0.50 → SL = entry (breakeven)
            # progress ≥ 0.75 → SL = entry + 0.40 R
            # progress ≥ 0.90 → SL = entry + 0.60 R
            # Partial exit at progress ≥ 0.75 — closes 50% of lots (≥2 lots
            # only); remainder runs to target with SL moved to breakeven.
            # All tiers only advance — SL never goes backwards.
            if live_entry:
                r = (pos.target - live_entry) if (pos.target and pos.target > live_entry) \
                    else live_entry * DEFAULT_TARGET_FRACTION
                progress = (ltp - live_entry) / r if r > 0 else 0.0

                # ── Partial exit — fires once per multi-lot position ──────────
                if (progress >= PARTIAL_CLOSE_PROGRESS and not pos.partial_closed
                        and (pos.lots or 0) >= 2):
                    await self._partial_close(pos, ltp)
                    return

                new_sl = _trailing_stop(live_entry, pos.target, ltp, live_sl)
                if new_sl:
                    async with AsyncSessionLocal() as db:
                        await db.execute(
                            update(PaperPosition)
                            .where(PaperPosition.id == pos.id)
                            .values(stop_loss=new_sl, trailing_sl=new_sl)
                        )
                        await db.commit()
                    logger.debug(
                        "Trailing SL → %.2f (progress=%.2f) for pos %d",
                        new_sl, progress, pos.id,
                    )

            # ── Max-hold-time exit ────────────────────────────────────────────
            # Options lose premium rapidly when the underlying doesn't move.
            # Mean-reversion strategies (VWAP_REVERSAL, RSI_VWAP) get 25 min —
            # if price hasn't snapped back in 25 min it's not going to.
            # Trend strategies (ORB, EMA, Supertrend, etc.) get 45 min.
            # Threshold: LTP must be at least 5% above entry to survive the hold.
            _MEAN_REV_STRATEGIES = {"VWAP_REVERSAL", "RSI_VWAP", "VWAP_PULLBACK"}
            _hold_limit = 25 if pos.strategy_name in _MEAN_REV_STRATEGIES else 45
            if pos.entry_time:
                entry_utc = pos.entry_time
                if entry_utc.tzinfo is None:
                    from datetime import timezone as _tz
                    entry_utc = entry_utc.replace(tzinfo=_tz.utc)
                hold_mins = (datetime.now(timezone.utc) - entry_utc).total_seconds() / 60
                if hold_mins > _hold_limit and ltp < (live_entry or 0) * 1.05:
                    logger.info(
                        "[PaperEngine] MAX-HOLD-TIME exit pos %d (%s) — "
                        "held %.0f min (limit %d), LTP=%.2f < entry×1.05=%.2f",
                        pos.id, pos.strategy_name, hold_mins, _hold_limit,
                        ltp, (live_entry or 0) * 1.05,
                    )
                    exit_reason = "TIMEOUT"
                    exit_price  = ltp
                    await self._close_position(pos.id, exit_price, exit_reason)
                    self._forget(pos.id)
                    return
            return

        await self._close_position(pos.id, exit_price, exit_reason)
        self._forget(pos.id)

    # ── Partial close ─────────────────────────────────────────────────────────

    async def _partial_close(self, pos: PaperPosition, ltp: float):
        """
        Close 50% of lots at market when progress to target reaches
        PARTIAL_CLOSE_PROGRESS (0.75). Remaining lots continue with SL moved to
        breakeven (actual_entry).

        Logic:
          partial_qty  = floor(lots / 2) lots  — at least one lot
          remain_qty   = quantity - partial_qty
          fill         = best bid when valid, else ltp × (1 − 0.1%)
          partial_pnl  = (fill - actual_entry) * partial_qty
                         − brokerage − statutory charges
          new SL       = actual_entry  (breakeven for the remainder)
        """
        from app.services.trading.mode_manager import mode_manager

        if not pos.actual_entry or not pos.lot_size:
            return

        # Calculate partial qty — must be a whole number of lots
        partial_lots = max(1, pos.lots // 2)
        partial_qty  = partial_lots * pos.lot_size
        remain_qty   = pos.quantity - partial_qty

        # If only 1 lot total, can't split — skip partial, let full close handle it
        if remain_qty <= 0:
            return

        tick = None
        try:
            tick = await redis_cache.get_tick(pos.token)
        except Exception:
            tick = None
        fill = _exit_fill(
            ltp, (tick or {}).get("best_bid"), (tick or {}).get("best_ask"),
            _tick_age_seconds(tick), "PARTIAL",
        )

        partial_gross = round((fill - pos.actual_entry) * partial_qty, 2)
        partial_brok  = round(BROKERAGE_PER_LOT * partial_lots, 2)
        # Statutory charges on both legs of the closed quantity (GST base =
        # entry-leg + exit-leg brokerage of the closed lots)
        partial_chg   = _statutory_charges(
            pos.actual_entry * partial_qty, fill * partial_qty, 2 * partial_brok,
        )
        partial_net   = round(partial_gross - partial_brok - partial_chg, 2)

        # Capital to release for the closed portion (proportional)
        capital_fraction = (partial_qty / pos.quantity) if pos.quantity else 0.5
        capital_released = round((pos.trade_amount_used or 0) * capital_fraction, 2)

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(PaperPosition)
                .where(PaperPosition.id == pos.id)
                .values(
                    # Mark partial close done
                    partial_closed      = True,
                    partial_exit_price  = fill,
                    partial_exit_qty    = partial_qty,
                    partial_pnl         = partial_net,
                    remaining_quantity  = remain_qty,
                    # Reduce active quantity to remaining
                    quantity            = remain_qty,
                    lots                = pos.lots - partial_lots,
                    # SL moves to breakeven for the remainder
                    stop_loss           = pos.actual_entry,
                    trailing_sl         = pos.actual_entry,
                    # Accumulate partial P&L into gross_pnl for display
                    gross_pnl           = round((pos.gross_pnl or 0) + partial_gross, 2),
                    net_pnl             = round((pos.net_pnl or 0) + partial_net, 2),
                    brokerage           = round((pos.brokerage or 0) + partial_brok, 2),
                    statutory_charges   = round((pos.statutory_charges or 0) + partial_chg, 2),
                    # Release proportional capital
                    trade_amount_used   = round((pos.trade_amount_used or 0) - capital_released, 2),
                )
            )
            await db.commit()

        # Release capital for the closed portion
        await mode_manager.on_position_closed(capital_released)

        logger.info(
            "[PaperEngine] ⚡ PARTIAL CLOSE pos %d | "
            "closed %d qty @ %.2f | partial_pnl=₹%.2f | "
            "remain=%d qty | SL → breakeven %.2f",
            pos.id, partial_qty, fill, partial_net,
            remain_qty, pos.actual_entry,
        )

        # Notify frontend
        await redis_cache.publish_signal("paper_trade", {
            "event":        "PARTIAL_CLOSE",
            "pos_id":       pos.id,
            "partial_qty":  partial_qty,
            "exit_price":   fill,
            "partial_pnl":  partial_net,
            "remain_qty":   remain_qty,
            "new_sl":       pos.actual_entry,
        })

    # ── Full close ────────────────────────────────────────────────────────────

    async def _close_position(self, pos_id: int, exit_price: float, reason: str):
        from app.services.trading.mode_manager import mode_manager

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PaperPosition).where(PaperPosition.id == pos_id)
            )
            pos = result.scalar_one_or_none()
            if not pos or not pos.is_open:
                return

            # Exit fill: TARGET = limit at target − 0.1%; market-style reasons
            # sell at best bid when the quote is valid, else price − 0.1%.
            tick = None
            try:
                tick = await redis_cache.get_tick(pos.token)
            except Exception:
                tick = None
            actual_exit = _exit_fill(
                exit_price, (tick or {}).get("best_bid"), (tick or {}).get("best_ask"),
                _tick_age_seconds(tick), reason,
            )
            exit_slip = round(exit_price - actual_exit, 2)

            # Remaining-lot P&L (pos.quantity already reflects partial close reduction)
            close_gross = round((actual_exit - pos.actual_entry) * pos.quantity, 2)

            # Brokerage for the remaining lots being closed NOW (not the full original)
            # pos.brokerage already accumulated partial-close brokerage — add only the
            # closing-leg cost for the remaining quantity.
            remain_lots = (pos.quantity // pos.lot_size) if pos.lot_size else pos.lots
            remain_lots = max(remain_lots or 0, 1)
            close_brok_now = round(BROKERAGE_PER_LOT * remain_lots, 2)
            total_brok  = round((pos.brokerage or 0.0) + close_brok_now, 2)

            # Statutory charges for the quantity closed now (both legs; GST base
            # = entry-leg + exit-leg brokerage of these lots)
            charges = _statutory_charges(
                pos.actual_entry * pos.quantity,
                actual_exit * pos.quantity,
                close_brok_now + BROKERAGE_PER_LOT * remain_lots,
            )
            total_charges = round((pos.statutory_charges or 0.0) + charges, 2)

            # Accumulate on top of any partial-close P&L already stored
            total_gross = round((pos.gross_pnl or 0) + close_gross, 2)
            total_net   = round(total_gross - total_brok - total_charges, 2)

            # Conditional on is_open so two concurrent closers (monitor loop,
            # EOD, kill switch) can never double-release capital.
            upd = await db.execute(
                update(PaperPosition)
                .where(PaperPosition.id == pos_id, PaperPosition.is_open == True)
                .values(
                    is_open      = False,
                    exit_price   = exit_price,
                    exit_slippage= exit_slip,
                    actual_exit  = actual_exit,
                    exit_reason  = reason,
                    exit_time    = datetime.now(timezone.utc),
                    gross_pnl    = total_gross,
                    brokerage    = total_brok,
                    statutory_charges = total_charges,
                    net_pnl      = total_net,
                )
            )
            await db.commit()
            if upd.rowcount != 1:
                logger.info("[PaperEngine] pos %d already closed by another task — skipping", pos_id)
                return

        # total_net / total_gross computed inside the block — use them explicitly
        # below so we never reference an uninitialised name after the block exits.
        logger.info(
            "[PaperEngine] 🔒 CLOSED pos %d | %s | exit=%.2f PnL=₹%.2f",
            pos_id, reason, exit_price, total_net
        )

        # Release locked capital.
        # IMPORTANT: re-read trade_amount_used from the DB record we just
        # fetched above — do NOT use pos.trade_amount_used from the in-memory
        # snapshot passed into this method.  After a partial close the DB row
        # already has the reduced value (proportional capital was released then),
        # but the stale pos object still carries the original full amount, which
        # would cause a double-release and push used_capital negative.
        capital_to_release = pos.trade_amount_used or 0
        await mode_manager.on_position_closed(capital_to_release)

        # Stamp the fully-accumulated P&L onto the in-memory pos object so that
        # _update_daily_pnl receives the correct total (pos.gross_pnl is still
        # the pre-close snapshot from the DB fetch above).
        pos.gross_pnl = total_gross
        pos.net_pnl   = total_net

        # Update daily P&L (a DB failure here must not break the monitor loop)
        try:
            await self._update_daily_pnl(pos, total_net)
        except Exception as _dpe:
            logger.error("[PaperEngine] Daily P&L update failed for pos %d (non-fatal): %s", pos_id, _dpe)

        # ── Record regime performance (own session; log-and-continue) ─────────────
        if getattr(pos, 'entry_regime', None):
            try:
                trade_rr_for_regime = None
                orig_sl = pos.initial_stop_loss or pos.stop_loss
                if (pos.actual_entry and actual_exit and orig_sl
                        and pos.actual_entry > orig_sl):
                    risk = pos.actual_entry - orig_sl
                    reward = actual_exit - pos.actual_entry
                    if risk > 0:
                        trade_rr_for_regime = round(reward / risk, 3)
                async with AsyncSessionLocal() as rdb:
                    await strategy_orchestrator.record_trade_result(
                        regime=pos.entry_regime,
                        underlying=pos.underlying,
                        strategy_name=pos.strategy_name or "UNKNOWN",
                        pnl=total_net,
                        rr=trade_rr_for_regime or 0.0,
                        db_session=rdb,
                    )
                    await rdb.commit()
            except Exception as _rpe:
                logger.warning("[PaperEngine] Regime performance record failed (non-fatal): %s", _rpe)

        # Cleanup Redis
        await redis_cache.delete(f"pos:{pos_id}")

        # Notify frontend
        await redis_cache.publish_signal("paper_trade", {
            "event":   "POSITION_CLOSED",
            "pos_id":  pos_id,
            "reason":  reason,
            "net_pnl": total_net,
            "exit":    exit_price,
        })

    # ── Daily P&L aggregation ─────────────────────────────────────────────────

    async def _update_daily_pnl(self, pos: PaperPosition, net_pnl: float):
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()

        # ── Compute avg_rr for this trade ──────────────────────────────────────
        # Achieved R:R = (actual_exit - actual_entry) / (actual_entry - initial SL)
        # Uses initial_stop_loss (never trailed) for a fair R:R measurement;
        # legacy rows without it fall back to stop_loss.
        # Falls back to None if we can't compute it (e.g. stop_loss not set).
        trade_rr: float | None = None
        orig_sl = pos.initial_stop_loss or pos.stop_loss
        if (pos.actual_entry and pos.actual_exit and orig_sl
                and pos.actual_entry > orig_sl):
            risk   = pos.actual_entry - orig_sl
            reward = pos.actual_exit  - pos.actual_entry
            if risk > 0:
                trade_rr = round(reward / risk, 3)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DailyPnLSummary).where(
                    DailyPnLSummary.date          == today,
                    DailyPnLSummary.strategy_name == pos.strategy_name,
                    DailyPnLSummary.mode          == pos.mode,
                )
            )
            summary = result.scalar_one_or_none()
            won = net_pnl > 0

            # ── max_drawdown: largest single-trade loss for this strategy today ──
            # Defined as the most negative net_pnl among all closed trades today.
            # A positive number means "₹X was the worst loss" (stored as positive).
            new_max_drawdown: float | None = None
            if net_pnl < 0:
                prev_dd = summary.max_drawdown if summary and summary.max_drawdown else 0.0
                new_max_drawdown = round(max(prev_dd, abs(net_pnl)), 2)
            elif summary:
                new_max_drawdown = summary.max_drawdown  # keep existing

            # ── avg_rr: running mean of achieved R:R across all trades today ─────
            new_avg_rr: float | None = None
            if trade_rr is not None and summary and summary.total_trades > 0:
                # Incremental mean: new_avg = (prev_avg * n + new_val) / (n + 1)
                prev_avg = summary.avg_rr or 0.0
                prev_n   = summary.total_trades   # count BEFORE this trade
                new_avg_rr = round((prev_avg * prev_n + trade_rr) / (prev_n + 1), 3)
            elif trade_rr is not None:
                new_avg_rr = trade_rr  # first trade for this strategy today

            if summary:
                summary.total_trades   += 1
                summary.winning_trades += int(won)
                summary.losing_trades  += int(not won)
                summary.net_pnl    = round(summary.net_pnl   + net_pnl, 2)
                summary.gross_pnl  = round(summary.gross_pnl + (pos.gross_pnl or 0), 2)
                summary.brokerage  = round(summary.brokerage  + (pos.brokerage or 0), 2)
                summary.win_rate   = round(
                    summary.winning_trades / summary.total_trades * 100, 1
                )
                summary.total_lots    = (summary.total_lots or 0) + pos.lots
                summary.max_drawdown  = new_max_drawdown
                summary.avg_rr        = new_avg_rr
            else:
                summary = DailyPnLSummary(
                    date           = today,
                    strategy_name  = pos.strategy_name or "UNKNOWN",
                    mode           = pos.mode,
                    total_trades   = 1,
                    winning_trades = int(won),
                    losing_trades  = int(not won),
                    win_rate       = 100.0 if won else 0.0,
                    gross_pnl      = round(pos.gross_pnl or 0, 2),
                    brokerage      = round(pos.brokerage or 0, 2),
                    net_pnl        = round(net_pnl, 2),
                    total_lots     = pos.lots,
                    max_drawdown   = new_max_drawdown,
                    avg_rr         = new_avg_rr,
                )
                db.add(summary)
            await db.commit()

        # ── Update all-time StrategyPerf for half-Kelly sizing ────────────────
        # This is fire-and-forget — a failure here must never break P&L recording.
        from app.services.trading.mode_manager import mode_manager as _mm
        await _mm.update_strategy_perf(
            strategy_name = pos.strategy_name or "UNKNOWN",
            won           = won,
            achieved_rr   = trade_rr,
        )

    # ── Signal persistence ────────────────────────────────────────────────────

    async def _save_signal(
        self,
        signal:   SignalEvent,
        status:   SignalStatus,
        selected: Optional[SelectedOption] = None,
        sl:       Optional[float] = None,
        tgt:      Optional[float] = None,
        rr:       Optional[float] = None,
        reason:   str = "",
        db:       Optional[AsyncSession] = None,
    ) -> Optional[int]:
        from app.services.trading.mode_manager import mode_manager
        cfg  = await mode_manager.get_config()
        mode = TradingMode(cfg.mode) if cfg else TradingMode.PAPER

        sig = StrategySignal(
            strategy_name    = signal.strategy_name,
            underlying       = signal.underlying,
            direction        = SignalDirection(signal.direction.value),
            status           = status,
            spot_price       = signal.spot_price,
            vwap             = signal.vwap,
            signal_time      = signal.signal_time,
            suggested_strike = selected.strike if selected else signal.suggested_strike,
            suggested_token  = selected.token  if selected else None,
            suggested_symbol = selected.symbol if selected else None,
            entry_price_ref  = selected.ltp    if selected else None,
            stop_loss_ref    = sl,
            target_ref       = tgt,
            risk_reward      = rr,
            indicators       = signal.indicators,
            mode             = mode,
            skip_reason      = reason[:300] if reason else None,
        )

        async def _publish_recorded(channel: str, payload: dict) -> None:
            try:
                await redis_cache.publish_signal(channel, payload)
            except Exception as exc:        # observability must never break persistence
                logger.debug("Signal publish failed (non-fatal): %s", exc)

        async def _persist(session: AsyncSession) -> int:
            session.add(sig)
            await session.commit()
            await session.refresh(sig)
            payload = {                     # built before scheduling — never touches an expired instance
                "event":       "SIGNAL_RECORDED",
                "signal_id":   sig.id,
                "strategy":    sig.strategy_name,
                "underlying":  sig.underlying,
                "direction":   sig.direction.value,
                "status":      sig.status.value,
                "time":        sig.signal_time.isoformat() if sig.signal_time else None,
                "skip_reason": sig.skip_reason,
            }
            channel = "live_trade" if mode == TradingMode.LIVE else "paper_trade"
            task = asyncio.create_task(_publish_recorded(channel, payload))   # fire-and-forget, never awaited
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)
            return sig.id

        if db:
            return await _persist(db)
        async with AsyncSessionLocal() as session:
            return await _persist(session)

    # ── Manual close ──────────────────────────────────────────────────────────

    async def manual_close(self, pos_id: int) -> bool:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PaperPosition).where(PaperPosition.id == pos_id)
            )
            pos = result.scalar_one_or_none()
            if not pos or not pos.is_open:
                return False
        ltp = await redis_cache.get_ltp(pos.token) or pos.entry_price
        await self._close_position(pos_id, ltp, "MANUAL")
        return True


# ── IST time helper ───────────────────────────────────────────────────────────

def _ist_time() -> dtime:
    """Return current IST time (UTC+5:30)."""
    from datetime import timezone, timedelta
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    return ist.time().replace(tzinfo=None)


# Singleton
paper_engine = PaperEngine()
