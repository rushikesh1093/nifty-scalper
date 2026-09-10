"""
Trading Mode Manager.
Controls PAPER ↔ LIVE switching with safety guards.
Enforces daily loss limits, capital limits, daily trade count.
Tracks used_capital in real-time as positions open/close.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select, update, func

from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import (
    TradingConfig, TradingMode, PaperPosition, DailyPnLSummary, StrategyPerf,
)

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# ── Half-Kelly constants (capital-relative) ───────────────────────────────────
KELLY_MIN_TRADES    = 20      # need at least this many closed trades before Kelly applies
KELLY_MIN_FRACTION  = 0.10   # floor: never bet less than 10% of capital per trade
KELLY_MAX_FRACTION  = 0.30   # cap:   never bet more than 30% of capital on one trade
# Both are fractions of live capital so they scale correctly from ₹15k to ₹10L.
# Absolute amounts are computed at call-time: min = KELLY_MIN_FRACTION × capital


class ModeManager:

    # ── Config ────────────────────────────────────────────────────────────────

    async def get_config(self) -> Optional[TradingConfig]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradingConfig).where(TradingConfig.id == 1)
            )
            return result.scalar_one_or_none()

    async def ensure_config(self) -> TradingConfig:
        """
        Guarantee that the single TradingConfig row (id=1) exists.
        Call once at startup. Idempotent — safe to call multiple times.
        Always ensures study_phase=False so paper trading is active.
        Also corrects any legacy rows that have under-sized capital or lot limits.

        All risk parameters are computed as fractions of capital so the system
        behaves correctly from ₹15,000 to ₹10,00,000 without manual tuning:
          trade_amount      = 20% of capital  (fallback before Kelly kicks in)
          max_open_positions = max(2, floor(capital / trade_amount × 0.6))
                             → 60% max deployment across concurrent positions
          max_daily_loss    = 5% of capital
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradingConfig).where(TradingConfig.id == 1)
            )
            cfg = result.scalar_one_or_none()
            if not cfg:
                capital      = 100_000.0
                trade_amount = round(capital * 0.20 / 500) * 500   # 20% fallback
                max_pos      = max(2, int(capital / trade_amount * 0.6))
                cfg = TradingConfig(
                    id=1,
                    study_phase=False,
                    capital=capital,
                    trade_amount=trade_amount,
                    max_lots_per_trade=5,
                    max_open_positions=max_pos,
                    max_daily_loss=round(capital * 0.05),
                    max_daily_loss_pct=5.0,
                )
                db.add(cfg)
                await db.commit()
                await db.refresh(cfg)
                logger.info(
                    "TradingConfig row created — capital=₹%.0f trade_amount=₹%.0f "
                    "max_open_positions=%d max_daily_loss=₹%.0f",
                    capital, trade_amount, max_pos, capital * 0.05,
                )
                return cfg

            # Patch fields that may have been set to bad values
            patches: dict = {"mode": TradingMode.PAPER, "live_mode_confirmed": False}

            if cfg.study_phase:
                patches["study_phase"] = False
                logger.warning("study_phase was True — reset to False. Paper trading is now active.")

            capital = cfg.capital or 100_000.0

            if capital < 5000.0:
                # Implausibly low — reset to a sane default
                patches["capital"] = 100_000.0
                capital = 100_000.0
                logger.warning("capital was ₹%.0f (too low) — reset to ₹1,00,000.", cfg.capital)

            # trade_amount: should be ~20% of capital as a sane fallback
            # If it's > 40% of capital it will block after 2 positions — fix it
            if (cfg.trade_amount or 0) > capital * 0.40 or (cfg.trade_amount or 0) < 500:
                new_ta = round(capital * 0.20 / 500) * 500
                patches["trade_amount"] = new_ta
                logger.warning(
                    "trade_amount was ₹%.0f (out of range for capital ₹%.0f) — "
                    "reset to ₹%.0f (20%% of capital).",
                    cfg.trade_amount or 0, capital, new_ta,
                )

            trade_amount = patches.get("trade_amount", cfg.trade_amount) or capital * 0.20

            if cfg.max_lots_per_trade < 1:
                patches["max_lots_per_trade"] = max(1, min(5, int(capital / 10_000)))
                logger.warning(
                    "max_lots_per_trade was %d — corrected to %d.",
                    cfg.max_lots_per_trade, patches["max_lots_per_trade"],
                )

            # max_open_positions: should allow 60% deployment max
            # At ₹15k cap with ₹3k/trade: 3 positions = ₹9k = 60% → correct
            # At ₹15k cap with ₹10k/trade (wrong): 6 positions = ₹60k → wrong
            ideal_pos = max(2, int(capital / trade_amount * 0.6))
            if (cfg.max_open_positions or 0) < 2 or (cfg.max_open_positions or 0) * trade_amount > capital:
                patches["max_open_positions"] = ideal_pos
                logger.warning(
                    "max_open_positions was %d — reset to %d to fit within capital ₹%.0f.",
                    cfg.max_open_positions or 0, ideal_pos, capital,
                )

            # max_daily_loss: should be ~5% of capital
            if (cfg.max_daily_loss or 0) > capital * 0.20 or (cfg.max_daily_loss or 0) < 100:
                new_loss = round(capital * 0.05 / 100) * 100
                patches["max_daily_loss"]     = new_loss
                patches["max_daily_loss_pct"] = 5.0
                logger.warning(
                    "max_daily_loss was ₹%.0f (out of range) — reset to ₹%.0f (5%% of capital).",
                    cfg.max_daily_loss or 0, new_loss,
                )

            if patches:
                await db.execute(
                    update(TradingConfig)
                    .where(TradingConfig.id == 1)
                    .values(**patches)
                )
                await db.commit()
                await db.refresh(cfg)

            return cfg

    async def update_config(self, **kwargs) -> TradingConfig:
        if "capital" in kwargs:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(func.count(PaperPosition.id)).where(PaperPosition.is_open == True)
                )
                if (result.scalar() or 0) > 0:
                    raise ValueError("Cannot change capital while positions are open")

        kwargs["mode"] = TradingMode.PAPER
        kwargs["live_mode_confirmed"] = False
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(TradingConfig).where(TradingConfig.id == 1).values(**kwargs)
            )
            await db.commit()
            result = await db.execute(
                select(TradingConfig).where(TradingConfig.id == 1)
            )
            return result.scalar_one()

    # ── Mode switching ────────────────────────────────────────────────────────

    async def switch_to_paper(self) -> dict:
        await self.update_config(mode=TradingMode.PAPER, live_mode_confirmed=False)
        logger.warning("⚠️  Switched to PAPER mode")
        return {"mode": "PAPER", "message": "Switched to paper trading mode"}

    async def switch_to_live(self, confirm: bool = False) -> dict:
        return {
            "mode": "PAPER",
            "error": "Live trading is disabled. This application currently supports paper trading only.",
        }

    async def reserve_capital(self, capital_used: float) -> bool:
        """Atomically reserve capital so concurrent signals cannot overspend."""
        if capital_used <= 0:
            return False
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(TradingConfig)
                .where(
                    TradingConfig.id == 1,
                    TradingConfig.used_capital + capital_used <= TradingConfig.capital,
                )
                .values(
                    used_capital=TradingConfig.used_capital + capital_used,
                    daily_trades_count=TradingConfig.daily_trades_count + 1,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def get_mode(self) -> str:
        cfg = await self.get_config()
        return cfg.mode if cfg else "PAPER"

    # ── Capital tracking (called by PaperEngine) ──────────────────────────────

    async def on_position_opened(self, capital_used: float):
        """
        Lock capital when a new position is opened.
        Also increment daily_trades_count.
        """
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(TradingConfig)
                .where(TradingConfig.id == 1)
                .values(
                    used_capital      = TradingConfig.used_capital + capital_used,
                    daily_trades_count= TradingConfig.daily_trades_count + 1,
                )
            )
            await db.commit()
        logger.debug("Capital locked: ₹%.2f", capital_used)

    async def on_position_closed(self, capital_released: float):
        """
        Release capital when a position is closed.
        Never let used_capital go below 0.
        """
        async with AsyncSessionLocal() as db:
            # Fetch current used_capital first to avoid going negative
            result = await db.execute(
                select(TradingConfig.used_capital).where(TradingConfig.id == 1)
            )
            current = result.scalar() or 0.0
            new_used = max(0.0, round(current - capital_released, 2))
            await db.execute(
                update(TradingConfig)
                .where(TradingConfig.id == 1)
                .values(used_capital=new_used)
            )
            await db.commit()
        logger.debug("Capital released: ₹%.2f | remaining used: ₹%.2f",
                     capital_released, new_used)

    async def release_reserved_capital(self, capital_reserved: float):
        """
        Roll back an atomic capital reservation that did not result in a
        position being opened (e.g. ATR check fail, R:R fail, unexpected error).
        Also decrements daily_trades_count since reserve_capital incremented it.
        """
        if capital_reserved <= 0:
            return
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradingConfig.used_capital, TradingConfig.daily_trades_count)
                .where(TradingConfig.id == 1)
            )
            row = result.one_or_none()
            if not row:
                return
            new_used  = max(0.0, round(row.used_capital - capital_reserved, 2))
            new_count = max(0, row.daily_trades_count - 1)
            await db.execute(
                update(TradingConfig)
                .where(TradingConfig.id == 1)
                .values(used_capital=new_used, daily_trades_count=new_count)
            )
            await db.commit()
        logger.debug(
            "Reserved capital rolled back: ₹%.2f (trade count also decremented)",
            capital_reserved,
        )

    # ── Half-Kelly dynamic trade sizing ──────────────────────────────────────

    async def kelly_trade_amount(
        self, strategy_name: str, cfg: Optional[TradingConfig] = None
    ) -> float:
        """
        Return the half-Kelly trade amount for a strategy, scaled to actual capital.

        Formula:
            p = win_rate / 100
            q = 1 - p
            b = avg_rr  (average achieved risk:reward)
            kelly_full = (p*b - q) / b
            kelly_half = max(0, kelly_full) * 0.5
            amount = clamp(kelly_half × capital,
                           KELLY_MIN_FRACTION × capital,   ← scales with capital
                           KELLY_MAX_FRACTION × capital)

        Falls back to cfg.trade_amount when:
          - fewer than KELLY_MIN_TRADES closed trades exist for this strategy
          - avg_rr / win_rate not yet available (not enough data)
          - computed kelly is zero or negative (strategy losing)

        All thresholds are fractions of capital so the logic works correctly
        regardless of whether capital is ₹15,000 or ₹1,00,000.
        """
        if cfg is None:
            cfg = await self.get_config()
        if cfg is None:
            return 10_000.0  # hardcoded fallback if no config at all

        capital = cfg.capital or 100_000.0
        kelly_min_amount = KELLY_MIN_FRACTION * capital
        kelly_max_amount = KELLY_MAX_FRACTION * capital

        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(StrategyPerf).where(
                        StrategyPerf.strategy_name == strategy_name
                    )
                )
                perf = result.scalar_one_or_none()

            if (
                perf is None
                or (perf.total_trades or 0) < KELLY_MIN_TRADES
                or not perf.win_rate
                or not perf.avg_rr
                or perf.avg_rr <= 0
            ):
                # Not enough history — use the configured amount exactly. The
                # capital reservation and lot-sizing checks enforce affordability.
                logger.debug(
                    "[Kelly] %s: %d or fewer completed trades — using configured "
                    "trade amount ₹%.0f",
                    strategy_name, perf.total_trades if perf else 0, cfg.trade_amount,
                )
                return float(cfg.trade_amount)

            p = perf.win_rate / 100.0
            q = 1.0 - p
            b = perf.avg_rr

            kelly_full = (p * b - q) / b
            kelly_half = max(0.0, kelly_full) * 0.5

            if kelly_half <= 0:
                logger.info(
                    "[Kelly] %s: negative edge (win_rate=%.1f%% avg_rr=%.2f) "
                    "→ using floor ₹%.0f (%.0f%% of capital)",
                    strategy_name, perf.win_rate, perf.avg_rr,
                    kelly_min_amount, KELLY_MIN_FRACTION * 100,
                )
                return round(kelly_min_amount / 500) * 500

            amount = kelly_half * capital
            amount = max(kelly_min_amount, min(amount, kelly_max_amount))
            amount = round(amount / 500) * 500  # round to nearest ₹500

            logger.debug(
                "[Kelly] %s: p=%.2f b=%.2f → half_kelly=%.3f → ₹%.0f "
                "(min=₹%.0f max=₹%.0f capital=₹%.0f)",
                strategy_name, p, b, kelly_half, amount,
                kelly_min_amount, kelly_max_amount, capital,
            )
            return float(amount)

        except Exception as exc:
            logger.warning(
                "[Kelly] Error computing trade amount for %s: %s — using fallback",
                strategy_name, exc,
            )
            fallback = min(cfg.trade_amount, kelly_max_amount)
            return max(fallback, kelly_min_amount)

    async def update_strategy_perf(
        self, strategy_name: str, won: bool, achieved_rr: Optional[float]
    ) -> None:
        """
        Incrementally update StrategyPerf after a closed trade.
        Called from PaperEngine._update_daily_pnl().

        Uses incremental mean formula to avoid storing all historical trades:
            new_avg = (prev_avg × n + new_val) / (n + 1)

        The kelly_fraction is recomputed and cached so kelly_trade_amount()
        can read it cheaply without recalculating every time.
        """
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(StrategyPerf).where(
                        StrategyPerf.strategy_name == strategy_name
                    )
                )
                perf = result.scalar_one_or_none()

                if perf is None:
                    perf = StrategyPerf(
                        strategy_name  = strategy_name,
                        total_trades   = 0,
                        winning_trades = 0,
                    )
                    db.add(perf)

                n = perf.total_trades or 0
                perf.total_trades   = n + 1
                perf.winning_trades = (perf.winning_trades or 0) + int(won)
                perf.win_rate       = round(perf.winning_trades / perf.total_trades * 100, 2)

                # Update avg_rr via incremental mean (only when we have an rr value)
                if achieved_rr is not None:
                    prev_avg = perf.avg_rr or 0.0
                    perf.avg_rr = round(
                        (prev_avg * n + achieved_rr) / perf.total_trades, 4
                    )

                # Recompute and cache kelly_fraction
                if (
                    perf.total_trades >= KELLY_MIN_TRADES
                    and perf.win_rate
                    and perf.avg_rr
                    and perf.avg_rr > 0
                ):
                    p = perf.win_rate / 100.0
                    q = 1.0 - p
                    b = perf.avg_rr
                    kelly_full = (p * b - q) / b
                    perf.kelly_fraction = round(max(0.0, kelly_full) * 0.5, 6)
                else:
                    perf.kelly_fraction = None  # not enough data yet

                await db.commit()

                logger.debug(
                    "[Kelly] Updated %s: trades=%d wins=%d win_rate=%.1f%% "
                    "avg_rr=%.3f kelly_fraction=%s",
                    strategy_name, perf.total_trades, perf.winning_trades,
                    perf.win_rate or 0,
                    perf.avg_rr or 0,
                    f"{perf.kelly_fraction:.4f}" if perf.kelly_fraction else "N/A (< %d trades)" % KELLY_MIN_TRADES,
                )

        except Exception as exc:
            # Non-fatal — a failed perf update must never break position closing
            logger.warning(
                "[Kelly] update_strategy_perf failed for %s: %s",
                strategy_name, exc,
            )

    async def reset_daily_counter(self):
        """Call at midnight to reset daily_trades_count."""
        await self.update_config(daily_trades_count=0)
        logger.info("Daily trades counter reset.")

    async def reconcile_used_capital(self):
        """
        Recompute used_capital from actual open positions.
        Fixes drift caused by server restarts mid-session.
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.sum(PaperPosition.trade_amount_used))
                .where(PaperPosition.is_open == True)
            )
            actual_used = round(result.scalar() or 0.0, 2)
            await db.execute(
                update(TradingConfig)
                .where(TradingConfig.id == 1)
                .values(used_capital=actual_used)
            )
            await db.commit()
        logger.info("used_capital reconciled to ₹%.2f from open positions.", actual_used)

    # ── Pre-trade checks ──────────────────────────────────────────────────────

    async def pre_trade_check(self, underlying: str) -> Tuple[bool, str]:
        """
        Returns (True, "") if trade allowed, else (False, reason).
        Checks (in order):
          1. Daily loss circuit breaker
          2. Max open positions
          3. Max trades per day
          4. Capital availability (basic — detailed check in PaperEngine)
        """
        cfg = await self.get_config()
        if not cfg:
            return False, "No trading config found"

        # 1. Daily loss limit
        breached, reason = await self._check_daily_loss(cfg)
        if breached:
            return False, reason

        # 2. Max open positions
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count(PaperPosition.id))
                .where(PaperPosition.is_open == True)
            )
            open_count = result.scalar() or 0

        if open_count >= cfg.max_open_positions:
            return False, (
                f"Max open positions ({cfg.max_open_positions}) reached. "
                f"Wait for a position to close."
            )

        # 3. Max trades per day
        if cfg.daily_trades_count >= cfg.max_trades_per_day:
            return False, (
                f"Daily trade limit ({cfg.max_trades_per_day}) reached."
            )

        # 4. Sufficient capital for at least 1 lot
        available = cfg.capital - (cfg.used_capital or 0.0)
        if available < cfg.trade_amount * 0.5:
            return False, (
                f"Available capital ₹{available:.0f} is too low "
                f"(trade amount ₹{cfg.trade_amount:.0f})."
            )

        return True, ""

    async def _check_daily_loss(
        self, cfg: Optional[TradingConfig] = None
    ) -> Tuple[bool, str]:
        if not cfg:
            cfg = await self.get_config()
        if not cfg:
            return False, ""

        today = datetime.now(IST).date()
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.sum(DailyPnLSummary.net_pnl)).where(
                    DailyPnLSummary.date == today,
                    DailyPnLSummary.mode == TradingMode(cfg.mode),
                )
            )
            total_pnl = result.scalar() or 0.0

        if total_pnl <= -abs(cfg.max_daily_loss):
            return True, (
                f"Daily loss limit ₹{cfg.max_daily_loss:.0f} breached "
                f"(today: ₹{total_pnl:.0f})"
            )

        # Only check loss % when actually in a loss — positive P&L must never
        # trigger the circuit breaker regardless of its magnitude.
        if total_pnl < 0:
            loss_pct = abs(total_pnl) / cfg.capital * 100
            if loss_pct >= cfg.max_daily_loss_pct:
                return True, (
                    f"Daily loss % limit {cfg.max_daily_loss_pct}% breached "
                    f"({loss_pct:.2f}%)"
                )

        return False, ""

    # ── Capital summary ───────────────────────────────────────────────────────

    async def get_capital_summary(self) -> dict:
        """Returns the full capital breakdown for the frontend."""
        cfg = await self.get_config()
        if not cfg:
            return {}

        used      = round(cfg.used_capital or 0.0, 2)
        capital   = round(cfg.capital, 2)
        available = round(max(capital - used, 0.0), 2)

        # Today's realised P&L
        today = datetime.now(IST).date()
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.sum(DailyPnLSummary.net_pnl)).where(
                    DailyPnLSummary.date == today,
                    DailyPnLSummary.mode == TradingMode(cfg.mode),
                )
            )
            realised_pnl = round(result.scalar() or 0.0, 2)

            # Count open positions
            op_result = await db.execute(
                select(func.count(PaperPosition.id))
                .where(PaperPosition.is_open == True)
            )
            open_positions = op_result.scalar() or 0

        breached, breach_reason = await self._check_daily_loss(cfg)

        return {
            "capital":            capital,
            "used_capital":       used,
            "available_capital":  available,
            "trade_amount":       round(cfg.trade_amount, 2),
            "realised_pnl":       realised_pnl,
            "net_capital":        round(capital + realised_pnl, 2),
            "utilisation_pct":    round(used / capital * 100, 1) if capital else 0,
            "max_daily_loss":     cfg.max_daily_loss,
            "max_daily_loss_pct": cfg.max_daily_loss_pct,
            "daily_trades":       cfg.daily_trades_count,
            "max_trades_per_day": cfg.max_trades_per_day,
            "open_positions":     open_positions,
            "max_open_positions": cfg.max_open_positions,
            "circuit_breaker":    breached,
            "circuit_reason":     breach_reason,
            "mode":               cfg.mode,
        }

    # ── Daily stats (full) ────────────────────────────────────────────────────

    async def get_daily_stats(self) -> dict:
        today = datetime.now(IST).date()
        cfg   = await self.get_config()
        mode  = cfg.mode if cfg else "PAPER"

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DailyPnLSummary).where(
                    DailyPnLSummary.date == today,
                    DailyPnLSummary.mode == TradingMode(mode),
                )
            )
            summaries = result.scalars().all()

            open_r = await db.execute(
                select(func.count(PaperPosition.id))
                .where(PaperPosition.is_open == True)
            )
            open_count = open_r.scalar() or 0

        total_pnl    = sum(s.net_pnl    for s in summaries)
        total_trades = sum(s.total_trades for s in summaries)
        total_wins   = sum(s.winning_trades for s in summaries)
        breached, breach_reason = await self._check_daily_loss(cfg)

        return {
            "date":            str(today),
            "mode":            mode,
            "total_pnl":       round(total_pnl, 2),
            "total_trades":    total_trades,
            "win_rate":        round(total_wins / total_trades * 100, 1) if total_trades else 0,
            "open_positions":  open_count,
            "loss_limit":      cfg.max_daily_loss     if cfg else 5000,
            "capital":         cfg.capital             if cfg else 100000,
            "trade_amount":    cfg.trade_amount        if cfg else 10000,
            "used_capital":    round(cfg.used_capital or 0, 2) if cfg else 0,
            "available":       round(cfg.capital - (cfg.used_capital or 0), 2) if cfg else 100000,
            "loss_pct":        round(abs(min(total_pnl, 0)) / (cfg.capital if cfg else 100000) * 100, 2),
            "circuit_breaker_triggered": breached,
            "circuit_reason":  breach_reason,
            "strategy_breakdown": [
                {
                    "strategy": s.strategy_name,
                    "trades":   s.total_trades,
                    "wins":     s.winning_trades,
                    "net_pnl":  s.net_pnl,
                    "win_rate": s.win_rate,
                }
                for s in summaries
            ],
        }

    async def update_strategy_flag(self, strategy_name: str, enabled: bool):
        cfg   = await self.get_config()
        flags = cfg.strategy_flags or {}
        flags[strategy_name] = enabled
        await self.update_config(strategy_flags=flags)


# Singleton
mode_manager = ModeManager()
