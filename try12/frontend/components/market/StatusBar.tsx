"use client";
import axios from "axios";
import toast from "react-hot-toast";
import { getSessionStatus, getFeedStatus } from "@/lib/api";
import { useMarketStore } from "@/lib/store";
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { isExchangeOpenIst } from "@/lib/time";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { RegimeStatus } from "@/components/regime/RegimeStatus";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const api  = axios.create({ baseURL: BASE, timeout: 10000 });

function Clock() {
  const [time, setTime] = useState("");
  useEffect(() => {
    const tick = () =>
      setTime(new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span className="font-mono text-[11px]" style={{ color: "var(--text-3)" }}>
      {time} IST
    </span>
  );
}

export function StatusBar() {
  const { setSession, setFeedConnected } = useMarketStore();
  const qc = useQueryClient();

  const { data: session } = useQuery({
    queryKey: ["session-status"], queryFn: getSessionStatus, refetchInterval: 20000,
  });
  const { data: feedStatus } = useQuery({
    queryKey: ["feed-status"], queryFn: getFeedStatus, refetchInterval: 10000,
  });

  // Single effect to sync both store slices — avoids two separate re-renders
  useEffect(() => {
    if (session)    setSession(session);
    if (feedStatus) setFeedConnected(feedStatus.connected);
  }, [session, feedStatus, setSession, setFeedConnected]);

  const relogin = useMutation({
    mutationFn: () => api.post("/auth/refresh"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["session-status"] });
      qc.invalidateQueries({ queryKey: ["feed-status"] });
      toast.success("Session refreshed");
    },
    onError: (err: any) => {
      const detail = err?.response?.data?.detail ?? "Could not reach the backend";
      toast.error(`Reconnect failed: ${detail}`);
    },
  });

  const marketOpen = isExchangeOpenIst();
  const loggedIn   = session?.is_logged_in;
  const feedOk     = feedStatus?.connected;
  const subs       = feedStatus?.subscriptions ?? 0;

  return (
    <div
      className="flex items-center gap-0 px-4"
      style={{
        height: "28px",
        background: "var(--surface-2)",
        borderBottom: "1px solid var(--border)",
        fontSize: "11px",
      }}
    >
      {/* Market hours */}
      <div className="flex items-center gap-1.5 pr-3" style={{ borderRight: "1px solid var(--border)" }}>
        <span
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: marketOpen ? "var(--green)" : "var(--text-3)" }}
        />
        <span style={{ color: marketOpen ? "var(--green)" : "var(--text-3)" }}>
          {marketOpen ? "Market Open" : "Market Closed"}
        </span>
      </div>

      {/* Feed */}
      <div className="flex items-center gap-1.5 px-3" style={{ borderRight: "1px solid var(--border)" }}>
        <span
          className={cn("w-1.5 h-1.5 rounded-full shrink-0", feedOk && "animate-pulse-soft")}
          style={{ background: feedOk ? "var(--green)" : "var(--red)" }}
        />
        <span style={{ color: feedOk ? "var(--green)" : "var(--red)" }}>
          {feedOk ? `Live · ${subs} tokens` : "Feed Off"}
        </span>
      </div>

      {/* Broker */}
      <div className="flex items-center gap-2 px-3" style={{ borderRight: "1px solid var(--border)" }}>
        {loggedIn ? (
          <>
            <svg className="w-3 h-3 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"
              style={{ color: "var(--green)" }}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            <span className="font-medium" style={{ color: "var(--text-2)" }}>
              {session?.client_code}
            </span>
          </>
        ) : (
          <div className="flex items-center gap-2">
            <span style={{ color: "var(--red)" }}>Broker logged out</span>
            <button
              onClick={() => relogin.mutate()}
              disabled={relogin.isPending}
              className="px-2 py-0.5 rounded text-[10px] font-semibold transition-all disabled:opacity-50"
              style={{
                background: "var(--accent-dim)",
                color: "var(--accent)",
                border: "1px solid var(--accent-glow)",
              }}
            >
              {relogin.isPending ? "Connecting…" : "Reconnect"}
            </button>
          </div>
        )}
      </div>

      {/* Regime Status */}
      <RegimeStatus />

      {/* Spacer */}
      <div className="flex-1" />

      {/* Clock */}
      <Clock />
    </div>
  );
}
