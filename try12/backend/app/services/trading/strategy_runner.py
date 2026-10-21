"""
Strategy Runner and Scheduler.
Orchestrates all 12 strategies — feeds them candle data every minute,
injects OI/IV context, wires SignalBus → PaperEngine.
Also handles:
  - Pre-market setup: PDH/PDL, CPR levels, prev close
  - EOD cleanup: force-close all open positions at 15:15
"""
import asyncio
import logging
import time as _time
from datetime import datetime, date, time as dtime
from typing import Optional

from app.strategies.strategies import build_strategies, OISpikeStrategy, IVCrushStrategy, PDHPDLStrategy, CPRVWAPStrategy, GapVWAPFillStrategy, atr_floor
from app.strategies.indicators import compute_all, cpr as calc_cpr
from app.strategies.trend_engine import ENGINE_NAME, TrendFollowingEngine
from app.runtime.context import ctx, market, now_ist, params, sessions, trading_day
from app.services.trading.paper_engine import paper_engine
from app.services.trading.mode_manager import mode_manager
from app.core.config import settings
from app.services.trading.regime_detector import regime_detector
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.services.trading.regime_rules import get_regime_rule
from app.services.trading.session_times import entry_cutoff, parse_square_off
from app.services.trading.wiring import wire_signal_handlers
from app.services.trading import market_calendar
from app.services.trading.decision_telemetry import decision_telemetry
from app.services.trading import no_trade
from app.services.trading.no_trade import ReasonCode, Stage
from app.services.market_data.timestamps import try_normalize_candle_timestamp

logger = logging.getLogger(__name__)

CANDLE_CHECK_INTERVAL = 60   # seconds — check for new candles every minute
VIX_TOKEN             = settings.VIX_TOKEN  # India VIX token (centralised in config)

# Market gates
VIX_MIN           = 10.0   # below → no movement, options go nowhere
                            # (was 11.0 — a VIX of 10.5-11.0 is still a tradeable
                            #  session; raising the floor to 11.0 was blocking real days)
VIX_MAX           = 25.0   # above → extreme fear/erratic, hard SLs get blown
                            # (raised from 20 → 25: Nifty VIX regularly hits 18-22
                            #  on normal volatile days, 20 was blocking good sessions)
# ATR floor: app.strategies.strategies.atr_floor (0.025% of price, one
# definition shared with every strategy's own _atr_ok check).
FUTURES_TOKEN_KEY = "futures_token:{underlying}"   # published by market_manager
AVG_VOLUME_LOOKBACK = 10   # candles in the volume average (current candle excluded)
MIN_TODAY_CANDLES = 25     # today-only 1m candles required before strategies run
EXPIRY_MIN_SL_PCT = 0.15   # on expiry day skip ALL strategies with sl_pct < 15%
                            # Raised from 0.08 — a 10% SL on a cheap OTM option
                            # (e.g. ₹23 LTP → SL at ₹20.70) is blown by a single
                            # adverse spot tick on expiry day.
OI_IV_WARN_INTERVAL_SECS = 600   # rate-limit the OI/IV injection WARNING per underlying


def dedupe_session_candles(rows: list[tuple]) -> list[dict]:
    """(minute_dt, candle) pairs → candles sorted by minute, ONE per minute.

    The historical seed and the live candle builder can both write the same
    minute into the cache; a repeated bar double-counts that minute in EMA,
    RSI, ATR and VWAP. The LAST-written row for a minute wins (the live
    builder writes after the seed)."""
    by_minute: dict = {}
    for dt, c in rows:
        by_minute[dt.replace(second=0, microsecond=0)] = c
    return [by_minute[k] for k in sorted(by_minute)]


def merge_futures_volume(spot: list[dict], fut_volume: dict) -> tuple[list[dict], int]:
    """Copies of *spot* candles whose ``volume`` is the index future's volume
    for the same minute (0 when the future has no candle for it). Returns the
    candles and how many minutes matched. Spot prices are untouched, so VWAP
    becomes the spot price weighted by futures volume."""
    out, matched = [], 0
    for c in spot:
        dt = try_normalize_candle_timestamp(c.get("timestamp"))
        key = dt.replace(second=0, microsecond=0) if dt else None
        v = fut_volume.get(key) if key is not None else None
        nc = dict(c)
        if v is not None and v > 0:
            nc["volume"] = float(v)
            matched += 1
        else:
            nc["volume"] = 0.0
        out.append(nc)
    return out, matched


def _atm_pair(universe: list, expiry: date, atm_strike: float) -> tuple:
    """Pure pick of the ATM CE and PE for *expiry* from an instrument universe."""
    from app.db.models.instrument import InstrumentType, OptionType
    ce = pe = None
    for i in universe:
        if i.instrument_type != InstrumentType.OPTIDX or i.expiry != expiry or i.strike != atm_strike:
            continue
        if i.option_type == OptionType.CE and ce is None:
            ce = i
        elif i.option_type == OptionType.PE and pe is None:
            pe = i
    return ce, pe


def _as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class StrategyRunner:

    def __init__(self):
        self._running      = False
        self._run_task:    Optional[asyncio.Task] = None
        self._eod_task:    Optional[asyncio.Task] = None
        self._strategies:  dict[str, list] = {}   # underlying → [strategy list]
        self._eod_done_date: Optional[date] = None   # IST date EOD square-off last ran
        self._oi_iv_warned: dict[str, float] = {}     # underlying -> monotonic ts of last WARNING
        # no-trade dedupe (design §12): keys seen today, and the last
        # STRATEGY_GATE code per (run_id, underlying, strategy); both reset daily
        self._seen: set[tuple] = set()
        self._seen_day: Optional[date] = None
        self._strategy_gate_last: dict[tuple, Optional[str]] = {}
        self._cache_warned_at: float = float("-inf")
        # per-cycle research telemetry state, underlying → {candles, indicators,
        # regime_state, gate}; written to trend_decisions after the cycle
        self._tele: dict[str, dict] = {}

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
        Fetch previous day OHLC and set PDH/PDL, CPR, prev_close on strategies.
        Called on startup and at 9:00 daily. The daily trade count is DERIVED from
        persisted positions (risk_state, design §9.2), so nothing is reset here:
        a restart cannot clear it, and replay calls this once per replayed day
        without writing trading_config.
        """
        # Reset the signal aggregator cooldown map so yesterday's last-acted
        # timestamps don't suppress the first valid signals of the new session.
        # Without this, a signal fired at 14:55 yesterday would still be in
        # cooldown at 9:16 today (90s same-direction cooldown is fine intraday
        # but must not carry across calendar days).
        from app.strategies.base import signal_aggregator
        signal_aggregator.reset()
        logger.info("Signal aggregator cleared for new session.")
        from app.db.models.market_study import MarketDailySnapshot
        from sqlalchemy import select, desc

        # The run's underlyings (live: both; a replay: the ones it replays), so a
        # NIFTY-only replay does not record a BANKNIFTY NO_PREV_DAY_SNAPSHOT gate.
        for underlying in ctx().cfg.underlyings:
            try:
                async with sessions()() as db:
                    result = await db.execute(
                        select(MarketDailySnapshot)
                        .where(
                            MarketDailySnapshot.underlying == underlying,
                            MarketDailySnapshot.date < self._ist_now().date(),
                            # a snapshot saved from a single price (open = high =
                            # low = close) is not a session: skip it, never use
                            # it as PDH = PDL
                            MarketDailySnapshot.high > MarketDailySnapshot.low,
                        )
                        .order_by(desc(MarketDailySnapshot.date))
                        .limit(1)
                    )
                    prev = result.scalar_one_or_none()

                if prev is None:
                    await self._record_day_gate(
                        underlying, ReasonCode.NO_PREV_DAY_SNAPSHOT,
                        f"No previous-day snapshot for {underlying}: PDH_PDL, CPR_VWAP and "
                        f"GAP_VWAP_FILL cannot fire today",
                    )

                for strat in self._strategies.get(underlying, []):
                    if isinstance(strat, PDHPDLStrategy):
                        if prev:
                            strat.set_levels(pdh=prev.high, pdl=prev.low)
                            logger.info("[%s] PDH=%.1f PDL=%.1f", underlying, prev.high, prev.low)
                        else:
                            logger.warning(
                                "[%s] No previous-day snapshot found — PDHPDLStrategy "
                                "will not fire today. Run market_study collection for at least 1 day.",
                                underlying,
                            )

                    if isinstance(strat, CPRVWAPStrategy):
                        if prev:
                            cpr_lvl = calc_cpr(prev.high, prev.low, prev.close)
                            strat.set_cpr(cpr_lvl.pivot, cpr_lvl.bc, cpr_lvl.tc)
                            logger.info("[%s] CPR pivot=%.1f bc=%.1f tc=%.1f",
                                        underlying, cpr_lvl.pivot, cpr_lvl.bc, cpr_lvl.tc)
                        else:
                            logger.warning(
                                "[%s] No previous-day snapshot found — CPRVWAPStrategy "
                                "will not fire today.",
                                underlying,
                            )

                    if isinstance(strat, TrendFollowingEngine):
                        # previous-day high/low = breakout evidence and room-to-target levels
                        strat.set_levels(prev.high if prev else None, prev.low if prev else None)

                    if isinstance(strat, GapVWAPFillStrategy):
                        if prev:
                            strat.set_prev_close(prev.close)
                        else:
                            logger.warning(
                                "[%s] No previous-day snapshot found — GapVWAPFillStrategy "
                                "will not fire today.",
                                underlying,
                            )

            except Exception as e:
                logger.error("Pre-market setup error for %s: %s", underlying, e)

    # ── Main candle loop ──────────────────────────────────────────────────────

    async def _candle_loop(self):
        while self._running:
            try:
                await asyncio.sleep(CANDLE_CHECK_INTERVAL)
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
        closed = market_calendar.closed_reason(trading_day())
        if closed:
            await self._record_day_gate(
                underlying, ReasonCode.OUTSIDE_SESSION_WINDOW,
                f"exchange closed today: {closed}",
            )
            return
        # Research telemetry: fill +15/+30/+60 min prices of earlier decisions.
        # Runs before the window gates so decisions near the entry cutoff are
        # completed through the close. Independent of trading; never raises.
        await self._telemetry_fill(underlying, ist_t)
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
            self._tele[underlying] = {}
            try:
                evaluated = await self._run_underlying(underlying, ctx().user_flags.current())
            finally:
                await self._flush_cycle(underlying, tally, bool(evaluated))
                await self._record_telemetry(underlying)

    # ── research telemetry (trend_decisions; independent of execution) ───────

    def _tele_gate(self, underlying: str, code: Optional[str]) -> None:
        """Remember the first gate that stopped this cycle's live path."""
        cyc = self._tele.get(underlying)
        if cyc is not None and code and cyc.get("gate") is None:
            cyc["gate"] = code

    async def _record_telemetry(self, underlying: str) -> None:
        cyc = self._tele.pop(underlying, None)
        if not cyc or not cyc.get("candles"):
            return
        engine = next((s for s in self._strategies.get(underlying, [])
                       if isinstance(s, TrendFollowingEngine)), None)
        if engine is None:
            return
        await decision_telemetry.record(
            underlying=underlying, engine=engine, candles=cyc["candles"],
            indicators=cyc["indicators"], gate=cyc.get("gate"),
            regime_state=cyc.get("regime_state"),
        )

    async def _telemetry_fill(self, underlying: str, ist_t: dtime) -> None:
        if not (dtime(9, 16) <= ist_t <= dtime(16, 0)):
            return
        try:
            spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
            raw = await market().get_candles(spot_token, "ONE_MINUTE", 500)
        except Exception as exc:
            logger.debug("[%s] telemetry candles unavailable: %s", underlying, exc)
            return
        await decision_telemetry.fill_forward(underlying, raw or [], self._ist_now())

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
        self._tele_gate(underlying, code.value)
        self._queue(tally, (underlying, candle_ts, code.value), stage=Stage.CYCLE, code=code,
                    reason=reason, underlying=underlying,
                    context=no_trade.as_context(candle_ts=candle_ts, **context))

    def _queue_strategy_gate(self, tally, underlying: str, strategy: str,
                             code: Optional[ReasonCode], reason: str = "") -> None:
        """STRATEGY_GATE rows are written on a state change per strategy and
        day, not per candle: a strategy disabled all session is one row."""
        if strategy == ENGINE_NAME and code is not None:
            self._tele_gate(underlying, code.value)
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
        from collections import Counter
        rejected = Counter()
        rejected_examples = []
        for c in all_candles:
            candle_dt = try_normalize_candle_timestamp(c.get("timestamp"))
            if candle_dt is None:
                rejected["invalid_timestamp"] += 1
                rejected_examples.append(str(c.get("timestamp")))
                continue
            if candle_dt.date() != today:
                rejected["wrong_session_date"] += 1
                continue
            if candle_dt.time().replace(tzinfo=None) < dtime(9, 15):
                rejected["before_session_open"] += 1
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
            # Redis/feed order is not a contract. Keep indicators, ORB, VWAP,
            # and the latest-candle signal aligned even when the provider sends
            # UTC/offset timestamps out of order.
            normalized_candles.append((candle_dt, c))

        n_raw = len(normalized_candles)
        candles = dedupe_session_candles(normalized_candles)
        if len(candles) < n_raw:
            rejected["duplicate_minute"] += n_raw - len(candles)

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

        # ── Volume from the near-month index future ──────────────────────────
        # NSE index spot candles carry volume=0, which made VWAP collapse to the
        # current candle's typical price and every volume check meaningless.
        candles, vol_gap = await self._with_futures_volume(underlying, candles, today)
        if vol_gap is not None:
            self._queue(tally, (underlying, str(today), "futures_volume"),
                        stage=Stage.DEGRADATION, code=ReasonCode.DATA_GAP,
                        reason=f"No index-futures volume for {underlying}: {vol_gap} — "
                               f"VWAP falls back to the session TWAP and volume-confirmed "
                               f"breakouts cannot fire",
                        underlying=underlying,
                        context={"what": "futures_volume", "cause": vol_gap})

        # Compute indicators once, share with all strategies
        indicators = compute_all(candles)
        indicators["volume_source"] = "NONE" if vol_gap is not None else "FUTURES"

        # Open TREND_ENGINE positions exit when their trend structure breaks.
        # Runs before the entry gates: an exit must not wait for an ATR/VIX/regime gate.
        await self._exit_invalidated_engine_positions(underlying, candles)

        # ── Average volume — inject BEFORE any gates or strategy calls ────────
        # Mean of the previous AVG_VOLUME_LOOKBACK candles' (futures) volume,
        # EXCLUDING the current candle, so "volume > k × average" compares the
        # signal candle against what came before it. Deterministic from the
        # candle list (no cache state), so replay and live agree.
        prior = [float(c.get("volume") or 0) for c in candles[-AVG_VOLUME_LOOKBACK - 1:-1]]
        prior = [v for v in prior if v > 0]
        indicators["avg_volume"] = (sum(prior) / len(prior)) if prior else None
        cyc = self._tele.get(underlying)
        if cyc is not None:
            cyc["candles"], cyc["indicators"] = candles, indicators

        # ── Gate 1: ATR filter — skip if market is too choppy / dead ─────────
        atr_min = atr_floor(indicators.get("close"))
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
        # Same InstrumentPort universe as _inject_oi_iv, so "nearest expiry"
        # can never disagree between the two (design §14.2).
        today = trading_day()
        async with sessions()() as db:
            universe = await ctx().instruments.instruments_for(db, underlying)
        expiries = sorted({i.expiry for i in universe if i.expiry and i.expiry >= today})
        is_expiry_day = bool(expiries and expiries[0] == today)
        if is_expiry_day:
            logger.info("[%s] Expiry day — strategies with sl_pct < %.2f will be skipped",
                        underlying, EXPIRY_MIN_SL_PCT)

        # ── Inject OI / IV into relevant strategies ───────────────────────────
        # Pass is_expiry_day so OISpikeStrategy can self-block (it also checks
        # this flag internally, but setting it here ensures the state is always
        # current before evaluate() is called this cycle).
        oi_iv_gap = await self._inject_oi_iv(underlying, indicators, is_expiry_day=is_expiry_day)
        if oi_iv_gap is not None:
            # §14.2: counted every candle, one DATA_GAP row per underlying-day
            tally.oi_iv_context_missing += 1
            self._queue(tally, (underlying, str(today), "oi_iv_context"),
                        stage=Stage.DEGRADATION, code=ReasonCode.DATA_GAP,
                        reason=f"OI/IV context not populated for {underlying}: {oi_iv_gap}",
                        underlying=underlying,
                        context={"what": "oi_iv_context", "cause": oi_iv_gap})

        # ── Regime detection ─────────────────────────────────────────────────────
        # Detect current market regime, get updated strategy flags from orchestrator.
        # user-disable takes precedence: a user-disabled strategy stays disabled
        # regardless of what regime says.
        try:
            final_flags, regime_state = await self._detect_and_merge_regime(
                underlying, candles, indicators, is_expiry_day, flags,
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
            if underlying in self._tele:
                self._tele[underlying]["regime_state"] = regime_state
            await self._persist_regime_state(underlying, regime_state)
            if self._queue_no_trade_regime(tally, underlying, candle_ts, regime_state):
                return True

        # ── Run strategies ────────────────────────────────────────────────────
        # The merged flags are the only gate (the detector-owned cooldown is
        # expressed through the REGIME_TRANSITION rule enabling nothing).
        unified = params().decision_engine == "UNIFIED"
        for strat in self._strategies.get(underlying, []):
            # UNIFIED: only TREND_ENGINE is evaluated and trades; the legacy 12 are
            # skipped entirely (the engine computes the same indicators — VWAP,
            # EMA9/21, Supertrend, ORB, PDH/PDL, volume — itself; it never sees the
            # legacy strategies' signals). LEGACY: the 12 trade independently, first
            # signal wins (SignalAggregator only de-duplicates), TREND_ENGINE idle.
            if unified != (strat.name == ENGINE_NAME):
                if strat.name == ENGINE_NAME:
                    self._tele_gate(underlying, "LEGACY_MODE")
                continue
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
                await strat.on_candle(candles, indicators)
            except Exception as e:
                logger.error("[%s] Strategy error: %s", strat.name, e, exc_info=True)
        return True

    async def _exit_invalidated_engine_positions(self, underlying: str,
                                                 candles: list[dict]) -> None:
        """Close open TREND_ENGINE positions on *underlying* whose trend is no
        longer valid (CE: close below VWAP and EMA9 < EMA21; PE: mirrored).
        Never raises; the stop-loss keeps protecting the position regardless."""
        engine = next((s for s in self._strategies.get(underlying, [])
                       if isinstance(s, TrendFollowingEngine)), None)
        if engine is None:
            return
        try:
            from sqlalchemy import select
            from app.db.models.paper_trade import PaperPosition
            async with sessions()() as db:
                rows = (await db.execute(select(PaperPosition).where(
                    PaperPosition.run_id == ctx().run_id,
                    PaperPosition.is_open == True,  # noqa: E712
                    PaperPosition.underlying == underlying,
                    PaperPosition.strategy_name == ENGINE_NAME,
                ))).scalars().all()
            for pos in rows:
                reason = engine.invalidation(pos.option_type, candles)
                if not reason:
                    continue
                # Closes only on a fresh quote; otherwise the exit is recorded as
                # pending and the position monitor executes it on the first fresh
                # tick (never at the entry price as if it were a quote).
                outcome = await paper_engine.exit_on_invalidation(pos, reason)
                logger.info("[%s] TREND_ENGINE invalidation pos %d (%s): %s — %s",
                            underlying, pos.id, pos.symbol, reason, outcome)
        except Exception as exc:
            logger.warning("[%s] invalidation check failed (non-fatal): %s", underlying, exc)

    async def _with_futures_volume(self, underlying: str, candles: list[dict],
                                   today) -> tuple[list[dict], Optional[str]]:
        """(spot candles carrying futures volume, None) or (candles, cause) when
        no usable futures volume exists. Never raises."""
        try:
            fut_token = await market().get(FUTURES_TOKEN_KEY.format(underlying=underlying))
            if not fut_token:
                return candles, "futures token not published (market_manager startup)"
            raw = await market().get_candles(str(fut_token), "ONE_MINUTE", 500)
            rows = []
            for c in raw or []:
                dt = try_normalize_candle_timestamp(c.get("timestamp"))
                if dt is None or dt.date() != today:
                    continue
                rows.append((dt, c))
            fut = dedupe_session_candles(rows)
            vol_by_minute = {}
            for c in fut:
                dt = try_normalize_candle_timestamp(c.get("timestamp"))
                try:
                    vol_by_minute[dt.replace(second=0, microsecond=0)] = float(c.get("volume") or 0)
                except (TypeError, ValueError):
                    continue
            merged, matched = merge_futures_volume(candles, vol_by_minute)
            if matched == 0:
                return candles, f"no futures candles with volume for today (token {fut_token})"
            return merged, None
        except Exception as exc:
            logger.warning("[%s] futures volume unavailable: %s", underlying, exc)
            return candles, f"{type(exc).__name__}: {exc}"

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

    def _warn_oi_iv(self, underlying: str, exc: Exception) -> None:
        """OI/IV I/O failures are visible (R19.4), at most once per 10 min per underlying."""
        now_m = _time.monotonic()
        last = self._oi_iv_warned.get(underlying)
        if last is None or now_m - last >= OI_IV_WARN_INTERVAL_SECS:
            self._oi_iv_warned[underlying] = now_m
            logger.warning("[%s] OI/IV context unavailable: %s", underlying, exc)

    async def _inject_oi_iv(self, underlying: str, indicators: dict,
                            is_expiry_day: bool = False) -> Optional[str]:
        """Update OI/IV context in OISpikeStrategy and IVCrushStrategy.

        The universe comes from ctx().instruments (design §14.2): the nearest
        expiry on or after the ctx trading day, then a pure ATM CE/PE pick.
        Only the Redis/DB I/O is guarded, and a failure logs at WARNING.

        Returns None when OI or IV was populated, else the cause, so the
        caller can count oi_iv_context_missing and record the DATA_GAP.
        """
        try:
            spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
            spot_ltp = await market().get_ltp(spot_token) or 0
            if not spot_ltp:
                return "no spot LTP"

            step = 50 if underlying == "NIFTY" else 100
            atm  = round(spot_ltp / step) * step

            async with sessions()() as db:
                universe = await ctx().instruments.instruments_for(db, underlying)
            today = trading_day()
            future = [i.expiry for i in universe if i.expiry and i.expiry >= today]
            if not future:
                return "no active expiry in the instrument universe"
            expiry = min(future)
            ce_inst, pe_inst = _atm_pair(universe, expiry, atm)

            ce_tick = await market().get_tick(ce_inst.token) if ce_inst else {}
            pe_tick = await market().get_tick(pe_inst.token) if pe_inst else {}
            # IV is NOT in the WebSocket binary feed — AngelOne only sends it via the
            # option chain REST snapshot.  Look it up from the option chain cache
            # (populated by study_collector).  Fall back to the tick "iv" field in
            # case a future feed version does carry it.
            chain = await market().get_option_chain(underlying, str(expiry))
        except Exception as e:      # Redis / DB I/O only
            self._warn_oi_iv(underlying, e)
            return f"I/O error: {type(e).__name__}: {e}"

        ce_oi   = _as_int((ce_tick or {}).get("oi", 0))
        pe_oi   = _as_int((pe_tick or {}).get("oi", 0))

        def _norm_iv(raw: float) -> float:
            """Normalise IV to percentage form (e.g. 0.15 → 15.0, 15.0 → 15.0)."""
            return raw * 100 if 0 < raw < 1.0 else raw

        chain_iv: dict[str, float] = {}
        if isinstance(chain, dict):
            for s in chain.get("strikes", []) or []:
                token_s = s.get("token")
                raw_iv  = _as_float(s.get("iv"))
                if token_s and raw_iv:
                    chain_iv[token_s] = raw_iv * 100 if raw_iv < 1.0 else raw_iv

        def _get_iv(inst, tick) -> float:
            """Prefer option-chain IV; fall back to tick IV; normalise to %."""
            if inst and inst.token in chain_iv:
                return chain_iv[inst.token]
            return _norm_iv(_as_float((tick or {}).get("iv", 0)))

        atm_ce_iv = _get_iv(ce_inst, ce_tick)
        atm_pe_iv = _get_iv(pe_inst, pe_tick)
        atm_iv  = (atm_ce_iv + atm_pe_iv) / 2 if atm_ce_iv and atm_pe_iv else atm_ce_iv or atm_pe_iv

        for strat in self._strategies.get(underlying, []):
            if isinstance(strat, OISpikeStrategy):
                strat.update_oi(ce_oi, pe_oi)
                strat.is_expiry_day = is_expiry_day   # propagate expiry flag
            if isinstance(strat, IVCrushStrategy) and atm_iv:
                strat.update_iv(atm_iv)
                logger.debug("[%s] IVCrush ATM IV=%.2f%% (ce=%.2f pe=%.2f)",
                             underlying, atm_iv, atm_ce_iv, atm_pe_iv)
        if not (ce_oi or pe_oi or atm_iv):
            return "no OI or IV for the ATM pair"
        return None

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
