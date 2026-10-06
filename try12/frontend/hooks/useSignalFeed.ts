"use client";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/queryKeys";

// Signals WebSocket accelerator (design §6.9, §7.2). The 5 s HTTP poll is the primary path; any
// frame on /trading/ws/signals only triggers a debounced refetch — the payload itself is never
// trusted or rendered. StrictMode-safe: every effect run owns its socket/timers via `cancelled`.

const DEFAULT_WS_BASE = "ws://localhost:8000/api/v1";

function resolveWsBase(raw: string | undefined): string {
  if (!raw || raw.trim() === "") return DEFAULT_WS_BASE;
  try {
    const u = new URL(raw.trim());
    if (u.protocol !== "ws:" && u.protocol !== "wss:") return DEFAULT_WS_BASE;
    return raw.trim().replace(/\/+$/, "");
  } catch {
    return DEFAULT_WS_BASE;
  }
}

export const SIGNALS_WS_URL = `${resolveWsBase(process.env.NEXT_PUBLIC_WS_URL)}/trading/ws/signals`;

const BACKOFF_BASE_MS = 1_000;
const BACKOFF_CAP_MS = 30_000;
const INVALIDATE_DEBOUNCE_MS = 500;
/** Consecutive failed attempts after which the socket is reported "down" (chip → polling). */
const DOWN_AFTER_FAILURES = 2;

/** connecting: first attempt in flight · live: open · reconnecting: retrying after a drop · down: retries failing. */
export type SignalSocketStatus = "connecting" | "live" | "reconnecting" | "down";

export interface SignalFeedState {
  status:        SignalSocketStatus;
  /** epoch ms of the last frame received, or null. */
  lastMessageAt: number | null;
  lastError:     string | null;
}

/** Exponential backoff capped at 30 s, with ±50 % jitter so many tabs never reconnect in lockstep. */
function backoffDelay(attempt: number): number {
  const exp = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1));
  return Math.round(exp * (0.5 + Math.random() * 0.5));
}

export function useSignalFeed(): SignalFeedState {
  const qc = useQueryClient();
  const qcRef = useRef(qc);
  qcRef.current = qc;

  const [state, setState] = useState<SignalFeedState>({ status: "connecting", lastMessageAt: null, lastError: null });

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    let failures = 0;
    let warnedConnect = false;
    let malformed = 0;
    let pendingWhileHidden = false;

    const invalidateSoon = () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        if (cancelled) return;
        void qcRef.current.invalidateQueries({ queryKey: queryKeys.signalsAll });
        void qcRef.current.invalidateQueries({ queryKey: queryKeys.diagnostics });
      }, INVALIDATE_DEBOUNCE_MS);
    };

    const scheduleReconnect = () => {
      if (cancelled || retryTimer) return;
      // No reconnect while the tab is hidden; the visibility listener resumes it.
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        pendingWhileHidden = true;
        return;
      }
      retryTimer = setTimeout(() => {
        retryTimer = null;
        connect();
      }, backoffDelay(failures));
    };

    function connect() {
      if (cancelled) return;
      let opened = false;
      let socket: WebSocket;
      try {
        socket = new WebSocket(SIGNALS_WS_URL);
      } catch (e) {
        failures++;
        setState((s) => ({ ...s, status: "down", lastError: e instanceof Error ? e.message : String(e) }));
        scheduleReconnect();
        return;
      }
      ws = socket;

      socket.onopen = () => {
        if (cancelled) return;
        opened = true;
        failures = 0;
        warnedConnect = false;
        setState((s) => ({ ...s, status: "live", lastError: null }));
      };

      socket.onmessage = (ev: MessageEvent) => {
        if (cancelled) return;
        try {
          JSON.parse(typeof ev.data === "string" ? ev.data : "");
        } catch {
          malformed++;
          console.warn(`[signals-ws] malformed frame ignored (${malformed} so far)`);
        }
        // Any frame (even a malformed one) means "something changed" — refetch, never trust the payload.
        setState((s) => ({ ...s, lastMessageAt: Date.now() }));
        invalidateSoon();
      };

      // onerror is always followed by onclose; all handling lives there.
      socket.onerror = () => undefined;

      socket.onclose = (ev: CloseEvent) => {
        if (ws === socket) ws = null;
        if (cancelled) return;
        if (!opened) failures++;
        else failures = 1; // a drop after a healthy connection: first retry is quick
        const reason = opened
          ? `socket closed (code ${ev.code}${ev.reason ? `: ${ev.reason}` : ""})`
          : `cannot connect to ${SIGNALS_WS_URL}`;
        if (!warnedConnect) {
          // Warn on the first failure only, not on every retry (design §7.2).
          warnedConnect = true;
          console.warn(`[signals-ws] ${reason}; falling back to polling and retrying with backoff`);
        }
        setState((s) => ({
          ...s,
          status: failures >= DOWN_AFTER_FAILURES ? "down" : "reconnecting",
          lastError: reason,
        }));
        scheduleReconnect();
      };
    }

    const onVisibility = () => {
      if (cancelled || document.visibilityState !== "visible") return;
      if (pendingWhileHidden && !ws && !retryTimer) {
        pendingWhileHidden = false;
        connect();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    connect();

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      if (retryTimer) clearTimeout(retryTimer);
      if (debounceTimer) clearTimeout(debounceTimer);
      const s = ws;
      ws = null;
      if (s) {
        s.onmessage = null;
        s.onerror = null;
        s.onclose = null;
        if (s.readyState === WebSocket.CONNECTING) {
          // Closing mid-handshake logs a browser warning (StrictMode double-mount); close once open.
          s.onopen = () => s.close();
        } else {
          s.onopen = null;
          s.close();
        }
      }
    };
  }, []);

  return state;
}
