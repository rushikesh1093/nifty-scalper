import asyncio, sys
sys.path.insert(0, '/app')
from datetime import datetime, timezone, timedelta
from app.services.cache.redis_client import redis_cache
from app.strategies.indicators import compute_all
from app.core.config import settings

IST = timezone(timedelta(hours=5, minutes=30))

async def main():
    await redis_cache.connect()
    today_str = str(datetime.now(IST).date())

    for name, token in [('NIFTY', settings.NIFTY_TOKEN), ('BANKNIFTY', settings.BANKNIFTY_TOKEN)]:
        all_c = await redis_cache.get_candles(token, 'ONE_MINUTE', 500)
        today_c = []
        for c in all_c:
            ts = str(c.get('timestamp', ''))
            if not ts.startswith(today_str):
                continue
            try:
                h = int(ts[11:13]); m = int(ts[14:16])
                if h * 60 + m < 9 * 60 + 15:
                    continue
            except Exception:
                pass
            today_c.append(c)

        if len(today_c) >= 15:
            ind      = compute_all(today_c)
            atr      = ind.get('atr') or 0
            last     = today_c[-1]
            high     = float(last.get('high', 0))
            low      = float(last.get('low', 0))
            close    = float(last.get('close', 0))
            cr       = round(high - low, 2)
            ts_last  = last.get('timestamp', '?')

            runner_min = 1.5  if name == 'NIFTY' else 5.0
            strat_min  = 3.0  if name == 'NIFTY' else 10.0
            engine_cap = 7.5  if name == 'NIFTY' else 25.0

            print("=" * 52)
            print(f"  {name}  ({len(today_c)} candles today)")
            print(f"  Last candle : {ts_last}  close={close}")
            print(f"  Candle range: H-L = {cr} pts")
            print(f"  ATR(14) 1min: {atr:.2f} pts")
            print()
            print(f"  Gate 1 runner  >= {runner_min}  : {'PASS' if atr >= runner_min else 'FAIL  <-- all strategies skipped'}")
            print(f"  Gate 2 strat   >= {strat_min}  : {'PASS' if atr >= strat_min else 'FAIL  <-- individual strategy blocks'}")
            print(f"  Gate 3 engine  >= {engine_cap} : {'PASS' if atr >= engine_cap else 'FAIL  <-- position blocked at entry'}")
        else:
            print(f'{name}: only {len(today_c)} candles today — need 15 minimum')

    await redis_cache.disconnect()

asyncio.run(main())
