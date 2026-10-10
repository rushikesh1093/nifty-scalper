"use client";
import type { UseQueryResult } from "@tanstack/react-query";
import { Badge } from "@/components/ui/Badge";
import { DataSurface } from "@/components/ui/DataSurface";
import { EmptyState } from "@/components/ui/EmptyState";
import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/Skeleton";
import { StatTile, type StatIntent } from "@/components/ui/StatTile";
import { SourceMarker } from "./ChartWithTable";
import { fmtFraction, fmtInr, fmtInt, fmtPct, fmtR } from "./format";
import type { Interval, RunDetail, RunMetrics } from "@/types/backtest";

// Headline metrics (design §5.4, §6.2). Null is "—" everywhere (never 0 / 0 %); the mean-cost tiles
// are "—" on the artefact tier because only per-trade rows can produce them. low_sample puts a
// badge on the panel and the Wilson interval renders at full width.

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const num = (v: number | null | undefined, d = 2) => (isNum(v) ? v.toFixed(d) : "—");
const sign = (v: number | null | undefined): StatIntent => (!isNum(v) || v === 0 ? "neutral" : v > 0 ? "positive" : "negative");

function interval(iv: Interval | null | undefined, f: (v: number | null) => string): string | null {
  if (!iv || (iv[0] === null && iv[1] === null)) return null;
  return `[${f(iv[0])}, ${f(iv[1])}]`;
}

export function LowSampleBadge() {
  return (
    <Badge intent="warning" caps={false} status="LOW_SAMPLE" title="fewer than 20 closed trades — ratios are not reliable">
      low sample (n &lt; 20)
    </Badge>
  );
}

function Tiles({ m }: { m: RunMetrics }) {
  const s = m.metrics;
  const artifact = m.source === "artifact";
  const costHint = artifact ? "not in the report artefact" : m.cost_sample_inr ? `over ${m.cost_sample_inr} trades` : undefined;
  const wilson = interval(m.win_rate_wilson_95, (v) => fmtFraction(v));
  const ciR = interval(m.expectancy_r_ci_day_block, (v) => fmtR(v, 4));
  const ciInr = interval(m.expectancy_inr_ci_day_block, (v) => fmtInr(v, 2));
  const ciNote = m.ci_source === "none" ? "CI unavailable" : undefined;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-8">
        <StatTile label="Trades" value={fmtInt(s.trades)} hint={`${fmtInt(s.wins)} W · ${fmtInt(s.losses)} L · ${fmtInt(s.flats)} flat`} />
        <StatTile label="Sessions" value={fmtInt(m.sessions)} hint="distinct TRADEABLE days" />
        <StatTile label="Trades / day" value={num(m.trades_per_day)} />
        <StatTile label="Win rate" value={fmtFraction(s.win_rate)} />
        <StatTile label="Exp R (gross)" value={fmtR(s.expectancy_r)} intent={sign(s.expectancy_r)}
          hint={ciR ? `day-block 95 % CI ${ciR}` : ciNote} />
        <StatTile label="Exp R (net)" value={fmtR(s.expectancy_r_net)} intent={sign(s.expectancy_r_net)} />
        <StatTile label="Exp ₹ / trade" value={fmtInr(s.expectancy_inr, 2)} intent={sign(s.expectancy_inr)}
          hint={ciInr ? `day-block 95 % CI ${ciInr}` : ciNote} />
        <StatTile label="Profit factor" value={num(s.profit_factor)} />
        <StatTile label="Avg win R" value={fmtR(s.avg_win_r)} />
        <StatTile label="Avg loss R" value={fmtR(s.avg_loss_r)} />
        <StatTile label="Max DD ₹" value={fmtInr(s.max_equity_drawdown_inr)} />
        <StatTile label="Max DD %" value={fmtPct(s.max_equity_drawdown_pct)}
          intent={isNum(s.max_equity_drawdown_pct) && s.max_equity_drawdown_pct > 10 ? "negative" : "neutral"}
          hint={isNum(s.max_equity_drawdown_pct) && s.max_equity_drawdown_pct > 10 ? "above criterion 4's 10 % limit" : "of capital"} />
        <StatTile label="Longest losing streak" value={fmtInt(s.longest_losing_streak)} />
        <StatTile label="Avg hold" value={isNum(s.avg_hold_minutes) ? `${s.avg_hold_minutes.toFixed(1)} min` : "—"} />
        <StatTile label="Mean cost ₹" value={fmtInr(m.mean_cost_inr, 2)} hint={costHint} />
        <StatTile label="Mean cost R" value={isNum(m.mean_cost_r) ? `${m.mean_cost_r.toFixed(3)}R` : "—"}
          hint={artifact ? "not in the report artefact" : m.cost_sample_r ? `over ${m.cost_sample_r} trades` : undefined} />
      </div>
      <div className="rounded-lg border border-border bg-surface-2 px-3 py-2 text-table text-text-2">
        <span className="font-semibold text-text-1">Win rate 95 % Wilson interval: </span>
        <span className="font-mono text-text-1">{wilson ?? "unavailable (no closed trades)"}</span>
        <span className="ml-2 text-text-3">
          — the range the true win rate plausibly lies in given {fmtInt(s.trades)} trade{s.trades === 1 ? "" : "s"}
        </span>
      </div>
      {m.warnings.includes("params_defaults_used") && (
        <p role="note" className="text-micro text-yellow">
          The run&apos;s frozen params could not be rebuilt; display knobs fell back to defaults.
        </p>
      )}
    </div>
  );
}

export function HeadlineMetrics({ run, query }: { run: RunDetail; query: UseQueryResult<RunMetrics, unknown> }) {
  const m = query.data;
  return (
    <Panel
      title="Headline metrics"
      meta={
        <span className="flex items-center gap-1.5">
          {m?.metrics.low_sample && <LowSampleBadge />}
          <SourceMarker source={m?.source} />
        </span>
      }
    >
      {!run.metrics_available ? (
        <EmptyState title="This artefact has no metrics section" />
      ) : (
        <DataSurface query={query} skeleton={<Skeleton label="Loading metrics" />} hideFreshness>
          {(d) => <Tiles m={d} />}
        </DataSurface>
      )}
    </Panel>
  );
}
