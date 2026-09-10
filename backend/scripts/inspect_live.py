"""Inspect live Redis candles, database signals, and paper positions."""
import asyncio
import json
from datetime import date, datetime
import redis.asyncio as aioredis
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import StrategySignal, PaperPosition, DailyPnLSummary, TradingConfig
from sqlalchemy import select, func, desc


async def inspect():
    print("================ LIVE SYSTEM DIAGNOSTIC ================")
    # 1. Check Redis
    r = aioredis.from_url("redis://localhost:6379/0", encoding="utf-8", decode_responses=True)
    await r.ping()
    print("[1] REDIS STATE:")
    for token, name in [("26000", "NIFTY (Spot)"), ("26009", "BANKNIFTY (Spot)"), ("26017", "INDIA VIX")]:
        ltp = await r.get(f"ltp:{token}")
        candles_list = await r.lrange(f"ohlc:{token}:ONE_MINUTE", 0, -1)
        count = len(candles_list)
        print(f"  - {name} ({token}): LTP = {ltp} | Candles in Redis List: {count}")
        if count > 0:
            first_c = json.loads(candles_list[0])
            last_c = json.loads(candles_list[-1])
            print(f"      First Candle: {first_c.get('timestamp')} | O={first_c.get('open')} H={first_c.get('high')} L={first_c.get('low')} C={first_c.get('close')} V={first_c.get('volume')}")
            print(f"      Latest Candle: {last_c.get('timestamp')} | O={last_c.get('open')} H={last_c.get('high')} L={last_c.get('low')} C={last_c.get('close')} V={last_c.get('volume')}")

    keys = await r.keys("ltp:*")
    print(f"  - Total active LTP keys in Redis: {len(keys)}")
    vix_ltp = await r.get("ltp:26017")
    print(f"  - VIX LTP: {vix_ltp}")

    # 2. Check Database Signals & Positions
    async with AsyncSessionLocal() as db:
        print("\n[2] DATABASE STATE:")
        # Config
        r_cfg = await db.execute(select(TradingConfig).where(TradingConfig.id == 1))
        cfg = r_cfg.scalar_one_or_none()
        if cfg:
            print(f"  - Config: Mode={cfg.mode}, StudyPhase={cfg.study_phase}, Capital=₹{cfg.capital}, TradeAmount=₹{cfg.trade_amount}, MaxOpenPos={cfg.max_open_positions}, MaxTradesDay={cfg.max_trades_per_day}")

        # Signals today
        r_sig = await db.execute(select(StrategySignal).order_by(desc(StrategySignal.signal_time)).limit(10))
        sigs = r_sig.scalars().all()
        print(f"  - Total recent signals in DB: {len(sigs)}")
        for s in sigs:
            print(f"      Signal #{s.id} [{s.signal_time}] {s.strategy_name} on {s.underlying} {s.direction.value} | Status={s.status.value} | Spot={s.spot_price} | Strike={s.suggested_strike} | Symbol={s.suggested_symbol}")

        # Open Positions
        r_pos = await db.execute(select(PaperPosition).order_by(desc(PaperPosition.entry_time)).limit(10))
        positions = r_pos.scalars().all()
        print(f"  - Positions in DB: {len(positions)}")
        for p in positions:
            print(f"      Pos #{p.id} {p.strategy_name} {p.symbol} | Dir={p.direction.value} | Open={p.is_open} | Lots={p.lots} | Entry={p.actual_entry} | Exit={p.actual_exit} | SL={p.stop_loss} | TGT={p.target} | NetPnL=₹{p.net_pnl}")

        # Daily PnL summary
        r_pnl = await db.execute(select(DailyPnLSummary).where(DailyPnLSummary.date == date.today()))
        pnls = r_pnl.scalars().all()
        print(f"  - Today PnL Summaries: {len(pnls)}")
        for pnl in pnls:
            print(f"      {pnl.strategy_name}: Trades={pnl.total_trades}, Wins={pnl.winning_trades}, NetPnL=₹{pnl.net_pnl}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(inspect())
