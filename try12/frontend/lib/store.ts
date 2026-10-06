import { create } from "zustand";
import type { Tick, OptionChain, SessionStatus, FeedStatusKind, CandleInterval } from "@/types/market";

export interface MarketState {
  // Live ticks: token → Tick
  ticks: Record<string, Tick>;
  setTick: (token: string, tick: Tick) => void;

  // Selected instrument
  selectedToken:     string | null;
  selectedUnderlying: "NIFTY" | "BANKNIFTY";
  selectedExpiry:    string | null;
  setSelectedToken:  (token: string | null) => void;
  setUnderlying:     (u: "NIFTY" | "BANKNIFTY") => void;
  setExpiry:         (e: string | null) => void;

  // Chart timeframe
  selectedInterval:    CandleInterval;
  setSelectedInterval: (i: CandleInterval) => void;

  // Option chain
  optionChain:    OptionChain | null;
  setOptionChain: (chain: OptionChain | null) => void;

  // Session
  session:    SessionStatus | null;
  setSession: (s: SessionStatus | null) => void;

  // Feed status. `feedStatus` is the enum; `feedConnected` is kept for existing consumers.
  feedStatus:       FeedStatusKind;
  setFeedStatus:    (s: FeedStatusKind) => void;
  feedConnected:    boolean;
  setFeedConnected: (v: boolean) => void;
}

export const useMarketStore = create<MarketState>((set) => ({
  ticks: {},
  // Write only the changed token — Zustand merges at the top level,
  // so we write a nested path via a functional update that mutates only [token].
  setTick: (token, tick) =>
    set((s) => ({ ticks: { ...s.ticks, [token]: tick } })),

  selectedToken:      null,
  selectedUnderlying: "NIFTY",
  selectedExpiry:     null,
  setSelectedToken:   (token) => set({ selectedToken: token }),
  setUnderlying:      (u)     => set({ selectedUnderlying: u }),
  setExpiry:          (e)     => set({ selectedExpiry: e }),

  selectedInterval:    "ONE_MINUTE",
  setSelectedInterval: (i) => set({ selectedInterval: i }),

  optionChain:    null,
  setOptionChain: (chain) => set({ optionChain: chain }),

  session:    null,
  setSession: (s) => set({ session: s }),

  feedStatus:       "unknown",
  setFeedStatus:    (s) => set({ feedStatus: s, feedConnected: s === "live" }),
  feedConnected:    false,
  setFeedConnected: (v) => set({ feedConnected: v, feedStatus: v ? "live" : "down" }),
}));

// ── Field selectors ──────────────────────────────────────────────────────────
// Subscribe to one field at a time. Never `useMarketStore()` (whole store → re-render on every
// tick) and never an object-literal selector (new object each call → getSnapshot warning / loop).

/** `useMarketField("selectedToken")` — re-renders only when that field changes. */
export function useMarketField<K extends keyof MarketState>(key: K): MarketState[K] {
  return useMarketStore((s) => s[key]);
}

/** Stable per-token selector: `useMarketStore(selectTick(token))`. */
export const selectTick = (token: string | null | undefined) => (s: MarketState): Tick | undefined =>
  token ? s.ticks[token] : undefined;
