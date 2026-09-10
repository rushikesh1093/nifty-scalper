"""Quick Redis diagnostic — check candles, LTP, session."""
import asyncio
import json
from datetime import date
import redis.asyncio as aioredis


async def check():
    r = aioredis.from_url("redis://localhost:6379/0", encoding="utf-8", decode_responses=True)
    await r.ping()
    print("Redis: connected")

    for token, name in [("26000", "NIFTY"), ("26009", "BANKNIFTY")]:
        key = f"ohlc:{token}:ONE_MINUTE"
        raw = await r.lrange(key, 0, -1)
        if raw:
            candles = [json.loads(x) for x in raw]
            print(f"\n{name} candles: {len(candles)} total")
            if candles:
                first_ts = candles[0].get("timestamp", "?")
                last_ts = candles[-1].get("timestamp", "?")
                print(f"  First: {first_ts}")
                print(f"  Last:  {last_ts}")
                today_str = str(date.today())
                today_candles = [c for c in candles if str(c.get("timestamp", "")).startswith(today_str)]
                print(f"  Today candles: {len(today_candles)}")
        else:
            print(f"\n{name} candles: NONE in Redis")

    for token, name in [("26000", "NIFTY"), ("26009", "BANKNIFTY"), ("26017", "VIX")]:
        ltp = await r.get(f"ltp:{token}")
        print(f"{name} LTP: {ltp}")

    session = await r.get("session:angel")
    print(f"\nAngelOne session: {'exists' if session else 'MISSING'}")

    await r.aclose()


asyncio.run(check())
