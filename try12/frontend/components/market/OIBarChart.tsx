"use client";
import { useMemo, useRef } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";
import { useMarketStore } from "@/lib/store";
import { formatNumber } from "@/lib/utils";
import type { OptionStrike } from "@/types/market";

/**
 * Custom Zustand selector that returns a stable OI map for the given tokens.
 * Only triggers a re-render when an OI value actually changes — not on every tick.
 */
function useTokenOIs(tokens: string[]): Record<string, number | undefined> {
  const prevRef = useRef<Record<string, number | undefined>>({});

  return useMarketStore((s) => {
    const next: Record<string, number | undefined> = {};
    let changed = false;
    for (const t of tokens) {
      const oi = s.ticks[t]?.oi;
      next[t] = oi;
      if (oi !== prevRef.current[t]) changed = true;
    }
    if (!changed) return prevRef.current;   // same reference → no re-render
    prevRef.current = next;
    return next;
  });
}

export function OIBarChart({ strikes, atm }: { strikes: OptionStrike[]; atm: number }) {
  const tokens  = useMemo(() => strikes.map((s) => s.token), [strikes]);
  const tickOIs = useTokenOIs(tokens);

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

  const tooltipStyle = {
    background: "var(--surface-2)", border: "1px solid var(--border-2)",
    borderRadius: 8, fontSize: 11, color: "var(--text-1)",
  };

  return (
    <div className="rounded-xl p-4" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      <h3 className="text-[10px] font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-3)" }}>
        OI Distribution
      </h3>
      <ResponsiveContainer width="100%" height={180}>
        <BarChart data={data} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
          <XAxis dataKey="strike" tick={{ fontSize: 9, fill: "var(--text-3)" }} tickLine={false} axisLine={false}
            tickFormatter={(v) => v >= 1000 ? `${v / 1000}K` : v} />
          <YAxis tick={{ fontSize: 9, fill: "var(--text-3)" }} tickLine={false} axisLine={false} tickFormatter={formatNumber} />
          <Tooltip contentStyle={tooltipStyle} labelStyle={{ color: "var(--text-1)", fontWeight: "bold" }}
            formatter={(v: number, name: string) => [formatNumber(v), name === "ce_oi" ? "CE OI" : "PE OI"]} />
          <ReferenceLine x={atm} stroke="var(--yellow)" strokeDasharray="4 2" />
          <Bar dataKey="ce_oi" name="CE OI" fill="var(--green)" radius={[2, 2, 0, 0]} opacity={0.7} />
          <Bar dataKey="pe_oi" name="PE OI" fill="var(--red)"   radius={[2, 2, 0, 0]} opacity={0.7} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
