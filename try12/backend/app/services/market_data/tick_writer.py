"""
Tick writer — buffers incoming ticks in memory and batch-inserts to PostgreSQL.
Uses an asyncio queue to decouple the WebSocket receive loop from DB writes.
Also builds rolling 1-minute OHLC candles in memory.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import AsyncSessionLocal
from app.db.models.tick import Tick
from app.db.models.ohlc import OHLC
from app.services.market_data.feed import TickData
from app.services.cache.redis_client import redis_cache

logger = logging.getLogger(__name__)

BATCH_SIZE     = 200   # Insert ticks when queue hits this size
FLUSH_INTERVAL = 1.0   # Or flush every 1 second, whichever comes first
CANDLE_INTERVAL = 60   # 1-minute candles (seconds)


class CandleBuilder:
    """
    In-memory rolling 1-minute OHLC candle builder per token.
    Emits a completed candle dict when the minute rolls over.
    """
    def __init__(self):
        # token → {open, high, low, close, volume, oi, bucket_start}
        self._candles: dict[str, dict] = {}

    def update(self, tick: TickData) -> Optional[dict]:
        """
        Process a tick. Returns a completed candle dict if the minute flipped,
        otherwise None.
        """
        now = tick.received_at.replace(second=0, microsecond=0, tzinfo=timezone.utc)
        token = tick.token
        completed = None

        if token in self._candles:
            c = self._candles[token]
            if now > c["bucket_start"]:
                # Minute rolled — emit completed candle
                completed = dict(c)
                # Start fresh candle
                self._candles[token] = self._new_candle(tick, now)
            else:
                # Update running candle
                c["high"]   = max(c["high"],  tick.ltp)
                c["low"]    = min(c["low"],   tick.ltp)
                c["close"]  = tick.ltp
                c["volume"] = tick.volume or c["volume"]
                c["oi"]     = tick.oi or c["oi"]
        else:
            self._candles[token] = self._new_candle(tick, now)

        return completed

    def _new_candle(self, tick: TickData, bucket: datetime) -> dict:
        # Store bucket_start in IST so strategy time-window checks work correctly.
        # bucket is already UTC with tzinfo; convert to IST (UTC+5:30).
        ist_bucket = bucket.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return {
            "token":        tick.token,
            "exchange":     tick.exchange,
            "interval":     "ONE_MINUTE",
            "timestamp":    ist_bucket.isoformat(),   # IST ISO string for strategy use
            "open":         tick.ltp,
            "high":         tick.ltp,
            "low":          tick.ltp,
            "close":        tick.ltp,
            "volume":       tick.volume or 0,
            "oi":           tick.oi or 0,
            "bucket_start": bucket,                   # UTC datetime for DB write
        }


class TickWriter:
    """
    Async tick writer with:
      - In-memory queue
      - Batch DB insert
      - 1-minute candle aggregation → OHLC table
    """

    def __init__(self):
        self._queue: asyncio.Queue[TickData] = asyncio.Queue(maxsize=10_000)
        self._candle_builder = CandleBuilder()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Symbol lookup: token → symbol (populated lazily from ticks)
        self._token_symbol: dict[str, str] = {}

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._worker())
        logger.info("TickWriter started.")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        # Flush remaining ticks
        await self._flush()
        logger.info("TickWriter stopped.")

    async def push(self, tick: TickData):
        """Called by MarketFeed handler for every incoming tick."""
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            logger.warning("TickWriter queue full — dropping tick for token %s", tick.token)

    async def _worker(self):
        """Main drain loop: collect ticks and flush in batches."""
        buffer: list[TickData] = []
        last_flush = asyncio.get_event_loop().time()

        while self._running:
            try:
                # Wait up to FLUSH_INTERVAL for a tick
                tick = await asyncio.wait_for(self._queue.get(), timeout=FLUSH_INTERVAL)
                buffer.append(tick)

                # Drain any immediately available ticks
                while not self._queue.empty() and len(buffer) < BATCH_SIZE:
                    buffer.append(self._queue.get_nowait())

                now = asyncio.get_event_loop().time()
                if len(buffer) >= BATCH_SIZE or (now - last_flush) >= FLUSH_INTERVAL:
                    await self._flush_buffer(buffer)
                    buffer.clear()
                    last_flush = now

            except asyncio.TimeoutError:
                if buffer:
                    await self._flush_buffer(buffer)
                    buffer.clear()
                    last_flush = asyncio.get_event_loop().time()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("TickWriter worker error: %s", exc, exc_info=True)

    async def _flush_buffer(self, ticks: list[TickData]):
        tick_rows   = []
        candle_rows = []

        for tick in ticks:
            tick_rows.append({
                "token":        tick.token,
                "symbol":       self._token_symbol.get(tick.token, tick.token),
                "exchange":     tick.exchange,
                "ltp":          tick.ltp,
                "open":         tick.open   or None,
                "high":         tick.high   or None,
                "low":          tick.low    or None,
                "close":        tick.close  or None,
                "volume":       tick.volume or None,
                "oi":           tick.oi     or None,
                "best_bid":     tick.best_bid     or None,
                "best_ask":     tick.best_ask     or None,
                "best_bid_qty": tick.best_bid_qty or None,
                "best_ask_qty": tick.best_ask_qty or None,
                "exchange_ts":  tick.exchange_ts  or None,
                "timestamp":    tick.received_at,
            })

            completed = self._candle_builder.update(tick)
            if completed:
                # Push to Redis so strategy_runner can read candles in real-time
                redis_candle = {
                    "token":     completed["token"],
                    "symbol":    self._token_symbol.get(completed["token"], completed["token"]),
                    "interval":  completed["interval"],
                    "timestamp": completed["timestamp"],
                    "open":      completed["open"],
                    "high":      completed["high"],
                    "low":       completed["low"],
                    "close":     completed["close"],
                    "volume":    completed["volume"],
                    "oi":        completed["oi"],
                }
                try:
                    await redis_cache.push_candle(completed["token"], "ONE_MINUTE", redis_candle)
                except Exception as exc:
                    logger.debug("Redis candle push error: %s", exc)

                candle_rows.append({
                    "token":     completed["token"],
                    "symbol":    self._token_symbol.get(completed["token"], completed["token"]),
                    "exchange":  completed["exchange"],
                    "interval":  completed["interval"],
                    "open":      completed["open"],
                    "high":      completed["high"],
                    "low":       completed["low"],
                    "close":     completed["close"],
                    "volume":    completed["volume"],
                    "oi":        completed["oi"],
                    "timestamp": completed["bucket_start"],
                })

        async with AsyncSessionLocal() as db:
            try:
                if tick_rows:
                    await db.execute(pg_insert(Tick).values(tick_rows))

                if candle_rows:
                    stmt = pg_insert(OHLC).values(candle_rows)
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_ohlc_token_interval_ts",
                        set_={
                            "high":   stmt.excluded.high,
                            "low":    stmt.excluded.low,
                            "close":  stmt.excluded.close,
                            "volume": stmt.excluded.volume,
                            "oi":     stmt.excluded.oi,
                        },
                    )
                    await db.execute(stmt)

                await db.commit()
                logger.debug(
                    "Flushed %d ticks, %d candles to DB",
                    len(tick_rows), len(candle_rows)
                )
            except Exception as exc:
                await db.rollback()
                logger.error("DB flush error: %s", exc, exc_info=True)

    async def _flush(self):
        """Final flush on shutdown."""
        remaining = []
        while not self._queue.empty():
            remaining.append(self._queue.get_nowait())
        if remaining:
            await self._flush_buffer(remaining)

    def register_symbol(self, token: str, symbol: str):
        """Register token→symbol mapping so ticks carry symbol name."""
        self._token_symbol[token] = symbol


# App-level singleton
tick_writer = TickWriter()
