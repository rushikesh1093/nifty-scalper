"""
Clear ONE run's paper positions, strategy signals and daily P&L summaries.

    python scripts/clear_test_trades.py --run-id PAPER
    python scripts/clear_test_trades.py --run-id RPL-01J...

``--run-id`` is required with no default (design §17.2): wiping every run —
including the replay runs a validation report cites — must not be the
accidental case. The legacy paper Order/Trade bookkeeping is not run-shared
and is still cleared unscoped. The trading_config display caches are then
recomputed from what is left (reconcile_used_capital), never zeroed.
"""
import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import PaperPosition, StrategySignal, DailyPnLSummary
from app.db.models.trade import Trade
from app.db.models.order import Order
from app.services.cache.redis_client import redis_cache

RUN_ID_PATTERN = re.compile(r"^(PAPER|RPL-[A-Za-z0-9]{1,26})$")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-id", required=True,
                    help="the run to clear: PAPER or RPL-<id> (no default)")
    args = ap.parse_args(argv)
    if not RUN_ID_PATTERN.match(args.run_id):
        ap.error(f"--run-id must match {RUN_ID_PATTERN.pattern}")
    return args


async def clear_run(run_id: str, session_factory=None):
    session_factory = session_factory or AsyncSessionLocal
    print("=" * 60)
    print(f"         CLEARING RUN {run_id}")
    print("=" * 60)

    async with session_factory() as db:
        pos_deleted = (await db.execute(
            delete(PaperPosition).where(PaperPosition.run_id == run_id))).rowcount
        sig_deleted = (await db.execute(
            delete(StrategySignal).where(StrategySignal.run_id == run_id))).rowcount
        pnl_deleted = (await db.execute(
            delete(DailyPnLSummary).where(DailyPnLSummary.run_id == run_id))).rowcount
        # not run-shared (no replay path writes them): unscoped by design
        orders_deleted = (await db.execute(delete(Order).where(Order.is_paper == True))).rowcount
        trades_deleted = (await db.execute(delete(Trade).where(Trade.is_paper == True))).rowcount
        await db.commit()

    from app.services.trading.mode_manager import mode_manager
    await mode_manager.reconcile_used_capital(session_factory=session_factory)

    print(f"  [DB] Deleted {pos_deleted} paper positions of {run_id}.")
    print(f"  [DB] Deleted {sig_deleted} strategy signals of {run_id}.")
    print(f"  [DB] Deleted {pnl_deleted} daily P&L summaries of {run_id}.")
    print(f"  [DB] Deleted {orders_deleted} paper orders.")
    print(f"  [DB] Deleted {trades_deleted} paper trades.")
    print("  [DB] Recomputed the PAPER display caches from the remaining positions.")
    return pos_deleted, sig_deleted, pnl_deleted


async def clear_redis_positions():
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


async def main(argv=None):
    args = parse_args(argv)
    await clear_run(args.run_id)
    await clear_redis_positions()
    print("\n" + "=" * 60)
    print(f"  [SUCCESS] Run {args.run_id} cleared.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
