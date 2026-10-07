"use client";
import { useRegimePerformance, type Underlying } from "@/hooks/useRegime";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { DeltaText } from "@/components/ui/PriceText";
import { cn } from "@/lib/utils";
import { regimeLabel, regimeTone } from "@/components/regime/regimeMeta";

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function RegimePerformance({ underlying }: { underlying: Underlying }) {
  const query = useRegimePerformance(underlying);
  return (
    <Panel title="Performance by regime" meta={<Badge intent="neutral">{underlying}</Badge>} bodyClassName="p-3">
      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d) || d.length === 0}
        emptyTitle="No closed trades attributed to a regime yet"
        emptyHint="Rows appear once positions close with a recorded regime."
        skeleton={<TableSkeleton rows={4} cols={6} />}
      >
        {(rows) => (
          <div className="-mx-3 -mt-3 max-h-[360px] overflow-auto" data-testid="regime-performance">
            <table className="data-table">
              <caption className="sr-only">Closed-trade performance per regime and strategy for {underlying}</caption>
              <thead>
                <tr>
                  <th scope="col">Regime</th>
                  <th scope="col">Strategy</th>
                  <th scope="col" className="num">Trades</th>
                  <th scope="col" className="num">Win rate</th>
                  <th scope="col" className="num">Total P&amp;L</th>
                  <th scope="col" className="num">Avg P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.regime}|${r.underlying}|${r.strategy_name}`}>
                    <td className={cn("font-medium", regimeTone(r.regime))}>{regimeLabel(r.regime)}</td>
                    <td className="text-text-1">{r.strategy_name.replace(/_/g, " ")}</td>
                    <td className="num font-mono">{num(r.total_trades) ?? "—"}</td>
                    <td className="num font-mono">{num(r.win_rate) == null ? "—" : `${(r.win_rate as number).toFixed(1)}%`}</td>
                    <td className="num"><DeltaText value={num(r.total_pnl)} prefix="₹" /></td>
                    <td className="num"><DeltaText value={num(r.avg_pnl)} prefix="₹" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataSurface>
    </Panel>
  );
}
