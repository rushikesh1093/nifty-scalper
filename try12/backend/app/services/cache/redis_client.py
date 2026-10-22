"""
Redis cache layer.
Stores live market data so strategies and API endpoints can read
without hitting PostgreSQL on every request.

Key schema:
  ltp:{token}                    → float (last traded price)
  tick:{token}                   → JSON (full TickData)
  ohlc:{token}:{interval}        → JSON list of last N candles
  option_chain:{underlying}:{expiry} → JSON (full option chain snapshot)
  positions                      → JSON list of open positions
  order_book                     → JSON list of today's orders
  session:angel                  → JSON (JWT, refresh, feed tokens)
"""
import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

# TTLs (seconds)
TTL_TICK         = 300      # LTP / tick data — raised from 60s to 300s so option
                            # selector doesn't see stale/None on low-liquidity strikes
                            # where ticks arrive every 2-5 minutes
TTL_OHLC         = 4 * 86400  # OHLC candles — 4 days, so yesterday's session (and a
                            # weekend) survives overnight: the strategies' EMA / RSI /
                            # Supertrend / ADX warm up on it. Lists are trimmed to 500
                            # candles, so memory stays bounded.
TTL_OPTION_CHAIN = 30       # Option chain — very fresh
TTL_POSITIONS    = 10       # Positions — near real-time
TTL_ORDER_BOOK   = 10
TTL_SESSION      = 7 * 3600  # Session tokens — 7 hours


class RedisCache:
    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self):
        self._redis = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        await self._redis.ping()
        logger.info("Redis connected at %s", settings.REDIS_URL)

    async def disconnect(self):
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis disconnected.")

    def _r(self) -> aioredis.Redis:
        if not self._redis:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._redis

    # ── LTP ──────────────────────────────────────────────────────────────────

    async def set_ltp(self, token: str, ltp: float):
        await self._r().setex(f"ltp:{token}", TTL_TICK, str(ltp))

    async def get_ltp(self, token: str) -> Optional[float]:
        val = await self._r().get(f"ltp:{token}")
        return float(val) if val is not None else None

    async def get_multiple_ltp(self, tokens: list[str]) -> dict[str, Optional[float]]:
        keys = [f"ltp:{t}" for t in tokens]
        values = await self._r().mget(*keys)
        return {
            token: float(v) if v is not None else None
            for token, v in zip(tokens, values)
        }

    # ── Full tick ─────────────────────────────────────────────────────────────

    async def set_tick(self, token: str, tick_data: dict):
        await self._r().setex(f"tick:{token}", TTL_TICK, json.dumps(tick_data))

    async def get_tick(self, token: str) -> Optional[dict]:
        val = await self._r().get(f"tick:{token}")
        return json.loads(val) if val else None

    # ── OHLC candles ─────────────────────────────────────────────────────────

    async def push_candle(self, token: str, interval: str, candle: dict, max_candles: int = 500):
        """
        Push a new candle to a Redis list (newest at right).
        Keeps only the last max_candles entries.
        """
        key = f"ohlc:{token}:{interval}"
        pipe = self._r().pipeline()
        pipe.rpush(key, json.dumps(candle))
        pipe.ltrim(key, -max_candles, -1)
        pipe.expire(key, TTL_OHLC)
        await pipe.execute()

    async def get_candles(self, token: str, interval: str, count: int = 100) -> list[dict]:
        key = f"ohlc:{token}:{interval}"
        raw = await self._r().lrange(key, -count, -1)
        return [json.loads(r) for r in raw]

    async def set_candles(self, token: str, interval: str, candles: list[dict]):
        """Bulk-load candles (e.g., historical seed on startup)."""
        key = f"ohlc:{token}:{interval}"
        pipe = self._r().pipeline()
        pipe.delete(key)
        for candle in candles:
            pipe.rpush(key, json.dumps(candle))
        pipe.expire(key, TTL_OHLC)
        await pipe.execute()

    # ── Option chain ─────────────────────────────────────────────────────────

    async def set_option_chain(self, underlying: str, expiry: str, chain: dict):
        key = f"option_chain:{underlying}:{expiry}"
        await self._r().setex(key, TTL_OPTION_CHAIN, json.dumps(chain))

    async def get_option_chain(self, underlying: str, expiry: str) -> Optional[dict]:
        key = f"option_chain:{underlying}:{expiry}"
        val = await self._r().get(key)
        return json.loads(val) if val else None

    # ── Positions / Order book ────────────────────────────────────────────────

    async def set_positions(self, positions: list[dict]):
        await self._r().setex("positions", TTL_POSITIONS, json.dumps(positions))

    async def get_positions(self) -> list[dict]:
        val = await self._r().get("positions")
        return json.loads(val) if val else []

    async def set_order_book(self, orders: list[dict]):
        await self._r().setex("order_book", TTL_ORDER_BOOK, json.dumps(orders))

    async def get_order_book(self) -> list[dict]:
        val = await self._r().get("order_book")
        return json.loads(val) if val else []

    # ── Session tokens ────────────────────────────────────────────────────────

    async def set_angel_session(self, jwt: str, refresh: str, feed: str):
        data = {"jwt": jwt, "refresh": refresh, "feed": feed}
        await self._r().setex("session:angel", TTL_SESSION, json.dumps(data))

    async def get_angel_session(self) -> Optional[dict]:
        val = await self._r().get("session:angel")
        return json.loads(val) if val else None

    # ── Pub/Sub for real-time frontend push ───────────────────────────────────

    async def publish_tick(self, token: str, tick_data: dict):
        """Publish tick to Redis channel — consumed by WebSocket broadcaster."""
        await self._r().publish(f"feed:{token}", json.dumps(tick_data))

    async def publish_signal(self, strategy: str, signal: dict):
        """Publish a trading signal to the signals channel."""
        await self._r().publish(f"signals:{strategy}", json.dumps(signal))

    # ── Generic helpers ───────────────────────────────────────────────────────

    async def set(self, key: str, value: Any, ttl: int = 60):
        await self._r().setex(key, ttl, json.dumps(value))

    async def get(self, key: str) -> Optional[Any]:
        val = await self._r().get(key)
        return json.loads(val) if val else None

    async def delete(self, key: str):
        await self._r().delete(key)

    async def health(self) -> bool:
        try:
            await self._r().ping()
            return True
        except Exception:
            return False


# App-level singleton
redis_cache = RedisCache()


async def get_redis() -> RedisCache:
    """FastAPI dependency."""
    return redis_cache
