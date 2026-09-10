import { create } from "zustand";
import type { Tick, OptionChain, SessionStatus } from "@/types/market";

interface MarketState {
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

  // Option chain
  optionChain:    OptionChain | null;
  setOptionChain: (chain: OptionChain | null) => void;

  // Session
  session:    SessionStatus | null;
  setSession: (s: SessionStatus | null) => void;

  // Feed status
  feedConnected: boolean;
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

  optionChain:    null,
  setOptionChain: (chain) => set({ optionChain: chain }),

  session:    null,
  setSession: (s) => set({ session: s }),

  feedConnected:    false,
  setFeedConnected: (v) => set({ feedConnected: v }),
}));
