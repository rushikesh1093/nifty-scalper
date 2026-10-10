"use client";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useRunSeries } from "@/hooks/useBacktest";
import { cn } from "@/lib/utils";
import { DataSurface } from "@/components/ui/DataSurface";
import { Panel } from "@/components/ui/Panel";
import { ChartSkeleton } from "@/components/ui/Skeleton";
import { ChartWithTable, PerTradeEmpty } from "./ChartWithTable";
import { fmtInt } from "./format";
import type { BinWidth, RHistogram as RHistogramData, RunDetail } from "@/types/backtest";

// R-multiple histogram (design §5.4, §6.2). Bins are signed in their labels (never colour alone):
// bars left of 0 in --down, right of 0 in --up; values beyond ±3R are not binned but counted, and
// those clipped counts are drawn as labelled edge bars and printed beside the chart with r_unavailable.

const HEIGHT = 200;
const WIDTHS: BinWidth[] = [0.1, 0.25, 0.5];

const signed = (v: number) => (v > 0 ? `+${v}` : v < 0 ? `−${Math.abs(v)}` : "0");

interface Bin {
  label:   string;
  range:   string;
  count:   number;
  side:    "down" | "up";
  clipped: boolean;
}

function toBins(h: RHistogramData): Bin[] {
  const bins: Bin[] = h.bins.map((b) => ({
    label: signed(b.lo),
    range: `${signed(b.lo)}R to ${signed(b.hi)}R`,
    count: b.count,
    side: b.lo < 0 ? "down" : "up",
    clipped: false,
  }));
  if (h.clipped_low > 0) bins.unshift({ label: "< −3", range: "below −3R (clipped)", count: h.clipped_low, side: "down", clipped: true });
  if (h.clipped_high > 0) bins.push({ label: "> +3", range: "above +3R (clipped)", count: h.clipped_high, side: "up", clipped: true });
  return bins;
}

export function RHistogram({ run }: { run: RunDetail }) {
  const [binWidth, setBinWidth] = useState<BinWidth>(0.25);
  const query = useRunSeries(run.run_id, run.dataset, binWidth, run.per_trade_available);

  const widthSwitch = run.per_trade_available && (
    <div role="group" aria-label="Bin width" className="inline-flex overflow-hidden rounded border border-border-2">
      {WIDTHS.map((w) => (
        <button key={w} type="button" aria-pressed={binWidth === w} onClick={() => setBinWidth(w)}
          className={cn("px-2 py-0.5 text-micro font-semibold",
            binWidth === w ? "bg-surface-3 text-text-1" : "text-text-3 hover:text-text-1")}>
          {w}R
        </button>
      ))}
    </div>
  );

  return (
    <Panel title="R-multiple distribution" actions={widthSwitch || undefined}>
      {!run.per_trade_available ? (
        <PerTradeEmpty dataset={run.dataset} />
      ) : (
        <DataSurface
          query={query}
          isEmpty={(s) => s.r_histogram.r_valid === 0}
          emptyTitle="No trade has a valid R multiple"
          emptyHint="R needs a recorded initial risk; trades without one are counted as R-unavailable."
          skeleton={<ChartSkeleton height={HEIGHT + 40} />}
          hideFreshness
        >
          {(s) => {
            const h = s.r_histogram;
            const bins = toBins(h);
            const label = `Histogram of ${h.r_valid} R multiples in ${h.bin_width}R bins; `
              + `${h.clipped_low} below −3R, ${h.clipped_high} above +3R, ${h.r_unavailable} without a valid R`;
            return (
              <div className="space-y-2">
                <dl className="flex flex-wrap gap-x-4 gap-y-1 text-table text-text-2">
                  <div><dt className="inline">R valid: </dt><dd className="inline font-mono text-text-1">{fmtInt(h.r_valid)}</dd></div>
                  <div><dt className="inline">R unavailable: </dt><dd className="inline font-mono text-text-1">{fmtInt(h.r_unavailable)}</dd></div>
                  <div><dt className="inline">clipped below −3R: </dt><dd className="inline font-mono text-text-1">{fmtInt(h.clipped_low)}</dd></div>
                  <div><dt className="inline">clipped above +3R: </dt><dd className="inline font-mono text-text-1">{fmtInt(h.clipped_high)}</dd></div>
                </dl>
                <ChartWithTable
                  label={label}
                  height={HEIGHT}
                  chart={({ recharts: rc, color }) => (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={bins} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                        <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
                        <XAxis dataKey="label" stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false}
                          axisLine={rc.axis.axisLine} interval="preserveStartEnd" minTickGap={8} />
                        <YAxis stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={32} allowDecimals={false} />
                        <Tooltip contentStyle={rc.tooltip.contentStyle} labelStyle={rc.tooltip.labelStyle} itemStyle={rc.tooltip.itemStyle}
                          cursor={rc.tooltip.cursor}
                          labelFormatter={(_, p) => (p?.[0]?.payload as Bin | undefined)?.range ?? ""}
                          formatter={(v: number) => [v, "Trades"]} />
                        <ReferenceLine x="0" stroke={color("text-3")} strokeDasharray="4 2" />
                        <Bar dataKey="count" isAnimationActive={false} radius={[2, 2, 0, 0]}>
                          {bins.map((b) => (
                            <Cell key={b.range} fill={color(b.side, b.clipped ? 0.45 : 0.8)}
                              stroke={b.clipped ? color(b.side) : undefined} strokeDasharray={b.clipped ? "3 2" : undefined} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  )}
                  table={
                    <table className="data-table">
                      <caption className="sr-only">R-multiple bins with trade counts</caption>
                      <thead>
                        <tr><th scope="col">Bin</th><th scope="col" className="num">Trades</th></tr>
                      </thead>
                      <tbody>
                        {bins.map((b) => (
                          <tr key={b.range}>
                            <td className="font-mono">{b.range}</td>
                            <td className="num font-mono">{b.count}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  }
                />
              </div>
            );
          }}
        </DataSurface>
      )}
    </Panel>
  );
}
