import axios from "axios";
import type { Tick, Candle, OptionChain, Instrument, SessionStatus, FeedStatus, RegimeStatus, RegimeHistoryEntry } from "@/types/market";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

const api = axios.create({ baseURL: BASE, timeout: 10000 });

// ── Auth ─────────────────────────────────────────────────────────────────────
export const getSessionStatus  = () => api.get<SessionStatus>("/auth/status").then(r => r.data);
export const logout             = () => api.post("/auth/logout");

// ── Market ────────────────────────────────────────────────────────────────────
export const getTick = (token: string) =>
  api.get<Tick>(`/market/tick/${token}`).then(r => r.data);

export const getBulkLTP = (tokens: string[]) =>
  api.post<{ data: Record<string, number | null> }>("/market/ltp/bulk", { tokens }).then(r => r.data);

export const getCandles = (token: string, interval = "ONE_MINUTE", count = 200) =>
  api.get<Candle[]>(`/market/candles/${token}`, { params: { interval, count } }).then(r => r.data);

export const getOptionChain = (underlying: string, expiry?: string) =>
  api.get<OptionChain>(`/market/option-chain/${underlying}`, {
    params: expiry ? { expiry } : {},
  }).then(r => r.data);

export const getFeedStatus = () =>
  api.get<FeedStatus>("/market/feed/status").then(r => r.data);

// ── Instruments ───────────────────────────────────────────────────────────────
export const getExpiries = (underlying: string) =>
  api.get<string[]>(`/instruments/expiries/${underlying}`).then(r => r.data);

export const searchInstruments = (q: string, exchange = "NFO") =>
  api.get<Instrument[]>("/instruments/search", { params: { q, exchange } }).then(r => r.data);

export const triggerInstrumentSync = () =>
  api.post("/instruments/sync").then(r => r.data);

// ── Health ────────────────────────────────────────────────────────────────────
export const healthCheck = () =>
  axios.get(`${BASE.replace("/api/v1", "")}/health`).then(r => r.data);

// ── Regime ────────────────────────────────────────────────────────────────────
export const getCurrentRegime = (underlying = "NIFTY") =>
  api.get<RegimeStatus>(`/regime/current`, { params: { underlying } }).then(r => r.data);

export const getRegimeHistory = (underlying = "NIFTY", hours = 24) =>
  api.get<RegimeHistoryEntry[]>(`/regime/history`, { params: { underlying, hours } }).then(r => r.data);
