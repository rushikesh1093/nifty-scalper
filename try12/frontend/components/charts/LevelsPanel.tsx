"use client";
import { useMarketStore, selectTick } from "@/lib/store";
import { formatPrice } from "@/lib/utils";
import { formatIst } from "@/lib/time";
import { useLevels } from "@/hooks/useLevels";
import { Panel } from "@/components/ui/Panel";
import { DataSurface } from "@/components/ui/DataSurface";
import { TableSkeleton } from "@/components/ui/Skeleton";
import { Badge } from "@/components/ui/Badge";
import { PriceText } from "@/components/ui/PriceText";
import { TIMEFRAME_LABEL } from "@/components/charts/ChartToolbar";
import { INDEX_TOKEN } from "@/components/charts/CandleChart";
import { LEVELS_MIN_BARS, type LevelKind, type PriceLevel } from "@/types/levels";

// Accessible equivalent of the chart's S/R overlay (design §6.6): every level the backend returned,
// grouped by kind, with strength, touches and distance from spot. Renders the API payload only.

const GROUPS: { kind: LevelKind; title: string; tone: string }[] = [
  { kind: "RESISTANCE", title: "Resistance", tone: "!text-red" },
  { kind: "PIVOT",      title: "Pivot",      tone: "!text-yellow" },
  { kind: "SUPPORT",    title: "Support",    tone: "!text-green" },
];

const WARNING_TEXT: Record<string, string> = {
  insufficient_candles:   "not enough bars at this timeframe",
  no_prev_day_snapshot:   "no previous-day snapshot (PDH/PDL/CPR unavailable)",
  spot_from_candle_close: "spot taken from the last candle close (no live LTP)",
  malformed_candles:      "some candle rows were skipped as malformed",
  no_today_candles:       "no candles today yet (ORB/day range unavailable)",
};

function StrengthBars({ value }: { value: number }) {
  const s = Math.min(5, Math.max(1, Math.round(value)));
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden="true" className="inline-flex items-end gap-px">
        {[1, 2, 3, 4, 5].map((i) => (
          <span key={i} className={i <= s ? "w-1 rounded-sm bg-text-2" : "w-1 rounded-sm bg-surface-3"} style={{ height: 4 + i * 2 }} />
        ))}
      </span>
      <span className="font-mono tabular-nums text-text-2">{s}/5</span>
    </span>
  );
}

function Distance({ price, spot }: { price: number; spot: number | null }) {
  if (spot === null || !(spot > 0)) return <span className="text-text-3">—</span>;
  const pts = price - spot;
  const pct = (pts / spot) * 100;
  const sign = pts > 0 ? "+" : pts < 0 ? "−" : "";
  return (
    <span
      className="inline-flex flex-col items-end font-mono tabular-nums text-text-2"
      aria-label={`${Math.abs(pts).toFixed(1)} points (${Math.abs(pct).toFixed(2)}%) ${pts >= 0 ? "above" : "below"} spot`}
    >
      <span aria-hidden="true">{sign}{formatPrice(Math.abs(pts), 1)}</span>
      <span aria-hidden="true" className="text-micro text-text-3">{sign}{Math.abs(pct).toFixed(2)}%</span>
    </span>
  );
}

function LevelRow({ l, spot }: { l: PriceLevel; spot: number | null }) {
  return (
    <tr data-level-price={l.price} data-level-kind={l.kind} data-level-source={l.source}>
      {/* `!static !normal-case …` undo the sticky/uppercase `.data-table th` header rule (higher specificity) */}
      <th scope="row" className="!static whitespace-nowrap !bg-transparent !pl-3 !text-table !font-medium !normal-case !tracking-normal !text-text-1">
        <span className="block">{l.label}</span>
        <span className="block text-micro font-normal text-text-3">{l.source === "SWING" ? "swing cluster" : "structural"}</span>
      </th>
      <td className="num"><PriceText value={l.price} className="text-text-1" /></td>
      <td><StrengthBars value={l.strength} /></td>
      <td className="num font-mono" title={l.last_touch_at ? `last touch ${formatIst(l.last_touch_at, { seconds: false, withDate: true })}` : undefined}>
        {l.touches}
      </td>
      <td className="num"><Distance price={l.price} spot={spot} /></td>
    </tr>
  );
}

export function LevelsPanel({ className }: { className?: string }) {
  const underlying = useMarketStore((s) => s.selectedUnderlying);
  const timeframe  = useMarketStore((s) => s.selectedInterval);
  const liveSpot   = useMarketStore(selectTick(INDEX_TOKEN[underlying]))?.ltp;
  const query = useLevels(underlying, timeframe);
  const insufficient = query.data?.meta.warnings.includes("insufficient_candles") ?? false;
  const emptyTitle = insufficient
    ? `Levels unavailable — ${query.data?.meta.resampled_bars ?? 0} of ${LEVELS_MIN_BARS} bars at this timeframe`
    : "No levels returned for this timeframe";

  return (
    <Panel
      title="Support / Resistance"
      meta={<Badge intent="neutral">{underlying} · {TIMEFRAME_LABEL[timeframe]}</Badge>}
      className={className}
      dense
    >
      <DataSurface
        query={query}
        isEmpty={(d) => !Array.isArray(d.levels) || d.levels.length === 0}
        emptyTitle={emptyTitle}
        emptyHint={insufficient ? "Candles still render; levels need more history at this timeframe. Try a shorter timeframe." : undefined}
        skeleton={<TableSkeleton rows={6} cols={5} />}
        className="min-h-[120px]"
        hideFreshness
      >
        {(data) => {
          const spot = typeof liveSpot === "number" && liveSpot > 0 ? liveSpot : data.spot > 0 ? data.spot : null;
          const warnings = data.meta.warnings.filter((w) => w !== "insufficient_candles");
          return (
            <div data-testid="levels-panel" data-level-count={data.levels.length} className="-mx-3 -mt-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2 text-micro text-text-3">
                <span>Spot <PriceText value={spot} className="text-text-1" /></span>
                <span>{data.meta.resampled_bars} bars · {data.meta.span_days}d · {data.meta.candle_source}</span>
                <span>as of {formatIst(data.as_of, { seconds: false })}</span>
              </div>
              <div className="max-h-[420px] overflow-y-auto">
                <table className="data-table [&_td]:!px-2 [&_th]:!px-2">
                  <caption className="sr-only">
                    Support and resistance levels for {data.underlying} at {TIMEFRAME_LABEL[timeframe]}, grouped by kind
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col" className="!pl-3">Level</th>
                      <th scope="col" className="num">Price</th>
                      <th scope="col">Strength</th>
                      <th scope="col" className="num"><abbr title="Touches" className="no-underline">Tch</abbr></th>
                      <th scope="col" className="num">From spot</th>
                    </tr>
                  </thead>
                  {GROUPS.map((g) => {
                    const rows = data.levels.filter((l) => l.kind === g.kind);
                    if (rows.length === 0) return null;
                    return (
                      <tbody key={g.kind}>
                        <tr>
                          <th scope="rowgroup" colSpan={5} className={`!static !py-1 !pl-3 ${g.tone}`}>
                            {g.title} <span className="font-normal text-text-3">({rows.length})</span>
                          </th>
                        </tr>
                        {rows.map((l) => <LevelRow key={`${l.source}|${l.price}`} l={l} spot={spot} />)}
                      </tbody>
                    );
                  })}
                </table>
              </div>
              {warnings.length > 0 && (
                <ul className="mt-2 space-y-0.5 px-3 text-micro text-yellow">
                  {warnings.map((w) => <li key={w}>Note: {WARNING_TEXT[w] ?? w}</li>)}
                </ul>
              )}
            </div>
          );
        }}
      </DataSurface>
    </Panel>
  );
}
