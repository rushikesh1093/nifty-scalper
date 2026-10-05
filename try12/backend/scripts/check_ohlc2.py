"""
Check what OHLC dates exist and how today's data looks vs the full day.
"""
import asyncio, sys
sys.path.insert(0, '/app')
from app.db.base import AsyncSessionLocal
from app.db.models.ohlc import OHLC
from app.core.config import settings
from sqlalchemy import select, func, distinct
from datetime import timezone, timedelta, date

IST = timezone(timedelta(hours=5, minutes=30))

async def main():
    async with AsyncSessionLocal() as db:
        # What dates exist?
        for token, name in [(settings.NIFTY_TOKEN, 'NIFTY'), (settings.BANKNIFTY_TOKEN, 'BANKNIFTY')]:
            r = await db.execute(
                select(func.date(OHLC.timestamp), func.count(OHLC.id))
                .where(OHLC.token == token, OHLC.interval == 'ONE_MINUTE')
                .group_by(func.date(OHLC.timestamp))
                .order_by(func.date(OHLC.timestamp).desc())
                .limit(10)
            )
            rows = r.all()
            print(f"\n{name} — candle counts by date (most recent first):")
            for d, cnt in rows:
                print(f"  {d}: {cnt} candles")

        # Today full: check if 09:15 exists
        today = date.today()
        r = await db.execute(
            select(func.count(OHLC.id)).where(
                OHLC.token == settings.NIFTY_TOKEN,
                OHLC.interval == 'ONE_MINUTE',
                func.date(OHLC.timestamp) == today,
                func.extract('hour', OHLC.timestamp) * 60 + func.extract('minute', OHLC.timestamp) >= 9 * 60 + 15 - 330,
                # 9:15 IST = 3:45 UTC. DB stores UTC. 9*60+15 - 5*60-30 = 3:45
            )
        )
        print(f"\nNIFTY today candles where ts >= 03:45 UTC (= 09:15 IST): {r.scalar()}")

        # Earliest candle today
        r2 = await db.execute(
            select(OHLC.timestamp).where(
                OHLC.token == settings.NIFTY_TOKEN,
                OHLC.interval == 'ONE_MINUTE',
                func.date(OHLC.timestamp) == today,
            ).order_by(OHLC.timestamp.asc()).limit(1)
        )
        earliest = r2.scalar_one_or_none()
        if earliest:
            ist = earliest.replace(tzinfo=timezone.utc).astimezone(IST)
            print(f"Earliest NIFTY candle today: {ist.strftime('%H:%M IST')} (UTC: {earliest})")

asyncio.run(main())
