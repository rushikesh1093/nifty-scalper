"use client";
import { useEffect, useState } from "react";
import toast from "react-hot-toast";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getSessionStatus, getFeedStatus, reloginBroker } from "@/lib/api";
import { ApiError, errorMessage } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { useMarketStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import {
  DEFAULT_SIGNAL_WINDOW, formatIst, formatSessionWindow, isExchangeOpenIst, isSignalWindowOpenIst,
  parseSessionWindow,
} from "@/lib/time";
import { useDiagnostics } from "@/hooks/useDiagnostics";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";
import { RegimeStatus } from "@/components/regime/RegimeStatus";
import { BackendStatusChip } from "@/components/market/BackendStatusChip";

const CELL = "flex shrink-0 items-center gap-1.5 px-3 border-r border-border";

function Dot({ className, pulse }: { className: string; pulse?: boolean }) {
  return <span aria-hidden="true" className={cn("h-1.5 w-1.5 shrink-0 rounded-full", className, pulse && "animate-pulse-soft")} />;
}

function Clock() {
  const [time, setTime] = useState<string | null>(null);
  useEffect(() => {
    const tick = () => setTime(formatIst(new Date()));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="shrink-0 pl-3 font-mono text-table text-text-3">
      {time ?? "—"} IST
    </span>
  );
}

export function StatusBar() {
  const setSession    = useMarketStore((s) => s.setSession);
  const setFeedStatus = useMarketStore((s) => s.setFeedStatus);
  const qc = useQueryClient();

  const session = useQuery({ queryKey: queryKeys.session, queryFn: getSessionStatus, refetchInterval: 10_000 });
  const feed    = useQuery({ queryKey: queryKeys.feed,    queryFn: getFeedStatus,    refetchInterval: 10_000 });
  const diag    = useDiagnostics();
  const sessionErr = useStickyQueryError(session);
  const feedErr    = useStickyQueryError(feed);

  // Sync store slices from the queries. An errored feed query is "unknown", not "down".
  useEffect(() => {
    if (session.data) setSession(session.data);
  }, [session.data, setSession]);
  useEffect(() => {
    if (feedErr.isError) setFeedStatus("unknown");
    else if (feed.data) setFeedStatus(feed.data.connected ? "live" : "down");
  }, [feed.data, feedErr.isError, setFeedStatus]);

  const relogin = useMutation({
    mutationFn: reloginBroker,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.session });
      qc.invalidateQueries({ queryKey: queryKeys.feed });
      toast.success("Session refreshed");
    },
    onError: (err: unknown) => {
      const detail = err instanceof ApiError && err.detail ? err.detail : errorMessage(err);
      toast.error(`Reconnect failed: ${detail}`);
    },
  });

  // Signal window: label from the diagnostics payload (finding 13); local constants only pre-fetch.
  const sessionWindowText = diag.data?.session_window ?? null;
  const signalWindow = parseSessionWindow(sessionWindowText) ?? DEFAULT_SIGNAL_WINDOW;
  const signalLabel = `Signals ${formatSessionWindow(signalWindow)}`;

  // Both clock-derived chips are computed in an effect (mount + every 30 s) with a neutral `null`
  // first render, so SSR and the first client render agree (hydration fix).
  const [exchangeOpen, setExchangeOpen] = useState<boolean | null>(null);
  const [clockSignalOpen, setClockSignalOpen] = useState<boolean | null>(null);
  const { h: openH, m: openM } = signalWindow.open;
  const { h: closeH, m: closeM } = signalWindow.close;
  useEffect(() => {
    const win = { open: { h: openH, m: openM }, close: { h: closeH, m: closeM } };
    const tick = () => {
      const now = new Date();
      setExchangeOpen(isExchangeOpenIst(now));
      setClockSignalOpen(isSignalWindowOpenIst(now, win));
    };
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, [openH, openM, closeH, closeM]);
  // Open/closed state from the payload while it is fresh; the clock (with the payload's window) otherwise.
  const signalOpen = diag.data && !diag.isError ? diag.data.session_window_open : clockSignalOpen;

  const loggedIn = session.data?.is_logged_in;
  const feedOk   = feed.data?.connected;
  const subs     = feed.data?.subscriptions ?? 0;

  return (
    <div
      role="region"
      aria-label="System status"
      className="flex h-7 items-center overflow-x-auto whitespace-nowrap border-b border-border bg-surface-2 px-4 text-table"
    >
      {/* Exchange hours (09:15–15:30 IST) */}
      <div className={cn(CELL, "pl-0")} data-testid="market-chip">
        {exchangeOpen === null ? (
          <span className="text-text-3">Market —</span>
        ) : (
          <>
            <Dot className={exchangeOpen ? "bg-green" : "bg-text-3"} />
            <span className={exchangeOpen ? "text-green" : "text-text-2"}>
              {exchangeOpen ? "Market Open" : "Market Closed"}
            </span>
          </>
        )}
      </div>

      {/* Strategy signal window (label from diagnostics.session_window) */}
      <div
        className={CELL}
        data-testid="signal-window-chip"
        data-session-window={sessionWindowText ?? ""}
        title={diag.data?.session_window_note ?? "Strategy signal-generation window (fallback constants until diagnostics load)"}
      >
        {signalOpen === null ? (
          <span className="text-text-3">{signalLabel} —</span>
        ) : (
          <>
            <Dot className={signalOpen ? "bg-accent" : "bg-text-3"} />
            <span className={signalOpen ? "text-text-1" : "text-text-2"}>{signalLabel}</span>
            <span className={cn("text-micro", signalOpen ? "text-accent" : "text-text-3")}>
              {signalOpen ? "open" : "closed"}
            </span>
          </>
        )}
      </div>

      {/* Feed */}
      <div className={CELL} data-testid="feed-chip">
        {feedErr.isError ? (
          <span className="text-text-3" title={errorMessage(feedErr.error)}>Feed —</span>
        ) : feed.data === undefined ? (
          <span className="text-text-3">Feed …</span>
        ) : (
          <>
            <Dot className={feedOk ? "bg-green" : "bg-red"} pulse={feedOk} />
            <span className={feedOk ? "text-green" : "text-red"}>
              {feedOk ? `Live · ${subs} tokens` : "Feed Off"}
            </span>
          </>
        )}
      </div>

      {/* Broker */}
      <div className={cn(CELL, "gap-2")} data-testid="broker-chip">
        {sessionErr.isError ? (
          <span className="text-text-3" title={errorMessage(sessionErr.error)}>Broker —</span>
        ) : session.data === undefined ? (
          <span className="text-text-3">Broker …</span>
        ) : loggedIn ? (
          <>
            <svg aria-hidden="true" className="h-3 w-3 shrink-0 text-green" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            <span className="font-medium text-text-2">
              <span className="sr-only">Broker logged in as </span>{session.data.client_code}
            </span>
          </>
        ) : (
          <>
            <span className="text-red">Broker logged out</span>
            <button
              type="button"
              onClick={() => relogin.mutate()}
              disabled={relogin.isPending}
              className="rounded border border-accent/40 bg-accent/15 px-2 py-px text-micro font-semibold text-accent hover:bg-accent/25 disabled:opacity-50"
            >
              {relogin.isPending ? "Connecting…" : "Reconnect"}
            </button>
          </>
        )}
      </div>

      <RegimeStatus />
      <BackendStatusChip />

      <div className="flex-1" />
      <Clock />
    </div>
  );
}
