-- ============================================================================
-- Trading System Diagnostic Queries
-- Run these to diagnose why trades are not executing
-- ============================================================================

-- 1. Check if backend has connected to database
SELECT NOW() as current_time, version() as postgres_version;

-- 2. Check all table row counts
SELECT 
    'paper_positions' as table_name, 
    COUNT(*) as row_count 
FROM paper_positions
UNION ALL
SELECT 'strategy_signals', COUNT(*) FROM strategy_signals
UNION ALL
SELECT 'regime_states', COUNT(*) FROM regime_states
UNION ALL
SELECT 'regime_performance', COUNT(*) FROM regime_performance
UNION ALL
SELECT 'regime_config', COUNT(*) FROM regime_config
UNION ALL
SELECT 'trading_config', COUNT(*) FROM trading_config
UNION ALL
SELECT 'strategy_perf', COUNT(*) FROM strategy_perf
UNION ALL
SELECT 'daily_pnl_summary', COUNT(*) FROM daily_pnl_summary;

-- 3. Check trading configuration
SELECT 
    mode,
    study_phase,
    capital,
    used_capital,
    trade_amount,
    daily_trades_count,
    max_trades_per_day,
    max_open_positions,
    max_daily_loss,
    strategy_flags,
    live_mode_confirmed,
    updated_at
FROM trading_config;

-- 4. Check recent regime states (if any)
SELECT 
    underlying,
    regime,
    timestamp,
    adx,
    atr,
    atr_percentile,
    price,
    vwap,
    transition_detected,
    previous_regime
FROM regime_states
ORDER BY timestamp DESC
LIMIT 20;

-- 5. Check regime configuration
SELECT 
    underlying,
    adx_trend_threshold,
    adx_range_threshold,
    atr_low_percentile,
    atr_high_percentile,
    vwap_proximity_percent,
    vwap_cross_chop_threshold,
    volume_surge_multiplier,
    is_active,
    updated_at
FROM regime_config
WHERE is_active = TRUE;

-- 6. Check recent strategy signals (last 24 hours)
SELECT 
    strategy_name,
    underlying,
    direction,
    status,
    skip_reason,
    signal_time,
    spot_price,
    suggested_strike,
    suggested_symbol
FROM strategy_signals
WHERE signal_time > CURRENT_TIMESTAMP - INTERVAL '24 hours'
ORDER BY signal_time DESC
LIMIT 50;

-- 7. Count signals by status
SELECT 
    status,
    COUNT(*) as count
FROM strategy_signals
WHERE signal_time > CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY status
ORDER BY count DESC;

-- 8. Check skip reasons for signals
SELECT 
    skip_reason,
    COUNT(*) as count
FROM strategy_signals
WHERE status = 'SKIPPED' 
  AND signal_time > CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY skip_reason
ORDER BY count DESC
LIMIT 20;

-- 9. Check open positions (should be 0 or very few)
SELECT 
    id,
    strategy_name,
    symbol,
    underlying,
    entry_regime,
    direction,
    lots,
    actual_entry,
    stop_loss,
    target,
    entry_time,
    is_open
FROM paper_positions
WHERE is_open = TRUE;

-- 10. Check recent closed positions (if any)
SELECT 
    id,
    strategy_name,
    symbol,
    underlying,
    entry_regime,
    direction,
    lots,
    actual_entry,
    actual_exit,
    exit_reason,
    net_pnl,
    entry_time,
    exit_time
FROM paper_positions
WHERE is_open = FALSE
ORDER BY exit_time DESC
LIMIT 20;

-- 11. Check strategy performance stats
SELECT 
    strategy_name,
    total_trades,
    winning_trades,
    win_rate,
    avg_rr,
    kelly_fraction,
    updated_at
FROM strategy_perf
ORDER BY total_trades DESC;

-- 12. Check regime performance (if any trades completed)
SELECT 
    regime,
    underlying,
    strategy_name,
    total_trades,
    winning_trades,
    losing_trades,
    win_rate,
    total_pnl,
    avg_pnl,
    last_trade_at
FROM regime_performance
ORDER BY total_trades DESC
LIMIT 20;

-- 13. Check if any signals were generated TODAY
SELECT 
    DATE(signal_time AT TIME ZONE 'Asia/Kolkata') as date,
    strategy_name,
    status,
    COUNT(*) as count
FROM strategy_signals
WHERE DATE(signal_time AT TIME ZONE 'Asia/Kolkata') = CURRENT_DATE
GROUP BY DATE(signal_time AT TIME ZONE 'Asia/Kolkata'), strategy_name, status
ORDER BY count DESC;

-- 14. Check regime distribution over last 7 days
SELECT 
    regime,
    COUNT(*) as occurrences,
    MIN(timestamp) as first_seen,
    MAX(timestamp) as last_seen
FROM regime_states
WHERE timestamp > CURRENT_TIMESTAMP - INTERVAL '7 days'
GROUP BY regime
ORDER BY occurrences DESC;

-- 15. Identify if system is stuck in no-trade regimes
SELECT 
    regime,
    COUNT(*) as candle_count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM regime_states WHERE timestamp > CURRENT_TIMESTAMP - INTERVAL '7 days'), 2) as percentage
FROM regime_states
WHERE timestamp > CURRENT_TIMESTAMP - INTERVAL '7 days'
  AND regime IN ('HIGH_VOL_CHOP', 'REGIME_TRANSITION', 'EXPIRY_DAY', 'UNKNOWN')
GROUP BY regime
ORDER BY candle_count DESC;
