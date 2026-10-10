"use client";
import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { ChartTheme } from "@/lib/chartTheme";
import { useRunSeries } from "@/hooks/useBacktest";
import { formatIst } from "@/lib/time";
import { DataSurface } from "@/components/ui/DataSurface";
import { Panel } from "@/components/ui/Panel";
import { ChartSkeleton } from "@/components/ui/Skeleton";
import { ChartWithTable, PerTradeEmpty } from "./ChartWithTable";
import { fmtInr, fmtPct, fmtR } from "./format";
import type { EquityPoint, RunDetail } from "@/types/backtest";

// Equity + drawdown pair (design §5.4, §6.2): an AreaChart of cum_net_inr with a zero line and,
// directly beneath with the same x configuration, an inverted AreaChart of drawdown_inr in --down —
// the PnLChart two-chart pattern, since recharts cannot share an axis across containers.

const EQ_HEIGHT = 180;
const DD_HEIGHT = 90;
const GAP = 8;

const rupee = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? `₹${Math.round(v).toLocaleString("en-IN")}` : "—");

function Charts({ points, theme }: { points: EquityPoint[]; theme: ChartTheme }) {
  const { recharts: rc, color } = theme;
  const tooltip = { contentStyle: rc.tooltip.contentStyle, labelStyle: rc.tooltip.labelStyle, itemStyle: rc.tooltip.itemStyle };
  const xAxis = (
    <XAxis dataKey="i" type="number" domain={["dataMin", "dataMax"]} stroke={rc.axis.stroke} tick={rc.axis.tick}
      tickLine={false} axisLine={rc.axis.axisLine} allowDecimals={false} minTickGap={16} />
  );
  return (
    <div className="flex h-full flex-col" style={{ gap: GAP }}>
      <div style={{ height: EQ_HEIGHT }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={points} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
            {xAxis}
            <YAxis stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={72}
              tickFormatter={(v: number) => rupee(v)} />
            <Tooltip {...tooltip} cursor={{ stroke: color("text-3", 0.5) }}
              labelFormatter={(i) => `Trade ${i}`} formatter={(v: number) => [rupee(v), "Cumulative net"]} />
            <ReferenceLine y={0} stroke={color("text-3")} strokeDasharray="4 2" />
            <Area type="stepAfter" dataKey="cum_net_inr" stroke={color("accent")} fill={color("accent", 0.15)}
              strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <div style={{ height: DD_HEIGHT }}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={points} margin={{ top: 0, right: 8, left: 0, bottom: 0 }}>
            {xAxis}
            {/* inverted: 0 at the top, deeper drawdown further down */}
            <YAxis reversed stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={72}
              tickFormatter={(v: number) => (v === 0 ? "₹0" : `−${rupee(v)}`)} />
            <Tooltip {...tooltip} cursor={{ stroke: color("text-3", 0.5) }}
              labelFormatter={(i) => `Trade ${i}`} formatter={(v: number) => [`−${rupee(v)}`, "Drawdown"]} />
            <Area type="stepAfter" dataKey="drawdown_inr" stroke={color("down")} fill={color("down", 0.25)}
              strokeWidth={1} dot={false} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function EquityTable({ points }: { points: EquityPoint[] }) {
  return (
    <table className="data-table">
      <caption className="sr-only">Equity and drawdown after each closed trade</caption>
      <thead>
        <tr>
          <th scope="col" className="num">#</th>
          <th scope="col">Exit time</th>
          <th scope="col">Exit reason</th>
          <th scope="col" className="num">R</th>
          <th scope="col" className="num">Net ₹</th>
          <th scope="col" className="num">Cumulative ₹</th>
          <th scope="col" className="num">Cumulative %</th>
          <th scope="col" className="num">Drawdown ₹</th>
          <th scope="col" className="num">Drawdown %</th>
        </tr>
      </thead>
      <tbody>
        {points.map((p) => (
          <tr key={p.i}>
            <td className="num font-mono">{p.i}</td>
            <td className="whitespace-nowrap font-mono">{p.exit_time ? formatIst(p.exit_time, { withDate: true, seconds: false }) : p.trading_day ?? "—"}</td>
            <td>{p.exit_reason ?? "—"}</td>
            <td className="num font-mono">{fmtR(p.r_multiple, 2)}</td>
            <td className="num font-mono">{fmtInr(p.net_pnl, 2)}</td>
            <td className="num font-mono">{fmtInr(p.cum_net_inr, 2)}</td>
            <td className="num font-mono">{fmtPct(p.cum_net_pct)}</td>
            <td className="num font-mono">{fmtInr(p.drawdown_inr, 2)}</td>
            <td className="num font-mono">{fmtPct(p.drawdown_pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function EquityCurve({ run }: { run: RunDetail }) {
  // pass enabled = per_trade_available so the artefact tier never issues the 409 call
  const query = useRunSeries(run.run_id, run.dataset, 0.25, run.per_trade_available);
  return (
    <Panel title="Equity and drawdown">
      {!run.per_trade_available ? (
        <PerTradeEmpty dataset={run.dataset} />
      ) : (
        <DataSurface
          query={query}
          isEmpty={(s) => s.equity.length === 0}
          emptyTitle="No closed trades in this run"
          emptyHint="The equity curve starts after the first closed trade."
          skeleton={<ChartSkeleton height={EQ_HEIGHT + DD_HEIGHT + GAP + 32} />}
          hideFreshness
        >
          {(s) => {
            const last = s.equity[s.equity.length - 1];
            const label = `Cumulative net P&L over ${s.trades} trades, ending at ${rupee(last?.cum_net_inr)}; `
              + `maximum drawdown ${rupee(s.max_drawdown_inr)} (${fmtPct(s.max_drawdown_pct)} of capital)`;
            return (
              <div className="space-y-2">
                <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-table text-text-2">
                  <span>{s.trades} closed trade{s.trades === 1 ? "" : "s"}</span>
                  <span>ending <span className="font-mono text-text-1">{fmtInr(last?.cum_net_inr, 2)}</span></span>
                  <span>max drawdown <span className="font-mono text-down">{fmtInr(s.max_drawdown_inr, 2)}</span> ({fmtPct(s.max_drawdown_pct)})</span>
                  {s.downsampled && <span className="text-yellow">downsampled to {s.equity.length} points (first, last, peak and trough kept)</span>}
                </div>
                <ChartWithTable
                  label={label}
                  height={EQ_HEIGHT + DD_HEIGHT + GAP}
                  chart={(theme) => <Charts points={s.equity} theme={theme} />}
                  table={<EquityTable points={s.equity} />}
                />
              </div>
            );
          }}
        </DataSurface>
      )}
    </Panel>
  );
}
