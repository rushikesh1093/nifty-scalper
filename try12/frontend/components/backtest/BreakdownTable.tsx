"use client";
import { useState } from "react";
import { useRunBreakdown } from "@/hooks/useBacktest";
import { cn } from "@/lib/utils";
import { DataSurface } from "@/components/ui/DataSurface";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { SourceMarker } from "./ChartWithTable";
import { LowSampleBadge } from "./HeadlineMetrics";
import { fmtFraction, fmtInr, fmtInt, fmtPct, fmtR } from "./format";
import type { BreakdownDimension, RunDetail } from "@/types/backtest";

// One grouped MetricSet table with a dimension switch (design §5.4, §5.6, §6.2). `day` needs
// per-trade rows, so it is only offered on tier 1 — the artefact tier hides it rather than offering
// a control that 400s. low_sample rows carry a "low sample" badge.

const DIMENSIONS: { key: BreakdownDimension; label: string; perTrade?: boolean }[] = [
  { key: "strategy", label: "Strategy" },
  { key: "regime", label: "Regime" },
  { key: "strategy_regime", label: "Strategy × regime" },
  { key: "underlying", label: "Underlying" },
  { key: "day", label: "Day", perTrade: true },
];

const num = (v: number | null | undefined, d = 2) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(d) : "—");

export function BreakdownTable({ run }: { run: RunDetail }) {
  const [dimension, setDimension] = useState<BreakdownDimension>("strategy");
  const query = useRunBreakdown(run.run_id, dimension, run.dataset, run.breakdowns_available);
  const offered = DIMENSIONS.filter((d) => !d.perTrade || run.per_trade_available);
  const current = offered.find((d) => d.key === dimension) ?? offered[0];

  const switcher = run.breakdowns_available && (
    <div role="group" aria-label="Group by" className="inline-flex flex-wrap overflow-hidden rounded border border-border-2">
      {offered.map((d) => (
        <button key={d.key} type="button" aria-pressed={dimension === d.key} onClick={() => setDimension(d.key)}
          className={cn("px-2 py-0.5 text-micro font-semibold",
            dimension === d.key ? "bg-surface-3 text-text-1" : "text-text-3 hover:text-text-1")}>
          {d.label}
        </button>
      ))}
    </div>
  );

  return (
    <Panel
      title="Breakdown"
      meta={<SourceMarker source={query.data?.source} />}
      actions={switcher || undefined}
      flush={run.breakdowns_available}
    >
      {!run.breakdowns_available ? (
        <EmptyState title="This artefact has no breakdown section" />
      ) : (
        <DataSurface
          query={query}
          isEmpty={(g) => g.total === 0}
          emptyTitle={`No closed trades to group by ${current.label.toLowerCase()}`}
          skeleton={<div className="p-4"><TableSkeleton rows={4} cols={9} /></div>}
          hideFreshness
        >
          {(g) => (
            <div className="overflow-x-auto">
              <table className="data-table">
                <caption className="sr-only">Metrics grouped by {current.label.toLowerCase()}</caption>
                <thead>
                  <tr>
                    <th scope="col">{current.label}</th>
                    <th scope="col" className="num">Trades</th>
                    <th scope="col" className="num">WR</th>
                    <th scope="col" className="num">Exp R</th>
                    <th scope="col" className="num">Exp R net</th>
                    <th scope="col" className="num">Exp ₹</th>
                    <th scope="col" className="num">PF</th>
                    <th scope="col" className="num">Max DD ₹</th>
                    <th scope="col" className="num">Max DD %</th>
                    <th scope="col" className="num">Avg hold</th>
                    <th scope="col">Sample</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(g.groups).map(([name, m]) => (
                    <tr key={name}>
                      <td className="whitespace-nowrap font-mono text-text-1">{name}</td>
                      <td className="num font-mono">{fmtInt(m.trades)}</td>
                      <td className="num font-mono">{fmtFraction(m.win_rate)}</td>
                      <td className="num font-mono">{fmtR(m.expectancy_r)}</td>
                      <td className="num font-mono">{fmtR(m.expectancy_r_net)}</td>
                      <td className="num font-mono">{fmtInr(m.expectancy_inr, 2)}</td>
                      <td className="num font-mono">{num(m.profit_factor)}</td>
                      <td className="num font-mono">{fmtInr(m.max_equity_drawdown_inr)}</td>
                      <td className="num font-mono">{fmtPct(m.max_equity_drawdown_pct)}</td>
                      <td className="num font-mono">{typeof m.avg_hold_minutes === "number" ? `${m.avg_hold_minutes.toFixed(1)} min` : "—"}</td>
                      <td>{m.low_sample ? <LowSampleBadge /> : <span className="text-text-3">ok</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </DataSurface>
      )}
    </Panel>
  );
}
