"""
Check database table status.
"""
import psycopg2
from psycopg2.extras import RealDictCursor

conn = psycopg2.connect(
    host="localhost",
    port=5432,
    database="nifty_scalper",
    user="postgres",
    password="1093"
)

with conn.cursor(cursor_factory=RealDictCursor) as cur:
    print("=== PAPER_POSITIONS TABLE ===")
    cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN is_open THEN 1 ELSE 0 END) as open_count, SUM(CASE WHEN NOT is_open THEN 1 ELSE 0 END) as closed_count FROM paper_positions")
    result = cur.fetchone()
    print(f"Total positions: {result['total']}")
    print(f"Open: {result['open_count']}, Closed: {result['closed_count']}\n")
    
    print("=== RECENT POSITIONS (last 20) ===")
    cur.execute("""
        SELECT id, strategy_name, symbol, direction, entry_price, exit_price, net_pnl, exit_reason, is_open, entry_time
        FROM paper_positions
        ORDER BY entry_time DESC
        LIMIT 20
    """)
    positions = cur.fetchall()
    for p in positions:
        status = "OPEN" if p['is_open'] else "CLOSED"
        pnl = f"₹{p['net_pnl']:.2f}" if p['net_pnl'] else "N/A"
        print(f"{p['id']:4d} | {p['strategy_name']:20s} | {p['symbol']:30s} | {p['direction']:4s} | "
              f"Entry: ₹{p['entry_price']:.2f} | Exit: {p['exit_price'] or 'N/A':>7s} | "
              f"P&L: {pnl:>10s} | {p['exit_reason'] or 'N/A':10s} | {status}")
    
    print("\n=== STRATEGY_SIGNALS TABLE ===")
    cur.execute("SELECT COUNT(*) as total FROM strategy_signals")
    sig_count = cur.fetchone()['total']
    print(f"Total signals: {sig_count}\n")
    
    if sig_count > 0:
        print("=== RECENT SIGNALS (last 20) ===")
        cur.execute("""
            SELECT id, strategy_name, direction, status, spot_price, vwap, signal_time, skip_reason
            FROM strategy_signals
            ORDER BY signal_time DESC
            LIMIT 20
        """)
        signals = cur.fetchall()
        for s in signals:
            skip = f" - {s['skip_reason']}" if s['skip_reason'] else ""
            print(f"{s['id']:4d} | {s['strategy_name']:20s} | {s['direction']:4s} | {s['status']:10s} | "
                  f"Spot: {s['spot_price']:.2f} | VWAP: {s['vwap'] or 'N/A':>7s}{skip}")

conn.close()
