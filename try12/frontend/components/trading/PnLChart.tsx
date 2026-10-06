"use client";
import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, BarChart, Bar, Cell,
} from "recharts";
import { getPnLHistory } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { getChartTheme, type ChartTheme } from "@/lib/chartTheme";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { ChartSkeleton } from "@/components/ui/Skeleton";
import { DeltaText } from "@/components/ui/PriceText";
import type { PnLHistory } from "@/types/trading";

const DAY_OPTIONS = [7, 22, 60, 90] as const;
const AREA_HEIGHT = 150;
const BAR_HEIGHT = 80;

const rupee = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? `₹${Math.round(v).toLocaleString("en-IN")}` : "—");

function Charts({ history, theme, gradientId }: { history: PnLHistory[]; theme: ChartTheme; gradientId: string }) {
  const { recharts: rc, color } = theme;
  const last = history[history.length - 1];
  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between text-table text-text-2">
        <span>Cumulative net P&amp;L · {history.length} trading day{history.length === 1 ? "" : "s"}</span>
        <DeltaText value={last?.cumulative_pnl ?? null} prefix="₹" decimals={0} />
      </div>
      {/* recharts needs a measured parent: explicit pixel height on the wrapper. */}
      <div style={{ height: AREA_HEIGHT }} role="img" aria-label={`Cumulative net P&L over ${history.length} days, ending at ${rupee(last?.cumulative_pnl)}`}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={history} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color("accent")} stopOpacity={0.35} />
                <stop offset="100%" stopColor={color("accent")} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
            <XAxis dataKey="date" stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={rc.axis.axisLine}
              tickFormatter={(v: string) => String(v).slice(5)} minTickGap={16} />
            <YAxis stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={64}
              tickFormatter={(v: number) => rupee(v)} />
            <Tooltip contentStyle={rc.tooltip.contentStyle} labelStyle={rc.tooltip.labelStyle} itemStyle={rc.tooltip.itemStyle}
              cursor={{ stroke: color("text-3", 0.5) }} formatter={(v: number) => [rupee(v), "Cumulative"]} />
            <ReferenceLine y={0} stroke={color("text-3")} strokeDasharray="4 2" />
            <Area type="monotone" dataKey="cumulative_pnl" stroke={color("accent")} fill={`url(#${gradientId})`}
              strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="text-table text-text-2">Daily net P&amp;L</div>
      <div style={{ height: BAR_HEIGHT }} role="img" aria-label={`Daily net P&L bars for ${history.length} days`}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={history} margin={{ top: 0, right: 8, left: 0, bottom: 0 }}>
            <XAxis dataKey="date" stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={rc.axis.axisLine}
              tickFormatter={(v: string) => String(v).slice(5)} minTickGap={16} />
            <YAxis hide />
            <Tooltip contentStyle={rc.tooltip.contentStyle} labelStyle={rc.tooltip.labelStyle} itemStyle={rc.tooltip.itemStyle}
              cursor={rc.tooltip.cursor} formatter={(v: number) => [rupee(v), "Daily net"]} />
            <ReferenceLine y={0} stroke={color("text-3")} />
            <Bar dataKey="net_pnl" radius={[2, 2, 0, 0]} isAnimationActive={false}>
              {history.map((d) => (
                <Cell key={d.date} fill={d.net_pnl >= 0 ? color("up", 0.8) : color("down", 0.8)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function PnLChart() {
  const [days, setDays] = useState<number>(22);
  const selectId = useId();
  const gradientId = `pnl-grad-${useId().replace(/:/g, "")}`;
  // Theme colours are read from CSS variables, which only exist in the browser.
  const [theme, setTheme] = useState<ChartTheme | null>(null);
  useEffect(() => setTheme(getChartTheme()), []);

  const query = useQuery({
    queryKey: queryKeys.pnlHistory(days),
    queryFn: () => getPnLHistory(days),
    refetchInterval: 60_000,
  });

  return (
    <Panel
      title="Equity curve"
      actions={
        <label htmlFor={selectId} className="flex items-center gap-1.5 text-table text-text-2">
          Range
          <select
            id={selectId}
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-border bg-surface-2 px-2 py-1 text-table text-text-1"
          >
            {DAY_OPTIONS.map((d) => <option key={d} value={d}>{d} days</option>)}
          </select>
        </label>
      }
    >
      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d) || d.length === 0}
        emptyTitle="No closed trades yet"
        emptyHint="The equity curve starts after the first closed position."
        skeleton={<ChartSkeleton height={AREA_HEIGHT + BAR_HEIGHT + 40} />}
      >
        {(history) =>
          theme ? <Charts history={history} theme={theme} gradientId={gradientId} /> : <ChartSkeleton height={AREA_HEIGHT + BAR_HEIGHT + 40} />
        }
      </DataSurface>
    </Panel>
  );
}
