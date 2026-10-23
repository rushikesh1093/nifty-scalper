export const STRATEGY_META = [
  { key: "VWAP_EMA_PULLBACK", label: "VWAP + EMA Pullback",      edge: "3m trend pullback to EMA 9/21" },
  { key: "ORB_BREAKOUT",      label: "Opening Range Breakout",   edge: "5m close beyond 9:15-9:30 range" },
  { key: "SUPERTREND_EMA",    label: "Supertrend + EMA 5/13",    edge: "1m momentum flip, ADX > 20" },
  { key: "FIRST_CANDLE_BREAKOUT", label: "9:15 Candle Breakout",  edge: "5m close beyond the 9:15 candle ±10" },
  { key: "EMA_RSI_PULLBACK_15M",  label: "15m EMA + RSI Pullback", edge: "15m EMA 9/21 trend, RSI 40-50 dip" },
  { key: "PIVOT_BOUNCE",          label: "Pivot Point Bounce",     edge: "3m bounce at PP / R1 / S1" },
  { key: "MOMENTUM_BURST",        label: "5m Momentum Burst",      edge: "3 strong candles, EMA 20, ATR > 15" },
];
