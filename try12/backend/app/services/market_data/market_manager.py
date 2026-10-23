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
from app.services.broker.instrument_sync import sync_instruments, get_active_expiries, get_option_tokens
from app.services.cache.redis_client import redis_cache
from app.runtime.context import trading_day
from app.services.market_data.feed import market_feed, TickData
from app.services.market_data.tick_writer import tick_writer

logger = logging.getLogger(__name__)

# How many strikes around ATM to subscribe (each side)
ATM_STRIKE_RADIUS = 10
# How many of the nearest expiries to subscribe. The option selector trades a
# 2-14 DTE expiry, so the day before an expiry (DTE 1) it picks NEXT week's;
# subscribing only the nearest left it with no quotes on those days.
WATCH_EXPIRIES = 2
# How many historical 1m candles to seed on startup
HISTORICAL_CANDLE_LIMIT = 390  # full trading day
# Spot / futures seed: this many calendar days back (covers a weekend plus a
# holiday), trimmed to the newest SEED_KEEP_CANDLES — yesterday's session plus
# today's, which is what the strategies' indicators warm up on.
SEED_LOOKBACK_DAYS = 6
SEED_KEEP_CANDLES = 500
# AngelOne's historical API knows the NSE indices by "999"-prefixed tokens; the
# short feed tokens (26000 / 26009 / 26017) return SUCCESS with zero candles.
# Candles are still stored under the feed token, which is what everything reads.
INDEX_HISTORICAL_TOKENS = {"26000": "99926000", "26009": "99926009", "26017": "99926017"}


class MarketManager:

    async def startup(self):
        """Full startup sequence. Called from FastAPI lifespan."""
        logger.info("=== MarketManager startup ===")

        # 0. Pre-flight: verify credentials are configured
        if not settings.angel_credentials_configured:
            raise RuntimeError(
                "AngelOne credentials are not configured. "
                "Set ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, "
                "ANGEL_TOTP_SECRET in your .env file."
            )

        # 1. Login to AngelOne
        await angel_client.login()
        logger.info("AngelOne login OK")

        # 2. Cache session tokens in Redis
        from app.services.broker.angel_client import _session
        await redis_cache.set_angel_session(
            _session.jwt_token, _session.refresh_token, _session.feed_token
        )

        # 3. Sync instrument master
        async with AsyncSessionLocal() as db:
            count = await sync_instruments(db)
            logger.info("Synced %d instruments", count)

        # 4. Build watchlist (ATM ± radius for nearest expiry)
        watchlist = await self._build_watchlist()
        logger.info("Watchlist: %d tokens", len(watchlist))

        # 5b. Seed historical OHLC for SPOT underlyings (NIFTY / BANKNIFTY).
        # The strategy_runner reads spot candles from Redis
        # (`get_candles(spot_token, "ONE_MINUTE")`), but the watchlist
        # only contains NFO option tokens.  Without this, the spot candle
        # keys stay empty and no strategies can run.
        spot_tokens = [
            (settings.NIFTY_TOKEN,    "NIFTY",     "NSE"),
            (settings.BANKNIFTY_TOKEN,"BANKNIFTY", "NSE"),
        ]
        for s_token, s_name, s_exchange in spot_tokens:
            tick_writer.register_symbol(s_token, s_name)
            try:
                await self._seed_one_token_exchange(s_token, s_name, s_exchange)
            except Exception as exc:
                logger.warning("Spot OHLC seed error for %s: %s", s_name, exc)

        # 5c. Nearest-expiry index futures: the spot index carries no volume, and
        # the ORB rule reads volume off the future. Published as fut_token:<NAME>.
        fut_tokens = await self._nearest_futures()
        for f_token, f_symbol, f_name in fut_tokens:
            tick_writer.register_symbol(f_token, f_symbol)
            try:
                await self._seed_one_token_exchange(f_token, f_symbol, "NFO")
            except Exception as exc:
                logger.warning("Futures OHLC seed error for %s: %s", f_symbol, exc)
            await redis_cache.set(f"fut_token:{f_name}", f_token, ttl=86400)

        # 5d. Option watchlist history in the BACKGROUND: ~170 tokens at AngelOne's
        # rate limit take minutes, only the option charts read it, and the spot /
        # futures history above is what the strategies need. Startup no longer
        # waits for it (it blocked the API for ~7 minutes on 23 Oct).
        self._option_seed_task = asyncio.create_task(self._seed_option_history(watchlist))

        # 6. Register tick handler
        market_feed.add_async_handler(self._on_tick)

        # 7. Subscribe to watchlist
        nfo_tokens = [t for t, _ in watchlist] + [t for t, _, _ in fut_tokens]
        # Core NSE underlyings — VIX is subscribed separately below
        nse_tokens = [settings.NIFTY_TOKEN, settings.BANKNIFTY_TOKEN]
        market_feed.subscribe("NFO", nfo_tokens)
        market_feed.subscribe("NSE", nse_tokens)

        # 8. Start TickWriter
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
        await market_feed.start()

        logger.info("=== MarketManager startup complete ===")

    async def _seed_option_history(self, watchlist) -> None:
        try:
            await self._seed_historical_ohlc(watchlist)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                 # never takes the app down
            logger.warning("Background option history seed failed: %s", exc)

    async def shutdown(self):
        task = getattr(self, "_option_seed_task", None)
        if task is not None and not task.done():
            task.cancel()
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
          - Nifty CE/PE, the WATCH_EXPIRIES nearest expiries, ATM ± ATM_STRIKE_RADIUS strikes
          - BankNifty CE/PE, the same
        Built at startup only: restart the backend each trading morning.
        """
        result = []
        async with AsyncSessionLocal() as db:
            for underlying in ["NIFTY", "BANKNIFTY"]:
                expiries = await get_active_expiries(db, underlying, as_of=trading_day())
                if not expiries:
                    logger.warning("No expiries found for %s", underlying)
                    continue


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

                for expiry in expiries[:WATCH_EXPIRIES]:
                    instruments = await get_option_tokens(db, underlying, expiry, strike_range)
                    for inst in instruments:
                        result.append((inst.token, inst.symbol))
                        tick_writer.register_symbol(inst.token, inst.symbol)
                    logger.info("[%s] watchlist: %d options for expiry %s",
                                underlying, len(instruments), expiry)

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
        sem = asyncio.Semaphore(3)    # angel_client also spaces historical calls out

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
                candle_dicts = await self._rest_candles_to_feed_units(token, "NFO", candle_dicts)
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

    async def _seed_one_token_exchange(self, token: str, symbol: str, exchange: str = "NFO"):
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
        from datetime import timedelta as _seed_td
        today = date.today()
        from_dt = f"{today - _seed_td(days=SEED_LOOKBACK_DAYS)} 09:15"
        to_dt   = f"{today} 15:30"

        candle_dicts: list[dict] = []

        # ── Attempt 1: AngelOne historical REST ────────────────────────────────
        try:
            candles = await angel_client.get_candle_data(
                exchange=exchange,
                symbol_token=(INDEX_HISTORICAL_TOKENS.get(token, token)
                              if exchange == "NSE" else token),
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
                        "volume":    row[5] if len(row) > 5 else 0,
                    }
                    for row in candles
                ]
                candle_dicts = await self._rest_candles_to_feed_units(token, exchange, candle_dicts)
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
                    today - _seed_td(days=SEED_LOOKBACK_DAYS), _dt_mod.time(3, 45),
                    tzinfo=_tz.utc  # 09:15 IST
                )

                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(OHLC)
                        .where(
                            OHLC.token    == token,
                            OHLC.interval == "ONE_MINUTE",
                            OHLC.timestamp >= session_open_utc,
                        )
                        .order_by(OHLC.timestamp.desc())
                        .limit(SEED_KEEP_CANDLES)
                    )
                    rows = list(reversed(result.scalars().all()))

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
            await redis_cache.set_candles(token, "ONE_MINUTE", candle_dicts[-SEED_KEEP_CANDLES:])

    async def _rest_candles_to_feed_units(self, token: str, exchange: str,
                                          candles: list[dict]) -> list[dict]:
        """Make REST history match the candles the live feed builds:
          - AngelOne's historical F&O volume is in LOTS, the feed's in units
            (quantity): on 23 Oct the same minutes differed ~65x, the NIFTY lot
            size, so after a mid-session restart every live minute looked like a
            volume surge to the ORB and regime volume tests. NFO volume is scaled
            by the instrument's lot size.
          - the still-forming current minute is dropped: the live builder emits
            that minute when it closes, and keeping both left a duplicate."""
        from datetime import datetime, timezone
        from app.services.market_data.timestamps import try_normalize_candle_timestamp as _ts
        now_min = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        out = []
        for c in candles:
            t = _ts(c.get("timestamp"))
            if t is not None and t.astimezone(timezone.utc) >= now_min:
                continue
            out.append(c)
        if exchange == "NFO":
            lot = await self._lot_size(token)
            if lot and lot > 1:
                for c in out:
                    c["volume"] = int((c.get("volume") or 0) * lot)
        return out

    async def _lot_size(self, token: str) -> Optional[int]:
        cache = self.__dict__.setdefault("_lot_cache", {})
        if token in cache:
            return cache[token]
        try:
            from sqlalchemy import select
            from app.db.models.instrument import Instrument
            async with AsyncSessionLocal() as db:
                lot = (await db.execute(select(Instrument.lot_size).where(
                    Instrument.token == token).limit(1))).scalar_one_or_none()
        except Exception as exc:
            logger.debug("lot size lookup failed for %s: %s", token, exc)
            lot = None
        cache[token] = lot
        return lot

    async def _nearest_futures(self) -> list[tuple[str, str, str]]:
        """(token, symbol, underlying) of the nearest-expiry NIFTY / BANKNIFTY
        index future in the synced instrument master; [] on any error."""
        from sqlalchemy import select
        from app.db.models.instrument import Instrument, InstrumentType
        out: list[tuple[str, str, str]] = []
        try:
            async with AsyncSessionLocal() as db:
                for name in ("NIFTY", "BANKNIFTY"):
                    row = (await db.execute(
                        select(Instrument).where(
                            Instrument.name == name,
                            Instrument.instrument_type == InstrumentType.FUTIDX,
                            Instrument.expiry >= date.today(),
                        ).order_by(Instrument.expiry).limit(1)
                    )).scalar_one_or_none()
                    if row is not None:
                        out.append((row.token, row.symbol, name))
                        logger.info("Futures for %s volume: %s (%s)", name, row.symbol, row.token)
                    else:
                        logger.warning("No %s index future in the instrument master — "
                                       "ORB volume rule will not be checked", name)
        except Exception as exc:
            logger.warning("Futures lookup failed: %s", exc)
        return out

    # ── Dynamic subscription management ──────────────────────────────────────

    async def subscribe_tokens(self, exchange: str, tokens: list[str]):
        """Add tokens to live feed at runtime (e.g. when user focuses a strike)."""
        await market_feed.send_subscribe(exchange, tokens)

    async def unsubscribe_tokens(self, exchange: str, tokens: list[str]):
        await market_feed.send_unsubscribe(exchange, tokens)

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
