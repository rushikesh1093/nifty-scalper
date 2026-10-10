"use client";
import { useId, useState } from "react";
import { useRunBreakdown, useRunSeries, useRunTrades } from "@/hooks/useBacktest";
import { formatIst } from "@/lib/time";
import { Badge } from "@/components/ui/Badge";
import { DataSurface } from "@/components/ui/DataSurface";
import { Panel } from "@/components/ui/Panel";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { DeltaText } from "@/components/ui/PriceText";
import { PerTradeEmpty } from "./ChartWithTable";
import { fmtInt, fmtR } from "./format";
import type { RunDetail, TradeOutcome, TradesQuery } from "@/types/backtest";

// Paginated trade blotter (design §5.4, §6.2), tier 1 only, with strategy / regime / outcome /
// exit-reason filters. Filter options come from the run's own breakdowns and exit-reason buckets,
// so every option names a value the run actually has.

const LIMIT = 50;
const OUTCOMES: TradeOutcome[] = ["WIN", "LOSS", "FLAT"];
const OUTCOME_INTENT = { WIN: "positive", LOSS: "negative", FLAT: "neutral" } as const;

const px = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(2) : "—");

function FilterSelect({
  label, value, options, onChange,
}: { label: string; value: string; options: string[]; onChange: (v: string) => void }) {
  const id = useId();
  return (
    <div className="flex items-center gap-1.5">
      <label htmlFor={id} className="text-micro font-semibold text-text-3">{label}</label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-border-2 bg-surface-2 px-2 py-1 text-table text-text-1">
        <option value="">All</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );
}

export function TradeBlotter({ run }: { run: RunDetail }) {
  const enabled = run.per_trade_available;
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState<{ strategy: string; regime: string; outcome: string; exit_reason: string }>(
    { strategy: "", regime: "", outcome: "", exit_reason: "" },
  );
  const p: TradesQuery = {
    limit: LIMIT, offset,
    strategy: filters.strategy || undefined,
    regime: filters.regime || undefined,
    outcome: (filters.outcome || undefined) as TradeOutcome | undefined,
    exit_reason: filters.exit_reason || undefined,
  };
  const query = useRunTrades(run.run_id, run.dataset, p, enabled);
  const strategies = useRunBreakdown(run.run_id, "strategy", run.dataset, enabled);
  const regimes = useRunBreakdown(run.run_id, "regime", run.dataset, enabled);
  const series = useRunSeries(run.run_id, run.dataset, 0.25, enabled);

  const set = (k: keyof typeof filters) => (v: string) => { setOffset(0); setFilters((f) => ({ ...f, [k]: v })); };
  const total = query.data?.total ?? 0;
  const filtered = Object.values(filters).some(Boolean);

  const toolbar = enabled && (
    <div className="flex flex-wrap items-center gap-3 px-4 pt-3">
      <FilterSelect label="Strategy" value={filters.strategy} onChange={set("strategy")}
        options={Object.keys(strategies.data?.groups ?? {})} />
      <FilterSelect label="Regime" value={filters.regime} onChange={set("regime")}
        options={Object.keys(regimes.data?.groups ?? {})} />
      <FilterSelect label="Outcome" value={filters.outcome} onChange={set("outcome")} options={OUTCOMES} />
      <FilterSelect label="Exit reason" value={filters.exit_reason} onChange={set("exit_reason")}
        options={(series.data?.exit_reasons ?? []).map((b) => b.exit_reason)} />
      {filtered && (
        <button type="button" onClick={() => { setOffset(0); setFilters({ strategy: "", regime: "", outcome: "", exit_reason: "" }); }}
          className="text-micro text-text-3 underline hover:text-text-1">
          clear filters
        </button>
      )}
    </div>
  );

  const pager = enabled && total > LIMIT && (
    <div className="flex items-center gap-2">
      <span>Showing {offset + 1}–{Math.min(offset + LIMIT, total)} of {total}</span>
      <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}
        className="rounded border border-border-2 px-2 py-0.5 text-text-2 hover:bg-surface-3 disabled:opacity-50">
        Previous
      </button>
      <button type="button" disabled={offset + LIMIT >= total} onClick={() => setOffset(offset + LIMIT)}
        className="rounded border border-border-2 px-2 py-0.5 text-text-2 hover:bg-surface-3 disabled:opacity-50">
        Next
      </button>
    </div>
  );

  return (
    <Panel
      title="Trade blotter"
      meta={query.data ? <span className="text-micro text-text-3">{total}</span> : undefined}
      footer={pager || undefined}
      flush={enabled}
    >
      {!enabled ? (
        <PerTradeEmpty dataset={run.dataset} />
      ) : (
        <>
          {toolbar}
          <DataSurface
            query={query}
            isEmpty={(d) => d.items.length === 0}
            emptyTitle={filtered ? "No trades match these filters" : "No closed trades in this run"}
            skeleton={<div className="p-4"><TableSkeleton rows={8} cols={13} /></div>}
            hideFreshness
            className="pb-2 pt-2"
          >
            {(d) => (
              <div className="overflow-x-auto">
                <table className="data-table">
                  <caption className="sr-only">Closed trades of this run, oldest first</caption>
                  <thead>
                    <tr>
                      <th scope="col">Entry</th>
                      <th scope="col">Exit</th>
                      <th scope="col">Underlying</th>
                      <th scope="col">Strategy</th>
                      <th scope="col">Regime</th>
                      <th scope="col">Type</th>
                      <th scope="col" className="num">Strike</th>
                      <th scope="col" className="num">DTE</th>
                      <th scope="col" className="num">Lots</th>
                      <th scope="col" className="num">Entry ₹</th>
                      <th scope="col" className="num">Exit ₹</th>
                      <th scope="col">Exit reason</th>
                      <th scope="col" className="num">R</th>
                      <th scope="col" className="num">Net ₹</th>
                      <th scope="col">Outcome</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.items.map((t) => (
                      <tr key={t.position_id}>
                        <td className="whitespace-nowrap font-mono">{formatIst(t.entry_time, { withDate: true, seconds: false })}</td>
                        <td className="whitespace-nowrap font-mono">{formatIst(t.exit_time, { seconds: false })}</td>
                        <td>{t.underlying}</td>
                        <td className="whitespace-nowrap">{t.strategy_name}</td>
                        <td className="whitespace-nowrap">{t.entry_regime ?? "—"}</td>
                        <td className="font-mono">{t.option_type ?? "—"}</td>
                        <td className="num font-mono">{t.strike ?? "—"}</td>
                        <td className="num font-mono">{t.dte_at_entry ?? "—"}</td>
                        <td className="num font-mono">{fmtInt(t.lots)}</td>
                        <td className="num font-mono">{px(t.actual_entry)}</td>
                        <td className="num font-mono">{px(t.actual_exit)}</td>
                        <td className="font-mono">{t.exit_reason ?? "—"}</td>
                        <td className="num font-mono">{t.r_valid ? fmtR(t.r_multiple, 2) : "—"}</td>
                        <td className="num"><DeltaText value={t.net_pnl} prefix="₹" decimals={2} /></td>
                        <td><Badge intent={OUTCOME_INTENT[t.outcome] ?? "neutral"} status={t.outcome}>{t.outcome}</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </DataSurface>
        </>
      )}
    </Panel>
  );
}
