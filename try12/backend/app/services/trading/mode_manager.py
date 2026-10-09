"""
Trading Mode Manager.
Controls PAPER ↔ LIVE switching with safety guards.
Enforces daily loss limits, capital limits, daily trade count.
Capital in use, the trade count and every halt are DERIVED per run_id from persisted
positions (risk_state); nothing here reserves or releases capital.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select, update, func

from app.runtime.context import ctx, market, now_utc, params, sessions, trading_day
from app.db.models.paper_trade import (
    TradingConfig, TradingMode, PaperPosition, DailyPnLSummary, StrategyPerf,
)

logger = logging.getLogger(__name__)
LIVE_RUN = "PAPER"      # the live paper run id; display methods are scoped to it
IST = timezone(timedelta(hours=5, minutes=30))


class ConfigValidationError(ValueError):
    """A trading_config write would introduce TradingParams violations (design §17.2,
    §21): nothing is persisted and the API answers 400 naming each key."""

    def __init__(self, violations: list[str]):
        self.violations = list(violations)
        super().__init__("; ".join(self.violations))


def _params_overlay(row) -> dict:
    """The row's TradingParams-backed columns (the same overlay DbTradingParams reads)."""
    from app.runtime.live import _overlay_columns
    return {k: getattr(row, k) for k in _overlay_columns() if getattr(row, k, None) is not None}


def config_write_violations(row, updates: dict) -> list[str]:
    """Violations the merged row would have that the current row does not. Only
    NEW violations block a write, so an unrelated pre-existing problem (which
    startup check 6 reports) never makes the row uneditable."""
    from app.core.trading_params import validate_overrides
    from app.runtime.live import _overlay_columns

    base = _params_overlay(row)
    cols = set(_overlay_columns())
    merged = dict(base)
    for k, v in updates.items():
        if k in cols:
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
    before = set(validate_overrides(base))
    return [v for v in validate_overrides(merged) if v not in before]

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
                    select(func.count(PaperPosition.id)).where(
                        PaperPosition.run_id == LIVE_RUN, PaperPosition.is_open == True)  # noqa: E712
                )
                if (result.scalar() or 0) > 0:
                    raise ValueError("Cannot change capital while positions are open")

        # Validation precondition (design §17.2): the proposed merged row must not
        # introduce a TradingParams violation, whoever the caller is.
        from app.runtime.live import _overlay_columns
        if set(kwargs) & set(_overlay_columns()):
            current = await self.get_config()
            if current is not None:
                if ("capital" in kwargs and "trade_amount" not in kwargs
                        and current.trade_amount is not None):
                    # keep the display cache equal to fixed_fraction × capital (§9.5)
                    frac = kwargs.get("fixed_fraction_pct", current.fixed_fraction_pct)
                    kwargs["trade_amount"] = round(float(frac) / 100.0 * float(kwargs["capital"]), 2)
                violations = config_write_violations(current, kwargs)
                if violations:
                    raise ConfigValidationError(violations)

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
                select(func.count(PaperPosition.id)).where(
                    PaperPosition.run_id == LIVE_RUN, PaperPosition.is_open == True)  # noqa: E712
            )
            open_positions = result.scalar() or 0
        db_halted = bool(cfg.trading_halted) if cfg else False
        return {
            "halted":         self._halted or db_halted,
            "reason":         cfg.halted_reason if cfg else None,
            "halted_at":      cfg.halted_at.isoformat() if cfg and cfg.halted_at else None,
            "open_positions": open_positions,
        }

    async def get_mode(self) -> str:
        cfg = await self.get_config()
        return cfg.mode if cfg else "PAPER"

    async def update_strategy_perf(self, db, strategy_name: str, outcome) -> None:
        """
        Add one TradeOutcome to the run's all-time StrategyPerf row, on the
        CLOSE's session *db* (design §7); the caller commits. Write failures
        propagate so paper_engine records PERF_WRITE_FAILED and reconcile can
        backfill — a silently skipped update would desynchronise Kelly.

        Exact sums plus the R-sign buckets Kelly reads (§9.5):
        winning_trades_r / losing_trades_r count only R-valid trades, keyed on
        the sign of r_multiple, never on net P&L. avg_rr is kept equal to
        expectancy_r, and kelly_fraction caches the applied Kelly fraction
        risk_gate.fraction_decision would use (None until the sample is large
        enough or when there is no edge). Nothing reads the cache for a decision.
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

        perf.kelly_fraction = self._cached_kelly_fraction(perf)
        await db.flush()

        logger.debug(
            "[Kelly] Updated %s: trades=%d wins=%d win_rate=%.1f%% expectancy_r=%s "
            "R buckets=%d/%d",
            strategy_name, perf.total_trades, perf.winning_trades, perf.win_rate,
            perf.expectancy_r, perf.winning_trades_r, perf.losing_trades_r,
        )

    @staticmethod
    def _cached_kelly_fraction(perf) -> Optional[float]:
        from app.services.trading import risk_gate
        res = risk_gate.kelly_from_stats(
            int(perf.winning_trades_r or 0), int(perf.losing_trades_r or 0),
            float(perf.sum_r_win or 0.0), float(perf.sum_r_loss or 0.0))
        if isinstance(res, str) or res[4] <= 0:
            return None
        p = params()
        return round(min(p.kelly_multiplier * res[4], p.kelly_fraction_cap_pct / 100.0), 6)

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
                .where(PaperPosition.run_id == LIVE_RUN,
                       PaperPosition.entry_time >= day_start_utc)
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
        Recompute the DISPLAY caches ``used_capital`` and ``daily_trades_count`` from
        the live PAPER run's positions. This is the only writer of
        ``trading_config.used_capital``; no decision and no API response reads either
        cache (the gate derives both per run_id, design §9.2). Recomputed, never
        incremented, so it is correct at any time.
        The session factory is explicit because the lifespan calls this
        before any RuntimeContext is bound (design §6.6).
        """
        from app.services.trading.session_times import trading_day_bounds_utc
        from app.services.trading.session_times import trading_day as _day_of
        from datetime import datetime as _dt

        async with session_factory() as db:
            actual_used = round(float((await db.execute(
                select(func.coalesce(func.sum(PaperPosition.trade_amount_used), 0.0))
                .where(PaperPosition.run_id == LIVE_RUN, PaperPosition.is_open == True)  # noqa: E712
            )).scalar() or 0.0), 2)
            today = _day_of(_dt.now(timezone.utc))
            start, end = trading_day_bounds_utc(today)
            count = (await db.execute(
                select(func.count(PaperPosition.id)).where(
                    PaperPosition.run_id == LIVE_RUN,
                    PaperPosition.entry_time >= start, PaperPosition.entry_time < end)
            )).scalar() or 0
            await db.execute(
                update(TradingConfig)
                .where(TradingConfig.id == 1)
                .values(used_capital=actual_used, daily_trades_count=count,
                        daily_trades_date=today)
            )
            await db.commit()
        logger.info("used_capital cache reconciled to ₹%.2f (%d trades today).",
                    actual_used, count)

    # ── Pre-trade checks ──────────────────────────────────────────────────────

    async def kill_switch_state(self) -> Tuple[bool, str]:
        """(halted, reason): the in-memory flag or the persisted halt (gate check 2)."""
        if self._halted:
            return True, "manual halt"
        cfg = await self.get_config()
        if cfg is not None and getattr(cfg, "trading_halted", False):
            return True, cfg.halted_reason or "manual halt"
        return False, ""

    async def pre_trade_check(self, underlying: str) -> Tuple[bool, str]:
        """(True, "") if a new entry would pass the risk gate's phase one,
        else (False, reason). A thin wrapper over risk_gate.evaluate_pre_selection
        for callers that have no signal in hand; it probes with the configured
        stop/target percentages on a placeholder strategy."""
        from app.runtime.context import params as _params
        from app.services.trading import risk_gate
        from app.services.trading.strategy_orchestrator import strategy_orchestrator
        from app.strategies.base import Direction, SignalEvent

        p = _params()
        probe = SignalEvent(
            strategy_name="PRE_TRADE_CHECK", underlying=underlying, direction=Direction.BUY,
            spot_price=0.0, signal_time=now_utc(), vwap=0.0, indicators={},
            sl_pct=p.sl_pct, target_pct=p.target_pct, min_rr=p.min_rr,
        )
        regime = await strategy_orchestrator.get_current_regime(underlying)
        async with sessions()() as db:
            decision = await risk_gate.evaluate_pre_selection(db, probe, regime)
        return (True, "") if decision.allowed else (False, decision.reason)

    # ── Capital summary ───────────────────────────────────────────────────────

    async def _live_capital_figures(self, db) -> tuple[float, float, int]:
        """(capital, capital in use, trades today) for the live PAPER run, derived
        from positions (design §9.2); never from the trading_config caches."""
        from app.services.trading import risk_state
        used = await risk_state.open_capital_used(db, LIVE_RUN)
        count = await risk_state.derived_trade_count(db, LIVE_RUN, trading_day())
        return float(ctx().cfg.capital), used, count

    async def capital_figures(self) -> tuple[float, float, int]:
        """(capital, capital in use, trades today) for the live PAPER run, in its
        own session. Every API field named used_capital / available_capital is
        computed from this, never from the trading_config display cache."""
        async with sessions()() as db:
            return await self._live_capital_figures(db)

    async def _loss_status(self, db) -> tuple[bool, str]:
        from app.services.trading import risk_state
        loss = await risk_state.daily_loss_figure(db)
        if not loss.breached:
            return False, ""
        return True, (f"Daily loss cap breached ({loss.breach_source}): realised "
                      f"₹{loss.realised:.0f} + unrealised ₹{loss.unrealised:.0f} = "
                      f"₹{loss.combined:.0f}")

    async def get_capital_summary(self) -> dict:
        """Returns the full capital breakdown for the frontend."""
        from app.services.trading.risk_gate import round_to_500
        cfg = await self.get_config()
        if not cfg:
            return {}

        async with sessions()() as db:
            capital, used, daily_trades = await self._live_capital_figures(db)
            realised_pnl = round(float((await db.execute(
                select(func.coalesce(func.sum(DailyPnLSummary.net_pnl), 0.0)).where(
                    DailyPnLSummary.run_id == LIVE_RUN,
                    DailyPnLSummary.date == trading_day(),
                    DailyPnLSummary.mode == TradingMode(cfg.mode),
                )
            )).scalar() or 0.0), 2)
            open_positions = (await db.execute(
                select(func.count(PaperPosition.id)).where(
                    PaperPosition.run_id == LIVE_RUN, PaperPosition.is_open == True)  # noqa: E712
            )).scalar() or 0
            breached, breach_reason = await self._loss_status(db)

        used = round(used, 2)
        capital = round(capital, 2)
        trade_amount = round_to_500(params().fixed_fraction_pct / 100.0 * capital)
        return {
            "capital":            capital,
            "used_capital":       used,
            "available_capital":  round(max(capital - used, 0.0), 2),
            "trade_amount":       trade_amount,
            "realised_pnl":       realised_pnl,
            "net_capital":        round(capital + realised_pnl, 2),
            "utilisation_pct":    round(used / capital * 100, 1) if capital else 0,
            "max_daily_loss":     ctx().cfg.max_daily_loss,
            "max_daily_loss_pct": ctx().cfg.max_daily_loss_pct,
            "daily_trades":       daily_trades,
            "max_trades_per_day": ctx().cfg.max_trades_per_day,
            "open_positions":     open_positions,
            "max_open_positions": ctx().cfg.max_open_positions,
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
                    DailyPnLSummary.run_id == LIVE_RUN,
                    DailyPnLSummary.date == today,
                    DailyPnLSummary.mode == TradingMode(mode),
                )
            )
            summaries = result.scalars().all()
            capital, used, _ = await self._live_capital_figures(db)
            open_count = (await db.execute(
                select(func.count(PaperPosition.id)).where(
                    PaperPosition.run_id == LIVE_RUN, PaperPosition.is_open == True)  # noqa: E712
            )).scalar() or 0
            breached, breach_reason = await self._loss_status(db)

        total_pnl    = sum(s.net_pnl    for s in summaries)
        total_trades = sum(s.total_trades for s in summaries)
        total_wins   = sum(s.winning_trades for s in summaries)

        return {
            "date":            str(today),
            "mode":            mode,
            "total_pnl":       round(total_pnl, 2),
            "total_trades":    total_trades,
            "win_rate":        round(total_wins / total_trades * 100, 1) if total_trades else 0,
            "open_positions":  open_count,
            "loss_limit":      ctx().cfg.max_daily_loss,
            "capital":         capital,
            "trade_amount":    cfg.trade_amount if cfg else None,
            "used_capital":    round(used, 2),
            "available":       round(capital - used, 2),
            "loss_pct":        round(abs(min(total_pnl, 0)) / capital * 100, 2) if capital else 0,
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
