"use client";
import { useEffect, useState } from "react";
import { useMarketStore } from "@/lib/store";
import type { FeedStatusKind, Tick } from "@/types/market";

// Live tick sockets (design §6.2, §7.2). One shared, ref-counted WebSocket per channel: the spot
// ticker, the chart and a position row asking for the same token share a single socket.
// StrictMode-safe: the last release closes the socket only after a short grace period, so the dev
// double-mount (mount → unmount → mount) re-acquires the same socket instead of closing it
// mid-handshake. Reconnects use capped exponential backoff with jitter and never run while the tab
// is hidden. The aggregate state is written to store.feedStatus.

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

export const TICK_WS_BASE = resolveWsBase(process.env.NEXT_PUBLIC_WS_URL);

const BACKOFF_BASE_MS = 1_000;
const BACKOFF_CAP_MS = 30_000;
/** Consecutive failures after which a channel reports "down" instead of "reconnecting". */
const DOWN_AFTER_FAILURES = 2;
/** Keep a released socket this long so a StrictMode remount reuses it. */
const RELEASE_GRACE_MS = 1_500;

/** idle: no token · connecting: first attempt · live: socket open · reconnecting / down: retrying. */
export type TickSocketStatus = "idle" | "connecting" | "live" | "reconnecting" | "down";

interface Channel {
  key:        string;
  url:        string;
  refs:       number;
  ws:         WebSocket | null;
  status:     TickSocketStatus;
  failures:   number;
  warned:     boolean;
  retryTimer: ReturnType<typeof setTimeout> | null;
  closeTimer: ReturnType<typeof setTimeout> | null;
  pendingWhileHidden: boolean;
  listeners:  Set<(s: TickSocketStatus) => void>;
  onFrame:    (raw: unknown) => void;
}

const channels = new Map<string, Channel>();
let visibilityHooked = false;
let malformedFrames = 0;

function backoffDelay(attempt: number): number {
  const exp = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1));
  return Math.round(exp * (0.5 + Math.random() * 0.5));
}

function isHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

function countMalformed(where: string) {
  malformedFrames++;
  // Counted, not silenced (design §7.2): first occurrence and every 100th after.
  if (malformedFrames === 1 || malformedFrames % 100 === 0) {
    console.warn(`[tick-ws] malformed frame ignored on ${where} (${malformedFrames} so far)`);
  }
}

/** Parse a frame into a Tick; server keep-alive frames ({"ping":1}) return null silently. */
function parseTick(raw: unknown, where: string): Tick | null {
  if (typeof raw !== "string") { countMalformed(where); return null; }
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch {
    countMalformed(where);
    return null;
  }
  if (!v || typeof v !== "object") { countMalformed(where); return null; }
  if ("ping" in v) return null;
  const t = v as Tick;
  if (typeof t.ltp !== "number" || !Number.isFinite(t.ltp)) { countMalformed(where); return null; }
  return t;
}

function aggregateStatus(): FeedStatusKind {
  let live = false, connecting = false, reconnecting = false, down = false;
  channels.forEach((ch) => {
    if (ch.refs <= 0) return;
    if (ch.status === "live") live = true;
    else if (ch.status === "connecting") connecting = true;
    else if (ch.status === "reconnecting") reconnecting = true;
    else if (ch.status === "down") down = true;
  });
  if (live) return "live";
  if (reconnecting) return "reconnecting";
  if (connecting) return "connecting";
  if (down) return "down";
  return "unknown";
}

function publishAggregate() {
  const next = aggregateStatus();
  const store = useMarketStore.getState();
  if (store.feedStatus !== next) store.setFeedStatus(next);
}

function setStatus(ch: Channel, s: TickSocketStatus) {
  if (ch.status === s) return;
  ch.status = s;
  ch.listeners.forEach((l) => l(s));
  publishAggregate();
}

function scheduleReconnect(ch: Channel) {
  if (ch.refs <= 0 || ch.retryTimer) return;
  if (isHidden()) { ch.pendingWhileHidden = true; return; }
  ch.retryTimer = setTimeout(() => {
    ch.retryTimer = null;
    connect(ch);
  }, backoffDelay(ch.failures));
}

function connect(ch: Channel) {
  if (ch.refs <= 0 || ch.ws) return;
  if (isHidden()) { ch.pendingWhileHidden = true; return; }
  let socket: WebSocket;
  try {
    socket = new WebSocket(ch.url);
  } catch (e) {
    ch.failures++;
    console.warn(`[tick-ws] cannot open ${ch.url}: ${e instanceof Error ? e.message : String(e)}`);
    setStatus(ch, "down");
    scheduleReconnect(ch);
    return;
  }
  ch.ws = socket;
  setStatus(ch, ch.failures > 0 ? (ch.failures >= DOWN_AFTER_FAILURES ? "down" : "reconnecting") : "connecting");

  socket.onopen = () => {
    if (ch.ws !== socket) return;
    ch.failures = 0;
    ch.warned = false;
    setStatus(ch, "live");
  };
  socket.onmessage = (ev: MessageEvent) => {
    if (ch.ws !== socket) return;
    ch.onFrame(ev.data);
  };
  // onerror is always followed by onclose; all handling lives there.
  socket.onerror = () => undefined;
  socket.onclose = (ev: CloseEvent) => {
    if (ch.ws !== socket) return;
    ch.ws = null;
    ch.failures++;
    if (!ch.warned) {
      ch.warned = true; // first failure only, not every retry (design §7.2)
      console.warn(`[tick-ws] ${ch.key} closed (code ${ev.code}); reconnecting with backoff`);
    }
    setStatus(ch, ch.failures >= DOWN_AFTER_FAILURES ? "down" : "reconnecting");
    scheduleReconnect(ch);
  };
}

function teardown(ch: Channel) {
  channels.delete(ch.key);
  if (ch.retryTimer) clearTimeout(ch.retryTimer);
  if (ch.closeTimer) clearTimeout(ch.closeTimer);
  ch.retryTimer = ch.closeTimer = null;
  const s = ch.ws;
  ch.ws = null;
  if (s) {
    s.onmessage = null;
    s.onerror = null;
    s.onclose = null;
    if (s.readyState === WebSocket.CONNECTING) {
      // Closing mid-handshake logs a browser warning; close as soon as it opens instead.
      s.onopen = () => s.close();
    } else {
      s.onopen = null;
      s.close();
    }
  }
  publishAggregate();
}

function onVisibility() {
  if (isHidden()) return;
  channels.forEach((ch) => {
    if (ch.pendingWhileHidden && !ch.ws && !ch.retryTimer && ch.refs > 0) {
      ch.pendingWhileHidden = false;
      connect(ch);
    }
  });
}

function acquire(key: string, url: string, onFrame: (raw: unknown) => void): Channel {
  if (!visibilityHooked && typeof document !== "undefined") {
    visibilityHooked = true;
    document.addEventListener("visibilitychange", onVisibility);
  }
  let ch = channels.get(key);
  if (!ch) {
    ch = {
      key, url, refs: 0, ws: null, status: "connecting", failures: 0, warned: false,
      retryTimer: null, closeTimer: null, pendingWhileHidden: false, listeners: new Set(), onFrame,
    };
    channels.set(key, ch);
  }
  ch.refs++;
  if (ch.closeTimer) { clearTimeout(ch.closeTimer); ch.closeTimer = null; }
  if (!ch.ws && !ch.retryTimer) connect(ch);
  publishAggregate();
  return ch;
}

function release(ch: Channel) {
  ch.refs = Math.max(0, ch.refs - 1);
  if (ch.refs > 0 || ch.closeTimer) return;
  ch.closeTimer = setTimeout(() => {
    ch.closeTimer = null;
    if (ch.refs === 0) teardown(ch);
  }, RELEASE_GRACE_MS);
}

type ChannelKind = "tick" | "chain";

/** Channel identity (key, url) and frame handler, derived from the kind + id only. */
function channelSpec(kind: ChannelKind, id: string) {
  const key = `${kind}:${id}`;
  if (kind === "tick") {
    return {
      key,
      url: `${TICK_WS_BASE}/market/ws/${id}`,
      onFrame: (raw: unknown) => {
        const t = parseTick(raw, key);
        if (t) useMarketStore.getState().setTick(id, t);
      },
    };
  }
  return {
    key,
    url: `${TICK_WS_BASE}/market/ws/option-chain/${id}`,
    onFrame: (raw: unknown) => {
      const t = parseTick(raw, key);
      if (t && t.token) useMarketStore.getState().setTick(t.token, t);
    },
  };
}

function useChannel(kind: ChannelKind, id: string | null): TickSocketStatus {
  const [status, setLocal] = useState<TickSocketStatus>("idle");
  useEffect(() => {
    if (!id) { setLocal("idle"); return; }
    const spec = channelSpec(kind, id);
    const ch = acquire(spec.key, spec.url, spec.onFrame);
    const listener = (s: TickSocketStatus) => setLocal(s);
    ch.listeners.add(listener);
    setLocal(ch.status);
    return () => {
      ch.listeners.delete(listener);
      release(ch);
    };
  }, [kind, id]);
  return status;
}

/** Live ticks for one token → store.ticks[token]. Returns this socket's state. */
export function useTickFeed(token: string | null): TickSocketStatus {
  return useChannel("tick", token || null);
}

/** Live ticks for every strike of an underlying's nearest-expiry chain → store.ticks[tick.token]. */
export function useOptionChainFeed(underlying: string | null): TickSocketStatus {
  return useChannel("chain", underlying || null);
}
