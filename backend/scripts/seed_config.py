"""Seed default trading_config row after migration."""
import json, psycopg2

conn = psycopg2.connect(host="localhost", port=5432, user="postgres", password="1093", dbname="nifty_scalper")
conn.autocommit = True
cur = conn.cursor()
flags = {s: True for s in ["ORB_VWAP","VWAP_PULLBACK","EMA_VWAP","SUPERTREND_VWAP",
                             "PDH_PDL","VWAP_REVERSAL","BOLLINGER_BREAKOUT","RSI_VWAP",
                             "OI_SPIKE","IV_CRUSH","CPR_VWAP","GAP_VWAP_FILL"]}
cur.execute(
    "INSERT INTO trading_config (id,mode,max_daily_loss,max_daily_loss_pct,"
    "max_open_positions,max_lots_per_trade,capital,slippage_pct,"
    "strategy_flags,square_off_time,live_mode_confirmed) "
    "VALUES (1,'PAPER',5000,2,3,1,100000,0.2,%s,'15:15',false) "
    "ON CONFLICT (id) DO NOTHING",
    (json.dumps(flags),)
)
print("trading_config seeded.")
cur.close(); conn.close()
