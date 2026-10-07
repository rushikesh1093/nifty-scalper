"use client";
import { Fragment, useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import toast from "react-hot-toast";
import { getSignals } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { cn, formatPrice } from "@/lib/utils";
import { DEFAULT_SIGNAL_WINDOW, formatIst, formatSessionWindow, parseSessionWindow, relativeAge } from "@/lib/time";
import { useDiagnostics } from "@/hooks/useDiagnostics";
import { useSignalFeed } from "@/hooks/useSignalFeed";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";
import { useNow } from "@/hooks/useNow";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { Badge, type BadgeIntent } from "@/components/ui/Badge";
import { ConnectionChip, type ConnectionStatus } from "@/components/ui/ConnectionChip";
import { Freshness } from "@/components/ui/Freshness";
import { PriceText } from "@/components/ui/PriceText";
import { STRATEGY_META } from "./strategyMeta";
import type { Signal } from "@/types/trading";

// The primary signal-delivery surface (design §6.9). The 5 s poll is the source of truth; the
// WebSocket only accelerates refetches. Strategy/status filters apply to the fetched rows, so the
// panel can say exactly how many rows a filter hides ("showing N of M (filtered)", AC-5).

const SIGNAL_STATUSES = ["GENERATED", "SELECTED", "ENTERED", "SL_HIT", "TARGET_HIT", "EXPIRED", "SKIPPED"] as const;
const BUCKETS = ["SELECTED", "ENTERED", "SKIPPED"] as const;
const LIMITS = [25, 60, 120, 250] as const;
const COLUMN_COUNT = 11;

const STATUS_INTENT: Record<string, BadgeIntent> = {
  GENERATED: "warning", SELECTED: "info", ENTERED: "positive", SL_HIT: "negative",
  TARGET_HIT: "positive", EXPIRED: "neutral", SKIPPED: "neutral",
};

const STRATEGY_LABEL: Record<string, string> = Object.fromEntries(STRATEGY_META.map((s) => [s.key, s.label]));
const strategyLabel = (key: string) => STRATEGY_LABEL[key] ?? key.replace(/_/g, " ");

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const timeOf = (s: Signal) => {
  const t = s.time ? new Date(s.time).getTime() : NaN;
  return Number.isNaN(t) ? 0 : t;
};

function formatIndicator(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isFinite(v) ? (Number.isInteger(v) ? String(v) : v.toFixed(2)) : "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "string") return v;
  try { return JSON.stringify(v).slice(0, 200); } catch { return "—"; }
}

const SELECT = "rounded-md border border-border bg-surface-2 px-2 py-1 text-table text-text-1 hover:border-border-2";

function copyReason(reason: string) {
  if (!navigator.clipboard) {
    toast.error("Clipboard unavailable in this browser context");
    return;
  }
  navigator.clipboard.writeText(reason).then(
    () => toast.success("Skip reason copied"),
    (e: unknown) => toast.error(`Copy failed: ${e instanceof Error ? e.message : String(e)}`),
  );
}

function SignalRows({
  rows, withDate, expanded, onToggle,
}: { rows: Signal[]; withDate: boolean; expanded: Set<number>; onToggle: (id: number) => void }) {
  return (
    <>
      {rows.map((s, i) => {
        const indicators = s.indicators && typeof s.indicators === "object" ? Object.entries(s.indicators) : [];
        const isOpen = expanded.has(s.id);
        const detailId = `signal-ind-${s.id}`;
        // One stripe per signal *group*: the skip-reason and indicator sub-rows share their parent's
        // background, which `odd:` on <tr> cannot express once sub-rows exist.
        const stripe = i % 2 === 1 ? "bg-surface-2" : undefined;
        const rr = num(s.rr);
        const strike = num(s.strike);
        const isBuy = s.direction === "BUY";
        return (
          <Fragment key={s.id}>
            <tr data-signal-id={s.id} className={stripe}>
              <td className="whitespace-nowrap">
                <span className="inline-flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => onToggle(s.id)}
                    aria-expanded={isOpen}
                    aria-controls={detailId}
                    aria-label={`${isOpen ? "Hide" : "Show"} indicators for signal ${s.id}`}
                    className="rounded px-1 text-text-2 hover:bg-surface-3 hover:text-text-1"
                  >
                    <span aria-hidden="true">{isOpen ? "▾" : "▸"}</span>
                  </button>
                  <time dateTime={s.time ?? undefined} className="font-mono tabular-nums text-text-2">
                    {formatIst(s.time, { withDate })}
                  </time>
                </span>
              </td>
              <td className="font-medium text-text-1">{strategyLabel(s.strategy)}</td>
              <td>{s.underlying}</td>
              <td>
                {/* `direction` is the underlying's direction; the engine always buys the option
                    (option_selector: BUY → CE, SELL → PE), so both legs read "BUY". */}
                <Badge intent={isBuy ? "positive" : "negative"} status={s.direction}>
                  <span aria-hidden="true">{isBuy ? "▲" : "▼"}</span>
                  {isBuy ? "BUY CE" : "BUY PE"}
                </Badge>
              </td>
              <td className="num"><PriceText value={num(s.spot)} /></td>
              <td className="num text-text-1">{strike === null ? "—" : <span className="font-mono tabular-nums">{formatPrice(strike, 0)}</span>}</td>
              <td className="num text-text-1"><PriceText value={num(s.entry_ref)} /></td>
              <td className="num text-red"><PriceText value={num(s.sl_ref)} /></td>
              <td className="num text-green"><PriceText value={num(s.target_ref)} /></td>
              <td className="num text-yellow">
                <span className="font-mono tabular-nums">{rr === null ? "—" : `${rr.toFixed(1)}x`}</span>
              </td>
              <td>
                <Badge intent={STATUS_INTENT[s.status] ?? "neutral"} status={s.status}>
                  {String(s.status).replace(/_/g, " ")}
                </Badge>
              </td>
            </tr>

            {s.skip_reason && (
              <tr className={stripe}>
                <td colSpan={COLUMN_COUNT} className="pt-0">
                  <div className="flex items-start gap-2 pl-7">
                    <span aria-hidden="true" className="text-text-2">↳</span>
                    <p data-role="skip-reason" className="min-w-0 flex-1 whitespace-pre-wrap break-words font-mono text-table text-text-2">
                      <span className="sr-only">Skip reason: </span>{s.skip_reason}
                    </p>
                    <button
                      type="button"
                      onClick={() => copyReason(s.skip_reason ?? "")}
                      className="shrink-0 rounded border border-border-2 px-1.5 py-px text-micro font-semibold text-text-2 hover:bg-surface-3 hover:text-text-1"
                      aria-label={`Copy skip reason for signal ${s.id}`}
                    >
                      Copy reason
                    </button>
                  </div>
                </td>
              </tr>
            )}

            {isOpen && (
              <tr className={stripe}>
                <td colSpan={COLUMN_COUNT} id={detailId} className="pt-0">
                  {indicators.length === 0 ? (
                    <p className="pl-7 text-table text-text-2">No indicator snapshot recorded for this signal.</p>
                  ) : (
                    <dl className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-x-4 gap-y-1 pl-7 text-table">
                      {indicators.map(([k, v]) => (
                        <div key={k} className="flex justify-between gap-2 border-b border-border/60 py-0.5">
                          <dt className="text-text-2">{k}</dt>
                          <dd className="font-mono tabular-nums text-text-1">{formatIndicator(v)}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </td>
              </tr>
            )}
          </Fragment>
        );
      })}
    </>
  );
}

export function SignalsPanel() {
  const [todayOnly, setTodayOnly] = useState(true);
  const [limit, setLimit] = useState<number>(60);
  const [strategy, setStrategy] = useState("");
  const [status, setStatus] = useState("");
  const [sortDir, setSortDir] = useState<"descending" | "ascending">("descending");
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const ids = useId();

  // today_only is always sent explicitly (AC-4); strategy/status filter the fetched rows.
  const params = { today_only: todayOnly, limit };
  const query = useQuery({
    queryKey: queryKeys.signals(params),
    queryFn: () => getSignals(params),
    refetchInterval: 5_000,
  });
  const sticky = useStickyQueryError(query);
  const feed = useSignalFeed();
  const diag = useDiagnostics();
  const now = useNow(15_000);

  const rows = useMemo(() => (Array.isArray(query.data) ? query.data : []), [query.data]);
  const filtersActive = strategy !== "" || status !== "";
  const visible = useMemo(() => {
    const filtered = rows.filter((s) => (!strategy || s.strategy === strategy) && (!status || s.status === status));
    const dir = sortDir === "descending" ? -1 : 1;
    return [...filtered].sort((a, b) => (timeOf(a) - timeOf(b)) * dir);
  }, [rows, strategy, status, sortDir]);

  const buckets = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const s of rows) counts[s.status] = (counts[s.status] ?? 0) + 1;
    return counts;
  }, [rows]);
  const extraBuckets = Object.keys(buckets).filter((k) => !(BUCKETS as readonly string[]).includes(k));

  const strategyOptions = useMemo(() => {
    const keys = new Set<string>(STRATEGY_META.map((s) => s.key));
    for (const s of rows) keys.add(s.strategy);
    return [...keys];
  }, [rows]);

  const newest = rows.reduce<Signal | null>((best, s) => (best === null || timeOf(s) > timeOf(best) ? s : best), null);

  const connection: ConnectionStatus = sticky.isError
    ? "error"
    : feed.status === "live"
      ? "live"
      : feed.status === "reconnecting"
        ? "reconnecting"
        : "polling";
  const connectionTitle =
    connection === "error"
      ? "Signals request failing"
      : connection === "live"
        ? "Realtime socket connected; 5 s poll continues as the source of truth"
        : `Realtime socket unavailable${feed.lastError ? ` (${feed.lastError})` : ""}; data arrives via the 5 s poll`;

  // Window text from the diagnostics payload (finding 13); local constants only before it loads.
  const signalWindow = parseSessionWindow(diag.data?.session_window) ?? DEFAULT_SIGNAL_WINDOW;
  const [openText, closeText] = formatSessionWindow(signalWindow).split("–");
  const emptyHint =
    `Strategies evaluate every minute between ${openText} and ${closeText} IST` +
    (diag.data && !diag.data.session_window_open ? " — the signal window is closed right now." : ".") +
    " See the diagnostics panel for what is blocking signals.";

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const clearFilters = () => { setStrategy(""); setStatus(""); };

  return (
    <Panel
      title="Strategy signals"
      meta={
        query.data !== undefined && (
          <span className="flex flex-wrap items-center gap-1" aria-label="Signal counts by status">
            {BUCKETS.map((b) => (
              <Badge key={b} intent={STATUS_INTENT[b]} strength="outline">{b} {buckets[b] ?? 0}</Badge>
            ))}
            {extraBuckets.map((b) => (
              <Badge key={b} intent={STATUS_INTENT[b] ?? "neutral"} strength="outline">{b.replace(/_/g, " ")} {buckets[b]}</Badge>
            ))}
          </span>
        )
      }
      actions={
        <>
          {newest && now && (
            <span className="text-table text-text-2" title={formatIst(newest.time, { withDate: true })}>
              last signal {relativeAge(newest.time, now)}
            </span>
          )}
          <Freshness dataUpdatedAt={query.dataUpdatedAt} isStale={sticky.isError && query.data !== undefined} isFetching={query.isFetching} />
          <ConnectionChip status={connection} title={connectionTitle} />
        </>
      }
      flush
    >
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-2">
        <div role="group" aria-label="Signal scope" className="inline-flex overflow-hidden rounded-md border border-border">
          {([true, false] as const).map((t) => (
            <button
              key={String(t)}
              type="button"
              aria-pressed={todayOnly === t}
              onClick={() => setTodayOnly(t)}
              className={cn(
                "px-2.5 py-1 text-table font-semibold",
                todayOnly === t ? "bg-accent/30 text-text-1" : "bg-surface-2 text-text-2 hover:text-text-1",
              )}
            >
              {t ? "Today" : "All"}
            </button>
          ))}
        </div>

        <label className="flex items-center gap-1.5 text-table text-text-2" htmlFor={`${ids}-strategy`}>
          Strategy
          <select id={`${ids}-strategy`} value={strategy} onChange={(e) => setStrategy(e.target.value)} className={SELECT}>
            <option value="">All strategies</option>
            {strategyOptions.map((k) => <option key={k} value={k}>{strategyLabel(k)}</option>)}
          </select>
        </label>

        <label className="flex items-center gap-1.5 text-table text-text-2" htmlFor={`${ids}-status`}>
          Status
          <select id={`${ids}-status`} value={status} onChange={(e) => setStatus(e.target.value)} className={SELECT}>
            <option value="">All statuses</option>
            {SIGNAL_STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
          </select>
        </label>

        <label className="flex items-center gap-1.5 text-table text-text-2" htmlFor={`${ids}-limit`}>
          Rows
          <select id={`${ids}-limit`} value={limit} onChange={(e) => setLimit(Number(e.target.value))} className={SELECT}>
            {LIMITS.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>

        {filtersActive && query.data !== undefined && (
          <span className="ml-auto flex items-center gap-2 text-table text-text-2" data-role="filter-summary" aria-live="polite">
            showing {visible.length} of {rows.length} (filtered)
            <button
              type="button"
              onClick={clearFilters}
              className="rounded border border-accent/40 bg-accent/15 px-2 py-px text-micro font-semibold text-text-1 hover:bg-accent/25"
            >
              Clear filters
            </button>
          </span>
        )}
      </div>

      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d) || d.length === 0}
        emptyTitle={todayOnly ? "No signals today" : "No signals recorded"}
        emptyHint={emptyHint}
        skeleton={<TableSkeleton rows={6} cols={COLUMN_COUNT} className="p-4" />}
        className="px-4 pb-4 pt-3"
        hideFreshness
      >
        {() =>
          visible.length === 0 ? (
            <div className="py-4 text-center text-table text-text-2">
              No rows match the current filters — showing 0 of {rows.length} (filtered).
            </div>
          ) : (
            // Table runs edge to edge: cancel the surface padding.
            <div className="-mx-4 -mb-4 max-h-[440px] overflow-auto">
              <table className="data-table">
                <caption className="sr-only">
                  Strategy signals {todayOnly ? "for today" : "across all days"}, {sortDir === "descending" ? "newest" : "oldest"} first.
                  Skip reasons follow their signal row.
                </caption>
                <thead>
                  <tr>
                    <th scope="col" aria-sort={sortDir}>
                      <button
                        type="button"
                        onClick={() => setSortDir((d) => (d === "descending" ? "ascending" : "descending"))}
                        className="inline-flex items-center gap-1 uppercase hover:text-text-1"
                        aria-label={`Time, sorted ${sortDir}; activate to reverse`}
                      >
                        Time <span aria-hidden="true">{sortDir === "descending" ? "↓" : "↑"}</span>
                      </button>
                    </th>
                    <th scope="col">Strategy</th>
                    <th scope="col">Underlying</th>
                    <th scope="col">Dir</th>
                    <th scope="col" className="num">Spot</th>
                    <th scope="col" className="num">Strike</th>
                    <th scope="col" className="num">Entry</th>
                    <th scope="col" className="num">SL</th>
                    <th scope="col" className="num">Target</th>
                    <th scope="col" className="num">R:R</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  <SignalRows rows={visible} withDate={!todayOnly} expanded={expanded} onToggle={toggle} />
                </tbody>
              </table>
            </div>
          )
        }
      </DataSurface>
    </Panel>
  );
}
