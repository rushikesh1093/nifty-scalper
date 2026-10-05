"""
Analyze per-strategy trading performance from the database.
"""
import psycopg2
from psycopg2.extras import RealDictCursor
import json

# Database connection
conn = psycopg2.connect(
    host="localhost",
    port=5432,
    database="nifty_scalper",
    user="postgres",
    password="1093"
)

def query_strategy_performance():
    """Query per-strategy P&L, win rate, trade count."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql = """
        SELECT 
            strategy_name,
            COUNT(*) as total_trades,
            SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            ROUND(AVG(net_pnl)::numeric, 2) as avg_pnl,
            ROUND(SUM(net_pnl)::numeric, 2) as total_pnl,
            ROUND(MAX(net_pnl)::numeric, 2) as best_trade,
            ROUND(MIN(net_pnl)::numeric, 2) as worst_trade,
            ROUND((SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)::float / COUNT(*)::float * 100)::numeric, 2) as win_rate_pct
        FROM paper_positions
        WHERE is_open = false AND strategy_name IS NOT NULL
        GROUP BY strategy_name
        ORDER BY total_pnl DESC
        """
        cur.execute(sql)
        return cur.fetchall()

def query_signal_vs_positions():
    """Compare signals generated vs actual positions opened."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql = """
        SELECT 
            strategy_name,
            COUNT(*) as total_signals,
            SUM(CASE WHEN status = 'ENTERED' THEN 1 ELSE 0 END) as entered_count,
            SUM(CASE WHEN status = 'SKIPPED' THEN 1 ELSE 0 END) as skipped_count,
            SUM(CASE WHEN status = 'EXPIRED' THEN 1 ELSE 0 END) as expired_count,
            ROUND((SUM(CASE WHEN status = 'ENTERED' THEN 1 ELSE 0 END)::float / COUNT(*)::float * 100)::numeric, 2) as entry_rate_pct
        FROM strategy_signals
        GROUP BY strategy_name
        ORDER BY total_signals DESC
        """
        cur.execute(sql)
        return cur.fetchall()

def query_exit_reasons():
    """Exit reason breakdown for closed positions."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql = """
        SELECT 
            strategy_name,
            exit_reason,
            COUNT(*) as count,
            ROUND(AVG(net_pnl)::numeric, 2) as avg_pnl
        FROM paper_positions
        WHERE is_open = false AND strategy_name IS NOT NULL
        GROUP BY strategy_name, exit_reason
        ORDER BY strategy_name, count DESC
        """
        cur.execute(sql)
        return cur.fetchall()

def query_worst_performers():
    """Get worst performing strategies with detailed metrics."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql = """
        SELECT 
            strategy_name,
            COUNT(*) as trades,
            ROUND(SUM(net_pnl)::numeric, 2) as total_pnl,
            ROUND(AVG(net_pnl)::numeric, 2) as avg_pnl,
            exit_reason,
            COUNT(*) as exit_reason_count
        FROM paper_positions
        WHERE is_open = false AND strategy_name IS NOT NULL
        GROUP BY strategy_name, exit_reason
        HAVING SUM(net_pnl) < 0
        ORDER BY total_pnl ASC
        """
        cur.execute(sql)
        return cur.fetchall()

def query_time_based_analysis():
    """Check if signals fire during market open chaos (9:15-9:30)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql = """
        SELECT 
            strategy_name,
            COUNT(*) as signals_915_930,
            ROUND(AVG(EXTRACT(EPOCH FROM (signal_time::time - '09:15:00'::time)) / 60)::numeric, 1) as avg_minutes_after_915
        FROM strategy_signals
        WHERE signal_time::time BETWEEN '09:15:00' AND '09:30:00'
        GROUP BY strategy_name
        ORDER BY signals_915_930 DESC
        """
        cur.execute(sql)
        return cur.fetchall()

def query_regime_performance():
    """Analyze performance by regime."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        sql = """
        SELECT 
            strategy_name,
            entry_regime,
            COUNT(*) as trades,
            ROUND(AVG(net_pnl)::numeric, 2) as avg_pnl,
            ROUND(SUM(net_pnl)::numeric, 2) as total_pnl,
            ROUND((SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)::float / COUNT(*)::float * 100)::numeric, 2) as win_rate
        FROM paper_positions
        WHERE is_open = false AND strategy_name IS NOT NULL AND entry_regime IS NOT NULL
        GROUP BY strategy_name, entry_regime
        ORDER BY strategy_name, total_pnl DESC
        """
        cur.execute(sql)
        return cur.fetchall()

def main():
    print("=== STRATEGY PERFORMANCE ANALYSIS ===\n")
    
    print("\n1. PER-STRATEGY PERFORMANCE\n")
    perf = query_strategy_performance()
    for row in perf:
        print(f"{row['strategy_name']:20s} | Trades: {row['total_trades']:3d} | Win Rate: {row['win_rate_pct']:5.1f}% | "
              f"Avg P&L: ₹{row['avg_pnl']:7.2f} | Total P&L: ₹{row['total_pnl']:9.2f} | "
              f"Best: ₹{row['best_trade']:7.2f} | Worst: ₹{row['worst_trade']:8.2f}")
    
    print("\n\n2. SIGNALS VS POSITIONS (Signal Conversion Rate)\n")
    signals = query_signal_vs_positions()
    for row in signals:
        print(f"{row['strategy_name']:20s} | Signals: {row['total_signals']:4d} | "
              f"Entered: {row['entered_count']:3d} ({row['entry_rate_pct']:5.1f}%) | "
              f"Skipped: {row['skipped_count']:3d} | Expired: {row['expired_count']:3d}")
    
    print("\n\n3. EXIT REASON BREAKDOWN\n")
    exits = query_exit_reasons()
    current_strat = None
    for row in exits:
        if row['strategy_name'] != current_strat:
            current_strat = row['strategy_name']
            print(f"\n{current_strat}:")
        print(f"  {row['exit_reason']:12s} : {row['count']:3d} trades, avg P&L: ₹{row['avg_pnl']:7.2f}")
    
    print("\n\n4. WORST PERFORMERS (negative total P&L)\n")
    worst = query_worst_performers()
    for row in worst:
        print(f"{row['strategy_name']:20s} | Exit: {row['exit_reason']:10s} | "
              f"Count: {row['exit_reason_count']:3d} | Total P&L: ₹{row['total_pnl']:9.2f}")
    
    print("\n\n5. MARKET OPEN CHAOS CHECK (9:15-9:30)\n")
    time_check = query_time_based_analysis()
    if time_check:
        for row in time_check:
            print(f"{row['strategy_name']:20s} | Signals in 9:15-9:30: {row['signals_915_930']:3d}")
    else:
        print("No signals fired during 9:15-9:30 (good!)")
    
    print("\n\n6. REGIME-BASED PERFORMANCE\n")
    regime = query_regime_performance()
    current_strat = None
    for row in regime:
        if row['strategy_name'] != current_strat:
            current_strat = row['strategy_name']
            print(f"\n{current_strat}:")
        print(f"  {row['entry_regime']:15s} : {row['trades']:3d} trades, "
              f"win rate {row['win_rate']:5.1f}%, avg ₹{row['avg_pnl']:7.2f}, total ₹{row['total_pnl']:9.2f}")
    
    conn.close()
    print("\n\n=== ANALYSIS COMPLETE ===\n")

if __name__ == "__main__":
    main()
