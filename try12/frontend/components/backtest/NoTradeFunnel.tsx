"use client";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useRunNoTrade } from "@/hooks/useBacktest";
import { DataSurface } from "@/components/ui/DataSurface";
import { Panel } from "@/components/ui/Panel";
import { ChartSkeleton } from "@/components/ui/Skeleton";
import { ChartWithTable, SourceMarker } from "./ChartWithTable";
import { fmtInt } from "./format";
import type { RunDetail } from "@/types/backtest";

// No-trade funnel (design §5.4, §6.2): a horizontal BarChart of decisions per stage (with its table
// equivalent behind the toggle), then the full (stage, code, count) table sorted by count. Served on
// both tiers; the artefact tier reads sections.8.decisions.

export function NoTradeFunnel({ run, promoted }: { run: RunDetail; promoted?: boolean }) {
  const query = useRunNoTrade(run.run_id, run.dataset);
  return (
    <Panel
      title={promoted ? "Why nothing traded — no-trade decisions" : "No-trade decisions"}
      meta={<SourceMarker source={query.data?.source} />}
    >
      <DataSurface
        query={query}
        isEmpty={(s) => s.total === 0}
        emptyTitle="No no-trade decisions recorded for this run"
        skeleton={<ChartSkeleton height={180} />}
        hideFreshness
      >
        {(s) => {
          const total = s.items.reduce((n, x) => n + x.count, 0);
          const height = Math.max(80, s.by_stage.length * 28 + 24);
          const label = `${fmtInt(total)} no-trade decisions across ${s.items.length} codes; by stage: `
            + s.by_stage.map((x) => `${x.stage} ${x.count}`).join(", ");
          return (
            <div className="space-y-3">
              <div className="text-table text-text-2">
                <span className="font-mono text-text-1">{fmtInt(total)}</span> decisions across{" "}
                <span className="font-mono text-text-1">{s.items.length}</span> codes
              </div>
              <ChartWithTable
                label={label}
                height={height}
                chart={({ recharts: rc, color }) => (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={s.by_stage} layout="vertical" margin={{ top: 0, right: 16, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} horizontal={false} />
                      <XAxis type="number" stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={rc.axis.axisLine} allowDecimals={false} />
                      <YAxis type="category" dataKey="stage" stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={140} />
                      <Tooltip contentStyle={rc.tooltip.contentStyle} labelStyle={rc.tooltip.labelStyle} itemStyle={rc.tooltip.itemStyle}
                        cursor={rc.tooltip.cursor} formatter={(v: number) => [fmtInt(v), "Decisions"]} />
                      <Bar dataKey="count" fill={color("accent", 0.8)} isAnimationActive={false} radius={[0, 2, 2, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
                table={
                  <table className="data-table">
                    <caption className="sr-only">No-trade decisions per stage</caption>
                    <thead><tr><th scope="col">Stage</th><th scope="col" className="num">Decisions</th></tr></thead>
                    <tbody>
                      {s.by_stage.map((x) => (
                        <tr key={x.stage}><td className="font-mono">{x.stage}</td><td className="num font-mono">{fmtInt(x.count)}</td></tr>
                      ))}
                    </tbody>
                  </table>
                }
              />
              <div className="max-h-[360px] overflow-auto">
                <table className="data-table">
                  <caption className="sr-only">Every no-trade decision code with its count, largest first</caption>
                  <thead>
                    <tr>
                      <th scope="col">Stage</th>
                      <th scope="col">Code</th>
                      <th scope="col" className="num">Count</th>
                      <th scope="col" className="num">Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.items.map((x) => (
                      <tr key={`${x.stage}:${x.code}`}>
                        <td className="font-mono text-text-2">{x.stage}</td>
                        <td className="font-mono text-text-1">{x.code}</td>
                        <td className="num font-mono">{fmtInt(x.count)}</td>
                        <td className="num font-mono">{total ? `${((x.count / total) * 100).toFixed(1)} %` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        }}
      </DataSurface>
    </Panel>
  );
}
