"""
Strategy Runner and Scheduler.
Orchestrates the strategies (app.strategies.names) — feeds them candle data
every minute, runs the open positions' candle-close exits, wires
SignalBus → PaperEngine.
Also handles:
  - Pre-market setup: signal-aggregator reset
  - EOD cleanup: force-close all open positions at 15:15
"""
import asyncio
import logging
import time as _time
from datetime import datetime, date, time as dtime
from typing import Optional

from app.strategies.strategies import build_strategies
from app.strategies.indicators import compute_all
from app.runtime.context import ctx, market, now_ist, sessions, trading_day
from app.services.trading.paper_engine import paper_engine
from app.services.trading.mode_manager import mode_manager
from app.core.config import settings
from app.services.trading.regime_detector import regime_detector
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.services.trading.regime_rules import get_regime_rule
from app.services.trading.session_times import entry_cutoff, parse_square_off
from app.services.trading.wiring import wire_signal_handlers
from app.services.trading import no_trade
from app.services.trading.no_trade import ReasonCode, Stage
from app.services.market_data.timestamps import try_normalize_candle_timestamp

logger = logging.getLogger(__name__)

CANDLE_CHECK_INTERVAL = 60   # seconds — check for new candles every minute
CANDLE_SETTLE_SECONDS = 2    # run each cycle this long after the minute boundary, once
                             # the feed has closed and pushed the previous 1-min candle
VIX_TOKEN             = settings.VIX_TOKEN  # India VIX token (centralised in config)

# Market gates
VIX_MIN           = 10.0   # below → no movement, options go nowhere
                            # (was 11.0 — a VIX of 10.5-11.0 is still a tradeable
                            #  session; raising the floor to 11.0 was blocking real days)
VIX_MAX           = 25.0   # above → extreme fear/erratic, hard SLs get blown
                            # (raised from 20 → 25: Nifty VIX regularly hits 18-22
                            #  on normal volatile days, 20 was blocking good sessions)
ATR_MIN_NIFTY     = 1.5    # Nifty 1-min ATR below this = dead chop (was 2.5 — blocked ORB window)
ATR_MIN_BANKNIFTY = 5.0    # BankNifty moves ~3× Nifty (was 8.0)
MIN_TODAY_CANDLES = 20     # today-only 1m candles required before strategies run:
                            # 09:15-09:34, so the ORB can act on the 09:30-09:35 bar.
                            # EMA / RSI / Supertrend / ADX warm up on the multi-day
                            # history, not on today's candles alone.
EXPIRY_MIN_SL_PCT = 0.15   # on expiry day skip ALL strategies with sl_pct < 15%
                            # Raised from 0.08 — a 10% SL on a cheap OTM option
                            # (e.g. ₹23 LTP → SL at ₹20.70) is blown by a single
                            # adverse spot tick on expiry day.


class StrategyRunner:

    def __init__(self):
        self._running      = False
        self._run_task:    Optional[asyncio.Task] = None
        self._eod_task:    Optional[asyncio.Task] = None
        self._strategies:  dict[str, list] = {}   # underlying → [strategy list]
        self._eod_done_date: Optional[date] = None   # IST date EOD square-off last ran
        # no-trade dedupe (design §12): keys seen today, and the last
        # STRATEGY_GATE code per (run_id, underlying, strategy); both reset daily
        self._seen: set[tuple] = set()
        self._seen_day: Optional[date] = None
        self._strategy_gate_last: dict[tuple, Optional[str]] = {}
        self._cache_warned_at: float = float("-inf")

    async def start(self):
        self._running = True

        # Wire paper engine to signal bus (idempotent; design §14.1a)
        wire_signal_handlers()

        # Start paper engine monitor
        await paper_engine.start()

        # Build strategies for both underlyings
        for u in ["NIFTY", "BANKNIFTY"]:
            self._strategies[u] = build_strategies(u)

        # Pre-market setup
        await self._pre_market_setup()

        # Start loops
        self._run_task  = asyncio.create_task(self._candle_loop())
        self._eod_task  = asyncio.create_task(self._eod_monitor())
        logger.info("StrategyRunner started with %d strategies per underlying",
                    len(self._strategies.get("NIFTY", [])))

    @property
    def is_running(self) -> bool:
        return self._running

    def find_strategy(self, underlying: str, name: Optional[str]):
        """The live strategy instance for (underlying, name), or None."""
        for strat in self._strategies.get(underlying, []):
            if strat.name == name:
                return strat
        return None

    async def stop(self):
        self._running = False
        # Close positions before cancelling the monitor so a manual shutdown
        # cannot leave an open paper position stranded until EOD.
        try:
            await paper_engine.square_off_all("SHUTDOWN")
        finally:
            await paper_engine.stop()
            tasks = [t for t in (self._run_task, self._eod_task) if t]
            self._run_task = None
            self._eod_task = None
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ── Strategy enable/disable runtime ──────────────────────────────────────

    async def _get_enabled_flags(self) -> dict:
        cfg = await mode_manager.get_config()
        return cfg.strategy_flags or {} if cfg else {}

    # ── Pre-market setup ──────────────────────────────────────────────────────

    async def _pre_market_setup(self):
        """
        Called on startup and at 9:00 daily. The daily trade count is DERIVED from
        persisted positions (risk_state, design §9.2), so nothing is reset here:
        a restart cannot clear it, and replay calls this once per replayed day
        without writing trading_config. No strategy needs previous-day levels.
        """
        # Reset the signal aggregator cooldown map so yesterday's last-acted
        # timestamps don't suppress the first valid signals of the new session.
        from app.strategies.base import signal_aggregator
        signal_aggregator.reset()
        logger.info("Signal aggregator cleared for new session.")
        await self._restore_strategy_state()

    async def _restore_strategy_state(self) -> None:
        """Rebuild each strategy's per-day state (open count, ORB trades and
        their exits, Supertrend count and last exit) from today's persisted
        positions, so a restart mid-session cannot reset the day's trade rules.
        Never raises: a DB problem leaves the in-memory state as it is."""
        from datetime import timedelta, timezone
        from sqlalchemy import select
        from app.db.models.paper_trade import PaperPosition
        from app.services.market_data.timestamps import IST

        day = trading_day()
        start = datetime.combine(day, dtime(0, 0), IST).astimezone(timezone.utc)
        try:
            async with sessions()() as db:
                rows = (await db.execute(select(PaperPosition).where(
                    PaperPosition.run_id == ctx().run_id,
                    PaperPosition.entry_time >= start,
                    PaperPosition.entry_time < start + timedelta(days=1),
                ).order_by(PaperPosition.entry_time))).scalars().all()
        except Exception as exc:
            logger.warning("Strategy state restore skipped (DB read failed): %s", exc)
            return
        for underlying, strategies in self._strategies.items():
            for strat in strategies:
                restore = getattr(strat, "restore_day", None)
                if restore is None:
                    continue
                mine = [r for r in rows if r.underlying == underlying
                        and r.strategy_name == strat.name]
                restore(mine)
                if mine:
                    logger.info("[%s] %s restored %d trade(s) from today (%d open)",
                                underlying, strat.name, len(mine),
                                sum(1 for r in mine if r.is_open))

    # ── Main candle loop ──────────────────────────────────────────────────────

    async def _candle_loop(self):
        while self._running:
            try:
                # Wake just after each minute boundary rather than every 60 s of
                # elapsed time: a fixed sleep drifts by the cycle's own run time,
                # so entries lagged the bar close by up to a minute and a cycle
                # now and then saw two new candles at once.
                now = ctx().clock.now_utc()      # the ctx clock: no wall-clock read here
                await asyncio.sleep(
                    CANDLE_CHECK_INTERVAL - now.second - now.microsecond / 1e6
                    + CANDLE_SETTLE_SECONDS)
                for underlying in ["NIFTY", "BANKNIFTY"]:
                    await self.run_cycle(underlying)
                await self._refresh_display_caches()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Candle loop error: %s", e, exc_info=True)

    async def _refresh_display_caches(self) -> None:
        """Live loop only (replay drives run_cycle and never starts this loop, so
        it never writes trading_config): recompute the PAPER display caches
        (used_capital, daily_trades_count) and publish the streak-halted
        strategies for GET /regime/strategy-map. Display only; a failure is a
        rate-limited WARNING and never touches trading (design §20.4)."""
        try:
            await mode_manager.reconcile_used_capital(session_factory=ctx().sessions)
            from app.services.trading import risk_state
            async with sessions()() as db:
                state = await risk_state.streak_state(db, ctx().run_id, trading_day())
            strategy_orchestrator.set_daily_halts(state.halted_strategies)
        except Exception as exc:
            now = _time.monotonic()
            if now - self._cache_warned_at >= 600:
                self._cache_warned_at = now
                logger.warning("Display cache refresh failed (display only): %s", exc)

    async def run_cycle(self, underlying: str) -> None:
        """One candle step for one underlying (design §14.2). The live loop and
        the replay engine both call this; there is no time or flags parameter —
        time comes from the ctx clock and user flags from ctx().user_flags.

        Every gate that stops the cycle writes one no_trade_decisions row
        (deduped per underlying/candle/code), and every in-window cycle adds
        to the cycle_stats row — the positive evidence of evaluation (§12)."""
        ist_t = self._ist_now().time().replace(tzinfo=None)

        # Only run during the signal window (settings.SESSION_*_IST,
        # 9:15 – 15:25) and stop evaluating strategies at the entry
        # cutoff (configured square-off − 10 min).
        # paper_engine.handle_signal enforces the same cutoff as the
        # authoritative gate. square_off_time comes through ctx().control
        # (live row / replay RunConfig), never the shared row in replay.
        start_t, end_t = settings.SESSION_START_IST, settings.SESSION_END_IST
        cutoff = entry_cutoff(await ctx().control.square_off_time())
        if ist_t < start_t or ist_t > end_t:
            await self._record_day_gate(
                underlying, ReasonCode.OUTSIDE_SESSION_WINDOW,
                f"{ist_t:%H:%M} IST is outside the signal window "
                f"{start_t:%H:%M}-{end_t:%H:%M}",
            )
            return
        if ist_t >= cutoff:
            await self._record_day_gate(
                underlying, ReasonCode.AFTER_ENTRY_CUTOFF,
                f"{ist_t:%H:%M} IST is at/after the entry cutoff {cutoff:%H:%M}",
            )
            return

        with no_trade.cycle_tally() as tally:
            evaluated = False
            try:
                evaluated = await self._run_underlying(underlying, ctx().user_flags.current())
            finally:
                await self._flush_cycle(underlying, tally, bool(evaluated))

    # ── no-trade bookkeeping (design §12) ─────────────────────────────────────

    def _roll_day(self) -> None:
        day = trading_day()
        if self._seen_day != day:
            self._seen_day = day
            self._seen.clear()
            self._strategy_gate_last.clear()

    def _dedupe(self, key: tuple) -> bool:
        """True the first time *key* is seen today (cleared on day rollover)."""
        self._roll_day()
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    async def _record_day_gate(self, underlying: str, code: ReasonCode, reason: str) -> None:
        """A CYCLE gate that fires before any candle is read: one row per day."""
        rc = ctx()
        if not self._dedupe((rc.run_id, underlying, str(trading_day()), code.value)):
            return
        await no_trade.record_isolated(stage=Stage.CYCLE, code=code, reason=reason,
                                       underlying=underlying)

    def _queue(self, tally, key: tuple, *, stage: Stage, code: ReasonCode, reason: str,
               underlying: str, strategy_name: Optional[str] = None,
               context: Optional[dict] = None) -> None:
        """Queue one row for the end-of-cycle write, at most once per *key*."""
        if not self._dedupe((ctx().run_id,) + key):
            return
        tally.pending.append(dict(stage=stage, code=code, reason=reason,
                                  context=context or {}, underlying=underlying,
                                  strategy_name=strategy_name))

    def _queue_cycle_gate(self, tally, underlying: str, candle_ts: str, code: ReasonCode,
                          reason: str, **context) -> None:
        self._queue(tally, (underlying, candle_ts, code.value), stage=Stage.CYCLE, code=code,
                    reason=reason, underlying=underlying,
                    context=no_trade.as_context(candle_ts=candle_ts, **context))

    def _queue_strategy_gate(self, tally, underlying: str, strategy: str,
                             code: Optional[ReasonCode], reason: str = "") -> None:
        """STRATEGY_GATE rows are written on a state change per strategy and
        day, not per candle: a strategy disabled all session is one row."""
        key = (ctx().run_id, underlying, strategy)
        self._roll_day()
        if self._strategy_gate_last.get(key) == (code.value if code else None):
            return
        self._strategy_gate_last[key] = code.value if code else None
        if code is None:
            return
        tally.pending.append(dict(stage=Stage.STRATEGY_GATE, code=code, reason=reason,
                                  context={}, underlying=underlying, strategy_name=strategy))

    async def _flush_cycle(self, underlying: str, tally, evaluated: bool) -> None:
        """Queued rows plus the cycle_stats upsert, one short-lived session.
        A write failure is a WARNING and never aborts the cycle."""
        try:
            rc = ctx()
            async with sessions()() as db:
                for row in tally.pending:
                    await no_trade.record(db, rc, **row)
                await no_trade.upsert_cycle_stats(
                    db, rc, underlying=underlying, trading_day=rc.clock.trading_day(),
                    at=rc.clock.now_utc(), candles=1 if evaluated else 0,
                    strategies=tally.strategies_evaluated, signals=tally.signals_emitted,
                    rejections=tally.rejections_recorded,
                    oi_iv_missing=tally.oi_iv_context_missing,
                )
                await db.commit()
        except Exception as exc:
            logger.warning("[%s] cycle_stats / no-trade write failed (non-fatal): %s",
                           underlying, exc)

    @staticmethod
    def _ist_now() -> datetime:
        """Current IST datetime from the ctx clock (timezone-aware)."""
        return now_ist()

    async def _run_underlying(self, underlying: str, flags: dict) -> bool:
        """Evaluate one candle. Returns True once today's candles were read
        (the cycle "evaluated a candle", whether or not a later gate fired)."""
        tally = no_trade.current_tally() or no_trade.CycleTally()
        spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN

        # Get candles from Redis — fetch extra in case window spans overnight,
        # then filter to today's IST session so ORB levels are always correct.
        all_candles = await market().get_candles(spot_token, "ONE_MINUTE", 500)
        if not all_candles:
            self._queue_cycle_gate(tally, underlying, str(trading_day()),
                                   ReasonCode.FEED_UNAVAILABLE,
                                   f"No ONE_MINUTE candles in the cache for {underlying}")
            return False

        today = self._ist_now().date()
        # Normalize every feed timestamp to IST before applying the session date
        # and open-time filters. Raw date-prefix matching misclassifies UTC rows
        # around the session boundary and can make a live session look empty.
        normalized_candles = []
        history = []        # every complete, valid candle of any day (indicator warm-up)
        from collections import Counter
        rejected = Counter()
        rejected_examples = []
        for c in all_candles:
            candle_dt = try_normalize_candle_timestamp(c.get("timestamp"))
            if candle_dt is None:
                rejected["invalid_timestamp"] += 1
                rejected_examples.append(str(c.get("timestamp")))
                continue
            if candle_dt >= self._ist_now().replace(second=0, microsecond=0):
                rejected["incomplete_current_candle"] += 1
                continue
            try:
                values = [float(c[k]) for k in ("open", "high", "low", "close")]
                if not all(map(__import__("math").isfinite, values)) or values[2] > values[1]:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                rejected["invalid_ohlc"] += 1
                continue
            if dtime(9, 15) <= candle_dt.time().replace(tzinfo=None) < dtime(15, 30):
                history.append((candle_dt, c))
            if candle_dt.date() != today:
                rejected["wrong_session_date"] += 1
                continue
            if candle_dt.time().replace(tzinfo=None) < dtime(9, 15):
                rejected["before_session_open"] += 1
                continue
            # Redis/feed order is not a contract. Keep indicators, ORB, VWAP,
            # and the latest-candle signal aligned even when the provider sends
            # UTC/offset timestamps out of order.
            normalized_candles.append((candle_dt, c))

        candles = [c for _, c in sorted(normalized_candles, key=lambda item: item[0])]
        history = [c for _, c in sorted(history, key=lambda item: item[0])]

        today_str = str(today)
        logger.info(
            "[%s] candle session filter: actual=%d expected_min=%d rejected=%s examples=%s",
            underlying, len(candles), MIN_TODAY_CANDLES, dict(rejected), rejected_examples[:5],
        )
        if not candles:
            self._queue_cycle_gate(tally, underlying, today_str, ReasonCode.FEED_UNAVAILABLE,
                                   f"No candles for today ({today_str}) in the cache for "
                                   f"{underlying}")
            return False
        candle_ts = str(candles[-1].get("timestamp", ""))

        if len(candles) < MIN_TODAY_CANDLES:
            # Not enough today-only candles yet — market just opened or no data.
            # Do NOT fall back to multi-day candles: that would corrupt ORB high/low
            # (computed from today's first 15 candles) and the intraday VWAP reset.
            logger.debug(
                "[%s] Only %d today-candles available (need %d) — skipping this cycle",
                underlying, len(candles), MIN_TODAY_CANDLES,
            )
            self._queue_cycle_gate(
                tally, underlying, candle_ts, ReasonCode.INSUFFICIENT_CANDLES,
                f"Only {len(candles)} today-candles (need {MIN_TODAY_CANDLES})",
                candles=len(candles), required=MIN_TODAY_CANDLES,
            )
            return True

        # Compute indicators once, share with all strategies
        indicators = compute_all(candles)

        # ── Spot volume EMA — inject BEFORE any gates or strategy calls ───────
        # Rolling average of spot volume stored in Redis ("avg_vol:spot:{underlying}").
        # EMA α=0.2 ≈ 5-candle memory.  Must be computed and injected here so
        # that every downstream strategy sees indicators["avg_volume"] on the
        # very first cycle.
        # NOTE: NSE index tokens (NIFTY/BANKNIFTY spot) carry volume=0 in the
        # AngelOne WebSocket feed. Skip the EMA update when volume is zero so we
        # don't corrupt the running average with zeros.  Strategies that need
        # vol_surge will fall back to _avg_volume(candles) which uses the candle
        # list directly — also zero for spot — making vol_surge always True for
        # spot-based signals (acceptable, never a blocker).
        avg_vol_key = f"avg_vol:spot:{underlying}"
        cur_volume  = indicators.get("volume") or 0.0
        prev_avg    = await market().get(avg_vol_key) or 0.0
        if isinstance(prev_avg, dict):   # guard: shouldn't happen but be safe
            prev_avg = 0.0
        prev_avg = float(prev_avg)
        if cur_volume > 0:
            # Only update EMA when we have real volume data
            if prev_avg <= 0:
                new_avg = cur_volume
            else:
                new_avg = round(0.2 * cur_volume + 0.8 * prev_avg, 2)   # α=0.2
            await market().set(avg_vol_key, new_avg, ttl=28800)
            indicators["avg_volume"] = new_avg
        else:
            # Volume=0 (NSE index feed limitation) — don't clobber the EMA.
            # Keep whatever was stored from a prior cycle if available.
            indicators["avg_volume"] = prev_avg if prev_avg > 0 else None

        # ── Candle-close exits of open positions — BEFORE any gate ────────────
        # The ATR/VIX/regime gates below stop new entries; they must never stop
        # an open position's EMA-trail / VWAP / Supertrend-flip exit.
        try:
            await paper_engine.run_candle_exits(
                underlying, self._strategies.get(underlying, []), candles, history, indicators)
        except Exception as exc:
            logger.error("[%s] candle-close exits failed: %s", underlying, exc, exc_info=True)

        # ── Gate 1: ATR filter — skip if market is too choppy / dead ─────────
        atr_min = ATR_MIN_NIFTY if underlying == "NIFTY" else ATR_MIN_BANKNIFTY
        atr_v   = indicators.get("atr") or 0
        if atr_v < atr_min:
            logger.debug(
                "[%s] ATR=%.2f < %.1f — market too choppy, skipping all strategies",
                underlying, atr_v, atr_min,
            )
            self._queue_cycle_gate(tally, underlying, candle_ts, ReasonCode.ATR_BELOW_FLOOR,
                                   f"ATR={atr_v:.2f} < floor {atr_min:.1f}",
                                   atr=float(atr_v), floor=atr_min)
            return True

        # ── Gate 2: VIX filter ──────────────────────────────────────────────���─
        vix_ltp = await market().get_ltp(VIX_TOKEN)
        if vix_ltp is not None:
            if vix_ltp > VIX_MAX:
                logger.info(
                    "[%s] VIX=%.1f > %.0f — high-risk environment, skipping strategies",
                    underlying, vix_ltp, VIX_MAX,
                )
                self._queue_cycle_gate(tally, underlying, candle_ts, ReasonCode.VIX_ABOVE_MAX,
                                       f"VIX={vix_ltp:.1f} > max {VIX_MAX:.0f}",
                                       vix=float(vix_ltp), max=VIX_MAX)
                return True
            if vix_ltp < VIX_MIN:
                logger.debug(
                    "[%s] VIX=%.1f < %.0f — options too cheap / no movement expected",
                    underlying, vix_ltp, VIX_MIN,
                )
                self._queue_cycle_gate(tally, underlying, candle_ts, ReasonCode.VIX_BELOW_MIN,
                                       f"VIX={vix_ltp:.1f} < min {VIX_MIN:.0f}",
                                       vix=float(vix_ltp), min=VIX_MIN)
                return True

        # ── Gate 3: Expiry day filter ─────────────────────────────────────────
        # Nearest expiry from the instrument master (design §14.2), the same
        # answer live and replay: a port without nearest_expiry falls back to
        # its universe.
        today = trading_day()
        port = ctx().instruments
        async with sessions()() as db:
            if hasattr(port, "nearest_expiry"):
                nearest = await port.nearest_expiry(db, underlying)
            else:
                universe = await port.instruments_for(db, underlying)
                exps = sorted({i.expiry for i in universe if i.expiry and i.expiry >= today})
                nearest = exps[0] if exps else None
        is_expiry_day = nearest == today
        if is_expiry_day:
            logger.info("[%s] Expiry day — strategies with sl_pct < %.2f will be skipped",
                        underlying, EXPIRY_MIN_SL_PCT)

        # ── Regime detection ─────────────────────────────────────────────────────
        # Detect current market regime, get updated strategy flags from orchestrator.
        # user-disable takes precedence: a user-disabled strategy stays disabled
        # regardless of what regime says.
        try:
            regime_ind = await self._regime_indicators(underlying, candles, indicators)
            final_flags, regime_state = await self._detect_and_merge_regime(
                underlying, candles, regime_ind, is_expiry_day, flags, history=history,
            )
        except Exception as _regime_err:
            # Never fall back to raw user flags: a Redis or DB blip must not
            # silently remove regime gating (§6.4).
            logger.warning("[%s] Regime detection error: %s — every strategy disabled",
                           underlying, _regime_err)
            strategy_orchestrator.mark_regime_unavailable(underlying)
            self._queue_cycle_gate(tally, underlying, candle_ts, ReasonCode.REGIME_UNAVAILABLE,
                                   f"Regime detection failed for {underlying}: "
                                   f"{type(_regime_err).__name__}: {_regime_err}")
            return True

        # ── Observability only — isolated; cannot influence final_flags ───────
        if regime_state is not None:
            await self._persist_regime_state(underlying, regime_state)
            if self._queue_no_trade_regime(tally, underlying, candle_ts, regime_state):
                return True

        # ── Run strategies ────────────────────────────────────────────────────
        # The merged flags are the only gate (the detector-owned cooldown is
        # expressed through the REGIME_TRANSITION rule enabling nothing).
        for strat in self._strategies.get(underlying, []):
            if not final_flags.get(strat.name, False):
                if not flags.get(strat.name, True):
                    self._queue_strategy_gate(tally, underlying, strat.name,
                                              ReasonCode.STRATEGY_USER_DISABLED,
                                              f"{strat.name} disabled by user flag")
                else:
                    self._queue_strategy_gate(tally, underlying, strat.name,
                                              ReasonCode.STRATEGY_REGIME_DISABLED,
                                              f"{strat.name} disabled by the regime map")
                continue
            if is_expiry_day and strat.sl_pct < EXPIRY_MIN_SL_PCT:
                logger.debug(
                    "[%s] Expiry day — skipping %s (sl_pct=%.2f < %.2f)",
                    underlying, strat.name, strat.sl_pct, EXPIRY_MIN_SL_PCT,
                )
                self._queue_strategy_gate(
                    tally, underlying, strat.name, ReasonCode.EXPIRY_DAY_SL_TOO_TIGHT,
                    f"Expiry day: {strat.name} sl_pct={strat.sl_pct:.2f} < "
                    f"{EXPIRY_MIN_SL_PCT:.2f}",
                )
                continue
            # The strategy's own post-signal cooldown (BaseStrategy._cooldown_ok):
            # on_candle would return before evaluate(), so record it instead of
            # counting the strategy as evaluated. One row per cooldown episode.
            cooldown_ok = getattr(strat, "_cooldown_ok", None)
            if cooldown_ok is not None and not cooldown_ok():
                self._queue_strategy_gate(
                    tally, underlying, strat.name, ReasonCode.STRATEGY_COOLDOWN,
                    f"{strat.name} in its {getattr(strat, '_cooldown_minutes', '?')}-minute "
                    f"post-signal cooldown",
                )
                continue
            self._queue_strategy_gate(tally, underlying, strat.name, None)   # evaluated
            tally.strategies_evaluated += 1
            try:
                strat.history = history
                if hasattr(strat, "futures_candles"):
                    strat.futures_candles = await self._futures_candles(underlying)
                await strat.on_candle(candles, indicators)
            except Exception as e:
                logger.error("[%s] Strategy error: %s", strat.name, e, exc_info=True)
        return True

    async def _persist_regime_state(self, underlying: str, regime_state) -> None:
        """
        Persist one regime detection to Postgres so /regime/history accumulates.
        Runs after final_flags is computed and outside the regime-gating try, so a
        DB outage (session acquisition or commit failure) can never reset the
        gating to raw user flags. Never raises.
        """
        try:
            from app.services.trading.regime_persistence import regime_persistence
            async with sessions()() as db:
                await regime_persistence.persist_to_db(regime_state, db)
                await db.commit()
        except Exception as exc:
            logger.warning("[%s] regime persist failed (non-fatal): %s", underlying, exc)

    async def _detect_and_merge_regime(
        self,
        underlying: str,
        candles: list[dict],
        indicators: dict,
        is_expiry_day: bool,
        flags: dict,
        history: Optional[list[dict]] = None,
    ) -> tuple[dict, object]:
        """
        Detect the regime, then merge regime flags with user flags.
        Pure gating: no DB session is touched here, so a DB outage can never
        alter the flags. The regime_states row is written once per candle by
        _persist_regime_state, which the caller runs after gating.
        """
        regime_state = await regime_detector.detect_regime(
            candles=candles,
            indicators=indicators,
            underlying=underlying,
            is_expiry_day=is_expiry_day,
            db_session=None,
            history=history,
        )

        regime_flags = await strategy_orchestrator.on_regime_detected(regime_state)
        # Merge over the 12 canonical names: an absent regime flag is
        # disabled, and a user disable wins in every regime (§6.4).
        final_flags = strategy_orchestrator.merge_flags(underlying, regime_flags, flags)
        return final_flags, regime_state

    def _queue_no_trade_regime(self, tally, underlying: str, candle_ts: str,
                               regime_state) -> bool:
        """One CYCLE row per candle when the regime enables no strategy, and
        a DEGRADATION row when that regime is the detector's error fallback.
        Returns True when the candle is structurally ineligible."""
        regime = regime_state.regime
        rule = get_regime_rule(regime, regime_state.indicators.atr_percentile)
        if rule.enabled_strategies:
            return False
        if regime == "DETECTOR_ERROR":
            self._queue(tally, (underlying, candle_ts, ReasonCode.DETECTOR_ERROR.value),
                        stage=Stage.DEGRADATION, code=ReasonCode.DETECTOR_ERROR,
                        reason=f"Regime detector raised for {underlying}; no-trade fallback",
                        underlying=underlying,
                        context=no_trade.as_context(candle_ts=candle_ts))
        if regime == "REGIME_TRANSITION":
            self._queue_cycle_gate(
                tally, underlying, candle_ts, ReasonCode.TRANSITION_COOLDOWN,
                f"Regime transition cooldown for {underlying} "
                f"({regime_state.transition_cooldown_remaining} candle(s) remaining)",
                previous_regime=regime_state.previous_regime,
                remaining=int(regime_state.transition_cooldown_remaining or 0),
            )
        else:
            self._queue_cycle_gate(tally, underlying, candle_ts, ReasonCode.REGIME_NO_TRADE,
                                   f"Regime {regime} enables no strategy", regime=regime)
        return True

    REGIME_VOLUME_BARS = 20

    async def _regime_indicators(self, underlying: str, candles: list[dict],
                                 indicators: dict) -> dict:
        """*indicators* with ``volume`` / ``avg_volume`` taken from the index
        future: the spot index carries no volume, and compute_all never sets
        avg_volume, so the regime's volume-surge test always read "unavailable"
        and passed. volume = the future's 1-min volume for the latest spot
        candle's minute; avg_volume = the mean of the REGIME_VOLUME_BARS
        minutes before it, today only. No futures data → unchanged."""
        fut = await self._futures_candles(underlying)
        if not fut or not candles:
            return indicators
        from app.services.market_data.timestamps import try_normalize_candle_timestamp as _ts
        last = _ts(candles[-1].get("timestamp"))
        if last is None:
            return indicators
        rows = sorted(((t, float(c.get("volume") or 0)) for c in fut
                       if (t := _ts(c.get("timestamp"))) is not None
                       and t.date() == last.date() and t <= last), key=lambda r: r[0])
        if not rows or rows[-1][0] != last:
            return indicators
        prior = [v for _, v in rows[:-1]][-self.REGIME_VOLUME_BARS:]
        if len(prior) < 5 or rows[-1][1] <= 0:
            return indicators
        return dict(indicators, volume=rows[-1][1], avg_volume=sum(prior) / len(prior))

    async def _futures_candles(self, underlying: str) -> list[dict]:
        """1-min candles of the nearest index future (per-minute volume), via the
        token market_manager subscribed and published as ``fut_token:<UNDERLYING>``.
        [] when there is none (replay, feed down) — the caller then cannot check volume."""
        try:
            token = await market().get(f"fut_token:{underlying}")
            if not token:
                return []
            return await market().get_candles(str(token), "ONE_MINUTE", 500)
        except Exception as exc:
            logger.debug("[%s] futures candles unavailable: %s", underlying, exc)
            return []

    # ── EOD monitor ───────────────────────────────────────────────────────────

    async def _eod_monitor(self):
        """At the configured square_off_time (default 15:15 IST), force-close all
        open positions once per day; at 9:00 do pre-market setup."""
        while self._running:
            try:
                ist   = self._ist_now()
                ist_h = ist.hour
                ist_m = ist.minute

                square_off = parse_square_off(await ctx().control.square_off_time())
                if (ist.weekday() < 5
                        and ist.time().replace(tzinfo=None) >= square_off
                        and self._eod_done_date != ist.date()):
                    logger.info(
                        "EOD: Force-closing all open positions at %s IST",
                        square_off.strftime("%H:%M"),
                    )
                    await self._eod_square_off()
                    self._eod_done_date = ist.date()

                # 9:00 IST pre-market setup
                if ist_h == 9 and ist_m == 0:
                    await self._pre_market_setup()
                    await asyncio.sleep(120)

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("EOD monitor error: %s", e, exc_info=True)
                await asyncio.sleep(30)

    async def _eod_square_off(self):
        """
        Close all open positions at the best available price: fresh LTP, else
        the last known price (exit reason EOD_STALE). Entry price is only the
        last resort and is logged at ERROR by paper_engine.
        """
        result = await paper_engine.square_off_all("EOD")
        logger.info(
            "EOD square-off complete. closed=%d stale=%s failed=%s",
            result["closed"], result["stale"], result["failed"],
        )


# Singleton
strategy_runner = StrategyRunner()
