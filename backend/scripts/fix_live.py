"""
Fix live issues on the running container without a restart:
1. Patch capital to ₹1,00,000 in DB
2. Re-seed spot candles from OHLC DB table into Redis
3. Show final state

Run: docker exec nifty_backend python3 /app/scripts/fix_live.py
"""
import asyncio, sys
sys.path.insert(0, '/app')
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import TradingConfig
from app.db.models.ohlc import OHLC
from app.services.cache.redis_client import redis_cache
from app.core.config import settings
from sqlalchemy import select, update
from datetime import datetime, timezone, timedelta, date

IST = timezone(timedelta(hours=5, minutes=30))


async def patch_capital():
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(TradingConfig).where(TradingConfig.id == 1))
        cfg = r.scalar_one_or_none()
        if not cfg:
            print("ERROR: No TradingConfig row found!")
            return False

        print(f"Before: capital={cfg.capital}, trade_amount={cfg.trade_amount}, "
              f"used_capital={cfg.used_capital}, study_phase={cfg.study_phase}")

        patches = {}
        if cfg.capital < 50000:
            patches["capital"] = 100000.0
        if cfg.study_phase:
            patches["study_phase"] = False
        if cfg.max_lots_per_trade < 2:
            patches["max_lots_per_trade"] = 5

        if patches:
            await db.execute(
                update(TradingConfig).where(TradingConfig.id == 1).values(**patches)
            )
            await db.commit()
            print(f"Patched: {patches}")
        else:
            print("Capital already OK, no patch needed.")

        r2 = await db.execute(select(TradingConfig).where(TradingConfig.id == 1))
        cfg2 = r2.scalar_one_or_none()
        print(f"After: capital={cfg2.capital}, study_phase={cfg2.study_phase}, "
              f"max_lots={cfg2.max_lots_per_trade}")
        return True


async def reseed_spot():
    today = date.today()
    # 09:15 IST = 03:45 UTC
    session_open_utc = datetime.combine(today, datetime.min.time().replace(hour=3, minute=45),
                                        tzinfo=timezone.utc)

    for token, name in [
        (settings.NIFTY_TOKEN, "NIFTY"),
        (settings.BANKNIFTY_TOKEN, "BANKNIFTY"),
    ]:
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

        if not rows:
            print(f"{name}: No DB candles found for today (session >= 09:15 IST)")
            continue

        candle_dicts = [
            {
                "token":     token,
                "symbol":    name,
                "interval":  "ONE_MINUTE",
                "timestamp": row.timestamp.replace(tzinfo=timezone.utc).astimezone(IST).isoformat(),
                "open":      float(row.open),
                "high":      float(row.high),
                "low":       float(row.low),
                "close":     float(row.close),
                "volume":    int(row.volume or 0),
            }
            for row in rows
        ]

        await redis_cache.set_candles(token, "ONE_MINUTE", candle_dicts)

        # Verify
        loaded = await redis_cache.get_candles(token, "ONE_MINUTE", 500)
        print(f"{name}: Seeded {len(candle_dicts)} candles into Redis "
              f"(earliest: {candle_dicts[0]['timestamp'][:16]}, "
              f"latest: {candle_dicts[-1]['timestamp'][:16]}). "
              f"Now in Redis: {len(loaded)}")


async def main():
    await redis_cache.connect()

    print("=" * 60)
    print("STEP 1 — Patch capital")
    print("=" * 60)
    await patch_capital()

    print()
    print("=" * 60)
    print("STEP 2 — Re-seed spot candles from DB")
    print("=" * 60)
    await reseed_spot()

    print()
    print("=" * 60)
    print("STEP 3 — Final state")
    print("=" * 60)

    for token, name in [
        (settings.NIFTY_TOKEN, "NIFTY"),
        (settings.BANKNIFTY_TOKEN, "BANKNIFTY"),
    ]:
        candles = await redis_cache.get_candles(token, "ONE_MINUTE", 500)
        today_str = str(datetime.now(IST).date())
        today_c = [
            c for c in candles
            if str(c.get("timestamp","")).startswith(today_str)
        ]
        print(f"{name}: {len(today_c)} today-candles in Redis")
        if today_c:
            print(f"  earliest: {today_c[0]['timestamp'][:16]}")
            print(f"  latest:   {today_c[-1]['timestamp'][:16]}")
            print(f"  need 20 to start strategies: {'YES' if len(today_c) >= 20 else f'NO — wait {20-len(today_c)} more minutes'}")

    await redis_cache.disconnect()

asyncio.run(main())
