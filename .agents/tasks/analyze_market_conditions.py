"""
Trading System Market Conditions Analysis
Queries database to diagnose why trades are losing
"""
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import sys

# Database connection
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'database': 'nifty_scalper',
    'user': 'postgres',
    'password': '1093'
}

def connect_db():
    """Connect to PostgreSQL database"""
    try:
        conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        sys.exit(1)

def execute_query(conn, query, description):
    """Execute query and return results"""
    print(f"\n{'='*80}")
    print(f"📊 {description}")
    print(f"{'='*80}")
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            results = cur.fetchall()
            return results
    except Exception as e:
        print(f"❌ Query failed: {e}")
        conn.rollback()  # Rollback transaction on error
        return []

def format_currency(value):
    """Format value as Indian Rupees"""
    if value is None:
        return "₹0.00"
    return f"₹{float(value):,.2f}"

def format_percentage(value):
    """Format value as percentage"""
    if value is None:
        return "0.00%"
    return f"{float(value):.2f}%"

def main():
    conn = connect_db()
    print(f"\n✅ Connected to database: {DB_CONFIG['database']}")
    
    report_lines = []
    report_lines.append("# Market Conditions Analysis Report")
    report_lines.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append("\n" + "="*80 + "\n")

    # ============================================================================
    # 0. Quick Data Check
    # ============================================================================
    query_data_check = """
    SELECT 
        (SELECT COUNT(*) FROM paper_positions) as total_positions,
        (SELECT COUNT(*) FROM paper_positions WHERE is_open = FALSE) as closed_positions,
        (SELECT COUNT(*) FROM strategy_signals) as total_signals,
        (SELECT COUNT(*) FROM regime_states) as regime_states_count;
    """
    
    results = execute_query(conn, query_data_check, "0. DATABASE CONTENT CHECK")
    if results and results[0]:
        row = results[0]
        print(f"\nTotal Positions:       {row['total_positions']}")
        print(f"Closed Positions:      {row['closed_positions']}")
        print(f"Total Signals:         {row['total_signals']}")
        print(f"Regime States:         {row['regime_states_count']}")
        
        report_lines.append("## 0. Database Content Check\n")
        report_lines.append(f"- **Total Positions:** {row['total_positions']}")
        report_lines.append(f"- **Closed Positions:** {row['closed_positions']}")
        report_lines.append(f"- **Total Signals:** {row['total_signals']}")
        report_lines.append(f"- **Regime States:** {row['regime_states_count']}\n")
        
        if row['closed_positions'] == 0:
            print("\n⚠️  NO CLOSED TRADES FOUND - Cannot analyze performance")
            report_lines.append("\n⚠️ **NO CLOSED TRADES FOUND** - Cannot analyze performance\n")
            report_lines.append("**The system may still be running in study phase or no trades have completed yet.**\n")
    
    # ============================================================================
    # 1. Trade Performance by Time of Day
    # ============================================================================
    query_time_buckets = """
    WITH time_data AS (
        SELECT 
            CASE 
                WHEN EXTRACT(HOUR FROM entry_time AT TIME ZONE 'Asia/Kolkata') * 60 + 
                     EXTRACT(MINUTE FROM entry_time AT TIME ZONE 'Asia/Kolkata') BETWEEN 555 AND 599 
                    THEN '09:15-10:00 (OPEN)'
                WHEN EXTRACT(HOUR FROM entry_time AT TIME ZONE 'Asia/Kolkata') * 60 + 
                     EXTRACT(MINUTE FROM entry_time AT TIME ZONE 'Asia/Kolkata') BETWEEN 600 AND 719 
                    THEN '10:00-12:00 (MID-MORNING)'
                WHEN EXTRACT(HOUR FROM entry_time AT TIME ZONE 'Asia/Kolkata') * 60 + 
                     EXTRACT(MINUTE FROM entry_time AT TIME ZONE 'Asia/Kolkata') BETWEEN 720 AND 839 
                    THEN '12:00-14:00 (AFTERNOON)'
                WHEN EXTRACT(HOUR FROM entry_time AT TIME ZONE 'Asia/Kolkata') * 60 + 
                     EXTRACT(MINUTE FROM entry_time AT TIME ZONE 'Asia/Kolkata') BETWEEN 840 AND 929 
                    THEN '14:00-15:30 (CLOSE)'
                ELSE 'OTHER'
            END as time_bucket,
            net_pnl
        FROM paper_positions
        WHERE is_open = FALSE AND net_pnl IS NOT NULL
    )
    SELECT 
        time_bucket,
        COUNT(*) as total_trades,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 AS numeric), 2) as win_rate,
        ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl,
        ROUND(CAST(SUM(net_pnl) AS numeric), 2) as total_pnl,
        ROUND(CAST(MIN(net_pnl) AS numeric), 2) as worst_loss,
        ROUND(CAST(MAX(net_pnl) AS numeric), 2) as best_win
    FROM time_data
    GROUP BY time_bucket
    ORDER BY 
        CASE 
            WHEN time_bucket = '09:15-10:00 (OPEN)' THEN 1
            WHEN time_bucket = '10:00-12:00 (MID-MORNING)' THEN 2
            WHEN time_bucket = '12:00-14:00 (AFTERNOON)' THEN 3
            WHEN time_bucket = '14:00-15:30 (CLOSE)' THEN 4
            ELSE 5
        END;
    """
    
    results = execute_query(conn, query_time_buckets, "1. TRADE PERFORMANCE BY TIME OF DAY")
    report_lines.append("## 1. Trade Performance by Time of Day\n")
    
    if results:
        for row in results:
            print(f"\n{row['time_bucket']}")
            print(f"  Total Trades:   {row['total_trades']}")
            print(f"  Winning Trades: {row['winning_trades']}")
            print(f"  Win Rate:       {row['win_rate']}%")
            print(f"  Avg P&L:        {format_currency(row['avg_pnl'])}")
            print(f"  Total P&L:      {format_currency(row['total_pnl'])}")
            print(f"  Worst Loss:     {format_currency(row['worst_loss'])}")
            print(f"  Best Win:       {format_currency(row['best_win'])}")
            
            report_lines.append(f"### {row['time_bucket']}")
            report_lines.append(f"- **Total Trades:** {row['total_trades']}")
            report_lines.append(f"- **Winning Trades:** {row['winning_trades']}")
            report_lines.append(f"- **Win Rate:** {row['win_rate']}%")
            report_lines.append(f"- **Avg P&L:** {format_currency(row['avg_pnl'])}")
            report_lines.append(f"- **Total P&L:** {format_currency(row['total_pnl'])}")
            report_lines.append(f"- **Worst Loss:** {format_currency(row['worst_loss'])}")
            report_lines.append(f"- **Best Win:** {format_currency(row['best_win'])}\n")
    else:
        print("⚠️  No closed trades found")
        report_lines.append("⚠️ **No closed trades found**\n")

    # ============================================================================
    # 2. Check if VIX data exists and performance by VIX level
    # ============================================================================
    query_vix_check = """
    SELECT COUNT(*) as vix_count
    FROM information_schema.columns 
    WHERE table_name = 'paper_positions' AND column_name = 'vix';
    """
    
    results = execute_query(conn, query_vix_check, "2. CHECKING VIX DATA AVAILABILITY")
    report_lines.append("## 2. VIX Analysis\n")
    
    vix_available = results[0]['vix_count'] > 0 if results else False
    
    if not vix_available:
        print("⚠️  VIX column not found in paper_positions table")
        print("   VIX data is not currently being stored with trades")
        report_lines.append("⚠️ **VIX data is not currently being stored with trades**\n")
        report_lines.append("**Recommendation:** Add VIX value to paper_positions table at entry time\n")
    else:
        # Query performance by VIX level
        query_vix_performance = """
        SELECT 
            CASE 
                WHEN vix < 15 THEN 'LOW (<15)'
                WHEN vix BETWEEN 15 AND 20 THEN 'MEDIUM (15-20)'
                WHEN vix > 20 THEN 'HIGH (>20)'
                ELSE 'UNKNOWN'
            END as vix_level,
            COUNT(*) as total_trades,
            SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 AS numeric), 2) as win_rate,
            ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl,
            ROUND(CAST(SUM(net_pnl) AS numeric), 2) as total_pnl
        FROM paper_positions
        WHERE is_open = FALSE AND net_pnl IS NOT NULL AND vix IS NOT NULL
        GROUP BY vix_level
        ORDER BY 
            CASE 
                WHEN vix_level = 'LOW (<15)' THEN 1
                WHEN vix_level = 'MEDIUM (15-20)' THEN 2
                WHEN vix_level = 'HIGH (>20)' THEN 3
                ELSE 4
            END;
        """
        
        results = execute_query(conn, query_vix_performance, "Performance by VIX Level")
        if results:
            for row in results:
                print(f"\n{row['vix_level']}")
                print(f"  Total Trades: {row['total_trades']}")
                print(f"  Win Rate:     {row['win_rate']}%")
                print(f"  Avg P&L:      {format_currency(row['avg_pnl'])}")
                print(f"  Total P&L:    {format_currency(row['total_pnl'])}")

    # ============================================================================
    # 3. Trade Performance by Regime Classification
    # ============================================================================
    query_regime_performance = """
    SELECT 
        COALESCE(entry_regime, 'UNKNOWN') as regime,
        COUNT(*) as total_trades,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 AS numeric), 2) as win_rate,
        ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl,
        ROUND(CAST(SUM(net_pnl) AS numeric), 2) as total_pnl,
        ROUND(CAST(MIN(net_pnl) AS numeric), 2) as worst_loss,
        ROUND(CAST(MAX(net_pnl) AS numeric), 2) as best_win,
        COUNT(DISTINCT strategy_name) as strategies_used
    FROM paper_positions
    WHERE is_open = FALSE AND net_pnl IS NOT NULL
    GROUP BY entry_regime
    ORDER BY total_pnl DESC;
    """
    
    results = execute_query(conn, query_regime_performance, "3. TRADE PERFORMANCE BY REGIME")
    report_lines.append("## 3. Trade Performance by Regime Classification\n")
    
    if results:
        for row in results:
            print(f"\n{row['regime']}")
            print(f"  Total Trades:      {row['total_trades']}")
            print(f"  Winning Trades:    {row['winning_trades']}")
            print(f"  Win Rate:          {row['win_rate']}%")
            print(f"  Avg P&L:           {format_currency(row['avg_pnl'])}")
            print(f"  Total P&L:         {format_currency(row['total_pnl'])}")
            print(f"  Worst Loss:        {format_currency(row['worst_loss'])}")
            print(f"  Best Win:          {format_currency(row['best_win'])}")
            print(f"  Strategies Used:   {row['strategies_used']}")
            
            report_lines.append(f"### {row['regime']}")
            report_lines.append(f"- **Total Trades:** {row['total_trades']}")
            report_lines.append(f"- **Winning Trades:** {row['winning_trades']}")
            report_lines.append(f"- **Win Rate:** {row['win_rate']}%")
            report_lines.append(f"- **Avg P&L:** {format_currency(row['avg_pnl'])}")
            report_lines.append(f"- **Total P&L:** {format_currency(row['total_pnl'])}")
            report_lines.append(f"- **Worst Loss:** {format_currency(row['worst_loss'])}")
            report_lines.append(f"- **Best Win:** {format_currency(row['best_win'])}")
            report_lines.append(f"- **Strategies Used:** {row['strategies_used']}\n")
    else:
        print("⚠️  No regime data found")
        report_lines.append("⚠️ **No regime data found in closed trades**\n")

    # ============================================================================
    # 4. Strategy Performance by Regime
    # ============================================================================
    query_strategy_regime = """
    SELECT 
        strategy_name,
        COALESCE(entry_regime, 'UNKNOWN') as regime,
        COUNT(*) as total_trades,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 AS numeric), 2) as win_rate,
        ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl,
        ROUND(CAST(SUM(net_pnl) AS numeric), 2) as total_pnl
    FROM paper_positions
    WHERE is_open = FALSE AND net_pnl IS NOT NULL
    GROUP BY strategy_name, entry_regime
    ORDER BY strategy_name, total_pnl DESC;
    """
    
    results = execute_query(conn, query_strategy_regime, "4. STRATEGY PERFORMANCE BY REGIME")
    report_lines.append("## 4. Strategy Performance by Regime\n")
    
    if results:
        current_strategy = None
        for row in results:
            if row['strategy_name'] != current_strategy:
                current_strategy = row['strategy_name']
                print(f"\n{'─'*60}")
                print(f"Strategy: {current_strategy}")
                print(f"{'─'*60}")
                report_lines.append(f"### {current_strategy}\n")
            
            print(f"  Regime: {row['regime']}")
            print(f"    Trades: {row['total_trades']} | Win Rate: {row['win_rate']}% | "
                  f"Avg P&L: {format_currency(row['avg_pnl'])} | Total: {format_currency(row['total_pnl'])}")
            
            report_lines.append(f"- **{row['regime']}:** {row['total_trades']} trades, "
                              f"{row['win_rate']}% win rate, {format_currency(row['avg_pnl'])} avg P&L, "
                              f"{format_currency(row['total_pnl'])} total")

        report_lines.append("\n")
    else:
        print("⚠️  No strategy data found")
        report_lines.append("⚠️ **No strategy data found**\n")

    # ============================================================================
    # 5. Trading Frequency Analysis
    # ============================================================================
    query_trading_frequency = """
    SELECT 
        DATE(entry_time AT TIME ZONE 'Asia/Kolkata') as trade_date,
        COUNT(*) as trades_per_day,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        ROUND(CAST(SUM(net_pnl) AS numeric), 2) as daily_pnl
    FROM paper_positions
    WHERE is_open = FALSE AND net_pnl IS NOT NULL
    GROUP BY trade_date
    ORDER BY trade_date DESC;
    """
    
    results = execute_query(conn, query_trading_frequency, "5. TRADING FREQUENCY ANALYSIS")
    report_lines.append("## 5. Trading Frequency Analysis\n")
    
    if results:
        total_days = len(results)
        total_trades = sum(row['trades_per_day'] for row in results)
        avg_trades = total_trades / total_days if total_days > 0 else 0
        max_trades = max(row['trades_per_day'] for row in results)
        overtrading_days = sum(1 for row in results if row['trades_per_day'] > 10)
        
        print(f"\nTotal Trading Days:       {total_days}")
        print(f"Total Trades:             {total_trades}")
        print(f"Avg Trades per Day:       {avg_trades:.2f}")
        print(f"Max Trades in a Day:      {max_trades}")
        print(f"Days with >10 Trades:     {overtrading_days} ({overtrading_days/total_days*100:.1f}%)")
        
        report_lines.append(f"- **Total Trading Days:** {total_days}")
        report_lines.append(f"- **Total Trades:** {total_trades}")
        report_lines.append(f"- **Avg Trades per Day:** {avg_trades:.2f}")
        report_lines.append(f"- **Max Trades in a Day:** {max_trades}")
        report_lines.append(f"- **Days with >10 Trades (Potential Overtrading):** {overtrading_days} ({overtrading_days/total_days*100:.1f}%)\n")
        
        if overtrading_days > 0:
            print(f"\n⚠️  WARNING: Overtrading detected on {overtrading_days} days")
            report_lines.append(f"⚠️ **WARNING:** Overtrading detected on {overtrading_days} days\n")
        
        print("\nRecent Daily Performance:")
        report_lines.append("### Recent Daily Performance\n")
        for i, row in enumerate(results[:10]):  # Show last 10 days
            print(f"  {row['trade_date']}: {row['trades_per_day']} trades, "
                  f"{row['winning_trades']} wins, {format_currency(row['daily_pnl'])} P&L")
            report_lines.append(f"- **{row['trade_date']}:** {row['trades_per_day']} trades, "
                              f"{row['winning_trades']} wins, {format_currency(row['daily_pnl'])}")
        report_lines.append("\n")
    else:
        print("⚠️  No trading history found")
        report_lines.append("⚠️ **No trading history found**\n")

    # ============================================================================
    # 6. Regime Distribution at Trade Entry
    # ============================================================================
    query_regime_distribution = """
    SELECT 
        COALESCE(entry_regime, 'UNKNOWN') as regime,
        COUNT(*) as trade_count,
        ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM paper_positions WHERE is_open = FALSE), 2) as percentage
    FROM paper_positions
    WHERE is_open = FALSE
    GROUP BY entry_regime
    ORDER BY trade_count DESC;
    """
    
    results = execute_query(conn, query_regime_distribution, "6. REGIME DISTRIBUTION AT TRADE ENTRY")
    report_lines.append("## 6. Regime Distribution at Trade Entry\n")
    
    if results:
        for row in results:
            print(f"  {row['regime']}: {row['trade_count']} trades ({row['percentage']}%)")
            report_lines.append(f"- **{row['regime']}:** {row['trade_count']} trades ({row['percentage']}%)")
        report_lines.append("\n")
    else:
        print("⚠️  No regime data available")
        report_lines.append("⚠️ **No regime data available**\n")

    # ============================================================================
    # 7. Exit Reason Analysis
    # ============================================================================
    query_exit_reasons = """
    SELECT 
        exit_reason,
        COUNT(*) as count,
        ROUND(CAST(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM paper_positions WHERE is_open = FALSE) AS numeric), 2) as percentage,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl,
        ROUND(CAST(SUM(net_pnl) AS numeric), 2) as total_pnl
    FROM paper_positions
    WHERE is_open = FALSE AND net_pnl IS NOT NULL
    GROUP BY exit_reason
    ORDER BY count DESC;
    """
    
    results = execute_query(conn, query_exit_reasons, "7. EXIT REASON ANALYSIS")
    report_lines.append("## 7. Exit Reason Analysis\n")
    
    if results:
        for row in results:
            print(f"\n{row['exit_reason'] or 'UNKNOWN'}")
            print(f"  Count:       {row['count']} ({row['percentage']}%)")
            print(f"  Winners:     {row['winning_trades']}")
            print(f"  Avg P&L:     {format_currency(row['avg_pnl'])}")
            print(f"  Total P&L:   {format_currency(row['total_pnl'])}")
            
            report_lines.append(f"### {row['exit_reason'] or 'UNKNOWN'}")
            report_lines.append(f"- **Count:** {row['count']} ({row['percentage']}%)")
            report_lines.append(f"- **Winners:** {row['winning_trades']}")
            report_lines.append(f"- **Avg P&L:** {format_currency(row['avg_pnl'])}")
            report_lines.append(f"- **Total P&L:** {format_currency(row['total_pnl'])}\n")
    else:
        print("⚠️  No exit data found")
        report_lines.append("⚠️ **No exit data found**\n")

    # ============================================================================
    # 8. Overall Performance Summary
    # ============================================================================
    query_overall = """
    SELECT 
        COUNT(*) as total_trades,
        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
        ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 AS numeric), 2) as win_rate,
        ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl,
        ROUND(CAST(SUM(net_pnl) AS numeric), 2) as total_pnl,
        ROUND(CAST(MIN(net_pnl) AS numeric), 2) as worst_loss,
        ROUND(CAST(MAX(net_pnl) AS numeric), 2) as best_win,
        ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN net_pnl END) AS numeric), 2) as avg_win,
        ROUND(CAST(AVG(CASE WHEN net_pnl < 0 THEN net_pnl END) AS numeric), 2) as avg_loss
    FROM paper_positions
    WHERE is_open = FALSE AND net_pnl IS NOT NULL;
    """
    
    results = execute_query(conn, query_overall, "8. OVERALL PERFORMANCE SUMMARY")
    report_lines.append("## 8. Overall Performance Summary\n")
    
    if results and results[0]:
        row = results[0]
        print(f"\nTotal Trades:     {row['total_trades']}")
        print(f"Winning Trades:   {row['winning_trades']}")
        print(f"Losing Trades:    {row['losing_trades']}")
        print(f"Win Rate:         {row['win_rate']}%")
        print(f"Total P&L:        {format_currency(row['total_pnl'])}")
        print(f"Avg P&L:          {format_currency(row['avg_pnl'])}")
        print(f"Avg Win:          {format_currency(row['avg_win'])}")
        print(f"Avg Loss:         {format_currency(row['avg_loss'])}")
        print(f"Best Win:         {format_currency(row['best_win'])}")
        print(f"Worst Loss:       {format_currency(row['worst_loss'])}")
        
        if row['avg_win'] and row['avg_loss']:
            profit_factor = abs(row['avg_win'] / row['avg_loss']) if row['avg_loss'] != 0 else 0
            print(f"Profit Factor:    {profit_factor:.2f}")
            report_lines.append(f"- **Profit Factor:** {profit_factor:.2f}")
        
        report_lines.append(f"- **Total Trades:** {row['total_trades']}")
        report_lines.append(f"- **Winning Trades:** {row['winning_trades']}")
        report_lines.append(f"- **Losing Trades:** {row['losing_trades']}")
        report_lines.append(f"- **Win Rate:** {row['win_rate']}%")
        report_lines.append(f"- **Total P&L:** {format_currency(row['total_pnl'])}")
        report_lines.append(f"- **Avg P&L:** {format_currency(row['avg_pnl'])}")
        report_lines.append(f"- **Avg Win:** {format_currency(row['avg_win'])}")
        report_lines.append(f"- **Avg Loss:** {format_currency(row['avg_loss'])}")
        report_lines.append(f"- **Best Win:** {format_currency(row['best_win'])}")
        report_lines.append(f"- **Worst Loss:** {format_currency(row['worst_loss'])}\n")

    # ============================================================================
    # 9. Check Regime Configuration and Strategy Orchestrator
    # ============================================================================
    report_lines.append("## 9. Regime System Configuration Check\n")
    
    query_regime_config = """
    SELECT * FROM regime_config WHERE is_active = TRUE;
    """
    
    results = execute_query(conn, query_regime_config, "9. REGIME CONFIGURATION")
    
    if results:
        report_lines.append("### Active Regime Configurations:\n")
        for row in results:
            underlying = row.get('underlying', 'GLOBAL')
            print(f"\nConfiguration for: {underlying}")
            print(f"  ADX Trend Threshold:     {row.get('adx_trend_threshold')}")
            print(f"  ADX Range Threshold:     {row.get('adx_range_threshold')}")
            print(f"  ATR Low Percentile:      {row.get('atr_low_percentile')}")
            print(f"  ATR High Percentile:     {row.get('atr_high_percentile')}")
            
            report_lines.append(f"**{underlying}:**")
            report_lines.append(f"- ADX Trend Threshold: {row.get('adx_trend_threshold')}")
            report_lines.append(f"- ADX Range Threshold: {row.get('adx_range_threshold')}")
            report_lines.append(f"- ATR Low Percentile: {row.get('atr_low_percentile')}")
            report_lines.append(f"- ATR High Percentile: {row.get('atr_high_percentile')}\n")
    else:
        print("⚠️  No active regime configuration found")
        report_lines.append("⚠️ **No active regime configuration found**\n")

    # ============================================================================
    # 10. Signals Skipped Due to Regime
    # ============================================================================
    query_regime_skipped = """
    SELECT 
        status,
        COUNT(*) as signal_count,
        SUM(CASE WHEN skip_reason LIKE '%Regime%' OR skip_reason LIKE '%regime%' THEN 1 ELSE 0 END) as regime_skipped
    FROM strategy_signals
    WHERE status = 'SKIPPED'
    GROUP BY status;
    """
    
    results = execute_query(conn, query_regime_skipped, "10. SIGNALS SKIPPED DUE TO REGIME")
    report_lines.append("## 10. Signals Skipped Due to Regime\n")
    
    if results and results[0]:
        row = results[0]
        print(f"\nTotal Skipped Signals: {row.get('signal_count', 0)}")
        print(f"Regime-Related Skips:  {row.get('regime_skipped', 0)}")
        
        report_lines.append(f"- **Total Skipped Signals:** {row.get('signal_count', 0)}")
        report_lines.append(f"- **Regime-Related Skips:** {row.get('regime_skipped', 0)}\n")
        
        # Get detailed regime skip reasons
        query_regime_skip_details = """
        SELECT 
            skip_reason,
            COUNT(*) as count
        FROM strategy_signals
        WHERE status = 'SKIPPED' AND (skip_reason LIKE '%Regime%' OR skip_reason LIKE '%regime%')
        GROUP BY skip_reason
        ORDER BY count DESC
        LIMIT 10;
        """
        
        skip_details = execute_query(conn, query_regime_skip_details, "Regime Skip Reasons")
        if skip_details:
            report_lines.append("### Top Regime Skip Reasons:\n")
            for detail in skip_details:
                print(f"  {detail['count']}x: {detail['skip_reason']}")
                report_lines.append(f"- **{detail['count']}x:** {detail['skip_reason']}")
            report_lines.append("\n")
    else:
        print("⚠️  No skipped signals found")
        report_lines.append("⚠️ **No skipped signals found**\n")

    # ============================================================================
    # RECOMMENDATIONS
    # ============================================================================
    report_lines.append("\n" + "="*80)
    report_lines.append("\n## Key Findings and Recommendations\n")
    
    print("\n" + "="*80)
    print("KEY FINDINGS AND RECOMMENDATIONS")
    print("="*80)
    
    # Analyze the data and provide recommendations
    if results:
        # Check overall performance
        overall_query = """
        SELECT 
            ROUND(CAST(AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END) * 100 AS numeric), 2) as win_rate,
            ROUND(CAST(AVG(net_pnl) AS numeric), 2) as avg_pnl
        FROM paper_positions
        WHERE is_open = FALSE AND net_pnl IS NOT NULL;
        """
        with conn.cursor() as cur:
            cur.execute(overall_query)
            perf = cur.fetchone()
            
            if perf and perf['win_rate'] < 50:
                msg = f"⚠️  Low win rate ({perf['win_rate']}%) indicates strategies need optimization"
                print(f"\n{msg}")
                report_lines.append(f"### {msg}\n")
                report_lines.append("- Review strategy entry conditions\n")
                report_lines.append("- Consider tightening filters (ATR, momentum, regime)\n")
                report_lines.append("- Verify stop-loss placement\n\n")
            
            if perf and perf['avg_pnl'] and perf['avg_pnl'] < 0:
                msg = f"⚠️  Negative average P&L ({format_currency(perf['avg_pnl'])}) per trade"
                print(f"\n{msg}")
                report_lines.append(f"### {msg}\n")
                report_lines.append("- Reduce position sizing\n")
                report_lines.append("- Focus on best-performing time windows\n")
                report_lines.append("- Consider reducing max trades per day\n\n")

    # Check code for regime filtering issues
    report_lines.append("### Code Review Findings:\n")
    report_lines.append("Based on `paper_engine.py` analysis:\n\n")
    report_lines.append("1. **Regime filtering is implemented** in `handle_signal()` method\n")
    report_lines.append("2. **Position size multiplier** is applied based on regime\n")
    report_lines.append("3. **Multiple safeguards** exist:\n")
    report_lines.append("   - Option momentum check (falling LTP > 2%)\n")
    report_lines.append("   - ATR vs SL distance validation\n")
    report_lines.append("   - Minimum R:R enforcement\n")
    report_lines.append("   - Max hold time (25min mean-reversion, 45min trend)\n")
    report_lines.append("   - Three-tier trailing SL (10%, 20%, 35% profit levels)\n\n")
    
    report_lines.append("### Check These Points:\n\n")
    report_lines.append("1. **Is regime detection working correctly?**\n")
    report_lines.append("   - Check `regime_states` table for recent entries\n")
    report_lines.append("   - Verify ADX and ATR calculations are accurate\n\n")
    report_lines.append("2. **Are strategies properly mapped to regimes?**\n")
    report_lines.append("   - Review `strategy_orchestrator.should_allow_signal()` logic\n")
    report_lines.append("   - Confirm mean-reversion strategies blocked in TRENDING regimes\n")
    report_lines.append("   - Confirm trend strategies blocked in RANGING regimes\n\n")
    report_lines.append("3. **Is position size multiplier = 0 blocking all trades?**\n")
    report_lines.append("   - Check `strategy_orchestrator.get_position_multiplier()` return values\n\n")
    report_lines.append("4. **Are entry filters too aggressive?**\n")
    report_lines.append("   - Option momentum check may skip valid opportunities\n")
    report_lines.append("   - ATR validation may be too strict in quiet markets\n\n")

    conn.close()
    
    # Write report to file
    output_path = r"c:\Users\rushi\Downloads\try12fin\.agents\tasks\market-conditions-analysis.md"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    
    print(f"\n{'='*80}")
    print(f"✅ Analysis complete! Report written to:")
    print(f"   {output_path}")
    print(f"{'='*80}\n")

if __name__ == "__main__":
    main()
