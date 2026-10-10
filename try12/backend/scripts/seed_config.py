"""Seed default trading_config row after migration."""
import json, os, sys
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.strategies.names import ALL_STRATEGY_NAMES  # noqa: E402

conn = psycopg2.connect(host="localhost", port=5432, user="postgres", password="1093", dbname="nifty_scalper")
conn.autocommit = True
cur = conn.cursor()
flags = {s: True for s in ALL_STRATEGY_NAMES}
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
