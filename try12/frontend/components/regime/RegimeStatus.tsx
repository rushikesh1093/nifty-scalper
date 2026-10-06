"use client";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getCurrentRegime } from "@/lib/api";
import { ApiError, errorMessage } from "@/lib/http";
import { queryKeys } from "@/lib/queryKeys";
import { useDiagnostics } from "@/hooks/useDiagnostics";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";
import { cn } from "@/lib/utils";

// StatusBar regime chip (finding 4). A failed fetch is an error chip with Retry — never "UNKNOWN".
const UNDERLYING = "NIFTY";

const REGIME_TONE: Record<string, string> = {
  BULL_TREND_NORMAL_VOL:  "text-green",
  BREAKOUT_EVENT_BULLISH: "text-green",
  BEAR_TREND_NORMAL_VOL:  "text-red",
  BREAKOUT_EVENT_BEARISH: "text-red",
  HIGH_VOL_CHOP:          "text-red",
  TREND_HIGH_VOL:         "text-yellow",
  LOW_VOL_COMPRESSION:    "text-yellow",
  RANGE_NORMAL_VOL:       "text-yellow",
  REGIME_TRANSITION:      "text-yellow",
  EXPIRY_DAY:             "text-text-2",
};

function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  if (mins < 60) return `${mins}:${secs.toString().padStart(2, "0")}`;
  const hours = Math.floor(mins / 60);
  return `${hours}:${(mins % 60).toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
}

const CELL = "flex items-center gap-1.5 px-3 border-r border-border";

export function RegimeStatus() {
  const query = useQuery({
    queryKey: queryKeys.regimeCurrent(UNDERLYING),
    queryFn: () => getCurrentRegime(UNDERLYING),
    refetchInterval: 10_000,
  });
  const { data, isFetching, refetch, dataUpdatedAt } = query;
  const { error, isError } = useStickyQueryError(query);
  const diag = useDiagnostics();

  // One 1 s ticker for the component's lifetime (StrictMode-safe: cleared on unmount).
  // `now` stays null until mount so the first (SSR) render is neutral.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (isError) {
    const detail = error instanceof ApiError ? error.detail : null;
    return (
      <div className={CELL} data-testid="regime-chip" data-state="error">
        <span role="alert" className="flex items-center gap-1.5 text-red" title={detail ?? errorMessage(error)}>
          <span aria-hidden="true" className="h-1.5 w-1.5 shrink-0 rounded-full bg-red" />
          Regime: unavailable
        </span>
        <button
          type="button"
          onClick={() => { void refetch(); }}
          disabled={isFetching}
          aria-label="Retry loading the current regime"
          className="rounded border border-red/40 bg-red/10 px-1.5 py-px text-micro font-semibold text-red hover:bg-red/20 disabled:opacity-50"
        >
          {isFetching ? "Retrying…" : "Retry"}
        </button>
      </div>
    );
  }

  if (!data) {
    return (
      <div className={CELL} data-testid="regime-chip" data-state="pending">
        <span className="text-text-3">Regime —</span>
      </div>
    );
  }

  if (data.source === "absent") {
    return (
      <div className={CELL} data-testid="regime-chip" data-state="absent">
        <span aria-live="polite" className="flex items-center gap-1.5 text-text-2">
          <span aria-hidden="true" className="h-1.5 w-1.5 shrink-0 rounded-full bg-text-3" />
          No regime yet
        </span>
      </div>
    );
  }

  const elapsed = now === null ? null : data.seconds_in_regime + Math.max(0, (now - dataUpdatedAt) / 1000);
  const allows = diag.data?.underlyings.find((u) => u.underlying === UNDERLYING)?.regime_allows_trading;
  const tone = REGIME_TONE[data.regime] ?? "text-text-2";

  return (
    <div className={CELL} data-testid="regime-chip" data-state="ok">
      {/* Only the regime + policy are live-announced; the per-second duration is not. */}
      <span aria-live="polite" className="flex items-center gap-1.5">
        <span aria-hidden="true" className={cn("h-1.5 w-1.5 shrink-0 rounded-full bg-current", tone)} />
        <span className={cn("font-medium", tone)}>
          <span className="sr-only">{UNDERLYING} regime: </span>
          {data.regime.replace(/_/g, " ")}
        </span>
        {allows === true && <span className="text-micro text-green">permits trading</span>}
        {allows === false && <span className="text-micro text-red">blocks trading</span>}
      </span>
      <span className="font-mono text-micro text-text-3" title={data.since ? `since ${data.since}` : undefined}>
        {elapsed === null ? "—" : formatDuration(elapsed)}
      </span>
    </div>
  );
}
