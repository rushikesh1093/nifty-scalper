"""
Market data endpoints:
  GET  /market/ltp/{token}               — single LTP from Redis
  POST /market/ltp/bulk                  — multiple LTPs
  GET  /market/tick/{token}              — full tick snapshot
  GET  /market/candles/{token}           — OHLC candles (Redis or DB; 3m/5m/15m resampled from 1m)
  GET  /market/levels/{underlying}       — support / resistance levels (NIFTY | BANKNIFTY)
  GET  /market/option-chain/{underlying} — full option chain with Greeks
  GET  /market/feed/status               — WebSocket feed status
  POST /market/feed/subscribe            — add tokens to live feed
  WS   /market/ws/{token}               — WebSocket stream for frontend
"""
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.db.base import AsyncSessionLocal, get_db
from app.db.models.ohlc import OHLC
from app.db.models.market_study import MarketDailySnapshot
from app.db.models.option_chain import OptionChainSnapshot
from app.db.models.instrument import Instrument, InstrumentType
from app.schemas.market import (
    TickResponse, CandleResponse, OptionChainResponse, OptionStrikeResponse,
    LTPBulkRequest, LTPBulkResponse,
)
from app.schemas.levels import LevelsResponse
from app.services.cache.redis_client import redis_cache
from app.services.market_data.levels import (
    IST, compute_levels, resample_candles,
    merge_dedup_by_timestamp as _merge_dedup_by_timestamp,
)
from app.services.market_data.feed import market_feed
from app.services.market_data.market_manager import market_manager
from app.services.broker.angel_client import angel_client
from app.core.config import settings as _settings

router = APIRouter(prefix="/market", tags=["market"])
logger = logging.getLogger(__name__)


# ── LTP ───────────────────────────────────────────────────────────────────────

@router.get("/ltp/{token}", response_model=TickResponse)
async def get_ltp(token: str):
    """Get latest LTP for a token from Redis cache."""
    tick = await redis_cache.get_tick(token)
    if tick:
        return TickResponse(**tick)
    # Fallback to AngelOne REST
    try:
        quote = await angel_client.get_ltp("NFO", token, token)
        if quote:
            return TickResponse(
                token=token,
                exchange="NFO",
                ltp=float(quote.get("ltp", 0)),
            )
    except Exception:
        pass
    raise HTTPException(status_code=404, detail=f"No data for token {token}")


@router.post("/ltp/bulk", response_model=LTPBulkResponse)
async def get_bulk_ltp(req: LTPBulkRequest):
    """Get LTPs for multiple tokens from Redis in one call."""
    data = await redis_cache.get_multiple_ltp(req.tokens)
    return LTPBulkResponse(data=data)


@router.get("/tick/{token}", response_model=TickResponse)
async def get_tick(token: str):
    """Get full tick snapshot (LTP + OHLC + OI + bid/ask) from Redis."""
    tick = await redis_cache.get_tick(token)
    if not tick:
        raise HTTPException(status_code=404, detail=f"No tick data for token {token}")
    return TickResponse(**tick)


# ── OHLC Candles ──────────────────────────────────────────────────────────────

IntervalName = Literal["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"]
INTERVAL_MINUTES = {"ONE_MINUTE": 1, "THREE_MINUTE": 3, "FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15}
LEVELS_FETCH_HARD_CAP   = 4000    # ~10 sessions of 1m bars; bounds the DB read (NFR-4)
LEVELS_MIN_SPAN_CANDLES = 500     # ~1.3 sessions of 1m bars — floors the fetch so span_days > 1
                                  # whenever the ohlc table actually holds a prior session
RESAMPLE_SOURCE_CAP     = 1000    # max one-minute rows read to build resampled /candles bars


def _row_to_dict(r: OHLC) -> dict:
    """Normalise an ohlc row to the candle contract: same key names as the Redis
    candles; timestamps normalised to IST offset so the merge compares instants,
    not strings (ohlc stores UTC, Redis stores IST-offset ISO strings)."""
    return {
        "token": r.token, "symbol": r.symbol, "interval": r.interval,
        "timestamp": r.timestamp.astimezone(IST).isoformat(),
        "open": r.open, "high": r.high, "low": r.low, "close": r.close,
        "volume": r.volume, "oi": r.oi,
    }


async def _load_db_candles(session: AsyncSession, token: str, interval: str, count: int) -> list[dict]:
    rows = await session.execute(
        select(OHLC).where(OHLC.token == token, OHLC.interval == interval)
                    .order_by(desc(OHLC.timestamp)).limit(count)
    )
    return [_row_to_dict(r) for r in reversed(rows.scalars().all())]


async def _load_candles(session: AsyncSession, token: str, interval: str, count: int) -> tuple[list[dict], str]:
    """Cache-first loader for the /candles resample fallback. Takes a session rather than owning one."""
    cached = await redis_cache.get_candles(token, interval, count)
    if len(cached) >= count:
        return cached, "redis"
    db_candles = await _load_db_candles(session, token, interval, count)
    if not db_candles:
        return cached, "redis"
    if not cached:
        return db_candles, "db"
    merged = _merge_dedup_by_timestamp(db_candles, cached)   # union, ascending, cached wins on tie
    return merged[-count:], "merged"


async def _load_candles_for_levels(session: AsyncSession, token: str, count: int) -> tuple[list[dict], str]:
    """Levels-only loader: the ohlc table is ALWAYS queried, so levels can never
    silently degrade to a today-only view."""
    want    = max(count, LEVELS_MIN_SPAN_CANDLES)
    cached  = await redis_cache.get_candles(token, "ONE_MINUTE", want)
    db_rows = await _load_db_candles(session, token, "ONE_MINUTE", want)
    if not db_rows:
        return cached[-count:], "redis"          # only legitimate "redis" case: ohlc empty for this token
    if not cached:
        return db_rows[-count:], "db"
    return _merge_dedup_by_timestamp(db_rows, cached)[-count:], "merged"


@router.get("/candles/{token}", response_model=list[CandleResponse])
async def get_candles(
    token: str,
    interval: IntervalName = Query("ONE_MINUTE"),
    count:    int  = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Get OHLC candles for a token.
    Native interval: Redis first (fast), falls back to PostgreSQL — unchanged.
    Non-1m intervals with fewer than `count` native rows are resampled from
    one-minute data (Redis merged with the ohlc table).
    """
    # Native interval — cache-first exactly as before
    cached = await redis_cache.get_candles(token, interval, count)
    if interval == "ONE_MINUTE":
        if cached:
            return [CandleResponse(**c) for c in cached]
        rows = await _load_db_candles(db, token, interval, count)
        if not rows:
            raise HTTPException(status_code=404, detail="No candle data found")
        return [CandleResponse(**r) for r in rows]

    if len(cached) >= count:
        return [CandleResponse(**c) for c in cached]
    native_db = await _load_db_candles(db, token, interval, count)
    if len(native_db) >= count:
        return [CandleResponse(**r) for r in native_db]

    # Resample fallback from one-minute data
    minutes = INTERVAL_MINUTES[interval]
    one_min, _ = await _load_candles(db, token, "ONE_MINUTE", min(count * minutes, RESAMPLE_SOURCE_CAP))
    if not one_min:
        raise HTTPException(status_code=404, detail="No candle data found")
    bars = resample_candles(
        one_min, minutes, token=token, interval=interval, symbol=one_min[-1].get("symbol"),
    )[-count:]
    return [CandleResponse(**b) for b in bars]


# ── Support / resistance levels ───────────────────────────────────────────────

@router.get("/levels/{underlying}", response_model=LevelsResponse)
async def get_levels(
    underlying: str,
    interval:   IntervalName = Query("ONE_MINUTE"),
    lookback:   int = Query(500, ge=50, le=1000, description="bars at the requested interval"),
    max_levels: int = Query(12, ge=1, le=30),
):
    """
    Support/resistance levels for NIFTY / BANKNIFTY spot, computed by the pure
    app.services.market_data.levels module. The DB session is held only for the
    candle + previous-day snapshot reads, never across the computation.
    """
    u = underlying.upper()
    if u not in ("NIFTY", "BANKNIFTY"):
        raise HTTPException(status_code=400, detail="underlying must be NIFTY or BANKNIFTY")
    spot_token = _settings.NIFTY_TOKEN if u == "NIFTY" else _settings.BANKNIFTY_TOKEN
    minutes = INTERVAL_MINUTES[interval]
    fetch_count = min(lookback * minutes, LEVELS_FETCH_HARD_CAP)
    session_date = datetime.now(IST).date()

    try:
        async with AsyncSessionLocal() as db:
            candles, candle_source = await _load_candles_for_levels(db, spot_token, fetch_count)
            from app.services.market_data.prev_session import prev_session_snapshot
            snap = await prev_session_snapshot(db, u, session_date)
            prev_day = (
                {"high": snap.high, "low": snap.low, "close": snap.close, "date": snap.date.isoformat()}
                if snap else None
            )
        logger.debug("levels %s: db session closed, entering compute_levels (%d bars)", u, len(candles))
        spot = await redis_cache.get_ltp(spot_token)
    except (RedisError, RuntimeError) as exc:
        logger.error("levels %s: cache unavailable: %s", u, exc)
        raise HTTPException(status_code=503, detail="cache unavailable")

    if not candles:
        logger.warning("levels %s: no candle data for %s %s", u, spot_token, interval)
        raise HTTPException(status_code=404, detail=f"No candle data for {spot_token} {interval}")
    if candle_source == "redis":
        logger.info("levels %s: ohlc table empty for %s — levels from Redis only", u, spot_token)
    if prev_day is None:
        logger.info("levels %s: no previous-day snapshot before %s", u, session_date)

    extra_warnings: list[str] = []
    if spot is None or spot <= 0:
        try:
            spot = float(candles[-1]["close"])
        except Exception:
            spot = 0.0
        if spot <= 0:
            raise HTTPException(status_code=404, detail=f"No valid spot price for {u}")
        extra_warnings.append("spot_from_candle_close")
        logger.debug("levels %s: spot LTP missing — using last candle close", u)

    series = candles if minutes == 1 else resample_candles(
        candles, minutes, token=spot_token, interval=interval, symbol=candles[-1].get("symbol"),
    )
    try:
        levels, meta = compute_levels(
            series, spot, prev_day,
            session_date=session_date, tz=IST, max_levels=max_levels,
            orb_candles=max(1, 15 // minutes),   # opening range = 09:15-09:30 at every interval
        )
    except Exception:
        logger.error("levels %s: level computation failed", u, exc_info=True)
        raise HTTPException(status_code=500, detail="level computation failed")

    if "insufficient_candles" in meta["warnings"]:
        logger.debug("levels %s: insufficient candles (%d bars at %s)", u, len(series), interval)
    meta["warnings"] = meta["warnings"] + extra_warnings
    meta.update(candle_source=candle_source, candles_used=len(candles), resampled_bars=len(series))
    return LevelsResponse(
        underlying=u, token=spot_token, interval=interval, spot=spot,
        as_of=datetime.now(IST).isoformat(),
        levels=[asdict(l) for l in levels],
        meta=meta,
    )


# ── Option Chain ──────────────────────────────────────────────────────────────

@router.get("/option-chain/{underlying}", response_model=OptionChainResponse)
async def get_option_chain(
    underlying: str,
    expiry: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to nearest"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the full option chain for NIFTY or BANKNIFTY.
    Merges live LTP from Redis with instrument metadata from DB.
    Calculates PCR and max pain.
    """
    underlying = underlying.upper()

    # Resolve expiry
    if expiry:
        expiry_date = date.fromisoformat(expiry)
    else:
        result = await db.execute(
            select(Instrument.expiry)
            .where(
                Instrument.name == underlying,
                Instrument.instrument_type == InstrumentType.OPTIDX,
                Instrument.is_active == True,
                Instrument.expiry >= date.today(),
            )
            .order_by(Instrument.expiry)
            .limit(1)
        )
        row = result.scalar()
        if not row:
            raise HTTPException(status_code=404, detail=f"No active expiry for {underlying}")
        expiry_date = row

    # Fetch instruments for this expiry
    result = await db.execute(
        select(Instrument).where(
            Instrument.name == underlying,
            Instrument.instrument_type == InstrumentType.OPTIDX,
            Instrument.expiry == expiry_date,
            Instrument.is_active == True,
        ).order_by(Instrument.strike, Instrument.option_type)
    )
    instruments = result.scalars().all()

    if not instruments:
        raise HTTPException(status_code=404, detail="No instruments found for option chain")

    # Bulk LTP from Redis
    tokens = [i.token for i in instruments]
    ltps = await redis_cache.get_multiple_ltp(tokens)

    # Bulk-fetch all tick data in one Redis pipeline — avoids N sequential round-trips
    pipe = redis_cache._r().pipeline()
    for token in tokens:
        pipe.get(f"tick:{token}")
    raw_ticks = await pipe.execute()
    import json as _json
    tick_map: dict[str, dict] = {}
    for token, raw in zip(tokens, raw_ticks):
        if raw:
            try:
                tick_map[token] = _json.loads(raw)
            except Exception:
                pass

    # Pull IV from the cached REST option chain snapshot (IV is not in the WS binary tick)
    iv_map: dict[str, float] = {}
    chain_snap = await redis_cache.get_option_chain(underlying, str(expiry_date))
    if chain_snap:
        for s in chain_snap.get("strikes", []):
            token_s = s.get("token")
            iv_val  = s.get("iv")
            if token_s and iv_val:
                raw_iv = float(iv_val)
                # Normalise: AngelOne may return 0.20 (decimal) or 20.0 (percent)
                iv_map[token_s] = raw_iv * 100 if raw_iv < 1.0 else raw_iv

    # Build chain
    strikes: list[OptionStrikeResponse] = []
    ce_oi = 0
    pe_oi = 0
    pain_data: dict[float, float] = {}  # strike → total extrinsic value

    for inst in instruments:
        ltp  = ltps.get(inst.token)
        tick = tick_map.get(inst.token)
        oi     = tick.get("oi", 0) if tick else 0
        volume = tick.get("volume", 0) if tick else 0
        # IV from chain snapshot cache; tick.iv is empty (not in WS binary protocol)
        iv     = iv_map.get(inst.token) or (tick.get("iv") if tick else None)
        # Use ltp from tick if the bulk LTP missed it (e.g. key expired)
        if ltp is None and tick:
            ltp = tick.get("ltp")

        if inst.option_type and inst.option_type.value == "CE":
            ce_oi += oi or 0
        else:
            pe_oi += oi or 0

        strikes.append(OptionStrikeResponse(
            token=inst.token,
            symbol=inst.symbol,
            strike=inst.strike,
            option_type=inst.option_type.value if inst.option_type else "",
            expiry=inst.expiry,
            ltp=ltp,
            oi=oi,
            volume=volume,
            iv=float(iv) if iv else None,
        ))

    # PCR (Put-Call Ratio by OI)
    pcr = round(pe_oi / ce_oi, 3) if ce_oi > 0 else None

    # Get underlying spot
    spot_token = _settings.NIFTY_TOKEN if underlying == "NIFTY" else _settings.BANKNIFTY_TOKEN
    spot_ltp_val = await redis_cache.get_ltp(spot_token) or 0

    return OptionChainResponse(
        underlying=underlying,
        underlying_ltp=spot_ltp_val,
        expiry=expiry_date,
        strikes=strikes,
        pcr=pcr,
    )


# ── Feed management ────────────────────────────────────────────────────────────

@router.get("/data-health")
async def data_health():
    """Live market-data health (app.services.market_data.data_health): candle
    integrity for both indices and their futures, and quote freshness, crossed /
    one-sided / wide quotes and feed latency across every subscribed token.
    Read-only; nothing is repaired or substituted."""
    from datetime import timezone
    from app.runtime.context import params as _p
    from app.services.market_data import data_health as dh
    now = datetime.now(timezone.utc)
    p = _p()
    try:
        series = {"NIFTY": _settings.NIFTY_TOKEN, "BANKNIFTY": _settings.BANKNIFTY_TOKEN}
        for und in ("NIFTY", "BANKNIFTY"):
            fut = await redis_cache.get(f"fut_token:{und}")
            if fut:
                series[f"{und}_FUT"] = str(fut)
        candles = {name: dh.candle_checks(await redis_cache.get_candles(tok, "ONE_MINUTE", 500), now)
                   for name, tok in series.items()}
        tokens = [t for toks in market_feed._subscriptions.values() for t in toks]
        ticks = await redis_cache.get_multiple_ticks(tokens)       # one round trip
        quotes = dh.quote_checks(ticks, now, p.stale_tick_seconds, p.quote_max_spread_pct)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"cache unavailable: {exc}")
    return {"as_of": now.isoformat(), "in_session": dh.in_session(now),
            "feed_connected": market_feed.is_connected,
            "status": dh.overall([c["status"] for c in candles.values()] + [quotes["status"]]),
            "candles": candles, "quotes": quotes}


@router.get("/feed/status")
async def feed_status():
    return {
        "connected":     market_feed.is_connected,
        "subscriptions": market_feed.subscription_count,
    }


@router.post("/feed/subscribe")
async def subscribe_tokens(exchange: str, tokens: list[str] = None, body: dict = None):
    """Dynamically add tokens to the live WebSocket feed."""
    # Accept tokens from JSON body: {"tokens": [...]}
    token_list = tokens or (body or {}).get("tokens", [])
    if not token_list:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=422, detail="Provide tokens in request body: {\"tokens\": [...]}")
    await market_manager.subscribe_tokens(exchange, token_list)
    return {"status": "subscribed", "count": len(token_list)}


@router.post("/feed/unsubscribe")
async def unsubscribe_tokens(exchange: str, tokens: list[str]):
    await market_manager.unsubscribe_tokens(exchange, tokens)
    return {"status": "unsubscribed", "count": len(tokens)}


@router.post("/seed/spot")
async def reseed_spot_candles():
    """
    Re-seed NIFTY / BANKNIFTY spot candles from the OHLC DB table into Redis.
    Useful after a mid-session restart when AngelOne 403s on the historical API
    — strategies need ≥25 candles and this loads whatever the DB already has.
    """
    result = await market_manager.reseed_spot_candles()
    return {"status": "ok", "candles_loaded": result}


# ── WebSocket — live tick stream to frontend ──────────────────────────────────

@router.websocket("/ws/{token}")
async def websocket_tick_stream(websocket: WebSocket, token: str):
    """
    WebSocket endpoint that streams live ticks for a token to the frontend.
    Uses Redis pub/sub to decouple from the AngelOne feed loop.
    """
    await websocket.accept()
    logger.info("Frontend WS connected for token %s", token)

    import redis.asyncio as aioredis
    from app.core.config import settings

    pubsub_client = aioredis.from_url(
        settings.REDIS_URL, encoding="utf-8", decode_responses=True
    )
    pubsub = pubsub_client.pubsub()
    await pubsub.subscribe(f"feed:{token}")

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    await websocket.send_text(message["data"])
                except WebSocketDisconnect:
                    break
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        await pubsub.unsubscribe(f"feed:{token}")
        await pubsub_client.aclose()
        logger.info("Frontend WS disconnected for token %s", token)


@router.websocket("/ws/option-chain/{underlying}")
async def websocket_option_chain(websocket: WebSocket, underlying: str):
    """
    WebSocket that streams live LTP updates for all strikes of an option chain.
    Sends a JSON tick payload every time any subscribed token ticks.

    Performance notes:
    - Uses a dedicated aioredis pubsub connection (required by redis-py pubsub API).
    - Channel count is capped at MAX_CHANNELS to prevent thundering-herd on large
      underlyings (BankNifty can have 200+ strikes).
    - A background ping task keeps the WS alive; the listener task handles messages.
      Both tasks are cancelled cleanly on disconnect.
    """
    MAX_CHANNELS = 120  # ~60 strikes × CE+PE; more than enough for ATM ± 30

    await websocket.accept()
    import redis.asyncio as aioredis
    from app.core.config import settings
    from sqlalchemy import select
    from app.db.models.instrument import Instrument, InstrumentType
    from app.db.base import AsyncSessionLocal

    # ── Resolve tokens for nearest expiry (DB query, done once per connect) ──
    tokens: list[str] = []
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Instrument.token, Instrument.expiry)
                .where(
                    Instrument.name == underlying.upper(),
                    Instrument.instrument_type == InstrumentType.OPTIDX,
                    Instrument.is_active == True,
                    Instrument.expiry >= date.today(),
                )
                .order_by(Instrument.expiry)
                .limit(MAX_CHANNELS + 1)  # fetch only what we'll use
            )
            rows = result.fetchall()
    except Exception as exc:
        logger.error("WS option-chain DB lookup failed: %s", exc)
        await websocket.close(code=1011)
        return

    if rows:
        nearest_expiry = rows[0][1]
        tokens = [r[0] for r in rows if r[1] == nearest_expiry][:MAX_CHANNELS]

    if not tokens:
        logger.warning("WS option-chain: no tokens found for %s", underlying)
        await websocket.close(code=1000)
        return

    pubsub_client = aioredis.from_url(
        settings.REDIS_URL, encoding="utf-8", decode_responses=True,
        max_connections=2,  # pubsub only needs 1 connection
    )
    pubsub = pubsub_client.pubsub()
    channels = [f"feed:{t}" for t in tokens]
    await pubsub.subscribe(*channels)
    logger.info("WS option-chain %s: subscribed to %d channels", underlying, len(channels))

    async def _listen():
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])

    async def _ping():
        """Keep connection alive and detect dead clients."""
        while True:
            await asyncio.sleep(20)
            await websocket.send_text('{"ping":1}')

    listen_task = asyncio.create_task(_listen())
    ping_task   = asyncio.create_task(_ping())

    try:
        done, pending = await asyncio.wait(
            [listen_task, ping_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for t in pending:
            t.cancel()
    except (WebSocketDisconnect, asyncio.CancelledError):
        listen_task.cancel()
        ping_task.cancel()
    finally:
        await pubsub.unsubscribe(*channels)
        await pubsub_client.aclose()
        logger.info("WS option-chain %s: disconnected (%d channels released)", underlying, len(channels))

