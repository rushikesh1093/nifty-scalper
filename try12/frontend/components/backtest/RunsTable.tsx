"use client";
import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { formatIst } from "@/lib/time";
import { compareNullsLast, formatRunRef, type SortDir } from "@/lib/backtestMath";
import { Badge } from "@/components/ui/Badge";
import { HashCell } from "./HashCell";
import { ProvenanceChip } from "./ProvenanceBanner";
import {
  RECOMMENDATION_INTENT, STATUS_INTENT, fmtFraction, fmtInt, fmtPct, fmtR, runHref,
} from "./format";
import type { RunSummary, RunsResponse } from "@/types/backtest";

// The runs list (design §6.1). Client-side sort over the fetched page with nulls always last; the
// server's order (newest first) is the default. Two selected rows feed /backtest/compare.

type SortKey = "created" | "window" | "trades" | "win_rate" | "expectancy_r" | "max_dd";

const SORT_VALUE: Record<SortKey, (r: RunSummary) => number | string | null> = {
  created:      (r) => (r.created_at ? Date.parse(r.created_at) : null),
  window:       (r) => r.date_from,
  trades:       (r) => r.trades,
  win_rate:     (r) => r.win_rate,
  expectancy_r: (r) => r.expectancy_r,
  max_dd:       (r) => r.max_drawdown_pct,
};

/** Criterion 4's threshold: a max drawdown above 10 % of capital reads negative. */
const MAX_DD_WARN_PCT = 10;

const rowKey = (r: RunSummary) => formatRunRef({ dataset: r.dataset, runId: r.run_id });

function SortHeader({
  label, k, sort, onSort, num,
}: {
  label: string; k: SortKey; sort: { key: SortKey; dir: SortDir } | null;
  onSort: (k: SortKey) => void; num?: boolean;
}) {
  const dir: SortDir | null = sort && sort.key === k ? sort.dir : null;
  return (
    <th scope="col" aria-sort={dir ?? "none"} className={cn(num && "num")}>
      <button
        type="button"
        onClick={() => onSort(k)}
        className={cn("inline-flex items-center gap-1 uppercase hover:text-text-1", dir && "text-text-1")}
      >
        {label}
        <span aria-hidden="true" className="text-[0.8em]">
          {dir ? (dir === "ascending" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

function VerdictCell({ r }: { r: RunSummary }) {
  if (r.validation_source === "none" || r.recommendation === null) {
    return <span className="text-text-3" title="no validation report stored">—</span>;
  }
  const failing = r.failing_criteria ?? [];
  return (
    <div className="flex max-w-[260px] flex-wrap items-center gap-1">
      <Badge intent={RECOMMENDATION_INTENT[r.recommendation] ?? "neutral"} status={r.recommendation}>
        {r.recommendation}
      </Badge>
      {failing.length > 0 && <span className="text-micro text-text-2">fails {failing.join(", ")}</span>}
      {(r.caps ?? []).map((c) => (
        <Badge key={c} intent="neutral" strength="outline" caps={false} title={c} className="max-w-[220px] truncate text-micro">
          {c}
        </Badge>
      ))}
    </div>
  );
}

function DatasetChip({ dataset }: { dataset: string }) {
  const artifacts = dataset === "artifacts";
  return (
    <Badge
      intent={artifacts ? "neutral" : "accent"}
      strength={artifacts ? "outline" : "subtle"}
      caps={false}
      title={artifacts ? "report artefact (read-only tier)" : `replay store ${dataset}`}
      className="font-mono text-micro"
    >
      {dataset}
    </Badge>
  );
}

export function RunsTable({ data }: { data: RunsResponse }) {
  const router = useRouter();
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);

  const rows = useMemo(() => {
    if (!sort) return data.items;
    const get = SORT_VALUE[sort.key];
    return [...data.items].sort((a, b) => compareNullsLast(get(a), get(b), sort.dir));
  }, [data.items, sort]);

  // descending → ascending → back to the server's order
  const onSort = (k: SortKey) =>
    setSort((s) => (s?.key !== k ? { key: k, dir: "descending" } : s.dir === "descending" ? { key: k, dir: "ascending" } : null));

  const toggle = (key: string) =>
    setSelected((s) => (s.includes(key) ? s.filter((x) => x !== key) : s.length >= 2 ? s : [...s, key]));

  const compare = () => {
    if (selected.length !== 2) return;
    const [a, b] = selected;
    router.push(`/backtest/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
  };

  const capCount = data.caps.store ?? data.caps.artifacts;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 px-4 pt-3">
        <button
          type="button"
          onClick={compare}
          disabled={selected.length !== 2}
          className="rounded-md border border-accent/60 bg-accent/20 px-3 py-1 text-table font-semibold text-text-1 hover:bg-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Compare selected
        </button>
        <span className="text-micro text-text-3" aria-live="polite">
          {selected.length === 2 ? "2 runs selected" : `select ${2 - selected.length} more run${selected.length === 1 ? "" : "s"} to compare`}
        </span>
        {selected.length > 0 && (
          <button type="button" onClick={() => setSelected([])} className="text-micro text-text-3 underline hover:text-text-1">
            clear selection
          </button>
        )}
      </div>

      {data.truncated && (
        <p role="note" className="mx-4 rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow">
          Showing the {capCount} most recent runs per dataset — filter by dataset to see more.
        </p>
      )}

      <div className="overflow-x-auto">
        <table className="data-table">
          <caption className="sr-only">Backtest runs, newest first by default. Sortable columns have buttons in their headers.</caption>
          <thead>
            <tr>
              <th scope="col"><span className="sr-only">Select for comparison</span></th>
              <th scope="col">Run</th>
              <th scope="col">Label</th>
              <th scope="col">Dataset</th>
              <SortHeader label="Window" k="window" sort={sort} onSort={onSort} />
              <th scope="col">Underlyings</th>
              <th scope="col">Status</th>
              <SortHeader label="Trades" k="trades" sort={sort} onSort={onSort} num />
              <SortHeader label="WR" k="win_rate" sort={sort} onSort={onSort} num />
              <SortHeader label="Exp R" k="expectancy_r" sort={sort} onSort={onSort} num />
              <SortHeader label="Max DD %" k="max_dd" sort={sort} onSort={onSort} num />
              <th scope="col">Verdict</th>
              <th scope="col">Provenance</th>
              <SortHeader label="Created" k="created" sort={sort} onSort={onSort} />
              <th scope="col">Hashes</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const key = rowKey(r);
              const checked = selected.includes(key);
              const ddHigh = typeof r.max_drawdown_pct === "number" && r.max_drawdown_pct > MAX_DD_WARN_PCT;
              return (
                <tr key={key} data-run-id={r.run_id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!checked && selected.length >= 2}
                      onChange={() => toggle(key)}
                      aria-label={`Select ${r.run_id} (${r.dataset}) for comparison`}
                    />
                  </td>
                  <td className="whitespace-nowrap">
                    <Link
                      href={runHref(r.run_id, r.dataset)}
                      title={r.run_id}
                      aria-label={`Open run ${r.run_id}`}
                      className="font-mono text-text-1 underline decoration-border-2 underline-offset-2 hover:decoration-accent"
                    >
                      {r.run_id.length > 16 ? `${r.run_id.slice(0, 16)}…` : r.run_id}
                    </Link>
                  </td>
                  <td className="max-w-[200px] truncate text-text-1" title={r.label ?? undefined}>{r.label || "—"}</td>
                  <td><DatasetChip dataset={r.dataset} /></td>
                  <td className="whitespace-nowrap font-mono">
                    {r.date_from ?? "—"} → {r.date_to ?? "—"}
                  </td>
                  <td className="whitespace-nowrap">{r.underlyings?.length ? r.underlyings.join(", ") : "—"}</td>
                  <td>
                    <Badge intent={STATUS_INTENT[r.status] ?? "neutral"} status={r.status} dot={r.status === "RUNNING" ? "pulse" : undefined}>
                      {r.status}
                    </Badge>
                  </td>
                  <td className="num font-mono">{fmtInt(r.trades)}</td>
                  <td className="num font-mono">{fmtFraction(r.win_rate)}</td>
                  <td className="num font-mono">{fmtR(r.expectancy_r)}</td>
                  <td className={cn("num whitespace-nowrap font-mono", ddHigh && "text-down")}>
                    {fmtPct(r.max_drawdown_pct)}
                    {ddHigh && <span className="ml-1 text-micro" title="above criterion 4's 10 % threshold">&gt;10</span>}
                    {ddHigh && <span className="sr-only"> (above the 10 % drawdown threshold)</span>}
                  </td>
                  <td><VerdictCell r={r} /></td>
                  <td><ProvenanceChip state={r.synthetic_state} source={r.synthetic_source} /></td>
                  <td
                    className="whitespace-nowrap font-mono"
                    title={r.created_at_source === "file_mtime" ? "artefact file modification time — the artefact stores no creation time" : undefined}
                  >
                    {formatIst(r.created_at, { withDate: true, seconds: false })}
                    {r.created_at_source === "file_mtime" && <span aria-hidden="true" className="ml-0.5 text-text-3">*</span>}
                    {r.created_at_source === "file_mtime" && <span className="sr-only"> (file modification time)</span>}
                  </td>
                  <td>
                    <div className="flex flex-col gap-0.5">
                      <HashCell value={r.params_hash} label="params hash" />
                      <HashCell value={r.determinism_hash} label="determinism hash" />
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
