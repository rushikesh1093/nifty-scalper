"use client";
import { useQuery } from "@tanstack/react-query";
import { getDataHealth } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { Skeleton } from "@/components/ui/Skeleton";
import { Badge, type BadgeIntent } from "@/components/ui/Badge";
import type { DataHealth, HealthStatus } from "@/types/market";

const INTENT: Record<HealthStatus, BadgeIntent> = { OK: "positive", WARN: "warning", FAIL: "negative", CLOSED: "neutral" };

function age(s: number | null) {
  if (s == null) return "—";
  if (s < 120) return `${Math.max(0, Math.round(s))}s`;
  if (s < 7200) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

/**
 * Integrity and freshness of the data the strategies and the option selector trust
 * (GET /market/data-health). Counts, never repairs: a bad record is shown, not fixed.
 */
export function DataHealthPanel() {
  const query = useQuery({ queryKey: queryKeys.dataHealth, queryFn: getDataHealth, refetchInterval: 15_000 });
  return (
    <Panel title="Data health" dense>
      <DataSurface query={query} skeleton={<Skeleton label="Loading data health" />}>
        {(h: DataHealth) => (
          <div className="space-y-2" data-testid="data-health">
            <div className="flex flex-wrap items-center gap-2">
              <Badge intent={INTENT[h.status]} dot>{h.status === "CLOSED" ? "Market closed" : h.status}</Badge>
              <span className="text-micro text-text-3">feed {h.feed_connected ? "connected" : "disconnected"}</span>
            </div>
            <ul className="divide-y divide-border">
              {Object.entries(h.candles).map(([name, c]) => (
                <li key={name} className="flex flex-wrap items-center gap-x-2 py-1.5">
                  <span className="min-w-0 flex-1 text-table text-text-1">{name.replace("_", " ")} candles</span>
                  <Badge intent={INTENT[c.status]}>{c.status}</Badge>
                  <span className="w-full font-mono text-micro text-text-3">
                    {c.candles} · last {age(c.newest_age_s)} ago
                    {c.duplicates ? ` · ${c.duplicates} duplicate` : ""}
                    {c.invalid_ohlc.length ? ` · ${c.invalid_ohlc.length} invalid OHLC` : ""}
                    {c.outliers.length ? ` · ${c.outliers.length} outlier move` : ""}
                  </span>
                </li>
              ))}
              <li className="flex flex-wrap items-center gap-x-2 py-1.5">
                <span className="min-w-0 flex-1 text-table text-text-1">Quotes ({h.quotes.tokens} tokens)</span>
                <Badge intent={INTENT[h.quotes.status]}>{h.quotes.status}</Badge>
                <span className="w-full font-mono text-micro text-text-3">
                  stale {h.quotes.stale} · crossed {h.quotes.crossed} · one-sided {h.quotes.one_sided} ·
                  wide {h.quotes.wide_spread} · no tick {h.quotes.no_tick}
                  {h.quotes.latency_ms_p50 != null ? ` · latency ${h.quotes.latency_ms_p50} ms` : ""}
                  {h.quotes.latency_ms_p95 != null ? ` (p95 ${h.quotes.latency_ms_p95})` : ""}
                </span>
              </li>
            </ul>
          </div>
        )}
      </DataSurface>
    </Panel>
  );
}
