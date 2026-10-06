"use client";
import { formatIst } from "@/lib/time";
import { formatNumber, formatPrice } from "@/lib/utils";
import { DeltaText } from "@/components/ui/PriceText";

export interface ReadoutBar {
  /** epoch seconds */
  time:   number;
  open:   number;
  high:   number;
  low:    number;
  close:  number;
  volume: number | null;
  /** Previous bar's close, for the change figure (null for the first bar). */
  prevClose: number | null;
}

// Text OHLC strip for the hovered bar (falls back to the last bar). Rendered as DOM text, not a
// canvas tooltip, so it is reachable by screen readers (design §6.6).
export function CrosshairReadout({ bar, hovering, showVolume }: { bar: ReadoutBar | null; hovering: boolean; showVolume: boolean }) {
  if (!bar) {
    return <div aria-live="polite" className="h-5 text-table text-text-3">No bar data</div>;
  }
  const ref = bar.prevClose ?? bar.open;
  const change = bar.close - ref;
  const pct = ref ? (change / ref) * 100 : null;
  const cell = (label: string, v: number) => (
    <span className="whitespace-nowrap">
      <span className="text-text-3">{label}</span>{" "}
      <span className="font-mono tabular-nums text-text-1">{formatPrice(v)}</span>
    </span>
  );
  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      data-testid="crosshair-readout"
      className="flex min-h-5 flex-wrap items-center gap-x-3 gap-y-0.5 text-table"
    >
      <span className="font-mono tabular-nums text-text-2">
        {formatIst(bar.time * 1000, { seconds: false, withDate: true })}
        <span className="sr-only">{hovering ? " (hovered bar)" : " (latest bar)"}</span>
      </span>
      {cell("O", bar.open)}
      {cell("H", bar.high)}
      {cell("L", bar.low)}
      {cell("C", bar.close)}
      <span className="whitespace-nowrap">
        <DeltaText value={change} /> <DeltaText value={pct} suffix="%" noGlyph className="ml-1" />
      </span>
      {showVolume && bar.volume != null && (
        <span className="whitespace-nowrap">
          <span className="text-text-3">Vol</span>{" "}
          <span className="font-mono tabular-nums text-text-1">{formatNumber(bar.volume)}</span>
        </span>
      )}
    </div>
  );
}
