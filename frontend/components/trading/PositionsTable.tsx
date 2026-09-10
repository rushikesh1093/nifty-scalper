"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getPositions, closePosition, clearTestTrades } from "@/lib/tradingApi";
import { useMarketStore } from "@/lib/store";
import { formatPrice, cn } from "@/lib/utils";
import toast from "react-hot-toast";
import type { Position } from "@/types/trading";

const EXIT_BADGE: Record<string, { fg: string; bg: string }> = {
  TARGET:  { fg: "var(--green)",  bg: "var(--green-dim)"  },
  SL:      { fg: "var(--red)",    bg: "var(--red-dim)"    },
  EOD:     { fg: "var(--text-3)", bg: "var(--surface-3)"  },
  MANUAL:  { fg: "var(--blue)",   bg: "var(--blue-dim)"   },
  TIMEOUT: { fg: "var(--yellow)", bg: "var(--yellow-dim)" },
};

const TH = "px-3 py-2.5 text-left whitespace-nowrap";
const TD = "px-3 py-2.5 align-middle";

function PnLCell({ pos }: { pos: Position }) {
  const pnl = pos.net_pnl ?? 0;
  if (!pos.is_open) {
    return (
      <span className="font-mono font-semibold" style={{ color: pnl >= 0 ? "var(--green)" : "var(--red)" }}>
        {pnl >= 0 ? "+" : ""}₹{Math.abs(pnl).toLocaleString("en-IN", { maximumFractionDigits: 0 })}
      </span>
    );
  }
  return <span className="font-mono text-[10px]" style={{ color: "var(--text-3)" }}>live</span>;
}

export function PositionsTable({ showClosed = false }: { showClosed?: boolean }) {
  const qc = useQueryClient();
  const { data: positions = [], isLoading } = useQuery({
    queryKey:        ["positions", showClosed],
    queryFn:         () => getPositions({ is_open: showClosed ? false : true, limit: 100 }),
    refetchInterval: 6000,
  });

  const close = useMutation({
    mutationFn: closePosition,
    onSuccess:  () => { qc.invalidateQueries({ queryKey: ["positions"] }); toast.success("Position closed"); },
    onError:    () => toast.error("Failed to close"),
  });

  const clearTrades = useMutation({
    mutationFn: clearTestTrades,
    onSuccess:  () => {
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["pnl"] });
      qc.invalidateQueries({ queryKey: ["signals"] });
      qc.invalidateQueries({ queryKey: ["capital"] });
      toast.success("All test trades cleared");
    },
    onError:    () => toast.error("Failed to clear trades"),
  });

  const display   = positions;
  const totalPnL  = display.reduce((s: number, p: Position) => s + (p.net_pnl || 0), 0);


  if (isLoading) return <div className="rounded-xl h-24 animate-pulse" style={{ background: "var(--surface)", border: "1px solid var(--border)" }} />;

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-2)" }}>
            {showClosed ? "Closed Today" : "Open Positions"}
          </span>
          <span className="font-mono text-[10px] px-1.5 py-0.5 rounded" style={{ color: "var(--text-3)", background: "var(--surface-3)" }}>
            {display.length}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {display.length > 0 && (
            <span className="font-mono text-[11px] font-semibold" style={{ color: totalPnL >= 0 ? "var(--green)" : "var(--red)" }}>
              {totalPnL >= 0 ? "+" : ""}₹{Math.abs(totalPnL).toLocaleString("en-IN", { maximumFractionDigits: 0 })} net
            </span>
          )}
          {showClosed && display.length > 0 && (
            <button
              onClick={() => {
                if (window.confirm("Are you sure you want to clear all test trades?")) {
                  clearTrades.mutate();
                }
              }}
              disabled={clearTrades.isPending}
              className="text-[10px] font-semibold px-2 py-1 rounded transition-all hover:opacity-80 disabled:opacity-50"
              style={{ color: "var(--red)", background: "var(--red-dim)", border: "1px solid rgba(244, 63, 94, 0.2)" }}
              title="Clear all test/paper trades and signals"
            >
              {clearTrades.isPending ? "Clearing..." : "Clear Test Trades"}
            </button>
          )}
        </div>
      </div>


      {display.length === 0 ? (
        <div className="flex items-center justify-center py-8 text-[11px]" style={{ color: "var(--text-4)" }}>
          {showClosed ? "No closed trades today" : "No open positions"}
        </div>
      ) : (
        <div className="overflow-auto" style={{ maxHeight: 240 }}>
          <table className="w-full" style={{ fontSize: 11 }}>
            <thead className="sticky top-0 z-10" style={{ background: "var(--surface-2)" }}>
              <tr style={{ color: "var(--text-3)", borderBottom: "1px solid var(--border)", fontSize: 10, fontWeight: 600, letterSpacing: "0.05em", textTransform: "uppercase" }}>
                <th className={TH}>Strategy</th>
                <th className={TH}>Symbol</th>
                <th className={cn(TH, "text-right")}>Entry</th>
                <th className={cn(TH, "text-right")}>SL</th>
                <th className={cn(TH, "text-right")}>Target</th>
                {showClosed && <th className={cn(TH, "text-right")}>Exit</th>}
                {showClosed && <th className={cn(TH, "text-center")}>Reason</th>}
                <th className={cn(TH, "text-right")}>P&amp;L</th>
                <th className={cn(TH, "text-right")}>Lots</th>
                <th className={TH}>Time</th>
                {!showClosed && <th className={cn(TH, "text-center")}>Close</th>}
              </tr>
            </thead>
            <tbody>
              {display.map((p: Position, i: number) => {
                const eb = EXIT_BADGE[p.exit_reason || ""] ?? { fg: "var(--text-3)", bg: "var(--surface-3)" };
                return (
                  <tr key={p.id} style={{ borderBottom: "1px solid var(--border)", background: i % 2 ? "var(--surface-2)" : "transparent" }}>
                    <td className={TD}>
                      <span style={{ color: "var(--text-1)", fontWeight: 500 }}>{p.strategy?.replace(/_/g, " ")}</span>
                    </td>
                    <td className={TD}>
                      <div className="font-mono" style={{ color: "var(--text-2)", fontSize: 10 }}>{p.symbol}</div>
                      {p.capital_used && <div className="font-mono" style={{ color: "var(--text-4)", fontSize: 9 }}>₹{Math.round(p.capital_used).toLocaleString()}</div>}
                    </td>
                    <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--text-1)" }}>{formatPrice(p.entry)}</td>
                    <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--red)" }}>{formatPrice(p.sl)}</td>
                    <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--green)" }}>{formatPrice(p.target)}</td>
                    {showClosed && <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--text-2)" }}>{formatPrice(p.exit)}</td>}
                    {showClosed && (
                      <td className={cn(TD, "text-center")}>
                        <span className="tag" style={{ color: eb.fg, background: eb.bg }}>{p.exit_reason || "—"}</span>
                      </td>
                    )}
                    <td className={TD}><PnLCell pos={p} /></td>
                    <td className={cn(TD, "text-right font-mono")} style={{ color: "var(--text-3)" }}>{p.lots}</td>
                    <td className={TD}>
                      <span className="font-mono" style={{ color: "var(--text-4)", fontSize: 10 }}>
                        {p.entry_time ? new Date(p.entry_time).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }) : "—"}
                      </span>
                    </td>
                    {!showClosed && (
                      <td className={cn(TD, "text-center")}>
                        <button
                          onClick={() => close.mutate(p.id)}
                          disabled={close.isPending}
                          className="px-2.5 py-1 rounded text-[10px] font-semibold transition-all disabled:opacity-40"
                          style={{ color: "var(--red)", background: "var(--red-dim)", border: "1px solid #f43f5e25" }}
                        >
                          Close
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
