"use client";
import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, CartesianGrid, Legend } from "recharts";
import { useMarketStore } from "@/lib/store";
import { getChartTheme } from "@/lib/chartTheme";
import { formatNumber } from "@/lib/utils";
import { useOptionChain } from "@/hooks/useOptionChain";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { ChartSkeleton } from "@/components/ui/Skeleton";
import { atmStrikeFor } from "@/components/market/OptionChainTable";
import type { OptionStrike } from "@/types/market";

const HEIGHT = 200;

/** Live OI per token. useShallow keeps the snapshot stable until an OI value actually changes (E11). */
function useTokenOIs(tokens: string[]): Record<string, number | undefined> {
  return useMarketStore(
    useShallow((s) => {
      const out: Record<string, number | undefined> = {};
      for (const t of tokens) out[t] = s.ticks[t]?.oi;
      return out;
    }),
  );
}

function OIBars({ strikes, atm, maxPain }: { strikes: OptionStrike[]; atm: number; maxPain: number | null }) {
  const tokens  = useMemo(() => strikes.map((s) => s.token), [strikes]);
  const tickOIs = useTokenOIs(tokens);
  const theme   = useMemo(() => getChartTheme(), []);

  const data = useMemo(() => {
    const map: Record<number, { strike: number; ce_oi: number; pe_oi: number }> = {};
    for (const s of strikes) {
      if (!map[s.strike]) map[s.strike] = { strike: s.strike, ce_oi: 0, pe_oi: 0 };
      const oi = tickOIs[s.token] ?? s.oi ?? 0;
      if (s.option_type === "CE") map[s.strike].ce_oi = oi;
      else                        map[s.strike].pe_oi = oi;
    }
    return Object.values(map).sort((a, b) => a.strike - b.strike);
  }, [strikes, tickOIs]);

  const r = theme.recharts;
  const hasAtm = atm > 0 && data.some((d) => d.strike === atm);
  const hasMaxPain = maxPain != null && data.some((d) => d.strike === maxPain);
  const totalCe = data.reduce((s, d) => s + d.ce_oi, 0);
  const totalPe = data.reduce((s, d) => s + d.pe_oi, 0);

  return (
    <figure data-testid="oi-chart" data-strikes={data.length}>
      <div style={{ height: HEIGHT }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke={r.grid.stroke} strokeDasharray={r.grid.strokeDasharray} />
            <XAxis
              dataKey="strike" stroke={r.axis.stroke} tick={r.axis.tick} tickLine={false} axisLine={r.axis.axisLine}
              tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 2 : 0)}K` : String(v))}
            />
            <YAxis stroke={r.axis.stroke} tick={r.axis.tick} tickLine={false} axisLine={false} tickFormatter={(v: number) => formatNumber(v)} width={44} />
            <Tooltip
              contentStyle={r.tooltip.contentStyle}
              labelStyle={r.tooltip.labelStyle}
              itemStyle={r.tooltip.itemStyle}
              cursor={r.tooltip.cursor}
              labelFormatter={(v) => `Strike ${Number(v).toLocaleString("en-IN")}`}
              formatter={(v: number, name: string) => [formatNumber(v), name]}
            />
            <Legend wrapperStyle={{ fontSize: 11, color: theme.color("text-2") }} />
            {hasAtm && (
              <ReferenceLine x={atm} stroke={theme.color("yellow")} strokeDasharray="4 2"
                label={{ value: "ATM", position: "top", fill: theme.color("yellow"), fontSize: 10 }} />
            )}
            {hasMaxPain && maxPain !== atm && (
              <ReferenceLine x={maxPain} stroke={theme.color("blue")} strokeDasharray="2 2"
                label={{ value: "Max pain", position: "top", fill: theme.color("blue"), fontSize: 10 }} />
            )}
            <Bar dataKey="ce_oi" name="CE OI" fill={theme.color("green", 0.75)} radius={[2, 2, 0, 0]} isAnimationActive={false} />
            <Bar dataKey="pe_oi" name="PE OI" fill={theme.color("red", 0.75)} radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-1 text-micro text-text-3">
        Open interest by strike — CE total {formatNumber(totalCe)}, PE total {formatNumber(totalPe)}
        {hasAtm ? `, ATM ${atm.toLocaleString("en-IN")}` : ""}
        {hasMaxPain ? `, max pain ${maxPain?.toLocaleString("en-IN")}` : ""}.
      </figcaption>
    </figure>
  );
}

export function OIBarChart() {
  const underlying = useMarketStore((s) => s.selectedUnderlying);
  const { query } = useOptionChain();
  return (
    <Panel title="OI distribution" dense>
      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d.strikes) || d.strikes.length === 0}
        emptyTitle="No strikes to chart"
        skeleton={<ChartSkeleton height={HEIGHT} />}
        hideFreshness
      >
        {(chain) => (
          <OIBars
            strikes={chain.strikes}
            atm={atmStrikeFor(underlying, chain.underlying_ltp)}
            maxPain={typeof chain.max_pain === "number" ? chain.max_pain : null}
          />
        )}
      </DataSurface>
    </Panel>
  );
}
