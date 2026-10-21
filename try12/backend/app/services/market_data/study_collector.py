"""
Market Study Data Collector.
Runs during the 20-22 day observation period.
  - Every 5 minutes: captures intraday feature snapshot
  - Every 5 minutes: refreshes option chain REST snapshot → Redis (populates IV)
  - EOD: builds daily summary (OI, PCR, VIX, max pain, OHLC)
"""
import asyncio
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import httpx

from app.db.base import AsyncSessionLocal
from app.db.models.market_study import IntradayMarketFeature, MarketDailySnapshot, VIXData
from app.services.broker.angel_client import angel_client
from app.services.cache.redis_client import redis_cache
from app.strategies.indicators import compute_all, cpr
from app.core.config import settings
from app.runtime.context import now_utc, trading_day
from app.services.market_data import chain_snapshots as cs
from app.services.trading import session_times
from app.services.trading import market_calendar
from app.services.market_data.timestamps import try_normalize_candle_timestamp

# A daily snapshot needs a real session behind it (a full day is 375 candles).
MIN_SNAPSHOT_SESSION_CANDLES = 60
_IST = timezone(timedelta(hours=5, minutes=30))


def session_candles_for_day(candles: list, day: date) -> list:
    """The candles of ``day``'s 09:15–15:29 IST session, one per minute, in time
    order (the last copy of a duplicated minute wins)."""
    by_minute: dict = {}
    for c in candles or []:
        ts = try_normalize_candle_timestamp(c.get("timestamp"))
        if ts is None:
            continue
        ts = ts.astimezone(_IST)
        if ts.date() != day or not ((9, 15) <= (ts.hour, ts.minute) <= (15, 29)):
            continue
        by_minute[ts.replace(second=0, microsecond=0)] = c
    return [by_minute[k] for k in sorted(by_minute)]

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL      = 300    # 5 minutes in seconds
OPTION_CHAIN_INTERVAL  = 300    # refresh option chain REST snapshot every 5 minutes
VIX_TOKEN              = settings.VIX_TOKEN  # India VIX token (centralised in config)


class StudyCollector:

    def __init__(self, chain_source=None):
        self._running            = False
        # Option-chain source: async (name, expiry_str, strike_str) -> raw chain.
        # None = angel_client.get_option_chain, resolved at call time.
        self._chain_source       = chain_source
        self._chain_fail_streak  = 0     # consecutive intervals with a failing underlying
        self._chain_lock:       Optional[asyncio.Lock] = None
        self._snap_task:        Optional[asyncio.Task] = None
        self._eod_task:         Optional[asyncio.Task] = None
        self._chain_task:       Optional[asyncio.Task] = None
        # Track prev OI for change calculation
        self._prev_ce_oi:  dict[str, int] = {}   # underlying → oi
        self._prev_pe_oi:  dict[str, int] = {}

    async def start(self):
        self._running = True
        self._snap_task  = asyncio.create_task(self._snapshot_loop())
        self._eod_task   = asyncio.create_task(self._eod_loop())
        self._chain_task = asyncio.create_task(self._option_chain_loop())
        # If the backend restarted after 15:35 IST, the EOD window was missed.
        # Catch up immediately so today's summary isn't lost.
        asyncio.create_task(self._catchup_eod())
        # Seed option chains immediately on startup (don't wait 5 minutes for first IV)
        asyncio.create_task(self._refresh_all_option_chains())
        logger.info("StudyCollector started.")

    async def _catchup_eod(self):
        """
        Run once at startup: if it's already past 15:35 IST and today's
        daily summary is missing, save it now rather than waiting for tomorrow.
        """
        try:
            ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            if not (ist.hour > 15 or (ist.hour == 15 and ist.minute >= 35)):
                return  # Not past EOD time yet, normal loop will handle it
            today = date.today()
            async with AsyncSessionLocal() as db:
                for underlying in ["NIFTY", "BANKNIFTY"]:
                    from sqlalchemy import select as sa_select
                    from app.db.models.market_study import MarketDailySnapshot as MDS
                    exists = await db.execute(
                        sa_select(MDS.id).where(
                            MDS.date == today,
                            MDS.underlying == underlying,
                            MDS.close.isnot(None),
                        ).limit(1)
                    )
                    if not exists.scalar_one_or_none():
                        logger.info("Catch-up: saving missing EOD summary for %s %s", underlying, today)
                        await self._save_daily_summary(underlying)
            await self._fetch_vix()
        except Exception as exc:
            logger.error("EOD catch-up error: %s", exc, exc_info=True)

    async def stop(self):
        self._running = False
        for t in [self._snap_task, self._eod_task, self._chain_task]:
            if t: t.cancel()

    # ── Intraday snapshot loop ────────────────────────────────────────────────

    async def _snapshot_loop(self):
        while self._running:
            try:
                ist      = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                ist_mins = ist.hour * 60 + ist.minute

                # Only during market hours 9:15 – 15:30
                if 9 * 60 + 15 <= ist_mins <= 15 * 60 + 30:
                    for underlying in ["NIFTY", "BANKNIFTY"]:
                        await self._take_snapshot(underlying)
                else:
                    logger.debug("StudyCollector: outside market hours, sleeping")

                await asyncio.sleep(SNAPSHOT_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Snapshot error: %s", e, exc_info=True)
                await asyncio.sleep(30)

    async def _take_snapshot(self, underlying: str):
        try:
            # Spot LTP
            spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
            spot_tick  = await redis_cache.get_tick(spot_token)
            if not spot_tick:
                return
            spot_ltp = float(spot_tick.get("ltp", 0))
            if not spot_ltp:
                return

            # Get 1m candles for indicator calculation
            candles = await redis_cache.get_candles(spot_token, "ONE_MINUTE", 200)
            indicators = compute_all(candles) if len(candles) > 25 else {}

            # ATM strike
            step = 50 if underlying == "NIFTY" else 100
            atm_strike = round(spot_ltp / step) * step

            # ATM CE / PE LTP from Redis
            atm_ce_tick = None; atm_pe_tick = None
            from sqlalchemy import select
            from app.db.models.instrument import Instrument, InstrumentType, OptionType

            async with AsyncSessionLocal() as db:
                # Nearest expiry
                from app.services.broker.instrument_sync import get_active_expiries
                expiries = await get_active_expiries(db, underlying, as_of=trading_day())
                if not expiries:
                    return
                expiry = expiries[0]

                # ATM CE token
                ce_res = await db.execute(
                    select(Instrument).where(
                        Instrument.name == underlying,
                        Instrument.instrument_type == InstrumentType.OPTIDX,
                        Instrument.option_type == OptionType.CE,
                        Instrument.expiry == expiry,
                        Instrument.strike == atm_strike,
                        Instrument.is_active == True,
                    ).limit(1)
                )
                ce_inst = ce_res.scalar_one_or_none()

                pe_res = await db.execute(
                    select(Instrument).where(
                        Instrument.name == underlying,
                        Instrument.instrument_type == InstrumentType.OPTIDX,
                        Instrument.option_type == OptionType.PE,
                        Instrument.expiry == expiry,
                        Instrument.strike == atm_strike,
                        Instrument.is_active == True,
                    ).limit(1)
                )
                pe_inst = pe_res.scalar_one_or_none()

            if ce_inst:
                atm_ce_tick = await redis_cache.get_tick(ce_inst.token) or {}
            if pe_inst:
                atm_pe_tick = await redis_cache.get_tick(pe_inst.token) or {}

            ce_ltp = float((atm_ce_tick or {}).get("ltp", 0))
            pe_ltp = float((atm_pe_tick or {}).get("ltp", 0))
            ce_iv  = float((atm_ce_tick or {}).get("iv", 0))
            pe_iv  = float((atm_pe_tick or {}).get("iv", 0))
            ce_oi  = int((atm_ce_tick or {}).get("oi", 0))
            pe_oi  = int((atm_pe_tick or {}).get("oi", 0))
            ce_bid = float((atm_ce_tick or {}).get("best_bid", 0))
            ce_ask = float((atm_ce_tick or {}).get("best_ask", 0))
            pe_bid = float((atm_pe_tick or {}).get("best_bid", 0))
            pe_ask = float((atm_pe_tick or {}).get("best_ask", 0))

            # OI change since last snapshot
            ce_oi_chg = ce_oi - self._prev_ce_oi.get(underlying, ce_oi)
            pe_oi_chg = pe_oi - self._prev_pe_oi.get(underlying, pe_oi)
            self._prev_ce_oi[underlying] = ce_oi
            self._prev_pe_oi[underlying] = pe_oi

            vwap_v = indicators.get("vwap")
            spot_vs_vwap = round((spot_ltp - vwap_v) / vwap_v * 100, 3) if vwap_v else None

            # 5m and 15m return
            spot_5m = spot_15m = None
            if len(candles) >= 5:
                p5  = float(candles[-5].get("close", spot_ltp))
                spot_5m = round((spot_ltp - p5) / p5 * 100, 3)
            if len(candles) >= 15:
                p15 = float(candles[-15].get("close", spot_ltp))
                spot_15m = round((spot_ltp - p15) / p15 * 100, 3)

            # PCR
            total_ce = ce_oi; total_pe = pe_oi   # ATM only (full chain better)
            pcr = round(total_pe / total_ce, 3) if total_ce > 0 else None

            # VIX
            vix_tick = await redis_cache.get_tick(VIX_TOKEN)
            vix_ltp  = float(vix_tick.get("ltp", 0)) if vix_tick else None

            # ORB levels
            orb_h = indicators.get("orb_high"); orb_l = indicators.get("orb_low")
            orb_broken = indicators.get("orb_broken", "NONE")

            feature = IntradayMarketFeature(
                snapshot_time    = datetime.now(timezone.utc),  # timezone-aware UTC
                underlying       = underlying,
                spot_ltp         = spot_ltp,
                spot_change_pct  = None,
                vwap             = vwap_v,
                spot_vs_vwap_pct = spot_vs_vwap,
                atm_strike       = atm_strike,
                atm_ce_ltp       = ce_ltp,
                atm_pe_ltp       = pe_ltp,
                atm_ce_iv        = ce_iv or None,
                atm_pe_iv        = pe_iv or None,
                atm_straddle_val = round(ce_ltp + pe_ltp, 2) if ce_ltp and pe_ltp else None,
                atm_ce_spread    = round(ce_ask - ce_bid, 2) if ce_ask and ce_bid else None,
                atm_pe_spread    = round(pe_ask - pe_bid, 2) if pe_ask and pe_bid else None,
                atm_ce_volume    = int((atm_ce_tick or {}).get("volume", 0)) or None,
                atm_pe_volume    = int((atm_pe_tick or {}).get("volume", 0)) or None,
                ce_oi_change     = ce_oi_chg,
                pe_oi_change     = pe_oi_chg,
                pcr_oi           = pcr,
                spot_5m_return   = spot_5m,
                spot_15m_return  = spot_15m,
                vix_snapshot     = vix_ltp,
                ema9             = indicators.get("ema9"),
                ema21            = indicators.get("ema21"),
                rsi14            = indicators.get("rsi"),
                supertrend       = indicators.get("supertrend"),
                supertrend_dir   = "UP" if indicators.get("supertrend_dir", 0) == 1 else "DOWN",
                bb_upper         = indicators.get("bb_upper"),
                bb_lower         = indicators.get("bb_lower"),
                bb_mid           = indicators.get("bb_mid"),
                orb_high         = orb_h,
                orb_low          = orb_l,
                orb_broken       = orb_broken,
            )

            async with AsyncSessionLocal() as db:
                db.add(feature)
                await db.commit()

            logger.debug("Snapshot saved: %s spot=%.1f vwap=%.1f", underlying, spot_ltp, vwap_v or 0)

        except Exception as e:
            logger.error("Snapshot error for %s: %s", underlying, e, exc_info=True)

    # ── Option chain REST fetch loop ──────────────────────────────────────────

    async def _option_chain_loop(self):
        """
        Every 5 minutes during market hours, fetch the AngelOne option chain REST
        snapshot for NIFTY and BANKNIFTY, write it to Redis and persist it to
        option_chain_snapshots (the replay engine's chain/mark source).

        This is the ONLY source of IV data in the system — the WebSocket binary
        feed does not carry IV.  Without this loop:
          - IVCrushStrategy always sees atm_iv=0 and never fires
          - OptionSelector delta scoring uses fallback iv=20% for all strikes
          - strategy_runner._inject_oi_iv() always gets chain_iv={}

        The REST option chain also provides per-strike OI, bid/ask, and Greeks
        which improve option selector scoring quality.
        """
        while self._running:
            try:
                ist      = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                ist_mins = ist.hour * 60 + ist.minute

                # Only during market hours 9:15 – 15:30
                if 9 * 60 + 15 <= ist_mins <= 15 * 60 + 30:
                    await self._refresh_all_option_chains()
                else:
                    logger.debug("OptionChainLoop: outside market hours, sleeping")

                await asyncio.sleep(OPTION_CHAIN_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Option chain loop error: %s", e, exc_info=True)
                await asyncio.sleep(60)

    async def _refresh_all_option_chains(self):
        """
        Fetch, cache and persist option chains for both underlyings. Never raises.

        Logging per call (one call per 300 s interval): exactly one WARNING when
        any underlying failed, e.g.
          option-chain snapshot: NIFTY=EMPTY_BODY BANKNIFTY=EMPTY_BODY
          (consecutive failing intervals: 3); next attempt in 300 s
        per-underlying failure detail at DEBUG, one INFO on the first fully
        successful interval after failures, and one INFO per successful
        underlying with strikes and rows written.
        """
        if self._chain_lock is None:
            self._chain_lock = asyncio.Lock()
        # The startup seed and the loop's first pass run concurrently; serialise
        # them so the bucket check-then-insert in persist_chain_snapshot holds.
        async with self._chain_lock:
            try:
                now = now_utc()
            except Exception:
                now = datetime.now(timezone.utc)
            outcomes = []
            for underlying in ["NIFTY", "BANKNIFTY"]:
                try:
                    outcomes.append(await self._fetch_option_chain(underlying, now=now))
                except Exception as e:      # belt: _fetch_option_chain does not raise
                    outcomes.append(cs.ChainFetchOutcome(underlying, cs.ERROR, repr(e)))

            failed = [o for o in outcomes if o.failed]
            for o in failed:
                logger.debug("option-chain snapshot %s: %s %s", o.underlying, o.status, o.detail)
            if failed:
                self._chain_fail_streak += 1
                logger.warning(
                    "option-chain snapshot: %s (consecutive failing intervals: %d); "
                    "next attempt in %d s",
                    " ".join(f"{o.underlying}={o.status}" for o in outcomes),
                    self._chain_fail_streak, OPTION_CHAIN_INTERVAL,
                )
            elif self._chain_fail_streak:
                logger.info("option-chain snapshot recovered after %d failing intervals",
                            self._chain_fail_streak)
                self._chain_fail_streak = 0
            for o in outcomes:
                if not o.failed:
                    logger.info("Option chain cached: %s %s strikes=%d rows_written=%d%s",
                                o.underlying, o.detail, o.strikes, o.rows_written,
                                " (out of session: Redis only)"
                                if o.status == cs.OUT_OF_SESSION else "")
            return outcomes

    async def _fetch_option_chain(self, underlying: str,
                                  now: Optional[datetime] = None) -> cs.ChainFetchOutcome:
        """
        Fetch the AngelOne option chain REST snapshot for the nearest expiry,
        write it to Redis via redis_cache.set_option_chain() and, during market
        hours, persist it to option_chain_snapshots (one set per 300 s bucket).
        Never raises: every failure is returned as a ChainFetchOutcome status.

        Builds the chain dict in the same format consumed by:
          - strategy_runner._inject_oi_iv()  (reads chain["strikes"][*]["iv"])
          - option_selector._build_candidates()  (reads chain["strikes"][*]["token"/"iv"])
          - market.py GET /option-chain/{underlying}  (reads chain_snap["strikes"])

        Format written:
          {
            "underlying": "NIFTY",
            "expiry": "2024-09-26",
            "spot": 24500.0,
            "strikes": [
              {
                "token": "12345",
                "strike": 24500.0,
                "option_type": "CE",
                "ltp": 150.0,
                "bid": 149.5,
                "ask": 150.5,
                "oi": 500000,
                "volume": 12000,
                "iv": 18.5,    ← percentage form (18.5 = 18.5%)
              },
              ...
            ]
          }
        """
        from app.services.broker.instrument_sync import get_active_expiries

        Outcome = cs.ChainFetchOutcome
        if now is None:
            try:
                now = now_utc()
            except Exception:
                now = datetime.now(timezone.utc)

        # Get nearest expiry from DB
        try:
            async with AsyncSessionLocal() as db:
                expiries = await get_active_expiries(
                    db, underlying, as_of=session_times.trading_day(now))
        except Exception as exc:
            return Outcome(underlying, cs.DB_ERROR, f"expiry lookup: {exc!r}")
        if not expiries:
            return Outcome(underlying, cs.NO_EXPIRY, "no active expiry")
        expiry = expiries[0]

        # Get current spot LTP for ATM calculation
        spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
        try:
            spot_ltp = await redis_cache.get_ltp(spot_token) or 0.0
        except Exception as exc:
            logger.debug("_fetch_option_chain: spot LTP read failed for %s: %s", underlying, exc)
            spot_ltp = 0.0

        # ATM strike
        step    = 50 if underlying == "NIFTY" else 100
        atm     = round(spot_ltp / step) * step if spot_ltp else 0

        # Use AngelOne's option chain REST endpoint (requires login)
        # The endpoint accepts: name, expirydate (DD-Mon-YYYY), strikeprice
        source = self._chain_source or angel_client.get_option_chain
        try:
            expiry_str   = expiry.strftime("%d-%b-%Y").upper()  # e.g. "26-SEP-2024"
            strike_param = str(int(atm)) if atm else "0"
            raw_chain    = await source(underlying, expiry_str, strike_param)
        except Exception as exc:
            return Outcome(underlying, cs.classify_fetch_error(exc),
                           f"expiry={expiry}: {type(exc).__name__}: {exc}")

        try:
            strikes_out = cs.normalise_chain(raw_chain)
        except Exception as exc:
            return Outcome(underlying, cs.UNPARSEABLE, f"normalise failed: {exc!r}")
        if not strikes_out:
            return Outcome(
                underlying, cs.UNPARSEABLE,
                "no parseable strikes; keys=%s" % (
                    list(raw_chain.keys()) if isinstance(raw_chain, dict) else type(raw_chain)),
            )

        chain_doc = {
            "underlying": underlying,
            "expiry":     str(expiry),
            "spot":       spot_ltp,
            "strikes":    strikes_out,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await redis_cache.set_option_chain(underlying, str(expiry), chain_doc)
        except Exception as exc:
            logger.error("Option chain Redis write failed for %s: %s", underlying, exc)

        detail = f"expiry={expiry} spot={spot_ltp:.1f}"
        n = len(strikes_out)
        if not session_times.is_market_hours(now.astimezone(session_times.IST).time()):
            return Outcome(underlying, cs.OUT_OF_SESSION, detail, strikes=n)
        if spot_ltp <= 0:
            return Outcome(underlying, cs.NO_SPOT, detail, strikes=n)
        try:
            rows = await cs.persist_chain_snapshot(
                AsyncSessionLocal, underlying=underlying, expiry=expiry, spot=spot_ltp,
                strikes=strikes_out, snapshot_time=cs.snapshot_bucket(now, OPTION_CHAIN_INTERVAL),
            )
        except Exception as exc:
            return Outcome(underlying, cs.DB_ERROR, f"{detail}: {exc!r}", strikes=n)
        return Outcome(underlying, cs.OK, detail, strikes=n, rows_written=rows)

    # ── EOD summary loop ──────────────────────────────────────────────────────

    async def _eod_loop(self):
        """Save daily summary at 15:35 IST."""
        while self._running:
            try:
                ist   = datetime.now(timezone(timedelta(hours=5, minutes=30)))
                ist_h = ist.hour
                ist_m = ist.minute

                if ist_h == 15 and 35 <= ist_m <= 36:
                    for underlying in ["NIFTY", "BANKNIFTY"]:
                        await self._save_daily_summary(underlying)
                    await self._fetch_vix()
                    await asyncio.sleep(120)   # avoid double-save

                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("EOD loop error: %s", e, exc_info=True)
                await asyncio.sleep(60)

    async def _save_daily_summary(self, underlying: str):
        try:
            today = date.today()
            closed = market_calendar.closed_reason(today)
            if closed:
                logger.info("EOD summary skipped for %s: exchange closed (%s)", underlying, closed)
                return
            spot_token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN

            # Today's SESSION candles only. The cache holds the last 500 one-minute
            # candles, which span the previous session (and stray off-hours
            # snapshot candles); using them all put yesterday's open / high / low
            # / ORB into today's snapshot, which becomes tomorrow's PDH/PDL/CPR.
            raw = await redis_cache.get_candles(spot_token, "ONE_MINUTE", 500)
            candles = session_candles_for_day(raw, today)
            if len(candles) < MIN_SNAPSHOT_SESSION_CANDLES:
                logger.warning(
                    "EOD summary skipped for %s: only %d session candles on %s "
                    "(holiday, feed down or late start) — no snapshot written",
                    underlying, len(candles), today)
                return

            opens  = [float(c.get("open",  0)) for c in candles if c.get("open")]
            highs  = [float(c.get("high",  0)) for c in candles if c.get("high")]
            lows   = [float(c.get("low",   0)) for c in candles if c.get("low")]
            closes = [float(c.get("close", 0)) for c in candles if c.get("close")]
            volumes= [float(c.get("volume",0)) for c in candles if c.get("volume")]

            if not closes or max(highs) <= min(lows):
                logger.warning("EOD summary skipped for %s: degenerate OHLC", underlying)
                return

            day_open  = opens[0]  if opens  else closes[0]
            day_high  = max(highs)  if highs  else closes[-1]
            day_low   = min(lows)   if lows   else closes[-1]
            day_close = closes[-1]

            # ORB
            orb_h = max(highs[:15]) if len(highs) >= 15 else day_high
            orb_l = min(lows[:15])  if len(lows)  >= 15 else day_low

            # VWAP at close
            import numpy as np
            h_arr = np.array(highs); l_arr = np.array(lows)
            c_arr = np.array(closes); v_arr = np.array(volumes) if volumes else np.ones(len(closes))
            from app.strategies.indicators import vwap as calc_vwap
            vwap_arr = calc_vwap(h_arr, l_arr, c_arr, v_arr)
            vwap_close = float(vwap_arr[-1]) if len(vwap_arr) > 0 else None

            # Get OI data from latest intraday feature
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select, desc
                # Fetch last intraday feature saved today (IST date = UTC date + 5:30)
                # Use timezone-aware bounds to match the snapshot_time column correctly.
                today_ist_start = datetime.combine(today, datetime.min.time()).replace(
                    tzinfo=timezone(timedelta(hours=5, minutes=30))
                )
                feat_r = await db.execute(
                    select(IntradayMarketFeature)
                    .where(
                        IntradayMarketFeature.underlying == underlying,
                        IntradayMarketFeature.snapshot_time >= today_ist_start,
                    )
                    .order_by(desc(IntradayMarketFeature.snapshot_time))
                    .limit(1)
                )
                last_feat = feat_r.scalar_one_or_none()

                # Prev day close
                prev_r = await db.execute(
                    select(MarketDailySnapshot)
                    .where(
                        MarketDailySnapshot.underlying == underlying,
                        MarketDailySnapshot.date < today,
                    )
                    .order_by(desc(MarketDailySnapshot.date)).limit(1)
                )
                prev = prev_r.scalar_one_or_none()
                prev_close = prev.close if prev else day_open

                # Expiry info
                from app.services.broker.instrument_sync import get_active_expiries
                expiries = await get_active_expiries(db, underlying, as_of=trading_day())
                nearest_expiry = expiries[0] if expiries else None
                dte = (nearest_expiry - today).days if nearest_expiry else None

                # Upsert daily snapshot
                existing_r = await db.execute(
                    select(MarketDailySnapshot).where(
                        MarketDailySnapshot.date == today,
                        MarketDailySnapshot.underlying == underlying,
                    )
                )
                existing = existing_r.scalar_one_or_none()

                vals = dict(
                    date=today, underlying=underlying,
                    open=day_open, high=day_high, low=day_low, close=day_close,
                    prev_close=prev_close,
                    change_pct=round((day_close - prev_close) / prev_close * 100, 2) if prev_close else None,
                    orb_high=orb_h, orb_low=orb_l, vwap_close=vwap_close,
                    pdh=prev.high if prev else None,
                    pdl=prev.low  if prev else None,
                    atm_strike=round(day_close / (50 if underlying=="NIFTY" else 100)) * (50 if underlying=="NIFTY" else 100),
                    nearest_expiry=nearest_expiry,
                    days_to_expiry=dte,
                    pcr_oi=last_feat.pcr_oi if last_feat else None,
                    trend="BULLISH" if day_close > vwap_close else "BEARISH" if vwap_close else "SIDEWAYS",
                )

                if existing:
                    for k, v in vals.items():
                        setattr(existing, k, v)
                else:
                    snap = MarketDailySnapshot(**vals)
                    db.add(snap)
                await db.commit()

            logger.info("EOD summary saved for %s: close=%.1f", underlying, day_close)

        except Exception as e:
            logger.error("EOD summary error for %s: %s", underlying, e, exc_info=True)

    async def _fetch_vix(self):
        """Fetch India VIX from Redis (already subscribed in feed)."""
        try:
            vix_tick = await redis_cache.get_tick(VIX_TOKEN)
            if not vix_tick:
                return
            today = date.today()
            async with AsyncSessionLocal() as db:
                from sqlalchemy import select
                exists_r = await db.execute(
                    select(VIXData).where(VIXData.date == today)
                )
                if exists_r.scalar_one_or_none():
                    return
                vix = VIXData(
                    date=today,
                    close=float(vix_tick.get("ltp", 0)) or None,
                    open=float(vix_tick.get("open", 0)) or None,
                    high=float(vix_tick.get("high", 0)) or None,
                    low=float(vix_tick.get("low",  0)) or None,
                )
                db.add(vix)
                await db.commit()
            logger.info("VIX saved: %.2f", vix.close or 0)
        except Exception as e:
            logger.error("VIX fetch error: %s", e)


# Singleton
study_collector = StudyCollector()
