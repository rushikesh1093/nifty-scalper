"""
Check OHLC table for spot tokens to understand seed fallback data.
Run: docker exec nifty_backend python3 /app/scripts/check_ohlc.py
"""
import asyncio, sys
sys.path.insert(0, '/app')
from app.db.base import AsyncSessionLocal
from app.db.models.ohlc import OHLC
from app.core.config import settings
from sqlalchemy import select, func
from datetime import datetime, timezone, timedelta, date

IST = timezone(timedelta(hours=5, minutes=30))

async def main():
    async with AsyncSessionLocal() as db:
        today = date.today()

        for token, name in [(settings.NIFTY_TOKEN, 'NIFTY'), (settings.BANKNIFTY_TOKEN, 'BANKNIFTY')]:
            # Count today's candles
            r = await db.execute(
                select(func.count(OHLC.id)).where(
                    OHLC.token == token,
                    OHLC.interval == 'ONE_MINUTE',
                    func.date(OHLC.timestamp) == today,
                )
            )
            count_today = r.scalar()

            # Count all candles
            r2 = await db.execute(
                select(func.count(OHLC.id)).where(
                    OHLC.token == token,
                    OHLC.interval == 'ONE_MINUTE',
                )
            )
            count_all = r2.scalar()

            # Latest candle
            r3 = await db.execute(
                select(OHLC).where(
                    OHLC.token == token,
                    OHLC.interval == 'ONE_MINUTE',
                    func.date(OHLC.timestamp) == today,
                ).order_by(OHLC.timestamp.desc()).limit(1)
            )
            latest = r3.scalar_one_or_none()

            # Earliest today candle
            r4 = await db.execute(
                select(OHLC).where(
                    OHLC.token == token,
                    OHLC.interval == 'ONE_MINUTE',
                    func.date(OHLC.timestamp) == today,
                ).order_by(OHLC.timestamp.asc()).limit(5)
            )
            earliest_rows = r4.scalars().all()

            print(f"\n=== {name} (token={token}) ===")
            print(f"  Total 1m candles in DB: {count_all}")
            print(f"  Today's 1m candles: {count_today}")
            if latest:
                ts_ist = latest.timestamp.replace(tzinfo=timezone.utc).astimezone(IST)
                print(f"  Latest: {ts_ist.strftime('%H:%M IST')} O={latest.open} H={latest.high} L={latest.low} C={latest.close} V={latest.volume}")
            if earliest_rows:
                print(f"  First {len(earliest_rows)} today:")
                for c in earliest_rows:
                    ts_ist = c.timestamp.replace(tzinfo=timezone.utc).astimezone(IST) if c.timestamp.tzinfo is None else c.timestamp.astimezone(IST)
                    print(f"    {ts_ist.strftime('%H:%M IST')} O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume}")

asyncio.run(main())
