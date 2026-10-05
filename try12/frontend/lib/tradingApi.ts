import axios from "axios";
import type {
  Signal, Position, DailyStats, PnLHistory,
  OptionCandidate, TradingConfig, CapitalSummary, TradePreviewRow,
} from "@/types/trading";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const api  = axios.create({ baseURL: BASE, timeout: 10000 });

export const getMode          = ()         => api.get<{mode:string}>("/trading/mode").then(r=>r.data);
export const switchPaper      = ()         => api.post("/trading/mode/paper").then(r=>r.data);
export const switchLive       = (confirm:boolean) => api.post(`/trading/mode/live?confirm=${confirm}`).then(r=>r.data);
export const getConfig        = ()         => api.get<TradingConfig>("/trading/config").then(r=>r.data);
export const updateConfig     = (d:Partial<TradingConfig>) => api.put("/trading/config",d).then(r=>r.data);
export const setStrategyFlag  = (n:string,e:boolean) => api.put("/trading/config/strategy-flag",{strategy_name:n,enabled:e}).then(r=>r.data);
export const getDailyStats    = ()         => api.get<DailyStats>("/trading/stats").then(r=>r.data);

// ── Capital ────────────────────────────────────────────────────────────────
export const getCapital = () =>
  api.get<CapitalSummary>("/trading/capital").then(r => r.data);

export const setTradeAmount = (trade_amount: number, capital?: number) =>
  api.put<{ status: string; trade_amount: number; capital: number; available_capital: number; preview: TradePreviewRow[] }>(
    "/trading/capital/trade-amount",
    { trade_amount, capital }
  ).then(r => r.data);

export const setCapital = (capital: number) =>
  api.put(`/trading/capital/set?capital=${capital}`).then(r => r.data);

export const resetDailyCounter = () =>
  api.post("/trading/capital/reset-daily").then(r => r.data);

// ── Signals / Positions / P&L ─────────────────────────────────────────────
export const getSignals       = (p?:{strategy?:string;underlying?:string;limit?:number;today_only?:boolean}) =>
  api.get<Signal[]>("/trading/signals",{params:p}).then(r=>r.data);
export const getPositions     = (p?:{is_open?:boolean;limit?:number}) =>
  api.get<Position[]>("/trading/positions",{params:p}).then(r=>r.data);
export const closePosition    = (id:number) => api.post(`/trading/positions/${id}/close`).then(r=>r.data);
export const clearTestTrades  = () => api.post("/trading/clear-test-trades").then(r=>r.data);
export const getPnLToday      = ()         => api.get("/trading/pnl").then(r=>r.data);
export const getPnLHistory    = (days=22)  => api.get<PnLHistory[]>(`/trading/pnl/history?days=${days}`).then(r=>r.data);
export const selectOption     = (d:{direction:string;underlying:string;spot_price:number}) =>
  api.post<OptionCandidate[]>("/trading/option-selector",d).then(r=>r.data);
export const getIntradayFeatures = (underlying="NIFTY",date?:string) =>
  api.get("/trading/market-study/intraday",{params:{underlying,date}}).then(r=>r.data);
export const getDailySnapshots= (underlying="NIFTY",days=22) =>
  api.get("/trading/market-study/daily",{params:{underlying,days}}).then(r=>r.data);

