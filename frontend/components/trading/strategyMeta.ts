export const STRATEGY_META = [
  { key: "ORB_VWAP",           label: "ORB + VWAP + Volume",    edge: "Opening momentum breakout" },
  { key: "VWAP_PULLBACK",      label: "VWAP Pullback",          edge: "Pullback to VWAP in trend" },
  { key: "EMA_VWAP",           label: "EMA 9/21 + VWAP",        edge: "EMA cross + VWAP confirm" },
  { key: "SUPERTREND_VWAP",    label: "Supertrend + VWAP",      edge: "Trend flip confirmation" },
  { key: "PDH_PDL",            label: "Prev Day H/L",           edge: "Key level breakout" },
  { key: "VWAP_REVERSAL",      label: "VWAP Reversal",          edge: "Exhaustion at VWAP" },
  { key: "BOLLINGER_BREAKOUT", label: "Bollinger Breakout",     edge: "Squeeze expansion" },
  { key: "RSI_VWAP",           label: "RSI + VWAP",             edge: "Momentum + mean reversion" },
  { key: "OI_SPIKE",           label: "OI Spike",               edge: "Smart money positioning" },
  { key: "IV_CRUSH",           label: "IV Crush Play",          edge: "Volatility expansion" },
  { key: "CPR_VWAP",           label: "CPR + VWAP",             edge: "Central pivot confluence" },
  { key: "GAP_VWAP_FILL",      label: "Gap + VWAP Fill",        edge: "Gap fill tendency" },
];
