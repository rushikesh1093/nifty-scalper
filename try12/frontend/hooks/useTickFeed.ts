"use client";
import { useEffect, useRef } from "react";
import { useMarketStore } from "@/lib/store";
import type { Tick } from "@/types/market";

const WS_BASE = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/api/v1";

/**
 * Subscribe to a single token's live tick feed via WebSocket.
 * Automatically reconnects on disconnect.
 */
export function useTickFeed(token: string | null) {
  // Pin setTick in a ref so it never appears in the effect dep array.
  // Zustand store actions are stable, but the ref pattern is explicit insurance.
  const setTickRef = useRef(useMarketStore.getState().setTick);
  const wsRef      = useRef<WebSocket | null>(null);
  const retryRef   = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!token) return;

    let cancelled = false;

    function connect() {
      if (cancelled) return;
      const ws = new WebSocket(`${WS_BASE}/market/ws/${token!}`);
      wsRef.current = ws;

      ws.onmessage = (e) => {
        try {
          const tick: Tick = JSON.parse(e.data);
          if (token) setTickRef.current(token, tick);
        } catch {}
      };

      ws.onclose = () => {
        if (!cancelled) {
          retryRef.current = setTimeout(connect, 3000);
        }
      };

      ws.onerror = () => ws.close();
    }

    connect();

    return () => {
      cancelled = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [token]); // token is the only real dep — reconnect only when token changes
}

/**
 * Subscribe to full option chain live feed for an underlying.
 * Updates individual token ticks in the store.
 */
export function useOptionChainFeed(underlying: string | null) {
  const setTickRef = useRef(useMarketStore.getState().setTick);
  const wsRef      = useRef<WebSocket | null>(null);
  const retryRef   = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!underlying) return;

    let cancelled = false;

    function connect() {
      if (cancelled) return;
      const ws = new WebSocket(`${WS_BASE}/market/ws/option-chain/${underlying}`);
      wsRef.current = ws;

      ws.onmessage = (e) => {
        try {
          const tick: Tick = JSON.parse(e.data);
          if (tick.token) setTickRef.current(tick.token, tick);
        } catch {}
      };

      ws.onclose = () => {
        if (!cancelled) retryRef.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => ws.close();
    }

    connect();

    return () => {
      cancelled = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [underlying]); // only reconnect when underlying changes
}


