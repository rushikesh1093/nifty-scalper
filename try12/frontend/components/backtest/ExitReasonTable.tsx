"use client";
import { useRunSeries } from "@/hooks/useBacktest";
import { DataSurface } from "@/components/ui/DataSurface";
import { Panel } from "@/components/ui/Panel";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { DeltaText } from "@/components/ui/PriceText";
import { PerTradeEmpty } from "./ChartWithTable";
import { fmtInt, fmtR } from "./format";
import type { RunDetail } from "@/types/backtest";

// Exit-reason breakdown (design §5.4, §6.2), tier 1 only: reason · count · share · net ₹ · avg R ·
// wins / losses, from /series exit_reasons (shared with the equity panel's query).

export function ExitReasonTable({ run }: { run: RunDetail }) {
  const query = useRunSeries(run.run_id, run.dataset, 0.25, run.per_trade_available);
  return (
    <Panel title="Exit reasons" flush={run.per_trade_available}>
      {!run.per_trade_available ? (
        <PerTradeEmpty dataset={run.dataset} />
      ) : (
        <DataSurface
          query={query}
          isEmpty={(s) => s.exit_reasons.length === 0}
          emptyTitle="No closed trades in this run"
          skeleton={<div className="p-4"><TableSkeleton rows={3} cols={6} /></div>}
          hideFreshness
        >
          {(s) => (
            <div className="overflow-x-auto">
              <table className="data-table">
                <caption className="sr-only">Closed trades grouped by exit reason</caption>
                <thead>
                  <tr>
                    <th scope="col">Exit reason</th>
                    <th scope="col" className="num">Trades</th>
                    <th scope="col" className="num">Share</th>
                    <th scope="col" className="num">Net ₹</th>
                    <th scope="col" className="num">Avg R</th>
                    <th scope="col" className="num">Wins / losses</th>
                  </tr>
                </thead>
                <tbody>
                  {s.exit_reasons.map((b) => (
                    <tr key={b.exit_reason}>
                      <td className="font-mono text-text-1">{b.exit_reason}</td>
                      <td className="num font-mono">{fmtInt(b.count)}</td>
                      <td className="num font-mono">{s.trades ? `${((b.count / s.trades) * 100).toFixed(1)} %` : "—"}</td>
                      <td className="num"><DeltaText value={b.net_inr} prefix="₹" decimals={0} /></td>
                      <td className="num font-mono">{fmtR(b.avg_r, 2)}</td>
                      <td className="num font-mono">{fmtInt(b.wins)} / {fmtInt(b.losses)}</td>
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
