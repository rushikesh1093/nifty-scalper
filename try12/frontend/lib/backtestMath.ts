// Pure helpers for the backtest pages (design §6.1, §6.3). No React, no I/O, so a future test
// runner can cover them without refactoring.
import { differenceInCalendarDays, parseISO } from "date-fns";
import type { EquityPoint, NoTradeItem } from "@/types/backtest";

// ── Identifiers (mirror app/db/replay_store.py and app/services/backtest/artifacts.py) ──────

export const DATASET_ID_RE = /^(replay-db|artifacts|synthetic-\d{1,3}d-s\d{1,10}-\d{4}-\d{2}-\d{2})$/;
export const RUN_ID_RE = /^RPL-[A-Za-z0-9]{1,26}$/;

export interface RunRef {
  dataset: string;
  runId:   string;
}

/** "<dataset_id>:<run_id>", the compare-page parameter format. */
export function formatRunRef(ref: RunRef): string {
  return `${ref.dataset}:${ref.runId}`;
}

/** Split on the FIRST ":" only; null when either side fails its regex (§6.3). */
export function parseRunRef(raw: string | null | undefined): RunRef | null {
  if (!raw) return null;
  const i = raw.indexOf(":");
  if (i < 0) return null;
  const dataset = raw.slice(0, i);
  const runId = raw.slice(i + 1);
  return DATASET_ID_RE.test(dataset) && RUN_ID_RE.test(runId) ? { dataset, runId } : null;
}

// ── Compare: equity curves on a shared day-offset axis ──────────────────────

export interface DayOffsetPoint {
  day_offset:  number;
  cum_net_pct: number | null;
}

/** Map a run's equity to { day_offset, cum_net_pct }, day_offset counted from the run's date_from;
 *  the last point of each day wins. Points with no trading_day are dropped. */
export function toDayOffsets(equity: EquityPoint[], dateFrom: string): DayOffsetPoint[] {
  const start = parseISO(dateFrom);
  if (Number.isNaN(start.getTime())) return [];
  const byOffset = new Map<number, number | null>();
  for (const p of equity) {
    if (!p.trading_day) continue;
    const d = parseISO(p.trading_day);
    if (Number.isNaN(d.getTime())) continue;
    byOffset.set(differenceInCalendarDays(d, start), p.cum_net_pct);
  }
  return Array.from(byOffset, ([day_offset, cum_net_pct]) => ({ day_offset, cum_net_pct }))
    .sort((x, y) => x.day_offset - y.day_offset);
}

export interface MergedOffsetPoint {
  day_offset: number;
  a:          number | null;
  b:          number | null;
}

/** Merge two day-offset series into one array keyed on day_offset (missing side → null). */
export function mergeDayOffsets(a: DayOffsetPoint[], b: DayOffsetPoint[]): MergedOffsetPoint[] {
  const out = new Map<number, MergedOffsetPoint>();
  for (const p of a) out.set(p.day_offset, { day_offset: p.day_offset, a: p.cum_net_pct, b: null });
  for (const p of b) {
    const cur = out.get(p.day_offset);
    if (cur) cur.b = p.cum_net_pct;
    else out.set(p.day_offset, { day_offset: p.day_offset, a: null, b: p.cum_net_pct });
  }
  return Array.from(out.values()).sort((x, y) => x.day_offset - y.day_offset);
}

// ── Compare: no-trade funnel diff ───────────────────────────────────────────

export interface FunnelDiffRow {
  stage: string;
  code:  string;
  a:     number;
  b:     number;
  /** b − a */
  delta: number;
}

/** Full outer join of two no-trade summaries on (stage, code), sorted by |Δ| descending,
 *  then stage, then code. A side without the pair counts 0. */
export function funnelOuterJoin(a: NoTradeItem[], b: NoTradeItem[]): FunnelDiffRow[] {
  const key = (x: NoTradeItem) => `${x.stage}\u0000${x.code}`;
  const rows = new Map<string, FunnelDiffRow>();
  for (const x of a) {
    const cur = rows.get(key(x));
    if (cur) cur.a += x.count;
    else rows.set(key(x), { stage: x.stage, code: x.code, a: x.count, b: 0, delta: 0 });
  }
  for (const x of b) {
    const cur = rows.get(key(x));
    if (cur) cur.b += x.count;
    else rows.set(key(x), { stage: x.stage, code: x.code, a: 0, b: x.count, delta: 0 });
  }
  const out = Array.from(rows.values());
  for (const r of out) r.delta = r.b - r.a;
  return out.sort((x, y) =>
    Math.abs(y.delta) - Math.abs(x.delta) || x.stage.localeCompare(y.stage) || x.code.localeCompare(y.code));
}

// ── Compare: metric deltas ──────────────────────────────────────────────────

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** B − A; null when either side is null / not a finite number. */
export function metricDelta(a: number | null | undefined, b: number | null | undefined): number | null {
  return isNum(a) && isNum(b) ? b - a : null;
}

export interface MetricDeltaRow<K extends string = string> {
  key:   K;
  a:     number | null;
  b:     number | null;
  delta: number | null;
}

/** One row per key, read from two flat records, with Δ = B − A. */
export function metricDeltas<K extends string>(
  keys: readonly K[],
  a: Partial<Record<K, unknown>> | null | undefined,
  b: Partial<Record<K, unknown>> | null | undefined,
): MetricDeltaRow<K>[] {
  return keys.map((key) => {
    const va = a && isNum(a[key]) ? (a[key] as number) : null;
    const vb = b && isNum(b[key]) ? (b[key] as number) : null;
    return { key, a: va, b: vb, delta: metricDelta(va, vb) };
  });
}

// ── Runs list sorting ───────────────────────────────────────────────────────

export type SortDir = "ascending" | "descending";

/** Compare two nullable sort values with nulls ALWAYS last, regardless of direction. */
export function compareNullsLast(
  x: number | string | null | undefined,
  y: number | string | null | undefined,
  dir: SortDir,
): number {
  const xn = x === null || x === undefined;
  const yn = y === null || y === undefined;
  if (xn && yn) return 0;
  if (xn) return 1;
  if (yn) return -1;
  const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
  return dir === "ascending" ? c : -c;
}

/** Window span in days exactly as the server measures it, (date_to − date_from).days; null for an
 *  invalid pair. */
export function windowDays(from: string, to: string): number | null {
  const a = parseISO(from);
  const b = parseISO(to);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return null;
  return differenceInCalendarDays(b, a);
}
