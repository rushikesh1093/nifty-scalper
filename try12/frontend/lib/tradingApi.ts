import { ApiError, http } from "@/lib/http";
import type {
  Signal, Position, DailyStats, PnLHistory,
  OptionCandidate, TradingConfig, CapitalSummary, TradePreviewRow,
} from "@/types/trading";
import type { SignalDiagnostics } from "@/types/diagnostics";

// All calls go through the shared `http` instance; failures arrive as ApiError.
const api = http;

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
export interface SignalsQuery {
  strategy?:   string;
  underlying?: string;
  /** SignalStatus value; an invalid value is a 400 from the API. */
  status?:     string;
  limit?:      number;
  today_only?: boolean;
}
export const getSignals       = (p?:SignalsQuery) =>
  api.get<Signal[]>("/trading/signals",{params:p}).then(r=>r.data);
/** Read-only signal-pipeline diagnostics; total by construction (never 500). */
export const getDiagnostics   = () =>
  api.get<SignalDiagnostics>("/trading/signals/diagnostics").then(r=>r.data);
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

// ── Report ────────────────────────────────────────────────────────────────
/**
 * Fetch the trade-report PDF as a Blob. Checks content-type so a JSON error body is never
 * handed back as a corrupt .pdf; HTTP errors arrive as ApiError with FastAPI's detail.
 */
export async function downloadReportPdf(mode: "ALL" | "PAPER" | "LIVE", days: number): Promise<Blob> {
  const r = await api.get<Blob>("/trading/report/pdf", {
    params: { mode, days },
    responseType: "blob",
    timeout: 60_000,
  });
  const type = String(r.headers["content-type"] ?? "");
  if (!type.includes("application/pdf")) {
    throw new ApiError("parse", r.status, `content-type ${type || "missing"}`, "Report endpoint did not return a PDF");
  }
  return r.data;
}
