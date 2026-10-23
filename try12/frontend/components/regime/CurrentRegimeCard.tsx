"use client";
import { cn } from "@/lib/utils";
import { formatIst } from "@/lib/time";
import { useNow } from "@/hooks/useNow";
import { useDiagnostics } from "@/hooks/useDiagnostics";
import { useCurrentRegime, useRegimeHistory, type Underlying } from "@/hooks/useRegime";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { UNDETERMINED, formatDurationSecs, regimeLabel, regimeTone } from "@/components/regime/regimeMeta";

/**
 * Current regime (finding 4): a cache failure is a 503 → DataSurface's ErrorState + Retry, never
 * "UNKNOWN". `source: "absent"` (key genuinely missing) → an explicit "not detected yet" state.
 */
export function CurrentRegimeCard({ underlying }: { underlying: Underlying }) {
  const query = useCurrentRegime(underlying);
  const history = useRegimeHistory(underlying);
  const diag = useDiagnostics();
  const now = useNow(1_000);

  return (
    <Panel title="Current regime" meta={<Badge intent="neutral">{underlying}</Badge>}>
      <DataSurface query={query} skeleton={<Skeleton label="Loading current regime" />}>
        {(r, meta) => {
          if (r.source === "absent") {
            // The current key expires 10 min after the last cycle (e.g. after the entry
            // cutoff), so say what was last classified rather than "nothing yet".
            const past = Array.isArray(history.data) ? history.data : [];
            const last = past.length ? past.reduce((a, b) => (Date.parse(b.from) > Date.parse(a.from) ? b : a)) : null;
            return (
              <div data-testid="current-regime" data-state="absent">
                <EmptyState
                  title={last ? `Not classifying now · last ${regimeLabel(last.regime)} since ${formatIst(last.from)}`
                              : "No regime detected yet"}
                  hint="The strategy runner classifies the regime while it evaluates candles, 09:15 until the entry cutoff (15:05 IST)."
                />
              </div>
            );
          }
          const elapsed = now === null ? null : r.seconds_in_regime + Math.max(0, (now.getTime() - meta.dataUpdatedAt) / 1000);
          const spans = Array.isArray(history.data) ? history.data : [];
          const newest = spans.length ? spans.reduce((a, b) => (Date.parse(b.from) > Date.parse(a.from) ? b : a)) : null;
          const probe = diag.data?.underlyings.find((u) => u.underlying === underlying);
          const allows = probe?.regime_allows_trading;
          return (
            <div data-testid="current-regime" data-state="ok" data-regime={r.regime} className="space-y-3">
              <div aria-live="polite">
                <div className={cn("text-hero font-bold", regimeTone(r.regime))}>{regimeLabel(r.regime)}</div>
                <div className="text-table text-text-3">
                  {r.since ? `since ${formatIst(r.since, { seconds: false, withDate: true })}` : "start time unknown"}
                </div>
                {r.regime === UNDETERMINED && (
                  <p className="mt-1 text-table text-text-2">
                    The detector could not classify the market (too few candles or an indicator error).
                  </p>
                )}
              </div>
              <dl className="grid grid-cols-3 gap-2">
                <div className="rounded-md border border-border bg-surface-2 px-2.5 py-2">
                  <dt className="text-micro uppercase text-text-3">In regime</dt>
                  <dd className="font-mono text-body text-text-1">{formatDurationSecs(elapsed)}</dd>
                </div>
                <div className="rounded-md border border-border bg-surface-2 px-2.5 py-2">
                  <dt className="text-micro uppercase text-text-3">ADX</dt>
                  <dd className="font-mono text-body text-text-1">{newest?.adx != null ? newest.adx.toFixed(1) : "—"}</dd>
                </div>
                <div className="rounded-md border border-border bg-surface-2 px-2.5 py-2">
                  <dt className="text-micro uppercase text-text-3">ATR</dt>
                  <dd className="font-mono text-body text-text-1">{newest?.atr != null ? newest.atr.toFixed(2) : "—"}</dd>
                </div>
              </dl>
              <div data-testid="regime-permits" className="text-table">
                {allows === true && <Badge intent="positive" dot>Permits trading</Badge>}
                {allows === false && <Badge intent="negative" dot>Blocks trading — strategies are regime-disabled</Badge>}
                {allows == null && (
                  <span className="text-text-3">
                    {diag.isError ? "Trading permission unknown (diagnostics unavailable)" : "Trading permission not reported yet"}
                  </span>
                )}
              </div>
              <p className="text-micro text-text-3">ADX/ATR from the newest timeline span{newest ? ` (${formatIst(newest.to, { seconds: false })})` : " — timeline not loaded"}.</p>
            </div>
          );
        }}
      </DataSurface>
    </Panel>
  );
}
