"use client";
import { useQuery } from "@tanstack/react-query";
import { getSignals } from "@/lib/tradingApi";
import { formatPrice, cn } from "@/lib/utils";
import { useEffect, useRef } from "react";
import type { Signal } from "@/types/trading";

const STATUS_COLOR: Record<string, { fg: string; bg: string }> = {
  GENERATED:  { fg: "var(--yellow)", bg: "var(--yellow-dim)" },
  SELECTED:   { fg: "var(--blue)",   bg: "var(--blue-dim)"   },
  ENTERED:    { fg: "var(--green)",  bg: "var(--green-dim)"  },
  SL_HIT:     { fg: "var(--red)",    bg: "var(--red-dim)"    },
  TARGET_HIT: { fg: "var(--green)",  bg: "var(--green-dim)"  },
  EXPIRED:    { fg: "var(--text-3)", bg: "transparent"       },
  SKIPPED:    { fg: "var(--text-4)", bg: "transparent"       },
};

const DIR_COLOR: Record<string, { fg: string; bg: string; border: string }> = {
  BUY:  { fg: "var(--green)", bg: "var(--green-dim)", border: "#22d3a030" },
  SELL: { fg: "var(--red)",   bg: "var(--red-dim)",   border: "#f43f5e30" },
};

const TH = "px-3 py-2.5 text-left whitespace-nowrap";
const TD = "px-3 py-2.5 align-middle";

/** Truncate a skip reason string for inline display (full text in title tooltip). */
function truncateReason(r: string, max = 55): string {
  return r.length > max ? r.slice(0, max) + "…" : r;
}

export function SignalsTable() {
  const today = new Date().toISOString().slice(0, 10); // "YYYY-MM-DD" — key resets at midnight

  const { data: signals = [], refetch } = useQuery({
    queryKey: ["signals", today],
    queryFn:  () => getSignals({ limit: 60 }),
    refetchInterval: 30000,
  });

  const wsRef   = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const WS = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/api/v1";
    let cancelled = false;

    function connect() {
      if (cancelled) return;
      const ws = new WebSocket(`${WS}/trading/ws/signals`);
      wsRef.current = ws;
      ws.onmessage = () => {
        if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
        refetchTimerRef.current = setTimeout(() => refetch(), 500);
      };
      ws.onerror   = () => ws.close();
      ws.onclose   = () => {
        if (!cancelled) retryRef.current = setTimeout(connect, 3000);
      };
    }

    connect();
    return () => {
      cancelled = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
      wsRef.current?.close();
    };
  }, []);

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-2">
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24" style={{ color: "var(--accent)" }}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-2)" }}>Strategy Signals</span>
        </div>
        <span className="font-mono text-[10px] px-2 py-0.5 rounded" style={{ color: "var(--text-3)", background: "var(--surface-3)" }}>
          {signals.length === 0 ? "fires 9:15 AM IST" : `${signals.length} signals`}
        </span>
      </div>

      {signals.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-10 gap-2">
          <svg className="w-9 h-9 opacity-20" fill="none" stroke="currentColor" strokeWidth={1.2} viewBox="0 0 24 24" style={{ color: "var(--text-3)" }}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          <span className="text-[11px]" style={{ color: "var(--text-3)" }}>No signals yet</span>
          <span className="text-[10px]" style={{ color: "var(--text-4)" }}>Strategies evaluate every minute during market hours</span>
        </div>
      ) : (
        <div className="overflow-auto" style={{ maxHeight: 320 }}>
          <table className="w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 z-10" style={{ background: "var(--surface-2)" }}>
              <tr style={{ color: "var(--text-3)", borderBottom: "1px solid var(--border)", fontSize: 10, fontWeight: 600, letterSpacing: "0.05em", textTransform: "uppercase" }}>
                <th className={TH}>Time</th>
                <th className={TH}>Strategy</th>
                <th className={cn(TH, "text-center")}>Dir</th>
                <th className={cn(TH, "text-right")}>Spot</th>
                <th className={cn(TH, "text-right")}>Strike</th>
                <th className={cn(TH, "text-right")}>Entry</th>
                <th className={cn(TH, "text-right")}>SL</th>
                <th className={cn(TH, "text-right")}>Target</th>
                <th className={cn(TH, "text-right")}>R:R</th>
                <th className={cn(TH, "text-center")}>Status</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s: Signal, i: number) => {
                const sc  = STATUS_COLOR[s.status] ?? { fg: "var(--text-3)", bg: "transparent" };
                const dc  = DIR_COLOR[s.direction] ?? { fg: "var(--text-2)", bg: "transparent", border: "transparent" };
                const skipped = s.status === "SKIPPED";
                const rowBg   = i % 2 ? "var(--surface-2)" : "transparent";
                return (
                  <>
                    <tr key={s.id} style={{ borderBottom: skipped && s.skip_reason ? "none" : "1px solid var(--border)", background: rowBg }}>
                      <td className={TD}>
                        <span className="font-mono" style={{ color: "var(--text-3)", fontSize: 10 }}>
                          {s.time
                            ? new Date(s.time).toLocaleTimeString("en-IN", {
                                timeZone: "Asia/Kolkata",
                                hour: "2-digit", minute: "2-digit", second: "2-digit",
                              })
                            : "—"}
                        </span>
                      </td>
                      <td className={TD}>
                        <div style={{ color: "var(--text-1)", fontWeight: 500 }}>{s.strategy.replace(/_/g, " ")}</div>
                        <div style={{ color: "var(--text-4)", fontSize: 10 }}>{s.underlying}</div>
                      </td>
                      <td className={cn(TD, "text-center")}>
                        <span className="tag font-bold" style={{ color: dc.fg, background: dc.bg, border: `1px solid ${dc.border}` }}>
                          {s.direction === "BUY" ? "CE" : "PE"}
                        </span>
                      </td>
                      <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--text-2)" }}>{formatPrice(s.spot)}</td>
                      <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--text-1)" }}>{s.strike ? s.strike.toLocaleString("en-IN") : "—"}</td>
                      <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--text-1)" }}>{formatPrice(s.entry_ref)}</td>
                      <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--red)" }}>{formatPrice(s.sl_ref)}</td>
                      <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--green)" }}>{formatPrice(s.target_ref)}</td>
                      <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--yellow)" }}>{s.rr ? `${s.rr.toFixed(1)}x` : "—"}</td>
                      <td className={cn(TD, "text-center")}>
                        {/* title tooltip shows full reason on hover */}
                        <span
                          className="tag"
                          style={{ color: sc.fg, background: sc.bg, cursor: s.skip_reason ? "help" : "default" }}
                          title={s.skip_reason ?? undefined}
                        >
                          {s.status.replace("_", " ")}
                        </span>
                      </td>
                    </tr>

                    {/* Inline skip reason row — only rendered when reason is present */}
                    {skipped && s.skip_reason && (
                      <tr key={`${s.id}-reason`} style={{ borderBottom: "1px solid var(--border)", background: rowBg }}>
                        <td />
                        <td colSpan={9} style={{ paddingTop: 2, paddingBottom: 6, paddingLeft: 12 }}>
                          <span
                            className="font-mono"
                            style={{ fontSize: 9, color: "var(--text-4)", display: "block", cursor: "help" }}
                            title={s.skip_reason}
                          >
                            ↳ {truncateReason(s.skip_reason)}
                          </span>
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
