"use client";
import { cn } from "@/lib/utils";
import type { CandleInterval } from "@/types/market";

export const TIMEFRAMES: readonly CandleInterval[] = ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"];
export const TIMEFRAME_LABEL: Record<CandleInterval, string> = {
  ONE_MINUTE: "1m", THREE_MINUTE: "3m", FIVE_MINUTE: "5m", FIFTEEN_MINUTE: "15m",
};

const BTN =
  "inline-flex h-7 min-w-7 items-center justify-center rounded-md px-2 text-table font-semibold text-text-2 hover:bg-surface-3 hover:text-text-1 disabled:opacity-40";

export interface ChartToolbarProps {
  timeframe:     CandleInterval;
  onTimeframe:   (t: CandleInterval) => void;
  onFit:         () => void;
  onRealtime:    () => void;
  onZoomIn:      () => void;
  onZoomOut:     () => void;
  showLevels:    boolean;
  onToggleLevels: () => void;
  /** S/R overlay applies to index charts only (never drawn on an option's price scale). */
  levelsAvailable: boolean;
  disabled?:     boolean;
}

export function ChartToolbar({
  timeframe, onTimeframe, onFit, onRealtime, onZoomIn, onZoomOut,
  showLevels, onToggleLevels, levelsAvailable, disabled,
}: ChartToolbarProps) {
  return (
    <div role="toolbar" aria-label="Chart controls" className="flex flex-wrap items-center gap-2">
      <div role="group" aria-label="Timeframe" className="inline-flex rounded-lg border border-border bg-surface-2 p-0.5">
        {TIMEFRAMES.map((tf) => (
          <button
            key={tf}
            type="button"
            data-timeframe={tf}
            aria-pressed={timeframe === tf}
            onClick={() => onTimeframe(tf)}
            className={cn(
              "h-6 rounded-md px-2.5 text-table font-semibold",
              timeframe === tf ? "bg-accent/30 text-text-1" : "text-text-2 hover:text-text-1",
            )}
          >
            {TIMEFRAME_LABEL[tf]}
          </button>
        ))}
      </div>

      <div role="group" aria-label="Time scale" className="inline-flex items-center rounded-lg border border-border bg-surface-2 p-0.5">
        <button type="button" onClick={onZoomOut} disabled={disabled} className={BTN} aria-label="Zoom out" title="Zoom out">−</button>
        <button type="button" onClick={onZoomIn} disabled={disabled} className={BTN} aria-label="Zoom in" title="Zoom in">+</button>
        <button type="button" onClick={onFit} disabled={disabled} className={BTN} title="Fit all bars">Fit</button>
        <button type="button" onClick={onRealtime} disabled={disabled} className={BTN} title="Scroll to the latest bar">Live</button>
      </div>

      <button
        type="button"
        aria-pressed={showLevels && levelsAvailable}
        disabled={!levelsAvailable}
        onClick={onToggleLevels}
        title={levelsAvailable ? "Show support/resistance lines" : "S/R lines are drawn on the index chart only"}
        className={cn(
          "h-7 rounded-lg border px-2.5 text-table font-semibold disabled:opacity-40",
          showLevels && levelsAvailable
            ? "border-accent/50 bg-accent/20 text-text-1"
            : "border-border bg-surface-2 text-text-2 hover:text-text-1",
        )}
      >
        S/R lines
      </button>
    </div>
  );
}
