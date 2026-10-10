"use client";
import { useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import type { UseQueryResult } from "@tanstack/react-query";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useRun, useRunMetrics, useRunNoTrade, useRunSeries } from "@/hooks/useBacktest";
import { isHttpStatus } from "@/hooks/useBacktestJobs";
import {
  formatRunRef, funnelOuterJoin, mergeDayOffsets, metricDelta, parseRunRef, toDayOffsets, type RunRef,
} from "@/lib/backtestMath";
import { Badge } from "@/components/ui/Badge";
import { DataSurface } from "@/components/ui/DataSurface";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { PageHeader } from "@/components/ui/PageHeader";
import { Panel } from "@/components/ui/Panel";
import { ChartSkeleton, Skeleton, TableSkeleton } from "@/components/ui/Skeleton";
import { DeltaText } from "@/components/ui/PriceText";
import { ChartWithTable } from "./ChartWithTable";
import { ProvenanceBanner } from "./ProvenanceBanner";
import { RunPicker } from "./RunPicker";
import { RECOMMENDATION_INTENT, fmtFraction, fmtInr, fmtInt, fmtPct, fmtR, runHref } from "./format";
import type { NoTradeSummary, RunDetail, RunMetrics, SeriesPayload } from "@/types/backtest";

// /backtest/compare client content (design §6.3). Client-side composition of two run reads; each
// side degrades independently. Malformed or missing ?a= / ?b= render the RunPicker with no fetch
// and no error toast.

type Side = "A" | "B";
const HEIGHT = 240;

const shortId = (id: string) => (id.length > 16 ? `${id.slice(0, 16)}…` : id);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

// ── metric table rows ────────────────────────────────────────────────────────

interface Row {
  label:  string;
  get:    (m: RunMetrics | undefined, r: RunDetail | undefined) => number | null | undefined;
  fmt:    (v: number | null | undefined) => string;
  /** Δ rendering: the DeltaText props, and a multiplier (fraction → percentage points). */
  delta:  { decimals: number; prefix?: string; suffix?: string; scale?: number };
  /** Higher is not "better" here: render the Δ without the green / red judgement. */
  neutral?: boolean;
  /** Only per-trade rows can produce it: "—" plus a footnote on an artefact side. */
  perTrade?: boolean;
}

const ROWS: Row[] = [
  { label: "Trades", get: (m) => m?.metrics.trades, fmt: fmtInt, delta: { decimals: 0 }, neutral: true },
  { label: "Sessions", get: (m) => m?.sessions, fmt: fmtInt, delta: { decimals: 0 }, neutral: true },
  { label: "Trades / day", get: (m) => m?.trades_per_day, fmt: (v) => (isNum(v) ? v.toFixed(2) : "—"), delta: { decimals: 2 }, neutral: true },
  { label: "Win rate", get: (m) => m?.metrics.win_rate, fmt: (v) => fmtFraction(v), delta: { decimals: 2, suffix: " pp", scale: 100 } },
  { label: "Expectancy R (gross)", get: (m) => m?.metrics.expectancy_r, fmt: (v) => fmtR(v), delta: { decimals: 3, suffix: "R" } },
  { label: "Expectancy R (net)", get: (m) => m?.metrics.expectancy_r_net, fmt: (v) => fmtR(v), delta: { decimals: 3, suffix: "R" } },
  { label: "Expectancy ₹ / trade", get: (m) => m?.metrics.expectancy_inr, fmt: (v) => fmtInr(v, 2), delta: { decimals: 2, prefix: "₹" } },
  { label: "Profit factor", get: (m) => m?.metrics.profit_factor, fmt: (v) => (isNum(v) ? v.toFixed(2) : "—"), delta: { decimals: 2 } },
  { label: "Avg win R", get: (m) => m?.metrics.avg_win_r, fmt: (v) => fmtR(v), delta: { decimals: 3, suffix: "R" } },
  { label: "Avg loss R", get: (m) => m?.metrics.avg_loss_r, fmt: (v) => fmtR(v), delta: { decimals: 3, suffix: "R" } },
  { label: "Max DD ₹", get: (m) => m?.metrics.max_equity_drawdown_inr, fmt: (v) => fmtInr(v), delta: { decimals: 0, prefix: "₹" }, neutral: true },
  { label: "Max DD %", get: (m) => m?.metrics.max_equity_drawdown_pct, fmt: (v) => fmtPct(v), delta: { decimals: 2, suffix: " pp" }, neutral: true },
  { label: "Longest losing streak", get: (m) => m?.metrics.longest_losing_streak, fmt: fmtInt, delta: { decimals: 0 }, neutral: true },
  { label: "Avg hold", get: (m) => m?.metrics.avg_hold_minutes, fmt: (v) => (isNum(v) ? `${v.toFixed(1)} min` : "—"), delta: { decimals: 1, suffix: " min" }, neutral: true },
  { label: "Mean cost ₹", get: (m) => m?.mean_cost_inr, fmt: (v) => fmtInr(v, 2), delta: { decimals: 2, prefix: "₹" }, neutral: true, perTrade: true },
  { label: "Mean cost R", get: (m) => m?.mean_cost_r, fmt: (v) => (isNum(v) ? `${v.toFixed(3)}R` : "—"), delta: { decimals: 3, suffix: "R" }, neutral: true, perTrade: true },
  { label: "Capital", get: (_m, r) => (r?.capital ? r.capital : null), fmt: (v) => fmtInr(v), delta: { decimals: 0, prefix: "₹" }, neutral: true },
];

// ── per-side header ─────────────────────────────────────────────────────────

function SideHeader({ side, runRef, query }: { side: Side; runRef: RunRef; query: UseQueryResult<RunDetail, unknown> }) {
  if (isHttpStatus(query.error, 404) && !query.data) {
    return (
      <Panel title={`Run ${side}`}>
        <EmptyState title="Run not found in any dataset" hint={`${runRef.runId} is not in '${runRef.dataset}'.`} />
      </Panel>
    );
  }
  return (
    <Panel title={`Run ${side}`}>
      <DataSurface query={query} skeleton={<Skeleton label={`Loading run ${side}`} />} hideFreshness>
        {(r) => {
          const rec = r.validation?.recommendation;
          return (
            <div className="space-y-2">
              <ProvenanceBanner state={r.synthetic_state} source={r.synthetic_source} />
              <div className="flex flex-wrap items-center gap-2">
                <Link href={runHref(r.run_id, r.dataset)} title={r.run_id}
                  className="font-mono text-body text-text-1 underline decoration-border-2 underline-offset-2 hover:decoration-accent">
                  {r.run_id}
                </Link>
                {rec ? (
                  <Badge intent={RECOMMENDATION_INTENT[rec] ?? "neutral"} status={rec}>{rec}</Badge>
                ) : (
                  <span className="text-micro text-text-3">no validation report</span>
                )}
                {(r.validation?.failing_criteria ?? []).length > 0 && (
                  <span className="text-micro text-text-2">fails {(r.validation?.failing_criteria ?? []).join(", ")}</span>
                )}
              </div>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-table">
                <dt className="text-text-3">Label</dt><dd className="text-text-1">{r.label || "—"}</dd>
                <dt className="text-text-3">Dataset</dt><dd className="font-mono text-text-1">{r.dataset}</dd>
                <dt className="text-text-3">Window</dt><dd className="font-mono text-text-1">{r.date_from ?? "—"} → {r.date_to ?? "—"}</dd>
                <dt className="text-text-3">Capital</dt><dd className="font-mono text-text-1">{r.capital ? fmtInr(r.capital) : "unknown"}</dd>
                <dt className="text-text-3">Per-trade rows</dt><dd className="text-text-1">{r.per_trade_available ? "available" : "not in the report artefact"}</dd>
              </dl>
            </div>
          );
        }}
      </DataSurface>
    </Panel>
  );
}

// ── metric table ────────────────────────────────────────────────────────────

function sideState(run: UseQueryResult<RunDetail, unknown>, m: UseQueryResult<RunMetrics, unknown>): string | null {
  if (!run.data) return run.isError ? "run unavailable" : "loading…";
  if (!run.data.metrics_available) return "this artefact has no metrics section";
  if (m.isError && !m.data) return "metrics failed to load";
  if (!m.data) return "loading…";
  return null;
}

function MetricTable({
  runA, runB, mA, mB,
}: {
  runA: UseQueryResult<RunDetail, unknown>; runB: UseQueryResult<RunDetail, unknown>;
  mA: UseQueryResult<RunMetrics, unknown>; mB: UseQueryResult<RunMetrics, unknown>;
}) {
  const artA = mA.data?.source === "artifact" || runA.data?.per_trade_available === false;
  const artB = mB.data?.source === "artifact" || runB.data?.per_trade_available === false;
  const stA = sideState(runA, mA);
  const stB = sideState(runB, mB);
  const cell = (row: Row, m: RunMetrics | undefined, r: RunDetail | undefined, art: boolean) => {
    const v = row.get(m, r);
    return (
      <>
        {row.fmt(v)}
        {row.perTrade && art && <sup aria-label="not in the report artefact" className="ml-0.5 text-text-3">†</sup>}
      </>
    );
  };
  return (
    <Panel title="Metrics" flush>
      <div className="overflow-x-auto">
        <table className="data-table">
          <caption className="sr-only">Headline metrics of runs A and B with the difference B minus A</caption>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col" className="num">A{stA && <span className="block font-normal normal-case text-text-3">{stA}</span>}</th>
              <th scope="col" className="num">B{stB && <span className="block font-normal normal-case text-text-3">{stB}</span>}</th>
              <th scope="col" className="num">Δ (B − A)</th>
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => {
              const a = row.get(mA.data, runA.data);
              const b = row.get(mB.data, runB.data);
              const d = metricDelta(a, b);
              const scaled = d === null ? null : d * (row.delta.scale ?? 1);
              return (
                <tr key={row.label}>
                  <th scope="row" className="text-left font-normal text-text-1">{row.label}</th>
                  <td className="num font-mono">{cell(row, mA.data, runA.data, artA)}</td>
                  <td className="num font-mono">{cell(row, mB.data, runB.data, artB)}</td>
                  <td className="num">
                    <DeltaText value={scaled} decimals={row.delta.decimals} prefix={row.delta.prefix} suffix={row.delta.suffix}
                      className={row.neutral ? "text-text-1" : undefined} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {(artA || artB) && (
        <p className="px-4 py-2 text-micro text-text-3">† not in the report artefact — mean cost needs per-trade rows.</p>
      )}
      {mA.isError && !mA.data && <div className="px-4 pb-3"><ErrorState compact title="A metrics" error={mA.error} onRetry={() => { void mA.refetch(); }} /></div>}
      {mB.isError && !mB.data && <div className="px-4 pb-3"><ErrorState compact title="B metrics" error={mB.error} onRetry={() => { void mB.refetch(); }} /></div>}
    </Panel>
  );
}

// ── equity overlay ──────────────────────────────────────────────────────────

function EquityOverlay({
  refA, refB, runA, runB, sA, sB,
}: {
  refA: RunRef; refB: RunRef;
  runA: RunDetail | undefined; runB: RunDetail | undefined;
  sA: UseQueryResult<SeriesPayload, unknown>; sB: UseQueryResult<SeriesPayload, unknown>;
}) {
  const body = (() => {
    if (!runA || !runB) return <ChartSkeleton height={HEIGHT} />;
    const missing = ([["A", refA, runA], ["B", refB, runB]] as const).filter(([, , r]) => !r.per_trade_available);
    if (missing.length > 0) {
      return (
        <div className="space-y-1">
          {missing.map(([side, ref]) => (
            <EmptyState key={side}
              title={`No equity curve for ${side} (${shortId(ref.runId)}): the report artefact has no per-trade rows`}
              hint="Re-run it into a replay store to overlay its curve." />
          ))}
        </div>
      );
    }
    for (const [side, q] of [["A", sA], ["B", sB]] as const) {
      if (q.isError && !q.data) {
        return <ErrorState title={`Series for ${side} failed to load`} error={q.error} onRetry={() => { void q.refetch(); }} />;
      }
    }
    if (!sA.data || !sB.data) return <ChartSkeleton height={HEIGHT} />;
    const merged = mergeDayOffsets(
      toDayOffsets(sA.data.equity, runA.date_from ?? ""),
      toDayOffsets(sB.data.equity, runB.date_from ?? ""),
    );
    if (merged.length === 0) {
      return <EmptyState title="Neither run has a closed trade to plot" />;
    }
    const nameA = `A ${shortId(refA.runId)} (${fmtInr(runA.capital)})`;
    const nameB = `B ${shortId(refB.runId)} (${fmtInr(runB.capital)})`;
    const endA = [...merged].reverse().find((p) => p.a !== null)?.a ?? null;
    const endB = [...merged].reverse().find((p) => p.b !== null)?.b ?? null;
    const label = `Cumulative net P&L as % of capital by calendar day from each run's start: `
      + `${nameA} ends at ${fmtPct(endA)}, ${nameB} ends at ${fmtPct(endB)}`;
    return (
      <ChartWithTable
        label={label}
        height={HEIGHT}
        chart={({ recharts: rc, color }) => (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={merged} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke={rc.grid.stroke} strokeDasharray={rc.grid.strokeDasharray} vertical={false} />
              <XAxis dataKey="day_offset" type="number" domain={["dataMin", "dataMax"]} stroke={rc.axis.stroke} tick={rc.axis.tick}
                tickLine={false} axisLine={rc.axis.axisLine} allowDecimals={false} tickFormatter={(v: number) => `d${v}`} />
              <YAxis stroke={rc.axis.stroke} tick={rc.axis.tick} tickLine={false} axisLine={false} width={56}
                tickFormatter={(v: number) => `${v.toFixed(1)} %`} />
              <Tooltip contentStyle={rc.tooltip.contentStyle} labelStyle={rc.tooltip.labelStyle} itemStyle={rc.tooltip.itemStyle}
                labelFormatter={(v) => `Day ${v} from start`} formatter={(v: number, name: string) => [fmtPct(v), name]} />
              {/* legend text in the body colour (the accent series colour is < 4.5:1 at 11 px); the marker keeps the series colour */}
              <Legend wrapperStyle={{ fontSize: 11 }} formatter={(value) => <span style={{ color: color("text-2") }}>{value}</span>} />
              <ReferenceLine y={0} stroke={color("text-3")} strokeDasharray="4 2" />
              <Line type="stepAfter" dataKey="a" name={nameA} stroke={color("accent")} strokeWidth={1.5} dot={false} connectNulls isAnimationActive={false} />
              <Line type="stepAfter" dataKey="b" name={nameB} stroke={color("yellow")} strokeWidth={1.5} strokeDasharray="5 3" dot={false} connectNulls isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
        table={
          <table className="data-table">
            <caption className="sr-only">Cumulative net P&amp;L as % of capital per day offset</caption>
            <thead>
              <tr>
                <th scope="col" className="num">Day from start</th>
                <th scope="col" className="num">{nameA}</th>
                <th scope="col" className="num">{nameB}</th>
              </tr>
            </thead>
            <tbody>
              {merged.map((p) => (
                <tr key={p.day_offset}>
                  <td className="num font-mono">{p.day_offset}</td>
                  <td className="num font-mono">{fmtPct(p.a)}</td>
                  <td className="num font-mono">{fmtPct(p.b)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        }
      />
    );
  })();
  return <Panel title="Equity, % of capital">{body}</Panel>;
}

// ── funnel diff ─────────────────────────────────────────────────────────────

function FunnelDiff({ nA, nB }: { nA: UseQueryResult<NoTradeSummary, unknown>; nB: UseQueryResult<NoTradeSummary, unknown> }) {
  const body = (() => {
    for (const [side, q] of [["A", nA], ["B", nB]] as const) {
      if (q.isError && !q.data) {
        return <div className="p-4"><ErrorState title={`No-trade summary for ${side} failed to load`} error={q.error} onRetry={() => { void q.refetch(); }} /></div>;
      }
    }
    if (!nA.data || !nB.data) return <div className="p-4"><TableSkeleton rows={5} cols={5} /></div>;
    const rows = funnelOuterJoin(nA.data.items, nB.data.items);
    if (rows.length === 0) return <EmptyState title="Neither run recorded a no-trade decision" />;
    return (
      <div className="max-h-[480px] overflow-auto">
        <table className="data-table">
          <caption className="sr-only">No-trade decisions per stage and code for runs A and B, largest change first</caption>
          <thead>
            <tr>
              <th scope="col">Stage</th>
              <th scope="col">Code</th>
              <th scope="col" className="num">A</th>
              <th scope="col" className="num">B</th>
              <th scope="col" className="num">Δ (B − A)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.stage}:${r.code}`}>
                <td className="font-mono text-text-2">{r.stage}</td>
                <td className="font-mono text-text-1">{r.code}</td>
                <td className="num font-mono">{fmtInt(r.a)}</td>
                <td className="num font-mono">{fmtInt(r.b)}</td>
                <td className="num"><DeltaText value={r.delta} decimals={0} className="text-text-1" /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  })();
  return <Panel title="No-trade decisions, A vs B" flush>{body}</Panel>;
}

// ── comparison ──────────────────────────────────────────────────────────────

function Notes({ a, b }: { a: RunDetail | undefined; b: RunDetail | undefined }) {
  if (!a || !b) return null;
  const notes: string[] = [];
  if (a.date_from !== b.date_from || a.date_to !== b.date_to) {
    notes.push(`Metrics are not comparable like for like: the runs cover different windows (A ${a.date_from ?? "—"} → ${a.date_to ?? "—"}, B ${b.date_from ?? "—"} → ${b.date_to ?? "—"}).`);
  }
  if (a.capital !== b.capital) {
    notes.push(`Different capital: A ${a.capital ? fmtInr(a.capital) : "unknown"} vs B ${b.capital ? fmtInr(b.capital) : "unknown"}. Rupee figures scale with capital; compare the R and % rows.`);
  }
  if (a.synthetic_state !== b.synthetic_state) {
    notes.push(`A is ${a.synthetic_state} and B is ${b.synthetic_state} — neither is evidence of edge.`);
  }
  if (notes.length === 0) return null;
  return (
    <ul role="note" className="space-y-0.5 rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow">
      {notes.map((n) => <li key={n}>{n}</li>)}
    </ul>
  );
}

function Comparison({ refA, refB }: { refA: RunRef; refB: RunRef }) {
  const [picking, setPicking] = useState(false);
  const runA = useRun(refA.runId, refA.dataset);
  const runB = useRun(refB.runId, refB.dataset);
  const mA = useRunMetrics(refA.runId, refA.dataset, runA.data?.metrics_available ?? false);
  const mB = useRunMetrics(refB.runId, refB.dataset, runB.data?.metrics_available ?? false);
  const sA = useRunSeries(refA.runId, refA.dataset, 0.25, runA.data?.per_trade_available ?? false);
  const sB = useRunSeries(refB.runId, refB.dataset, 0.25, runB.data?.per_trade_available ?? false);
  const nA = useRunNoTrade(refA.runId, refA.dataset, !!runA.data);
  const nB = useRunNoTrade(refB.runId, refB.dataset, !!runB.data);

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <button type="button" aria-expanded={picking} onClick={() => setPicking((p) => !p)}
          className="rounded-md border border-border-2 px-3 py-1 text-table text-text-2 hover:bg-surface-3 hover:text-text-1">
          {picking ? "Hide run picker" : "Change runs"}
        </button>
      </div>
      {picking && <RunPicker a={refA} b={refB} />}
      <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
        <SideHeader side="A" runRef={refA} query={runA} />
        <SideHeader side="B" runRef={refB} query={runB} />
      </div>
      <Notes a={runA.data} b={runB.data} />
      <MetricTable runA={runA} runB={runB} mA={mA} mB={mB} />
      <EquityOverlay refA={refA} refB={refB} runA={runA.data} runB={runB.data} sA={sA} sB={sB} />
      <FunnelDiff nA={nA} nB={nB} />
    </div>
  );
}

export function CompareView() {
  const searchParams = useSearchParams();
  const rawA = searchParams.get("a");
  const rawB = searchParams.get("b");
  const a = parseRunRef(rawA);
  const b = parseRunRef(rawB);
  const same = !!a && !!b && formatRunRef(a) === formatRunRef(b);

  let body: React.ReactNode;
  if (!a || !b) {
    // a bad URL is a user mistake, not a failure: picker, no fetch of either side, no toast
    body = <RunPicker key={`${rawA ?? ""}|${rawB ?? ""}`} a={a} b={b} message="Pick two runs to compare" />;
  } else if (same) {
    body = (
      <div className="space-y-4">
        <div className="panel"><EmptyState title="Pick two different runs" hint="Both sides name the same run; choose another run for B." /></div>
        <RunPicker key={`${rawA ?? ""}|${rawB ?? ""}`} a={a} b={null} lockA />
      </div>
    );
  } else {
    body = <Comparison key={`${formatRunRef(a)}|${formatRunRef(b)}`} refA={a} refB={b} />;
  }

  return (
    <div className="mx-auto max-w-[2400px] space-y-4 p-4">
      <PageHeader
        title="Compare backtests"
        description="Two runs side by side. Δ is B − A. Each side states its own provenance; neither SYNTHETIC nor UNVERIFIED runs are evidence of edge."
        actions={
          <Link href="/backtest" className="text-table text-text-2 underline decoration-border-2 underline-offset-2 hover:text-text-1">
            ← Back to runs
          </Link>
        }
      />
      {body}
    </div>
  );
}
