"""
System diagnostics — checks candles, config, VIX, option chain.
Run: docker exec nifty_backend python3 /app/scripts/diag_system.py
"""
import asyncio, sys
sys.path.insert(0, '/app')
from app.services.cache.redis_client import redis_cache
from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import TradingConfig, PaperPosition
from sqlalchemy import select, func
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

async def main():
    await redis_cache.connect()

    # ── Config ────────────────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(TradingConfig).where(TradingConfig.id == 1))
        cfg = r.scalar_one_or_none()
    print("=== Trading Config ===")
    if cfg:
        print(f"  study_phase: {cfg.study_phase}")
        print(f"  mode: {cfg.mode}")
        print(f"  capital: {cfg.capital}")
        print(f"  used_capital: {cfg.used_capital}")
        print(f"  trade_amount: {cfg.trade_amount}")
        print(f"  max_lots: {cfg.max_lots_per_trade}")
        print(f"  max_open_pos: {cfg.max_open_positions}")
        print(f"  max_trades_per_day: {cfg.max_trades_per_day}")
        print(f"  daily_trades_count: {cfg.daily_trades_count}")
        print(f"  max_daily_loss: {cfg.max_daily_loss}")
        print(f"  strategy_flags: {cfg.strategy_flags}")
    else:
        print("  NO CONFIG ROW!")

    # ── Open positions ─────────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(PaperPosition).where(PaperPosition.is_open == True)
        )
        open_pos = r.scalars().all()
    print(f"\n=== Open Positions ({len(open_pos)}) ===")
    for p in open_pos:
        print(f"  #{p.id} {p.underlying} {p.symbol} entry={p.actual_entry} sl={p.stop_loss} tgt={p.target}")

    # ── VIX / LTPs ────────────────────────────────────────────────────────
    print("\n=== Market State ===")
    vix_ltp = await redis_cache.get_ltp(settings.VIX_TOKEN)
    nifty_ltp = await redis_cache.get_ltp(settings.NIFTY_TOKEN)
    bank_ltp  = await redis_cache.get_ltp(settings.BANKNIFTY_TOKEN)
    print(f"  VIX token={settings.VIX_TOKEN} LTP={vix_ltp}")
    print(f"  NIFTY token={settings.NIFTY_TOKEN} LTP={nifty_ltp}")
    print(f"  BANKNIFTY token={settings.BANKNIFTY_TOKEN} LTP={bank_ltp}")

    # ── Candle counts ─────────────────────────────────────────────────────
    print("\n=== Candles in Redis ===")
    n_candles = await redis_cache.get_candles(settings.NIFTY_TOKEN, 'ONE_MINUTE', 500)
    b_candles = await redis_cache.get_candles(settings.BANKNIFTY_TOKEN, 'ONE_MINUTE', 500)
    
    today_str = str(datetime.now(IST).date())
    
    def today_candles(candles):
        return [c for c in candles if str(c.get("timestamp","")).startswith(today_str)
                and int(str(c.get("timestamp",""))[11:13]) * 60 + int(str(c.get("timestamp",""))[14:16]) >= 9*60+15]
    
    n_today = today_candles(n_candles)
    b_today = today_candles(b_candles)
    print(f"  NIFTY: {len(n_candles)} total, {len(n_today)} today")
    print(f"  BANKNIFTY: {len(b_candles)} total, {len(b_today)} today")
    if n_today:
        print(f"  NIFTY latest: {n_today[-1].get('timestamp')} close={n_today[-1].get('close')}")
    if b_today:
        print(f"  BANKNIFTY latest: {b_today[-1].get('timestamp')} close={b_today[-1].get('close')}")

    # ── IST time ───────────────────────────────────────────────────────────
    ist_now = datetime.now(IST)
    print(f"\n=== IST time now: {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')} ===")
    ist_mins = ist_now.hour * 60 + ist_now.minute
    in_session = 9*60+15 <= ist_mins <= 15*60+25
    print(f"  In market session: {in_session}")
    print(f"  Minutes since open: {max(0, ist_mins - (9*60+15))}")
    print(f"  Need 20 candles, have {len(n_today)} today-candles")
    print(f"  Minutes until strategy can run: {max(0, 25 - len(n_today))} more candles needed")

    # ── Avg volume state ───────────────────────────────────────────────────
    print("\n=== Spot Avg Volume (EMA) ===")
    n_avg = await redis_cache.get("avg_vol:spot:NIFTY")
    b_avg = await redis_cache.get("avg_vol:spot:BANKNIFTY")
    print(f"  NIFTY avg_vol: {n_avg}")
    print(f"  BANKNIFTY avg_vol: {b_avg}")

    await redis_cache.disconnect()

asyncio.run(main())
