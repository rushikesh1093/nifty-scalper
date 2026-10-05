"use client";
import { useQuery } from "@tanstack/react-query";
import { getPnLHistory, getDailyStats } from "@/lib/tradingApi";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, BarChart, Bar, Cell,
} from "recharts";
import { cn } from "@/lib/utils";
import type { PnLHistory } from "@/types/trading";

const STAT_ITEMS = (stats: any, pnl: number) => [
  {
    label: "Today P&L",
    val:   `${pnl >= 0 ? "+" : ""}₹${Math.abs(pnl).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`,
    color: pnl > 0 ? "var(--green)" : pnl < 0 ? "var(--red)" : "var(--text-2)",
  },
  { label: "Trades",   val: stats?.total_trades ?? 0,        color: "var(--text-1)" },
  { label: "Win Rate", val: `${stats?.win_rate ?? 0}%`,       color: "var(--yellow)" },
  { label: "Open",     val: stats?.open_positions ?? 0,       color: "var(--blue)" },
];

function StatCard({ label, val, color }: { label: string; val: any; color: string }) {
  return (
    <div
      className="rounded-xl px-3 py-2.5 text-center"
      style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}
    >
      <div className="text-[10px] font-medium uppercase tracking-wider mb-1" style={{ color: "var(--text-3)" }}>
        {label}
      </div>
      <div className="text-lg font-bold font-mono leading-none" style={{ color }}>
        {val}
      </div>
    </div>
  );
}

export function PnLChart() {
  const { data: history = [] } = useQuery({
    queryKey: ["pnl-history"], queryFn: () => getPnLHistory(22), refetchInterval: 60000,
  });
  const { data: stats } = useQuery({
    queryKey: ["daily-stats"], queryFn: getDailyStats, refetchInterval: 8000,
  });

  const pnl        = stats?.total_pnl || 0;
  const hasHistory = history.length > 0;

  const tooltipStyle = {
    background: "var(--surface-2)",
    border: "1px solid var(--border-2)",
    borderRadius: 8,
    fontSize: 11,
    color: "var(--text-1)",
  };

  return (
    <div
      className="rounded-xl p-4 space-y-3"
      style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
    >
      {/* Stat cards */}
      <div className="grid grid-cols-4 gap-2">
        {STAT_ITEMS(stats, pnl).map(s => (
          <StatCard key={s.label} {...s} />
        ))}
      </div>

      {/* Circuit breaker */}
      {stats?.circuit_breaker_triggered && (
        <div
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-[11px] font-medium"
          style={{ background: "var(--red-dim)", border: "1px solid var(--red)30", color: "var(--red)" }}
        >
          <svg className="w-3.5 h-3.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clipRule="evenodd" />
          </svg>
          {stats.circuit_reason}
        </div>
      )}

      {/* Charts */}
      {hasHistory ? (
        <div className="space-y-3">
          {/* Cumulative */}
          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider mb-2" style={{ color: "var(--text-3)" }}>
              Cumulative P&L — {history.length} days
            </div>
            <ResponsiveContainer width="100%" height={120}>
              <AreaChart data={history} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%"   stopColor="#6366f1" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#6366f1" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="date" tick={{ fontSize: 9, fill: "var(--text-3)" }} tickLine={false} axisLine={false}
                  tickFormatter={v => v.slice(5)} />
                <YAxis tick={{ fontSize: 9, fill: "var(--text-3)" }} tickLine={false} axisLine={false}
                  tickFormatter={v => `₹${v}`} width={52} />
                <Tooltip contentStyle={tooltipStyle}
                  formatter={(v: number) => [`₹${v.toFixed(0)}`, "Cum. P&L"]} />
                <ReferenceLine y={0} stroke="var(--text-3)" strokeDasharray="4 2" />
                <Area type="monotone" dataKey="cumulative_pnl"
                  stroke="var(--accent)" fill="url(#pnlGrad)" strokeWidth={1.5} dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>

          {/* Daily bars */}
          <div>
            <div className="text-[10px] font-medium uppercase tracking-wider mb-2" style={{ color: "var(--text-3)" }}>
              Daily net P&L
            </div>
            <ResponsiveContainer width="100%" height={70}>
              <BarChart data={history} margin={{ top: 0, right: 4, left: 0, bottom: 0 }}>
                <XAxis dataKey="date" tick={{ fontSize: 9, fill: "var(--text-3)" }} tickLine={false} axisLine={false}
                  tickFormatter={v => v.slice(5)} />
                <YAxis hide />
                <Tooltip contentStyle={tooltipStyle}
                  formatter={(v: number) => [`₹${v.toFixed(0)}`, "Daily P&L"]} />
                <ReferenceLine y={0} stroke="var(--text-3)" />
                <Bar dataKey="net_pnl" radius={[2, 2, 0, 0]}>
                  {history.map((d: PnLHistory, i: number) => (
                    <Cell key={i}
                      fill={d.net_pnl >= 0 ? "var(--green)" : "var(--red)"}
                      opacity={0.75}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      ) : (
        <div
          className="flex flex-col items-center justify-center py-8 gap-1.5 text-center"
          style={{ color: "var(--text-3)" }}
        >
          <svg className="w-8 h-8 mb-1 opacity-30" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
          </svg>
          <span className="text-[11px]">P&L chart will populate after the first trading day.</span>
          <span className="text-[10px]" style={{ color: "var(--text-4)" }}>Strategies are active and collecting signals.</span>
        </div>
      )}
    </div>
  );
}
