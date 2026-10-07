"use client";
import { useState } from "react";
import { cn } from "@/lib/utils";
import { formatIst } from "@/lib/time";
import { useRegimeHistory, type Underlying } from "@/hooks/useRegime";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { formatDurationSecs, regimeLabel, regimeSwatch, regimeTone } from "@/components/regime/regimeMeta";
import type { RegimeSpan } from "@/types/market";

const MIN_WIDTH_PCT = 0.5;

function spanTime(v: string) {
  return formatIst(v, { seconds: false, withDate: true });
}

function Band({ spans }: { spans: RegimeSpan[] }) {
  // Collapsed spans arrive ascending; sort defensively by start instant.
  const sorted = [...spans].sort((a, b) => Date.parse(a.from) - Date.parse(b.from));
  const total = sorted.reduce((s, x) => s + Math.max(0, x.duration_secs || 0), 0);
  const [active, setActive] = useState<string | null>(sorted.at(-1)?.from ?? null);
  const current = sorted.find((s) => s.from === active) ?? sorted.at(-1) ?? null;
  const present = Array.from(new Set(sorted.map((s) => s.regime)));
  const isRedis = sorted.some((s) => s.source === "redis");

  return (
    <div className="space-y-3" data-testid="regime-timeline" data-spans={sorted.length} data-source={isRedis ? "redis" : "db"}>
      <div className="flex flex-wrap items-center justify-between gap-2 text-micro text-text-3">
        <span>
          {sorted.length} span{sorted.length === 1 ? "" : "s"} · {spanTime(sorted[0].from)} → {spanTime(sorted[sorted.length - 1].to)}
        </span>
        {isRedis && <Badge intent="warning" caps={false}>last 5 states only (cache)</Badge>}
      </div>

      <div role="group" aria-label="Regime spans, oldest first" className="flex h-10 w-full gap-px overflow-hidden rounded-md border border-border bg-surface-2">
        {sorted.map((s) => {
          const pct = total > 0 ? (Math.max(0, s.duration_secs) / total) * 100 : 100 / sorted.length;
          const label = `${regimeLabel(s.regime)}, ${formatDurationSecs(s.duration_secs)}, ${spanTime(s.from)}–${spanTime(s.to)}`;
          return (
            <button
              key={s.from}
              type="button"
              data-width-pct={pct.toFixed(3)}
              aria-label={label}
              aria-pressed={current?.from === s.from}
              title={label}
              onClick={() => setActive(s.from)}
              onFocus={() => setActive(s.from)}
              style={{ width: `${Math.max(MIN_WIDTH_PCT, pct)}%` }}
              className={cn(
                "h-full shrink-0 transition-opacity hover:opacity-80 focus-visible:z-10",
                regimeSwatch(s.regime),
                current?.from === s.from && "ring-2 ring-inset ring-text-1",
              )}
            />
          );
        })}
      </div>

      {current && (
        <dl aria-live="polite" className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-md border border-border bg-surface-2 p-3 text-table sm:grid-cols-3 lg:grid-cols-6" data-testid="regime-span-details">
          <div className="col-span-2 sm:col-span-3 lg:col-span-2">
            <dt className="text-micro text-text-3">Regime</dt>
            <dd className={cn("font-semibold", regimeTone(current.regime))}>{regimeLabel(current.regime)}</dd>
          </div>
          <div><dt className="text-micro text-text-3">From</dt><dd className="font-mono text-text-1">{spanTime(current.from)}</dd></div>
          <div><dt className="text-micro text-text-3">To</dt><dd className="font-mono text-text-1">{spanTime(current.to)}</dd></div>
          <div><dt className="text-micro text-text-3">Duration</dt><dd className="font-mono text-text-1">{formatDurationSecs(current.duration_secs)}</dd></div>
          <div>
            <dt className="text-micro text-text-3">ADX / ATR</dt>
            <dd className="font-mono text-text-1">{current.adx != null ? current.adx.toFixed(1) : "—"} / {current.atr != null ? current.atr.toFixed(2) : "—"}</dd>
          </div>
          <div className="col-span-2 sm:col-span-3 lg:col-span-6 text-micro text-text-3">
            {current.samples} sample{current.samples === 1 ? "" : "s"} · previous {regimeLabel(current.previous_regime)} · source {current.source}
          </div>
        </dl>
      )}

      <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-micro text-text-2">
        {present.map((r) => (
          <li key={r} className="flex items-center gap-1.5">
            <span aria-hidden="true" className={cn("h-2.5 w-2.5 rounded-sm", regimeSwatch(r))} />
            {regimeLabel(r)}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RegimeTimeline({ underlying }: { underlying: Underlying }) {
  const query = useRegimeHistory(underlying);
  return (
    <Panel title="Regime timeline" meta={<Badge intent="neutral">{underlying} · 24h</Badge>}>
      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d) || d.length === 0}
        emptyTitle="No regime detections in the last 24 hours"
        emptyHint="The runner records a regime each minute between 09:15 and 15:25 IST."
        skeleton={<Skeleton label="Loading regime timeline" />}
      >
        {/* key: reset the selected span when the underlying changes */}
        {(spans) => <Band key={underlying} spans={spans} />}
      </DataSurface>
    </Panel>
  );
}
