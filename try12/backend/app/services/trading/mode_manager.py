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

from app.runtime.context import ctx, market, now_utc, sessions, trading_day
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


def per_trade_budget(cfg) -> float:
    """The legacy fixed per-trade amount: the stored trade_amount display cache
    when set, else fixed_fraction_pct × capital. Since migration 0013 the column
    is NULL by default (design §9.5); risk_gate replaces this in FEAT-005."""
    stored = getattr(cfg, "trade_amount", None)
    if stored:
        return float(stored)
    pct = getattr(cfg, "fixed_fraction_pct", None) or 10.0
    return float(cfg.capital or 0.0) * float(pct) / 100.0


class ModeManager:

    def __init__(self) -> None:
        # In-memory kill-switch flag: blocks entries immediately, even before
        # (or if) the persisted trading_config.trading_halted write lands.
        self._halted: bool = False

    # ── Config ────────────────────────────────────────────────────────────────

    async def get_config(self) -> Optional[TradingConfig]:
        async with sessions()() as db:
            result = await db.execute(
                select(TradingConfig).where(TradingConfig.id == 1)
            )
            return result.scalar_one_or_none()

    async def _write_halt(self, halt_type: str, code: str, *, reason: str, session_factory) -> None:
        """
        Block entries and record why. The in-memory flag is set first so the
        block holds even if the DB write fails. Only the halt columns are
        written; mode / live_mode_confirmed are never touched here (refuse,
        don't patch — design §11).
        """
        self._halted = True
        logger.error("%s halt (%s): %s", code, halt_type, reason)
        try:
            async with session_factory() as db:
                await db.execute(
                    update(TradingConfig)
                    .where(TradingConfig.id == 1)
                    .values(
                        trading_halted=True,
                        halted_reason=f"{code}: {reason}"[:200],
                        halted_at=func.now(),   # runs before bind(): DB clock, not ctx
                    )
                )
                await db.commit()
        except Exception as exc:
            logger.error("%s halt DB write failed (in-memory halt holds): %s", code, exc)

        # The risk_halts row (design §11): one per (run, day, type, scope);
        # a repeat on the same day is the same halt. Runs before bind(), so
        # the live run id and the wall clock are used explicitly.
        from app.db.models.risk import RiskHalt
        from app.runtime.live import LIVE_RUN_ID, LiveClock
        clock = LiveClock()
        try:
            async with session_factory() as db:
                exists = (await db.execute(select(RiskHalt.id).where(
                    RiskHalt.run_id == LIVE_RUN_ID,
                    RiskHalt.trading_day == clock.trading_day(),
                    RiskHalt.halt_type == halt_type,
                    RiskHalt.scope.is_(None),
                ))).first()
                if exists is None:
                    db.add(RiskHalt(
                        run_id=LIVE_RUN_ID, trading_day=clock.trading_day(),
                        halt_type=halt_type, scope=None, reason=f"{code}: {reason}",
                        context={"code": code}, engaged_at=clock.now_utc(),
                    ))
                    await db.commit()
        except Exception as exc:
            logger.error("%s risk_halts write failed (in-memory halt holds): %s", code, exc)

    async def ensure_config(self, *, session_factory) -> TradingConfig:
        """
        Guarantee that the single TradingConfig row (id=1) exists.
        Call once at startup. Idempotent — safe to call multiple times.
        Always ensures study_phase=False so paper trading is active.
        Also corrects any legacy rows that have under-sized capital or lot limits.

        Runs before any RuntimeContext is bound, so the session factory is an
        explicit argument (design §6.6).

        mode / live_mode_confirmed are NEVER patched on an existing row. A row
        reading mode != PAPER or live_mode_confirmed = True gets a
        CONFIG_INVALID halt and is left as found, so startup check 1 sees the
        bad value and refuses to start (design §6.3 check 1, §11). A new row
        is still created as PAPER / not confirmed, and update_config still
        forces both on every write.

        All risk parameters are computed as fractions of capital so the system
        behaves correctly from ₹15,000 to ₹10,00,000 without manual tuning:
          trade_amount      = 20% of capital  (fallback before Kelly kicks in)
          max_open_positions = max(2, floor(capital / trade_amount × 0.6))
                             → 60% max deployment across concurrent positions
          max_daily_loss    = 5% of capital
        """
        async with session_factory() as db:
            result = await db.execute(
                select(TradingConfig).where(TradingConfig.id == 1)
            )
            cfg = result.scalar_one_or_none()
            if not cfg:
                capital      = 100_000.0
                trade_amount = round(capital * 0.20 / 500) * 500   # 20% — sizes max_pos only
                max_pos      = max(2, int(capital / trade_amount * 0.6))
                cfg = TradingConfig(
                    id=1,
                    mode=TradingMode.PAPER,
                    live_mode_confirmed=False,
                    study_phase=False,
                    capital=capital,
                    # trade_amount is a nullable display cache since 0013 (§9.5);
                    # the per-trade budget is fixed_fraction_pct × capital.
                    trade_amount=None,
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

            # Patch fields that may have been set to bad values.
            # mode / live_mode_confirmed are deliberately NOT in this dict:
            # refuse, don't patch (design §6.3 check 1a).
            patches: dict = {}
            if cfg.mode is not TradingMode.PAPER or cfg.live_mode_confirmed:
                await self._write_halt(
                    "MANUAL", "CONFIG_INVALID",
                    reason=f"mode={cfg.mode!r} live_mode_confirmed={cfg.live_mode_confirmed!r}",
                    session_factory=session_factory,
                )
                # do not patch -- let run_startup_checks fail

            if cfg.study_phase:
                patches["study_phase"] = False
                logger.warning("study_phase was True — reset to False. Paper trading is now active.")

            capital = cfg.capital or 100_000.0

            if capital < 5000.0:
                # Implausibly low — reset to a sane default
                patches["capital"] = 100_000.0
                capital = 100_000.0
                logger.warning("capital was ₹%.0f (too low) — reset to ₹1,00,000.", cfg.capital)

            # trade_amount is no longer patched: since 0013 it is a nullable
            # display cache (NULL = derived), and a stored value that diverges
            # from fixed_fraction_pct × capital is refused by startup check 6
            # rather than silently rewritten (design §9.5, §11).
            trade_amount = cfg.trade_amount or capital * 0.20

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
            async with sessions()() as db:
                result = await db.execute(
                    select(func.count(PaperPosition.id)).where(PaperPosition.is_open == True)
                )
                if (result.scalar() or 0) > 0:
                    raise ValueError("Cannot change capital while positions are open")

        kwargs["mode"] = TradingMode.PAPER
        kwargs["live_mode_confirmed"] = False
        async with sessions()() as db:
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

    # ── Kill switch ───────────────────────────────────────────────────────────

    def is_halted(self) -> bool:
        """In-memory kill-switch flag (the persisted flag is checked in pre_trade_check)."""
        return self._halted

    async def set_trading_halted(self, halted: bool, reason: Optional[str] = None) -> bool:
        """
        Engage / release the kill switch. The in-memory flag is set FIRST so
        new entries stop immediately; the DB flag makes a halt survive restarts.
        Returns True when the DB write succeeded.
        """
        self._halted = halted
        try:
            await self.update_config(
                trading_halted=halted,
                halted_reason=(reason or "manual halt")[:200] if halted else None,
                halted_at=now_utc() if halted else None,
            )
            logger.warning(
                "KILL SWITCH %s%s", "ENGAGED" if halted else "RELEASED",
                f" — {reason}" if halted and reason else "",
            )
            return True
        except Exception as exc:
            logger.error("Kill switch DB write failed (in-memory flag=%s): %s", halted, exc)
            return False

    async def get_halt_status(self) -> dict:
        cfg = await self.get_config()
        async with sessions()() as db:
            result = await db.execute(
                select(func.count(PaperPosition.id)).where(PaperPosition.is_open == True)
            )
            open_positions = result.scalar() or 0
        db_halted = bool(cfg.trading_halted) if cfg else False
        return {
            "halted":         self._halted or db_halted,
            "reason":         cfg.halted_reason if cfg else None,
            "halted_at":      cfg.halted_at.isoformat() if cfg and cfg.halted_at else None,
            "open_positions": open_positions,
        }

    async def reserve_capital(self, capital_used: float) -> bool:
        """Atomically reserve capital so concurrent signals cannot overspend."""
        if capital_used <= 0:
            return False
        async with sessions()() as db:
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
        async with sessions()() as db:
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
        async with sessions()() as db:
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
        async with sessions()() as db:
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
        kelly_min_amount = KELLY_MIN_FRACTION * capital   # e.g. ₹1,500 at ₹15k cap
        kelly_max_amount = KELLY_MAX_FRACTION * capital   # e.g. ₹4,500 at ₹15k cap

        try:
            async with sessions()() as db:
                result = await db.execute(
                    select(StrategyPerf).where(
                        StrategyPerf.strategy_name == strategy_name,
                        StrategyPerf.run_id == ctx().run_id,
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
                # Not enough history — use configured fixed sizing, clamped to capital
                fallback = min(per_trade_budget(cfg), kelly_max_amount)
                fallback = max(fallback, kelly_min_amount)
                return round(fallback / 500) * 500

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
            fallback = min(per_trade_budget(cfg), kelly_max_amount)
            return max(fallback, kelly_min_amount)

    async def update_strategy_perf(self, db, strategy_name: str, outcome) -> None:
        """
        Add one TradeOutcome to the run's all-time StrategyPerf row, on the
        CLOSE's session *db* (design §7); the caller commits. Write failures
        propagate so paper_engine records PERF_WRITE_FAILED and reconcile can
        backfill — a silently skipped update would desynchronise Kelly.

        Exact sums plus the R-sign buckets Kelly reads (§9.5):
        winning_trades_r / losing_trades_r count only R-valid trades, keyed on
        the sign of r_multiple, never on net P&L. avg_rr is kept equal to
        expectancy_r for the legacy kelly_trade_amount reader, and
        kelly_fraction is the cached value of that same (pre-FEAT-005) formula.
        """
        from app.services.trading.trade_outcome import accumulate

        if db is None:
            raise TypeError("update_strategy_perf requires the close's db session")
        perf = (await db.execute(
            select(StrategyPerf).where(
                StrategyPerf.strategy_name == strategy_name,
                StrategyPerf.run_id == ctx().run_id,
            )
        )).scalar_one_or_none()
        if perf is None:
            perf = StrategyPerf(run_id=ctx().run_id, strategy_name=strategy_name)
            db.add(perf)

        accumulate(perf, outcome)
        perf.winning_trades_r = (perf.winning_trades_r or 0) + int(outcome.r_bucket == "R_WIN")
        perf.losing_trades_r = (perf.losing_trades_r or 0) + int(outcome.r_bucket == "R_LOSS")
        perf.avg_win_r = (perf.sum_r_win / perf.winning_trades_r) if perf.winning_trades_r else None
        perf.avg_loss_r = (abs(perf.sum_r_loss) / perf.losing_trades_r) \
            if perf.losing_trades_r else None
        perf.avg_rr = perf.expectancy_r

        if (perf.total_trades >= KELLY_MIN_TRADES and perf.win_rate
                and perf.avg_rr and perf.avg_rr > 0):
            p = perf.win_rate / 100.0
            b = perf.avg_rr
            perf.kelly_fraction = round(max(0.0, (p * b - (1.0 - p)) / b) * 0.5, 6)
        else:
            perf.kelly_fraction = None          # not enough data yet
        await db.flush()

        logger.debug(
            "[Kelly] Updated %s: trades=%d wins=%d win_rate=%.1f%% expectancy_r=%s "
            "R buckets=%d/%d",
            strategy_name, perf.total_trades, perf.winning_trades, perf.win_rate,
            perf.expectancy_r, perf.winning_trades_r, perf.losing_trades_r,
        )

    async def reset_daily_counter(self):
        """Manual reset of daily_trades_count (stamps today's IST date)."""
        await self.update_config(
            daily_trades_count=0, daily_trades_date=trading_day(),
        )
        logger.info("Daily trades counter reset.")

    async def reset_daily_counter_if_new_day(self) -> bool:
        """
        Reset daily_trades_count only when the IST trading date has changed
        since it was last stamped. A mid-day restart (or uvicorn --reload)
        keeps today's count. On reset the count is set to the number of
        positions actually entered today (normally 0).
        Returns True when a reset happened.
        """
        cfg = await self.get_config()
        if not cfg:
            return False
        today = trading_day()
        if cfg.daily_trades_date == today:
            logger.debug(
                "Daily trades counter kept (%d) — already stamped for %s",
                cfg.daily_trades_count, today,
            )
            return False

        day_start_utc = datetime.combine(today, datetime.min.time(), tzinfo=IST) \
            .astimezone(timezone.utc)
        async with sessions()() as db:
            result = await db.execute(
                select(func.count(PaperPosition.id))
                .where(PaperPosition.entry_time >= day_start_utc)
            )
            entered_today = result.scalar() or 0

        await self.update_config(daily_trades_count=entered_today, daily_trades_date=today)
        logger.info(
            "Daily trades counter reset for %s (was %d for %s) → %d",
            today, cfg.daily_trades_count, cfg.daily_trades_date, entered_today,
        )
        return True

    async def reconcile_used_capital(self, *, session_factory):
        """
        Recompute used_capital from actual open positions.
        Fixes drift caused by server restarts mid-session.
        The session factory is explicit because the lifespan calls this
        before any RuntimeContext is bound (design §6.6).
        """
        async with session_factory() as db:
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
          0. Kill switch (in-memory or persisted)
          1. Daily loss circuit breaker (realised + unrealised)
          2. Max open positions
          3. Max trades per day
          4. Capital availability (basic — detailed check in PaperEngine)
        """
        cfg = await self.get_config()
        if not cfg:
            return False, "No trading config found"

        # 0. Kill switch
        if self._halted or getattr(cfg, "trading_halted", False):
            return False, f"Kill switch active: {cfg.halted_reason or 'manual halt'}"

        # 1. Daily loss limit
        breached, reason = await self._check_daily_loss(cfg)
        if breached:
            return False, reason

        # 2. Max open positions
        async with sessions()() as db:
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
        if available < per_trade_budget(cfg) * 0.5:
            return False, (
                f"Available capital ₹{available:.0f} is too low "
                f"(trade amount ₹{per_trade_budget(cfg):.0f})."
            )

        return True, ""

    async def _check_daily_loss(
        self, cfg: Optional[TradingConfig] = None
    ) -> Tuple[bool, str]:
        if not cfg:
            cfg = await self.get_config()
        if not cfg:
            return False, ""

        today = trading_day()
        async with sessions()() as db:
            result = await db.execute(
                select(func.sum(DailyPnLSummary.net_pnl)).where(
                    DailyPnLSummary.date == today,
                    DailyPnLSummary.mode == TradingMode(cfg.mode),
                )
            )
            realised = result.scalar() or 0.0

        # Unrealised P&L of open positions at current LTP, plus partial-close
        # P&L already booked on them (not yet in daily_pnl_summary).
        unrealised = 0.0
        try:
            async with sessions()() as db:
                result = await db.execute(
                    select(PaperPosition).where(
                        PaperPosition.is_open == True,
                        PaperPosition.mode == TradingMode(cfg.mode),
                    )
                )
                open_positions = result.scalars().all()
            for pos in open_positions:
                ltp = await market().get_ltp(pos.token)
                if ltp:
                    unrealised += (ltp - (pos.actual_entry or 0.0)) * (pos.quantity or 0)
                if pos.partial_closed:
                    unrealised += pos.net_pnl or 0.0
        except Exception as exc:
            logger.debug("Unrealised P&L unavailable (%s) — using realised only", exc)
            unrealised = 0.0

        total_pnl = realised + unrealised
        detail = f"realised ₹{realised:.0f} + unrealised ₹{unrealised:.0f}"

        if total_pnl <= -abs(cfg.max_daily_loss):
            return True, (
                f"Daily loss limit ₹{cfg.max_daily_loss:.0f} breached "
                f"(today: ₹{total_pnl:.0f} = {detail})"
            )

        # Only check loss % when actually in a loss — positive P&L must never
        # trigger the circuit breaker regardless of its magnitude.
        if total_pnl < 0:
            loss_pct = abs(total_pnl) / cfg.capital * 100
            if loss_pct >= cfg.max_daily_loss_pct:
                return True, (
                    f"Daily loss % limit {cfg.max_daily_loss_pct}% breached "
                    f"({loss_pct:.2f}% = {detail})"
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
        today = trading_day()
        async with sessions()() as db:
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
            "trade_amount":       round(per_trade_budget(cfg), 2),
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
        today = trading_day()
        cfg   = await self.get_config()
        mode  = cfg.mode if cfg else "PAPER"

        async with sessions()() as db:
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
