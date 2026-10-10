"use client";
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { getChartTheme, type ChartTheme } from "@/lib/chartTheme";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import type { DataSource } from "@/types/backtest";

// Shared chart wrapper (design §5.4): a two-button role="group" toggle (aria-pressed) between the
// chart and its data table, so every chart has a text alternative in the same place. The chart
// wrapper carries an explicit pixel height, role="img" and an aria-label with the headline numbers.

/** Theme colours are read from CSS variables, which only exist in the browser. */
export function useChartTheme(): ChartTheme | null {
  const [theme, setTheme] = useState<ChartTheme | null>(null);
  useEffect(() => setTheme(getChartTheme()), []);
  return theme;
}

/** The small "source: artifact" marker artefact-tier panels carry (§5.6). */
export function SourceMarker({ source }: { source: DataSource | "artifact" | "store" | null | undefined }) {
  if (source !== "artifact") return null;
  return (
    <Badge intent="neutral" strength="outline" caps={false} className="text-micro"
      title="served from the run's report artefact, not a replay store">
      source: artifact
    </Badge>
  );
}

/** The explicit empty state of the four per-trade panels on the artefact tier (§5.6) — never an
 *  empty chart, and the query behind it is never issued. */
export function PerTradeEmpty({ dataset }: { dataset: string }) {
  return (
    <EmptyState
      title="Per-trade rows are not stored in the report artefact"
      hint={`This run is read from the '${dataset}' tier, which keeps only the report. Per-trade rows live in a replay store (replay-db or a synthetic-* dataset) — re-run the backtest there to see them.`}
    />
  );
}

export function ChartWithTable({
  label, height, chart, table, className,
}: {
  /** aria-label of the chart, carrying the headline numbers. */
  label:  string;
  /** Explicit pixel height of the chart area (recharts needs a measured parent). */
  height: number;
  /** Render prop so the chart only mounts once the theme is known. */
  chart:  (theme: ChartTheme) => React.ReactNode;
  table:  React.ReactNode;
  className?: string;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");
  const theme = useChartTheme();
  const btn = (v: "chart" | "table", text: string) => (
    <button
      type="button"
      aria-pressed={view === v}
      onClick={() => setView(v)}
      className={cn(
        "px-2 py-0.5 text-micro font-semibold",
        view === v ? "bg-surface-3 text-text-1" : "text-text-3 hover:text-text-1",
      )}
    >
      {text}
    </button>
  );
  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex justify-end">
        <div role="group" aria-label="Show as chart or table"
          className="inline-flex overflow-hidden rounded border border-border-2">
          {btn("chart", "Chart")}
          {btn("table", "Table")}
        </div>
      </div>
      {view === "chart" ? (
        <div style={{ height }} role="img" aria-label={label}>
          {theme ? chart(theme) : <div aria-hidden="true" className="shimmer h-full w-full rounded-lg opacity-60" />}
        </div>
      ) : (
        <div className="max-h-[420px] overflow-auto">{table}</div>
      )}
    </div>
  );
}
