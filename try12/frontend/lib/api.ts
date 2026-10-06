import { http, rootHttp } from "@/lib/http";
import type {
  Tick, Candle, OptionChain, Instrument, SessionStatus, FeedStatus,
  RegimeStatus, RegimeSpan, RegimePerformanceRow,
} from "@/types/market";

// All versioned calls go through `http` (baseURL NEXT_PUBLIC_API_URL); failures arrive as ApiError.

// ── Auth ─────────────────────────────────────────────────────────────────────
export const getSessionStatus  = () => http.get<SessionStatus>("/auth/status").then(r => r.data);
export const logout             = () => http.post("/auth/logout");
/** Refresh the broker session (formerly StatusBar's private axios call). */
export const reloginBroker      = () => http.post("/auth/refresh").then(r => r.data);

// ── Market ────────────────────────────────────────────────────────────────────
export const getTick = (token: string) =>
  http.get<Tick>(`/market/tick/${token}`).then(r => r.data);

export const getBulkLTP = (tokens: string[]) =>
  http.post<{ data: Record<string, number | null> }>("/market/ltp/bulk", { tokens }).then(r => r.data);

export const getCandles = (token: string, interval = "ONE_MINUTE", count = 200) =>
  http.get<Candle[]>(`/market/candles/${token}`, { params: { interval, count } }).then(r => r.data);

export const getOptionChain = (underlying: string, expiry?: string) =>
  http.get<OptionChain>(`/market/option-chain/${underlying}`, {
    params: expiry ? { expiry } : {},
  }).then(r => r.data);

export const getFeedStatus = () =>
  http.get<FeedStatus>("/market/feed/status").then(r => r.data);

// ── Instruments ───────────────────────────────────────────────────────────────
export const getExpiries = (underlying: string) =>
  http.get<string[]>(`/instruments/expiries/${underlying}`).then(r => r.data);

export const searchInstruments = (q: string, exchange = "NFO") =>
  http.get<Instrument[]>("/instruments/search", { params: { q, exchange } }).then(r => r.data);

export const triggerInstrumentSync = () =>
  http.post("/instruments/sync").then(r => r.data);

// ── Health ────────────────────────────────────────────────────────────────────
// /health is app-mounted (main.py), not under /api/v1 → the root instance (design §6.4).
export interface HealthStatus { status: string; [k: string]: unknown }
export const healthCheck = () =>
  rootHttp.get<HealthStatus>("/health").then(r => r.data);

// ── Regime ────────────────────────────────────────────────────────────────────
export const getCurrentRegime = (underlying = "NIFTY") =>
  http.get<RegimeStatus>(`/regime/current`, { params: { underlying } }).then(r => r.data);

/** Collapsed, ascending spans (collapse=true is the backend default). */
export const getRegimeHistory = (underlying = "NIFTY", hours = 24, limit = 500, collapse = true) =>
  http.get<RegimeSpan[]>(`/regime/history`, { params: { underlying, hours, limit, collapse } }).then(r => r.data);

export const getRegimePerformance = (underlying?: string) =>
  http.get<RegimePerformanceRow[]>("/regime/performance", { params: underlying ? { underlying } : {} })
    .then(r => r.data);
