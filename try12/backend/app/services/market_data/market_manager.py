"""
MarketManager — orchestrates the full market data pipeline:
  1. AngelOne WebSocket feed  →  TickData
  2. TickData  →  Redis cache (LTP, full tick, pub/sub)
  3. TickData  →  TickWriter queue  →  PostgreSQL (ticks + 1m OHLC)
  4. On startup: instrument sync, historical OHLC seed, subscribe watchlist

Call market_manager.startup() from FastAPI lifespan.
"""
import asyncio
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.db.models.instrument import Instrument, InstrumentType
from app.services.broker.angel_client import angel_client
from app.services.trading import market_calendar
from app.services.broker.instrument_sync import sync_instruments, get_active_expiries, get_option_tokens
from app.services.cache.redis_client import redis_cache
from app.runtime.context import trading_day
from app.services.market_data.feed import market_feed, TickData
from app.services.market_data.tick_writer import tick_writer

logger = logging.getLogger(__name__)

# How many strikes around ATM to subscribe (each side)
ATM_STRIKE_RADIUS = 10
# How many historical 1m candles to seed on startup
HISTORICAL_CANDLE_LIMIT = 390  # full trading day
# Redis key the strategy runner reads to find the near-month index future
# whose volume weights VWAP (NSE index spot carries volume=0).
FUTURES_TOKEN_KEY = "futures_token:{underlying}"
FUTURES_TOKEN_TTL_S = 24 * 3600
# Roll to the next-month future this many days before the near month expires:
# volume migrates to the next contract during expiry week.
FUTURES_ROLL_DAYS = 2


async def resolve_index_future(db: AsyncSession, underlying: str,
                               as_of: date) -> Optional[Instrument]:
    """The near-month FUTIDX contract for *underlying*: the earliest active
    expiry more than FUTURES_ROLL_DAYS days away (else the earliest one)."""
    from sqlalchemy import select
    rows = (await db.execute(
        select(Instrument).where(
            Instrument.name == underlying.upper(),
            Instrument.instrument_type == InstrumentType.FUTIDX,
            Instrument.is_active == True,  # noqa: E712
            Instrument.expiry >= as_of,
        ).order_by(Instrument.expiry)
    )).scalars().all()
    if not rows:
        return None
    for r in rows:
        if r.expiry and (r.expiry - as_of).days > FUTURES_ROLL_DAYS:
            return r
    return rows[0]


async def instruments_cover_today(db: AsyncSession, as_of: date) -> bool:
    """True when the instruments table has active, unexpired index options AND an
    index future for both NIFTY and BANKNIFTY — enough to trade without today's
    scrip-master download."""
    from sqlalchemy import func, select
    for name in ("NIFTY", "BANKNIFTY"):
        for itype in (InstrumentType.OPTIDX, InstrumentType.FUTIDX):
            n = (await db.execute(select(func.count()).select_from(Instrument).where(
                Instrument.name == name, Instrument.instrument_type == itype,
                Instrument.is_active == True,  # noqa: E712
                Instrument.expiry >= as_of,
            ))).scalar_one()
            if not n:
                return False
    return True


class MarketManager:

    def __init__(self) -> None:
        # Startup bookkeeping (§ feedless-start visibility): the step reached, the
        # last startup error, and whether the one-time steps already ran, so a
        # retry after a failure resumes instead of repeating them.
        self.startup_stage: str = "not started"
        self.startup_error: Optional[str] = None
        self.startup_attempts: int = 0
        self._instruments_synced = False
        self._handler_added = False
        self._option_seed_task: Optional[asyncio.Task] = None

    async def startup(self):
        """Full startup sequence. Called from FastAPI lifespan (and retried by
        main.py while the feed is down). Records the step reached and the error,
        if any, for /health and the signal diagnostics."""
        self.startup_attempts += 1
        self.startup_error = None
        try:
            await self._startup()
            self.startup_stage = "complete"
        except Exception as exc:
            self.startup_error = f"{self.startup_stage}: {type(exc).__name__}: {exc}"
            raise

    async def _startup(self):
        logger.info("=== MarketManager startup (attempt %d) ===", self.startup_attempts)

        # 0. Pre-flight: verify credentials are configured
        if not settings.angel_credentials_configured:
            raise RuntimeError(
                "AngelOne credentials are not configured. "
                "Set ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, "
                "ANGEL_TOTP_SECRET in your .env file."
            )

        # 1. Login to AngelOne
        self.startup_stage = "AngelOne login"
        await angel_client.login()
        logger.info("AngelOne login OK")

        # 2. Cache session tokens in Redis
        from app.services.broker.angel_client import _session
        await redis_cache.set_angel_session(
            _session.jwt_token, _session.refresh_token, _session.feed_token
        )

        # 3. Sync instrument master (once per process; a retry reuses it)
        if not self._instruments_synced:
            self.startup_stage = "instrument sync"
            try:
                async with AsyncSessionLocal() as db:
                    count = await sync_instruments(db)
                    logger.info("Synced %d instruments", count)
            except Exception as exc:
                # The scrip master is a ~36 MB download that AngelOne's server
                # sometimes cuts off mid-transfer. The instruments table already
                # holds the contracts from earlier syncs; when it covers today's
                # expiries for both indexes, start with it instead of running feedless.
                async with AsyncSessionLocal() as db:
                    usable = await instruments_cover_today(db, trading_day())
                if not usable:
                    raise
                logger.warning("Instrument sync failed (%s: %s) — continuing with the "
                               "instruments already in the database (they cover today's "
                               "expiries)", type(exc).__name__, exc)
            self._instruments_synced = True

        # 4. Build watchlist (ATM ± radius for nearest expiry)
        self.startup_stage = "watchlist"
        watchlist = await self._build_watchlist()
        logger.info("Watchlist: %d tokens", len(watchlist))

        # 5b. Seed historical OHLC for SPOT underlyings (NIFTY / BANKNIFTY).
        # The strategy_runner reads spot candles from Redis
        # (`get_candles(spot_token, "ONE_MINUTE")`), but the watchlist
        # only contains NFO option tokens.  Without this, the spot candle
        # keys stay empty and no strategies can run.
        # On a weekend / NSE holiday there is no session to seed: skip every
        # historical call (~90 AngelOne requests) instead of fetching nothing.
        closed = market_calendar.closed_reason(date.today())
        if closed:
            logger.info("Exchange closed today (%s) — historical OHLC seeding skipped", closed)
        spot_tokens = [
            (settings.NIFTY_TOKEN,    "NIFTY",     "NSE"),
            (settings.BANKNIFTY_TOKEN,"BANKNIFTY", "NSE"),
        ]
        self.startup_stage = "index history"
        for s_token, s_name, s_exchange in spot_tokens:
            tick_writer.register_symbol(s_token, s_name)
            if closed:
                continue
            try:
                await self._seed_one_token_exchange(s_token, s_name, s_exchange)
            except Exception as exc:
                logger.warning("Spot OHLC seed error for %s: %s", s_name, exc)

        # 5c. Near-month index FUTURES: their volume weights the strategies'
        # VWAP and drives the volume-surge checks (spot volume is always 0).
        self.startup_stage = "futures history"
        fut_tokens = await self._setup_index_futures(seed=not closed)

        # 6. Register tick handler (once: a retry must not double-count ticks)
        self.startup_stage = "feed subscribe"
        if not self._handler_added:
            market_feed.add_async_handler(self._on_tick)
            self._handler_added = True

        # 7. Subscribe to watchlist
        nfo_tokens = [t for t, _ in watchlist] + [t for t in fut_tokens if t]
        # Core NSE underlyings — VIX is subscribed separately below
        nse_tokens = [settings.NIFTY_TOKEN, settings.BANKNIFTY_TOKEN]
        market_feed.subscribe("NFO", nfo_tokens)
        market_feed.subscribe("NSE", nse_tokens)

        # 8. Start TickWriter (once)
        self.startup_stage = "tick writer"
        if not getattr(tick_writer, "_running", False):
            await tick_writer.start()

        # Register symbol names
        for token, symbol in watchlist:
            tick_writer.register_symbol(token, symbol)

        # 8b. Also subscribe India VIX so the VIX gate in strategy_runner works.
        # Use settings.VIX_TOKEN — single source of truth.
        market_feed.subscribe("NSE", [settings.VIX_TOKEN])
        tick_writer.register_symbol(settings.VIX_TOKEN, "INDIA VIX")
        logger.info("Subscribed India VIX token %s", settings.VIX_TOKEN)

        # 9. Start WebSocket feed
        self.startup_stage = "feed start"
        if not getattr(market_feed, "_running", False):
            await market_feed.start()

        # 10. Option-contract history LAST and in the background. It is ~84
        # rate-limited AngelOne calls (minutes); the strategies do not need it,
        # so the live feed no longer waits for it. A live candle written while a
        # token is being seeded can be replaced by the fetched history, which
        # already contains that minute.
        if not closed and (self._option_seed_task is None or self._option_seed_task.done()):
            self._option_seed_task = asyncio.create_task(self._seed_options_background(watchlist))

        logger.info("=== MarketManager startup complete ===")

    async def _seed_options_background(self, watchlist) -> None:
        try:
            await self._seed_historical_ohlc(watchlist)
        except Exception as exc:                       # never kills the app
            logger.warning("Option history seed failed (non-fatal): %s", exc)

    async def shutdown(self):
        if self._option_seed_task is not None and not self._option_seed_task.done():
            self._option_seed_task.cancel()
        await market_feed.stop()
        await tick_writer.stop()
        await angel_client.logout()
        logger.info("MarketManager shutdown complete.")

    # ── Tick handler ──────────────────────────────────────────────────────────

    async def _on_tick(self, tick: TickData):
        """Central tick dispatcher — runs for every WebSocket tick."""
        tick_dict = {
            "token":        tick.token,
            "exchange":     tick.exchange,
            "ltp":          tick.ltp,
            "open":         tick.open,
            "high":         tick.high,
            "low":          tick.low,
            "close":        tick.close,
            "volume":       tick.volume,
            "oi":           tick.oi,
            "best_bid":     tick.best_bid,
            "best_ask":     tick.best_ask,
            "best_bid_qty": tick.best_bid_qty,
            "best_ask_qty": tick.best_ask_qty,
            "exchange_ts":  tick.exchange_ts,
            "ts":           tick.received_at.isoformat(),
        }

        try:
            # Cache LTP and full tick
            await redis_cache.set_ltp(tick.token, tick.ltp)
            await redis_cache.set_tick(tick.token, tick_dict)
            # Pub/sub for WebSocket broadcast to frontend
            await redis_cache.publish_tick(tick.token, tick_dict)
        except Exception as exc:
            logger.warning("Redis write failed for tick token=%s: %s", tick.token, exc)
            # Continue to DB queue even if Redis is temporarily unavailable

        # Queue for DB write — do this outside the Redis try/except so DB
        # writes are not affected by Redis blips.
        await tick_writer.push(tick)

    # ── Watchlist builder ─────────────────────────────────────────────────────

    async def _build_watchlist(self) -> list[tuple[str, str]]:
        """
        Returns list of (token, symbol) for:
          - Nifty CE/PE, nearest expiry, ATM ± ATM_STRIKE_RADIUS strikes
          - BankNifty CE/PE, nearest expiry, ATM ± ATM_STRIKE_RADIUS strikes
        """
        result = []
        async with AsyncSessionLocal() as db:
            for underlying in ["NIFTY", "BANKNIFTY"]:
                expiries = await get_active_expiries(db, underlying, as_of=trading_day())
                if not expiries:
                    logger.warning("No expiries found for %s", underlying)
                    continue

                nearest_expiry = expiries[0]

                # Get approximate ATM from spot LTP via REST
                spot_ltp = await self._get_spot_ltp(underlying)
                if spot_ltp:
                    step = 50 if underlying == "NIFTY" else 100
                    atm = round(spot_ltp / step) * step
                    low  = atm - ATM_STRIKE_RADIUS * step
                    high = atm + ATM_STRIKE_RADIUS * step
                    strike_range = (low, high)
                else:
                    # Could not get spot LTP — cap to 2× ATM_STRIKE_RADIUS so we
                    # don't flood AngelOne with 200+ token subscriptions.
                    # Use a wide but bounded fallback range around a typical ATM.
                    step = 50 if underlying == "NIFTY" else 100
                    default_atm = 23000 if underlying == "NIFTY" else 50000
                    low  = default_atm - ATM_STRIKE_RADIUS * step
                    high = default_atm + ATM_STRIKE_RADIUS * step
                    strike_range = (low, high)
                    logger.warning(
                        "[%s] Spot LTP unavailable — using fallback strike range "
                        "%.0f–%.0f for initial watchlist. Will resubscribe on first tick.",
                        underlying, low, high,
                    )

                instruments = await get_option_tokens(
                    db, underlying, nearest_expiry, strike_range
                )
                for inst in instruments:
                    result.append((inst.token, inst.symbol))
                    tick_writer.register_symbol(inst.token, inst.symbol)

        return result

    async def _get_spot_ltp(self, underlying: str) -> Optional[float]:
        token = settings.NIFTY_TOKEN if underlying == "NIFTY" else settings.BANKNIFTY_TOKEN
        # Try Redis first (already populated on restart), then REST
        try:
            cached = await redis_cache.get_ltp(token)
            if cached:
                return cached
        except Exception:
            pass
        try:
            quote = await angel_client.get_ltp("NSE", token, underlying)
            return float(quote.get("ltp", 0)) or None
        except Exception as exc:
            logger.warning("Could not fetch spot LTP for %s: %s", underlying, exc)
            return None

    # ── Historical OHLC seed ──────────────────────────────────────────────────

    async def _seed_historical_ohlc(self, watchlist: list[tuple[str, str]]):
        """
        Fetch today's 1m historical OHLC from AngelOne for all watchlist tokens
        and load them into Redis so strategies have a full day of candles immediately.
        Uses a semaphore to cap concurrent API requests at 10.
        """
        from datetime import datetime
        today = date.today()
        from_dt = f"{today} 09:15"
        to_dt   = f"{today} 15:30"

        logger.info("Seeding historical OHLC for %d tokens...", len(watchlist))
        sem = asyncio.Semaphore(10)   # max 10 concurrent requests to AngelOne

        async def _bounded(token, symbol):
            async with sem:
                await self._seed_one_token(token, symbol, from_dt, to_dt)

        results = await asyncio.gather(
            *[_bounded(token, symbol) for token, symbol in watchlist],
            return_exceptions=True,
        )
        errors = sum(1 for r in results if isinstance(r, Exception))
        logger.info(
            "Historical OHLC seed complete. %d OK, %d errors.",
            len(watchlist) - errors, errors
        )

    async def _seed_one_token(self, token: str, symbol: str, from_dt: str, to_dt: str):
        """
        Seed 1m candles for an NFO option token into Redis.
        Falls back to today's OHLC table if AngelOne 403s.
        """
        candle_dicts: list[dict] = []

        # Attempt 1: AngelOne REST
        try:
            candles = await angel_client.get_candle_data(
                exchange="NFO",
                symbol_token=token,
                interval="ONE_MINUTE",
                from_date=from_dt,
                to_date=to_dt,
            )
            if candles:
                candle_dicts = [
                    {
                        "token":     token,
                        "symbol":    symbol,
                        "interval":  "ONE_MINUTE",
                        "timestamp": row[0],
                        "open":      row[1],
                        "high":      row[2],
                        "low":       row[3],
                        "close":     row[4],
                        "volume":    row[5] if len(row) > 5 else 0,
                    }
                    for row in candles
                ]
        except Exception as exc:
            logger.debug("AngelOne seed error for token %s: %s — trying DB", token, exc)

        # Attempt 2: DB fallback
        if not candle_dicts:
            try:
                from app.db.models.ohlc import OHLC
                from sqlalchemy import select
                import datetime as _dt_mod
                from datetime import timezone as _tz, timedelta as _td
                _IST = _tz(_td(hours=5, minutes=30))
                session_open_utc = _dt_mod.datetime.combine(
                    date.today(), _dt_mod.time(3, 45), tzinfo=_tz.utc
                )
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(OHLC)
                        .where(
                            OHLC.token    == token,
                            OHLC.interval == "ONE_MINUTE",
                            OHLC.timestamp >= session_open_utc,
                        )
                        .order_by(OHLC.timestamp.asc())
                        .limit(500)
                    )
                    rows = result.scalars().all()

                if rows:
                    candle_dicts = [
                        {
                            "token":     token,
                            "symbol":    symbol,
                            "interval":  "ONE_MINUTE",
                            "timestamp": row.timestamp.replace(tzinfo=_tz.utc)
                                            .astimezone(_IST).isoformat(),
                            "open":      float(row.open),
                            "high":      float(row.high),
                            "low":       float(row.low),
                            "close":     float(row.close),
                            "volume":    int(row.volume or 0),
                        }
                        for row in rows
                    ]
            except Exception as exc2:
                logger.debug("DB fallback seed error for token %s: %s", token, exc2)

        if candle_dicts:
            await redis_cache.set_candles(token, "ONE_MINUTE", candle_dicts)
        else:
            raise RuntimeError(f"No candle data (REST or DB) for token {token}")

    async def _seed_one_token_exchange(self, token: str, symbol: str, exchange: str = "NFO",
                                       volume_multiplier: float = 1.0):
        """
        Seed historical 1m OHLC for a single token into Redis (supports any exchange).

        Strategy:
          1. Try AngelOne REST historical API.
          2. On failure (403, empty response, or any exception), fall back to
             today's candles already written to the local OHLC table by tick_writer.
             This handles the common case where the container restarts mid-session
             and AngelOne refuses the historical call — the DB already has candles
             from earlier in the same day.
        """
        today = date.today()
        from_dt = f"{today} 09:15"
        to_dt   = f"{today} 15:30"

        candle_dicts: list[dict] = []

        # ── Attempt 1: AngelOne historical REST ────────────────────────────────
        try:
            candles = await angel_client.get_candle_data(
                exchange=exchange,
                symbol_token=token,
                interval="ONE_MINUTE",
                from_date=from_dt,
                to_date=to_dt,
            )
            if candles:
                from datetime import timezone as _tz, timedelta as _td
                _IST = _tz(_td(hours=5, minutes=30))
                candle_dicts = [
                    {
                        "token":     token,
                        "symbol":    symbol,
                        "interval":  "ONE_MINUTE",
                        "timestamp": row[0],
                        "open":      row[1],
                        "high":      row[2],
                        "low":       row[3],
                        "close":     row[4],
                        # AngelOne historical F&O volume is in LOTS; live ticks
                        # (and so live candles) count QUANTITY. Callers pass the
                        # lot size so both are quantity (verified: live = 65 ×
                        # historical for NIFTY futures, minute by minute).
                        "volume":    (row[5] if len(row) > 5 else 0) * volume_multiplier,
                    }
                    for row in candles
                ]
                logger.info(
                    "Seeded %d candles for %s (%s/%s) from AngelOne REST",
                    len(candle_dicts), symbol, exchange, token,
                )
            else:
                logger.info(
                    "No historical candles from AngelOne for %s — will try DB fallback",
                    symbol,
                )
        except Exception as exc:
            logger.warning(
                "AngelOne historical seed failed for %s (%s/%s): %s — trying DB fallback",
                symbol, exchange, token, exc,
            )

        # ── Attempt 2: DB fallback — today's OHLC already written by tick_writer ─
        if not candle_dicts:
            try:
                from app.db.models.ohlc import OHLC
                from sqlalchemy import select
                from datetime import timezone as _tz, timedelta as _td

                _IST = _tz(_td(hours=5, minutes=30))
                # 09:15 IST = 03:45 UTC
                session_open_utc = date.today()  # placeholder; built below
                import datetime as _dt_mod
                session_open_utc = _dt_mod.datetime.combine(
                    today, _dt_mod.time(3, 45), tzinfo=_tz.utc  # 09:15 IST
                )

                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(OHLC)
                        .where(
                            OHLC.token    == token,
                            OHLC.interval == "ONE_MINUTE",
                            OHLC.timestamp >= session_open_utc,
                        )
                        .order_by(OHLC.timestamp.asc())
                        .limit(500)
                    )
                    rows = result.scalars().all()

                if rows:
                    candle_dicts = [
                        {
                            "token":     token,
                            "symbol":    symbol,
                            "interval":  "ONE_MINUTE",
                            # Store as IST ISO string to match tick_writer format
                            "timestamp": row.timestamp.replace(tzinfo=_tz.utc)
                                            .astimezone(_IST).isoformat(),
                            "open":      float(row.open),
                            "high":      float(row.high),
                            "low":       float(row.low),
                            "close":     float(row.close),
                            "volume":    int(row.volume or 0),
                        }
                        for row in rows
                    ]
                    logger.info(
                        "DB fallback: seeded %d candles for %s from OHLC table "
                        "(earliest %s)",
                        len(candle_dicts), symbol,
                        candle_dicts[0]["timestamp"],
                    )
                else:
                    logger.info(
                        "DB fallback: no today candles in OHLC table for %s — "
                        "strategies will wait for live candles to accumulate",
                        symbol,
                    )
            except Exception as exc2:
                logger.warning("DB fallback seed error for %s: %s", symbol, exc2)

        if candle_dicts:
            await redis_cache.set_candles(token, "ONE_MINUTE", candle_dicts)

    # ── Dynamic subscription management ──────────────────────────────────────

    async def subscribe_tokens(self, exchange: str, tokens: list[str]):
        """Add tokens to live feed at runtime (e.g. when user focuses a strike)."""
        await market_feed.send_subscribe(exchange, tokens)

    async def unsubscribe_tokens(self, exchange: str, tokens: list[str]):
        await market_feed.send_unsubscribe(exchange, tokens)

    async def _setup_index_futures(self, seed: bool = True) -> list[str]:
        """Resolve, register and seed the near-month NIFTY / BANKNIFTY futures
        and publish each token under FUTURES_TOKEN_KEY. Never raises: a missing
        future only means the strategies run without volume confirmation
        (strategy_runner records a DATA_GAP)."""
        tokens: list[str] = []
        for underlying in ("NIFTY", "BANKNIFTY"):
            try:
                async with AsyncSessionLocal() as db:
                    fut = await resolve_index_future(db, underlying, trading_day())
                if fut is None:
                    logger.warning("[%s] No active index future in instruments — "
                                   "VWAP/volume checks will report a DATA_GAP", underlying)
                    continue
                tick_writer.register_symbol(fut.token, fut.symbol)
                try:
                    if seed:
                        await self._seed_one_token_exchange(
                            fut.token, fut.symbol, "NFO",
                            volume_multiplier=float(fut.lot_size or 1))
                except Exception as exc:
                    logger.warning("Futures OHLC seed error for %s: %s", fut.symbol, exc)
                await redis_cache.set(FUTURES_TOKEN_KEY.format(underlying=underlying),
                                      fut.token, ttl=FUTURES_TOKEN_TTL_S)
                tokens.append(fut.token)
                logger.info("[%s] Volume source: %s (token %s, expiry %s)",
                            underlying, fut.symbol, fut.token, fut.expiry)
            except Exception as exc:
                logger.warning("[%s] index future setup failed: %s", underlying, exc)
        return tokens

    # ── On-demand re-seed ─────────────────────────────────────────────────────

    async def reseed_spot_candles(self) -> dict:
        """
        Re-seed spot (NIFTY / BANKNIFTY) candles into Redis from DB.
        Called from the API when the strategy runner is candle-starved after
        a mid-session container restart where the AngelOne historical API 403s.
        Returns a summary of how many candles were loaded for each underlying.
        """
        spot_tokens = [
            (settings.NIFTY_TOKEN,     "NIFTY",     "NSE"),
            (settings.BANKNIFTY_TOKEN, "BANKNIFTY", "NSE"),
        ]
        result = {}
        for s_token, s_name, s_exchange in spot_tokens:
            tick_writer.register_symbol(s_token, s_name)
            try:
                await self._seed_one_token_exchange(s_token, s_name, s_exchange)
                candles = await redis_cache.get_candles(s_token, "ONE_MINUTE", 500)
                result[s_name] = len(candles)
                logger.info("Re-seeded %s: %d candles in Redis", s_name, len(candles))
            except Exception as exc:
                result[s_name] = 0
                logger.warning("Re-seed failed for %s: %s", s_name, exc)
        return result


# App-level singleton
market_manager = MarketManager()
