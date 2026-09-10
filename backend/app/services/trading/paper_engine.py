"""
Paper Trading Engine.
Simulates order fills with configurable slippage, tracks open positions,
monitors SL/target every tick, handles EOD square-off at 15:15.

Lot sizing logic:
  user sets trade_amount = ₹10,000  (from config)
  cost_per_lot = ltp × lot_size     (e.g. 150 × 50 = ₹7,500)
  lots = floor(trade_amount / cost_per_lot)  → at least 1
  actual_capital_used = lots × cost_per_lot

Capital tracking:
  used_capital  += actual_capital_used   on position OPEN
  used_capital  -= actual_capital_used   on position CLOSE
  available     = capital - used_capital
"""
import asyncio
import logging
import math
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
from app.strategies.base import SignalEvent, Direction

logger = logging.getLogger(__name__)

BROKERAGE_PER_LOT = 20.0   # ₹20 per lot round-trip (Zerodha/flat-fee broker)


def _calc_lots(trade_amount: float, ltp: float, lot_size: int) -> tuple[int, float]:
    """
    Calculate lots and actual capital deployed.

    Returns:
        (lots, capital_used)
        lots       — zero when one lot does not fit the configured budget
        capital_used — lots × ltp × lot_size  (premium cost)
    """
    if trade_amount <= 0 or ltp <= 0 or lot_size <= 0:
        return 0, 0.0

    cost_per_lot = ltp * lot_size
    lots = math.floor(trade_amount / cost_per_lot)
    if lots < 1:
        return 0, 0.0
    capital_used = round(lots * cost_per_lot, 2)
    return lots, capital_used


class PaperEngine:
    """Full lifecycle: signal → lot sizing → fill → SL/target monitor → exit."""

    def __init__(self):
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        await self._reconcile_expired_positions()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._monitor_task.add_done_callback(self._on_monitor_done)
        logger.info("PaperEngine started.")

    async def _reconcile_expired_positions(self):
        """Close positions left open after their option contract expired."""
        ist = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(ist).date()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PaperPosition).where(
                    PaperPosition.is_open == True,
                    PaperPosition.expiry.is_not(None),
                    PaperPosition.expiry < today,
                )
            )
            expired = result.scalars().all()

        for position in expired:
            logger.warning(
                "[PaperEngine] Reconciling expired open position %d (%s, expiry=%s)",
                position.id, position.symbol, position.expiry,
            )
            # An expired option without a live quote is settled at zero in the
            # paper ledger; the important part is releasing its reserved capital.
            await self._close_position(position.id, 0.0, "EXPIRY")

    def _on_monitor_done(self, fut: asyncio.Future):
        """Auto-restart the monitor loop if it dies unexpectedly."""
        try:
            fut.result()
        except asyncio.CancelledError:
            pass   # normal shutdown
        except Exception as exc:
            logger.critical(
                "PaperEngine monitor loop DIED unexpectedly: %s — "
                "restarting to keep SL/target monitoring active", exc, exc_info=True,
            )
            if self._running:
                self._monitor_task = asyncio.create_task(self._monitor_loop())
                self._monitor_task.add_done_callback(self._on_monitor_done)

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

        # Study phase: record signal but do not open any position.
        if cfg.study_phase:
            await self._save_signal(signal, SignalStatus.SKIPPED, reason="study_phase=True — observing only")
            logger.info(
                "[PaperEngine] STUDY PHASE — signal recorded, no trade: %s %s %s",
                signal.strategy_name, signal.underlying, signal.direction,
            )
            return False

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
        # Compare against recent ticks, not tick.open. AngelOne's `open` field
        # is the session open, so comparing it with the current LTP would reject
        # a contract that fell earlier but is now recovering in our direction.
        # Skip this check if recent history is unavailable.
        try:
            raw_history = await redis_cache._r().lrange(
                f"ltp_hist:{selected.token}", -6, -1
            )
            history = [float(value) for value in raw_history if float(value) > 0]
            if len(history) >= 3:
                recent_avg = sum(history[:-1]) / len(history[:-1])
                current_ltp = history[-1]
                falling_pct = (recent_avg - current_ltp) / recent_avg * 100
                if falling_pct > 2.0:
                    reason = (
                        f"Option momentum check FAILED — recent LTP falling {falling_pct:.1f}% "
                        f"(recent_avg={recent_avg:.2f} now={current_ltp:.2f}). "
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

        # ── Lot sizing — MUST happen before reserve_capital ──────────────────
        lots, capital_used = _calc_lots(trade_amount, ltp, lot_size)

        # Hard cap from config
        lots = min(lots, cfg.max_lots_per_trade)
        capital_used = round(lots * ltp * lot_size, 2)

        if lots < 1 or capital_used <= 0:
            reason = (
                f"One lot costs ₹{ltp * lot_size:,.2f}, above trade amount "
                f"₹{trade_amount:,.2f}"
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

            # Slippage (entry) — pay ask + slippage
            slippage     = round(ltp * (cfg.slippage_pct / 100), 2)
            actual_entry = round(ltp + slippage, 2)
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

            async with AsyncSessionLocal() as db:
                sig_id = await self._save_signal(
                    signal, SignalStatus.SELECTED,
                    selected=selected, sl=sl, tgt=tgt, rr=rr, db=db
                )
                pos = PaperPosition(
                    strategy_name   = signal.strategy_name,
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
                    target          = tgt,
                    max_loss_per_lot= round((actual_entry - sl) * lot_size, 2),
                    brokerage       = brokerage,
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
            # Notify frontend — compute available AFTER capital_used was already
            # reserved by reserve_capital(), so subtract it from the pre-reservation
            # snapshot to get the correct post-trade available figure.
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
                "available":     round(max(0.0, available - capital_used), 2),
            })
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
                for pos in open_positions:
                    await self._check_position(pos, now_ist)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Monitor loop error: %s", e, exc_info=True)

    async def _check_position(self, pos: PaperPosition, now_ist: dtime):
        ltp = await redis_cache.get_ltp(pos.token)
        if not ltp:
            return

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
                    PaperPosition.partial_closed,   # refresh to prevent double-partial-close race
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
                # Refresh partial_closed flag — prevents double-fire of _partial_close
                # when monitor loop iterates before the partial-close commit is visible
                pos.partial_closed = _row.partial_closed

        exit_reason: Optional[str] = None
        exit_price:  Optional[float] = None

        if now_ist >= dtime(15, 15):
            exit_reason, exit_price = "EOD", ltp

        elif live_sl and ltp <= live_sl:
            # Gap-down protection: if LTP has already gone below SL (gapped),
            # use LTP as exit — can't fill at a price the market skipped past.
            exit_reason = "SL"
            exit_price  = ltp if ltp < live_sl else live_sl

        elif pos.target and ltp >= pos.target:
            exit_reason, exit_price = "TARGET", pos.target

        else:
            # ── Three-tier trailing SL ────────────────────────────────────────
            # Tier 1: up 10% → move SL to breakeven (entry × 0.99 — 1% buffer
            #          so rounding never puts SL above entry causing instant exit)
            # Tier 2: up 20% → lock 15% profit (SL → entry × 1.15)
            # Tier 3: up 35% → lock 25% profit (SL → entry × 1.25)
            # Partial exit at +20% — closes 50% of lots, remainder runs to target
            # with SL moved to breakeven.
            # All tiers only advance — SL never goes backwards.
            if live_entry:
                gain_pct = (ltp - live_entry) / live_entry * 100

                # ── Partial exit at +20% — fires once before the +30% target ─
                if gain_pct >= 20.0 and not pos.partial_closed:
                    await self._partial_close(pos, ltp)
                    return

                if gain_pct >= 35.0:
                    new_sl = round(live_entry * 1.25, 1)
                elif gain_pct >= 20.0:
                    new_sl = round(live_entry * 1.15, 1)
                elif gain_pct >= 10.0:
                    # Breakeven lock: SL → entry, floored to 1 decimal so
                    # rounding never pushes it ABOVE entry causing instant exit.
                    # e.g. entry=37.93 → floor(37.93×1.0, 1) = 37.9 (not 38.0)
                    # The trade is still effectively at breakeven — a 0.03-pt
                    # slip below entry is acceptable insurance against round-up traps.
                    import math as _math
                    new_sl = _math.floor(live_entry * 10) / 10   # floor to 1 d.p.
                else:
                    new_sl = None

                current_sl = pos.trailing_sl or pos.stop_loss or 0
                if new_sl and new_sl > current_sl:
                    async with AsyncSessionLocal() as db:
                        await db.execute(
                            update(PaperPosition)
                            .where(PaperPosition.id == pos.id)
                            .values(stop_loss=new_sl, trailing_sl=new_sl)
                        )
                        await db.commit()
                    logger.debug(
                        "Trailing SL → %.2f (gain=%.1f%%) for pos %d",
                        new_sl, gain_pct, pos.id,
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
                    return
            return

        await self._close_position(pos.id, exit_price, exit_reason)

    # ── Partial close ─────────────────────────────────────────────────────────

    async def _partial_close(self, pos: PaperPosition, ltp: float):
        """
        Close 50% of lots at current LTP when position is up 35%.
        Remaining lots continue with SL moved to breakeven (actual_entry).

        Logic:
          partial_qty  = floor(quantity / 2)  — at least lot_size
          remain_qty   = quantity - partial_qty
          partial_pnl  = (ltp - actual_entry) * partial_qty  (gross)
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

        partial_gross = round((ltp - pos.actual_entry) * partial_qty, 2)
        partial_brok  = round(BROKERAGE_PER_LOT * partial_lots, 2)
        partial_net   = round(partial_gross - partial_brok, 2)

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
                    partial_exit_price  = ltp,
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
            pos.id, partial_qty, ltp, partial_net,
            remain_qty, pos.actual_entry,
        )

        # Notify frontend
        await redis_cache.publish_signal("paper_trade", {
            "event":        "PARTIAL_CLOSE",
            "pos_id":       pos.id,
            "partial_qty":  partial_qty,
            "exit_price":   ltp,
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

            # Slippage on exit:
            #   SL / EOD / TIMEOUT / MANUAL → market order → pay full slippage (0.1%)
            #   TARGET → limit order fills at the target price → no slippage penalty
            if reason == "TARGET":
                exit_slip = 0.0
            elif reason in {"SL", "EOD", "TIMEOUT", "MANUAL"}:
                exit_slip = round(exit_price * 0.001, 2)
            else:
                exit_slip = 0.0
            actual_exit = round(exit_price - exit_slip, 2)

            # Remaining-lot P&L (pos.quantity already reflects partial close reduction)
            close_gross = round((actual_exit - pos.actual_entry) * pos.quantity, 2)

            # Brokerage for the remaining lots being closed NOW (not the full original)
            # pos.brokerage already accumulated partial-close brokerage — add only the
            # closing-leg cost for the remaining quantity.
            remain_lots = (pos.quantity // pos.lot_size) if pos.lot_size else pos.lots
            close_brok_now = round(BROKERAGE_PER_LOT * max(remain_lots, 1), 2)
            total_brok  = round((pos.brokerage or 0.0) + close_brok_now, 2)

            # Accumulate on top of any partial-close P&L already stored
            total_gross = round((pos.gross_pnl or 0) + close_gross, 2)
            total_net   = round(total_gross - total_brok, 2)

            await db.execute(
                update(PaperPosition)
                .where(PaperPosition.id == pos_id)
                .values(
                    is_open      = False,
                    exit_price   = exit_price,
                    exit_slippage= exit_slip,
                    actual_exit  = actual_exit,
                    exit_reason  = reason,
                    exit_time    = datetime.now(timezone.utc),
                    gross_pnl    = total_gross,
                    brokerage    = total_brok,
                    net_pnl      = total_net,
                )
            )
            await db.commit()

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

        # Update daily P&L
        await self._update_daily_pnl(pos, total_net)

        # Redis is only a cache/notification layer; the DB close above is the
        # source of truth and must remain successful if Redis is unavailable.
        try:
            await redis_cache.delete(f"pos:{pos_id}")
            await redis_cache.publish_signal("paper_trade", {
                "event":   "POSITION_CLOSED",
                "pos_id":  pos_id,
                "reason":  reason,
                "net_pnl": total_net,
                "exit":    exit_price,
            })
        except Exception as exc:
            logger.warning(
                "[PaperEngine] Redis cleanup/notification failed for closed pos %d: %s",
                pos_id, exc,
            )

    # ── Daily P&L aggregation ─────────────────────────────────────────────────

    async def _update_daily_pnl(self, pos: PaperPosition, net_pnl: float):
        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()

        # ── Compute avg_rr for this trade ──────────────────────────────────────
        # Achieved R:R = (actual_exit - actual_entry) / (actual_entry - stop_loss)
        # Use the original stop_loss (not trailing) for a fair R:R measurement.
        # Falls back to None if we can't compute it (e.g. stop_loss not set).
        trade_rr: float | None = None
        if (pos.actual_entry and pos.actual_exit and pos.stop_loss
                and pos.actual_entry > pos.stop_loss):
            risk   = pos.actual_entry - pos.stop_loss
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

        async def _persist(session: AsyncSession) -> int:
            session.add(sig)
            await session.commit()
            await session.refresh(sig)
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
