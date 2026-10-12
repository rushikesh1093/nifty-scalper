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
from datetime import datetime, date, time as dtime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

from app.strategies.strategies import build_strategies, OISpikeStrategy, IVCrushStrategy, PDHPDLStrategy, CPRVWAPStrategy, GapVWAPFillStrategy
from app.strategies.indicators import compute_all, cpr as calc_cpr
from app.strategies.base import SignalBus
from app.services.cache.redis_client import redis_cache
from app.services.trading.paper_engine import paper_engine
from app.services.trading.mode_manager import mode_manager
from app.core.config import settings
from app.services.trading.regime_detector import regime_detector
from app.services.trading.strategy_orchestrator import strategy_orchestrator

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
ATR_MIN_NIFTY     = 1.5    # Nifty 1-min ATR below this = dead chop (was 2.5 — blocked ORB window)
ATR_MIN_BANKNIFTY = 5.0    # BankNifty moves ~3× Nifty (was 8.0)
MIN_TODAY_CANDLES = 25     # today-only 1m candles required before strategies run
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

    async def start(self):
        self._running = True

        # Wire paper engine to signal bus
        SignalBus.subscribe(paper_engine.handle_signal)

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
        await paper_engine.stop()
        for t in [self._run_task, self._eod_task]:
            if t: t.cancel()

    # ── Strategy enable/disable runtime ──────────────────────────────────────

    async def _get_enabled_flags(self) -> dict:
        cfg = await mode_manager.get_config()
        return cfg.strategy_flags or {} if cfg else {}

    # ── Pre-market setup ──────────────────────────────────────────────────────

    async def _pre_market_setup(self):
        """
        Fetch previous day OHLC and set PDH/PDL, CPR, prev_close on strategies.
        Called on startup and at 9:00 daily. Also resets the daily trade counter.
        """
        # Reset daily counter so yesterday's count doesn't block today's trades
        await mode_manager.reset_daily_counter()
        # Also reconcile used_capital against actual open positions to fix any
        # drift caused by server restarts mid-session.
        await mode_manager.reconcile_used_capital()
        # Reset the signal aggregator cooldown map so yesterday's last-acted
        # timestamps don't suppress the first valid signals of the new session.
        # Without this, a signal fired at 14:55 yesterday would still be in
        # cooldown at 9:16 today (90s same-direction cooldown is fine intraday
        # but must not carry across calendar days).
        from app.strategies.base import signal_aggregator
        signal_aggregator.reset()
        logger.info("Daily trades counter reset and signal aggregator cleared for new session.")
        from app.db.base import AsyncSessionLocal
        from app.db.models.market_study import MarketDailySnapshot
        from sqlalchemy import select, desc

        for underlying in ["NIFTY", "BANKNIFTY"]:
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(MarketDailySnapshot)
                        .where(
                            MarketDailySnapshot.underlying == underlying,
                            MarketDailySnapshot.date < self._ist_now().date(),
                        )
                        .order_by(desc(MarketDailySnapshot.date))
                        .limit(1)
                    )
                    prev = result.scalar_one_or_none()

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
                ist      = self._ist_now()
                ist_mins = ist.hour * 60 + ist.minute

                # Only run during the signal window (settings.SESSION_*_IST, 9:15 – 15:25)
                start_t, end_t = settings.SESSION_START_IST, settings.SESSION_END_IST
                if not (start_t.hour * 60 + start_t.minute <= ist_mins <= end_t.hour * 60 + end_t.minute):
                    continue

                flags = await self._get_enabled_flags()

                for underlying in ["NIFTY", "BANKNIFTY"]:
                    await self._run_underlying(underlying, flags)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Candle loop error: %s", e, exc_info=True)

    @staticmethod
    def _ist_now() -> datetime:
        """Return current datetime in IST (UTC+5:30), timezone-aware."""
        return datetime.now(IST)

    @staticmethod
    def _candle_timestamp(value) -> Optional[datetime]:
        """Parse feed timestamps and return a timezone-aware datetime in IST."""
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        parsed = None
                if parsed is None:
                    return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(IST)

    async def _run_underlying(self, underlying: str, flags: dict):
        spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN

        # Get candles from Redis — fetch extra in case window spans overnight,
        # then filter to today's IST session so ORB levels are always correct.
        all_candles = await redis_cache.get_candles(spot_token, "ONE_MINUTE", 500)
        if not all_candles:
            return

        today = self._ist_now().date()
        session_open = datetime.combine(today, dtime(9, 15), tzinfo=IST)
        # Normalize every feed timestamp to IST before filtering. Redis may contain
        # naive IST strings, UTC strings, or ISO-8601 strings with an offset; a
        # string-prefix/date check misclassifies UTC candles near midnight and can
        # leave the strategy with no valid candles.
        candles = []
        for c in all_candles:
            ts = self._candle_timestamp(c.get("timestamp"))
            if ts is None or ts.date() != today or ts < session_open:
                continue
            candles.append(c)

        candles.sort(key=lambda candle: self._candle_timestamp(candle.get("timestamp")) or session_open)

        if len(candles) < MIN_TODAY_CANDLES:
            # Not enough today-only candles yet — market just opened or no data.
            # Do NOT fall back to multi-day candles: that would corrupt ORB high/low
            # (computed from today's first 15 candles) and the intraday VWAP reset.
            logger.debug(
                "[%s] Only %d today-candles available (need %d) — skipping this cycle",
                underlying, len(candles), MIN_TODAY_CANDLES,
            )
            return

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
        prev_avg    = await redis_cache.get(avg_vol_key) or 0.0
        if isinstance(prev_avg, dict):   # guard: shouldn't happen but be safe
            prev_avg = 0.0
        prev_avg = float(prev_avg)
        if cur_volume > 0:
            # Only update EMA when we have real volume data
            if prev_avg <= 0:
                new_avg = cur_volume
            else:
                new_avg = round(0.2 * cur_volume + 0.8 * prev_avg, 2)   # α=0.2
            await redis_cache.set(avg_vol_key, new_avg, ttl=28800)
            indicators["avg_volume"] = new_avg
        else:
            # Volume=0 (NSE index feed limitation) — don't clobber the EMA.
            # Keep whatever was stored from a prior cycle if available.
            indicators["avg_volume"] = prev_avg if prev_avg > 0 else None

        # ── Gate 1: ATR filter — skip if market is too choppy / dead ─────────
        atr_min = ATR_MIN_NIFTY if underlying == "NIFTY" else ATR_MIN_BANKNIFTY
        atr_v   = indicators.get("atr") or 0
        if atr_v < atr_min:
            logger.debug(
                "[%s] ATR=%.2f < %.1f — market too choppy, skipping all strategies",
                underlying, atr_v, atr_min,
            )
            return

        # ── Gate 2: VIX filter ────────────────────────────────────────────────
        vix_ltp = await redis_cache.get_ltp(VIX_TOKEN)
        if vix_ltp is not None:
            if vix_ltp > VIX_MAX:
                logger.info(
                    "[%s] VIX=%.1f > %.0f — high-risk environment, skipping strategies",
                    underlying, vix_ltp, VIX_MAX,
                )
                return
            if vix_ltp < VIX_MIN:
                logger.debug(
                    "[%s] VIX=%.1f < %.0f — options too cheap / no movement expected",
                    underlying, vix_ltp, VIX_MIN,
                )
                return

        # ── Gate 3: Expiry day filter ─────────────────────────────────────────
        from app.db.base import AsyncSessionLocal
        from app.services.broker.instrument_sync import get_active_expiries
        async with AsyncSessionLocal() as db:
            expiries = await get_active_expiries(db, underlying)
        is_expiry_day = bool(expiries and expiries[0] == self._ist_now().date())
        if is_expiry_day:
            logger.info("[%s] Expiry day — strategies with sl_pct < %.2f will be skipped",
                        underlying, EXPIRY_MIN_SL_PCT)

        # ── Inject OI / IV into relevant strategies ───────────────────────────
        # Pass is_expiry_day so OISpikeStrategy can self-block (it also checks
        # this flag internally, but setting it here ensures the state is always
        # current before evaluate() is called this cycle).
        await self._inject_oi_iv(underlying, indicators, is_expiry_day=is_expiry_day)

        # ── Regime detection ─────────────────────────────────────────────────────
        # Detect current market regime, get updated strategy flags from orchestrator.
        # user-disable takes precedence: a user-disabled strategy stays disabled
        # regardless of what regime says.
        try:
            regime_state = await regime_detector.detect_regime(
                candles=candles,
                indicators=indicators,
                underlying=underlying,
                is_expiry_day=is_expiry_day,
            )
            regime_flags = await strategy_orchestrator.on_regime_detected(regime_state)
            # Merge: user flag AND regime flag must both be True to enable
            final_flags = {
                name: regime_flags.get(name, True) and flags.get(name, True)
                for name in set(list(flags.keys()) + list(regime_flags.keys()))
            }
        except Exception as _regime_err:
            logger.warning("[%s] Regime detection error: %s — falling back to user flags", underlying, _regime_err)
            final_flags = flags
            regime_state = None

        # ── Observability only — isolated; cannot influence final_flags ───────
        if regime_state is not None:
            await self._persist_regime_state(underlying, regime_state)

        # ── Run strategies ────────────────────────────────────────────────────
        for strat in self._strategies.get(underlying, []):
            if not final_flags.get(strat.name, True):
                continue
            if strategy_orchestrator.is_in_cooldown(underlying):
                logger.debug("[%s] Transition cooldown active — skipping %s", underlying, strat.name)
                continue
            if is_expiry_day and strat.sl_pct < EXPIRY_MIN_SL_PCT:
                logger.debug(
                    "[%s] Expiry day — skipping %s (sl_pct=%.2f < %.2f)",
                    underlying, strat.name, strat.sl_pct, EXPIRY_MIN_SL_PCT,
                )
                continue
            try:
                await strat.on_candle(candles, indicators)
            except Exception as e:
                logger.error("[%s] Strategy error: %s", strat.name, e, exc_info=True)

    async def _persist_regime_state(self, underlying: str, regime_state) -> None:
        """
        Persist one regime detection to Postgres so /regime/history accumulates.
        Runs after final_flags is computed and outside the regime-gating try, so a
        DB outage (session acquisition or commit failure) can never reset the
        gating to raw user flags. Never raises.
        """
        try:
            from app.db.base import AsyncSessionLocal                    # file convention: function-local
            from app.services.trading.regime_persistence import regime_persistence
            async with AsyncSessionLocal() as db:
                await regime_persistence.persist_to_db(regime_state, db)
                await db.commit()
        except Exception as exc:
            logger.warning("[%s] regime persist failed (non-fatal): %s", underlying, exc)

    async def _inject_oi_iv(self, underlying: str, indicators: dict, is_expiry_day: bool = False):
        """Update OI/IV context in OISpikeStrategy and IVCrushStrategy."""
        try:
            spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
            spot_ltp = await redis_cache.get_ltp(spot_token) or 0
            if not spot_ltp:
                return

            step = 50 if underlying == "NIFTY" else 100
            atm  = round(spot_ltp / step) * step

            from app.db.base import AsyncSessionLocal
            from app.db.models.instrument import Instrument, InstrumentType, OptionType
            from sqlalchemy import select
            from app.services.broker.instrument_sync import get_active_expiries

            async with AsyncSessionLocal() as db:
                expiries = await get_active_expiries(db, underlying)
                if not expiries:
                    return
                expiry = expiries[0]

                ce_r = await db.execute(
                    select(Instrument).where(
                        Instrument.name == underlying,
                        Instrument.instrument_type == InstrumentType.OPTIDX,
                        Instrument.option_type == OptionType.CE,
                        Instrument.expiry == expiry,
                        Instrument.strike == atm,
                        Instrument.is_active == True,
                    ).limit(1)
                )
                ce_inst = ce_r.scalar_one_or_none()
                pe_r = await db.execute(
                    select(Instrument).where(
                        Instrument.name == underlying,
                        Instrument.instrument_type == InstrumentType.OPTIDX,
                        Instrument.option_type == OptionType.PE,
                        Instrument.expiry == expiry,
                        Instrument.strike == atm,
                        Instrument.is_active == True,
                    ).limit(1)
                )
                pe_inst = pe_r.scalar_one_or_none()

            ce_tick = await redis_cache.get_tick(ce_inst.token) if ce_inst else {}
            pe_tick = await redis_cache.get_tick(pe_inst.token) if pe_inst else {}
            ce_oi   = int((ce_tick or {}).get("oi", 0))
            pe_oi   = int((pe_tick or {}).get("oi", 0))

            def _norm_iv(raw: float) -> float:
                """Normalise IV to percentage form (e.g. 0.15 → 15.0, 15.0 → 15.0)."""
                return raw * 100 if 0 < raw < 1.0 else raw

            # IV is NOT in the WebSocket binary feed — AngelOne only sends it via the
            # option chain REST snapshot.  Look it up from the option chain cache
            # (populated by study_collector).  Fall back to the tick "iv" field in
            # case a future feed version does carry it.
            chain_iv: dict[str, float] = {}
            chain = await redis_cache.get_option_chain(underlying, str(expiry))
            if chain:
                for s in chain.get("strikes", []):
                    token_s = s.get("token")
                    raw_iv  = float(s.get("iv") or 0)
                    if token_s and raw_iv:
                        chain_iv[token_s] = raw_iv * 100 if raw_iv < 1.0 else raw_iv

            def _get_iv(inst, tick) -> float:
                """Prefer option-chain IV; fall back to tick IV; normalise to %."""
                if inst and inst.token in chain_iv:
                    return chain_iv[inst.token]
                raw = float((tick or {}).get("iv", 0))
                return _norm_iv(raw)

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

        except Exception as e:
            logger.debug("OI/IV inject error: %s", e)

    # ── EOD monitor ───────────────────────────────────────────────────────────

    async def _eod_monitor(self):
        """At 15:15 IST, force-close all open positions and do pre-market setup for next day."""
        while self._running:
            try:
                ist   = self._ist_now()
                ist_h = ist.hour
                ist_m = ist.minute

                # 15:15 IST = 09:45 UTC
                if ist_h == 15 and ist_m == 15:
                    logger.info("EOD: Force-closing all open positions at 15:15 IST")
                    await self._eod_square_off()
                    await asyncio.sleep(120)   # avoid double trigger

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
        from app.db.base import AsyncSessionLocal
        from app.db.models.paper_trade import PaperPosition
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(PaperPosition).where(PaperPosition.is_open == True)
            )
            open_positions = result.scalars().all()

        for pos in open_positions:
            try:
                ltp = await redis_cache.get_ltp(pos.token)
                if not ltp:
                    # No live price — fall back to entry price so the position
                    # is still closed (avoids permanent capital lock on restart).
                    # Log a warning so the P&L impact is visible.
                    fallback = pos.entry_price or pos.actual_entry
                    if fallback:
                        logger.warning(
                            "EOD: No live LTP for pos %d token %s — "
                            "closing at entry price %.2f to release capital",
                            pos.id, pos.token, fallback,
                        )
                        ltp = fallback
                    else:
                        logger.error(
                            "EOD: Cannot close pos %d — no LTP and no entry price available",
                            pos.id,
                        )
                        continue
                await paper_engine._close_position(pos.id, ltp, "EOD")
            except Exception as e:
                logger.error("EOD close error for pos %d: %s", pos.id, e)

        logger.info("EOD square-off complete. Closed %d positions", len(open_positions))


# Singleton
strategy_runner = StrategyRunner()
