"""Diagnose all blockers preventing trades from executing."""
import asyncio
from datetime import date
from app.services.cache.redis_client import redis_cache
from app.strategies.indicators import compute_all
from app.core.config import settings
from app.db.base import AsyncSessionLocal
from app.db.models.paper_trade import TradingConfig, StrategySignal, PaperPosition
from app.db.models.market_study import MarketDailySnapshot
from sqlalchemy import select, desc

async def main():
    print("=" * 60)
    print("       DIAGNOSTIC REPORT: WHY TRADES ARE NOT EXECUTING")
    print("=" * 60)

    # 1. Config Check
    async with AsyncSessionLocal() as db:
        cfg = (await db.execute(select(TradingConfig).where(TradingConfig.id == 1))).scalar_one_or_none()
        if cfg:
            print("\n[CHECK 1] CONFIGURATION:")
            print(f"  - Mode: {cfg.mode}")
            print(f"  - Study Phase: {cfg.study_phase}")
            print(f"  - Capital: Rs {cfg.capital}")
            print(f"  - Trade Amount: Rs {cfg.trade_amount}")
            print(f"  - Used Capital: Rs {cfg.used_capital}")
            print(f"  - Available Capital: Rs {cfg.capital - (cfg.used_capital or 0)}")
            print(f"  - Daily Trades Count: {cfg.daily_trades_count} / Max {cfg.max_trades_per_day}")

            if cfg.study_phase:
                print("  [!] BLOCKER: study_phase is True. Signals are logged but NO positions will open.")
            if (cfg.capital - (cfg.used_capital or 0)) < 2000:
                print("  [!] BLOCKER: Available capital is too low to buy even 1 lot of an option.")

        # Check pre-market daily snapshots
        prev_nifty = (await db.execute(select(MarketDailySnapshot).where(MarketDailySnapshot.underlying == "NIFTY").order_by(desc(MarketDailySnapshot.date)).limit(1))).scalar_one_or_none()
        print("\n[CHECK 2] PRE-MARKET DAILY SNAPSHOTS (for PDH/CPR/GAP strategies):")
        if prev_nifty:
            print(f"  - NIFTY Prev Day: Date={prev_nifty.date}, High={prev_nifty.high}, Low={prev_nifty.low}, Close={prev_nifty.close}")
        else:
            print("  [!] WARNING: No previous MarketDailySnapshot found for NIFTY in DB.")
            print("      PDH_PDL, CPR_VWAP, and GAP_VWAP_FILL strategies are DISABLED until 1 day snapshot exists.")

    # 2. Redis and Feed Data Check
    await redis_cache.connect()
    print("\n[CHECK 3] SPOT CANDLE DATA IN REDIS:")
    for u in ["NIFTY", "BANKNIFTY"]:
        token = settings.NIFTY_TOKEN if u == "NIFTY" else settings.BANKNIFTY_TOKEN
        candles = await redis_cache.get_candles(token, "ONE_MINUTE", 500)
        today_str = str(date.today())
        today_candles = [c for c in candles if str(c.get("timestamp", "")).startswith(today_str)]
        print(f"  - {u} (token {token}): Total={len(candles)} | Today-only={len(today_candles)}")

        if len(today_candles) < 20:
            print(f"    [!] HARD BLOCKER: len(today_candles) = {len(today_candles)} < 20.")
            print(f"        Line 189 in strategy_runner.py aborts before running ANY strategy because it requires >= 25 today-candles!")
        else:
            ind = compute_all(today_candles)
            atr_v = ind.get("atr") or 0
            atr_min = 2.5 if u == "NIFTY" else 8.0
            print(f"    - Indicators computed: ATR={atr_v:.2f}, VWAP={ind.get('vwap')}, Close={ind.get('close')}")
            if atr_v < atr_min:
                print(f"    [!] BLOCKER: ATR {atr_v:.2f} < minimum required {atr_min}. Market is considered too choppy.")

    print("\n[CHECK 4] INDIA VIX:")
    vix = await redis_cache.get_ltp("26017")
    print(f"  - VIX LTP in Redis: {vix}")
    if vix is not None:
        if vix < 12.0:
            print(f"  [!] BLOCKER: VIX {vix} < 12.0 (VIX_MIN). All strategies are skipped because VIX is too low.")
        elif vix > 20.0:
            print(f"  [!] BLOCKER: VIX {vix} > 20.0 (VIX_MAX). All strategies are skipped because VIX is too high.")
    else:
        print("  - VIX is not in Redis (VIX filter bypassed).")

    print("\n[CHECK 5] LIVE TICK STREAM (LTP KEYS):")
    ltp_keys = await redis_cache._redis.keys("ltp:*")
    print(f"  - Active LTP keys in Redis: {len(ltp_keys)}")
    if len(ltp_keys) == 0:
        print("  [!] BLOCKER: 0 option LTPs in Redis.")
        print("      Even if a strategy fires a signal, OptionSelector cannot find live option prices and will reject all options.")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
