"""
Clear all test trades, paper positions, strategy signals, and daily P&L summaries.
Resets used capital and daily trade counters back to 0.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, update
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import (
    PaperPosition, StrategySignal, DailyPnLSummary, TradingConfig,
)
from app.db.models.trade import Trade
from app.db.models.order import Order
from app.services.cache.redis_client import redis_cache


async def clear_all_test_trades():
    print("=" * 60)
    print("         CLEARING ALL TEST TRADES AND SIGNALS")
    print("=" * 60)

    # 1. Database cleanup
    async with AsyncSessionLocal() as db:
        r_pos = await db.execute(delete(PaperPosition))
        pos_deleted = r_pos.rowcount

        r_sig = await db.execute(delete(StrategySignal))
        sig_deleted = r_sig.rowcount

        r_pnl = await db.execute(delete(DailyPnLSummary))
        pnl_deleted = r_pnl.rowcount

        r_orders = await db.execute(delete(Order).where(Order.is_paper == True))
        orders_deleted = r_orders.rowcount

        r_trades = await db.execute(delete(Trade).where(Trade.is_paper == True))
        trades_deleted = r_trades.rowcount

        # Reset TradingConfig
        await db.execute(
            update(TradingConfig)
            .where(TradingConfig.id == 1)
            .values(
                daily_trades_count=0,
                used_capital=0.0,
            )
        )
        await db.commit()

    print(f"  [DB] Deleted {pos_deleted} paper positions.")
    print(f"  [DB] Deleted {sig_deleted} strategy signals.")
    print(f"  [DB] Deleted {pnl_deleted} daily P&L summaries.")
    print(f"  [DB] Deleted {orders_deleted} paper orders.")
    print(f"  [DB] Deleted {trades_deleted} paper trades.")
    print("  [DB] Reset TradingConfig: daily_trades_count=0, used_capital=0.0")

    # 2. Redis cleanup
    try:
        await redis_cache.connect()
        pos_keys = await redis_cache._redis.keys("pos:*")
        if pos_keys:
            await redis_cache._redis.delete(*pos_keys)
        await redis_cache.delete("positions")
        await redis_cache.delete("order_book")
        print(f"  [REDIS] Removed {len(pos_keys)} active position keys.")
        await redis_cache.disconnect()
    except Exception as e:
        print(f"  [REDIS] Warning clearing redis: {e}")

    print("\n" + "=" * 60)
    print("  [SUCCESS] All test trades and signals cleared successfully!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(clear_all_test_trades())

