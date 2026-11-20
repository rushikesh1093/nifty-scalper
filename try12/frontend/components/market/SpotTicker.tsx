"use client";
import { useQuery } from "@tanstack/react-query";
import { getCandles, getTick } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { useMarketStore, selectTick } from "@/lib/store";
import { formatIst } from "@/lib/time";
import { cn } from "@/lib/utils";
import { useTickFeed, type TickSocketStatus } from "@/hooks/useTickFeed";
import { useHydrated } from "@/hooks/useHydrated";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";
import { PriceText, DeltaText } from "@/components/ui/PriceText";
import { ErrorState } from "@/components/ui/ErrorState";
import { ConnectionChip, type ConnectionStatus } from "@/components/ui/ConnectionChip";
import type { Candle, FeedStatusKind } from "@/types/market";

const SPOTS = [
  { token: "26000", label: "NIFTY 50" },
  { token: "26009", label: "BANK NIFTY" },
] as const;

const SOCKET_TEXT: Record<TickSocketStatus, string> = {
  idle: "", connecting: "connecting…", live: "live", reconnecting: "feed disconnected — reconnecting", down: "feed disconnected",
};

/** Tick `ts` is the backend's naive-UTC received_at; mark it UTC so the browser zone never applies. */
function utcIso(ts: string): string {
  return /([zZ]|[+-]\d{2}:?\d{2})$/.test(ts) ? ts : `${ts}Z`;
}

/** High and low of the newest trading day in *candles* (timestamps carry their own offset). */
function todayRange(candles?: Candle[]): { high: number; low: number } | null {
  if (!candles?.length) return null;
  const day = candles[candles.length - 1].timestamp.slice(0, 10);
  const today = candles.filter((c) => c.timestamp.slice(0, 10) === day && c.high > 0 && c.low > 0);
  if (!today.length) return null;
  return { high: Math.max(...today.map((c) => c.high)), low: Math.min(...today.map((c) => c.low)) };
}

function SpotCard({ token, label }: { token: string; label: string }) {
  const hydrated = useHydrated();
  const socket = useTickFeed(token);
  const live = useMarketStore(selectTick(token));
  // Last cached tick from Redis (GET /market/tick) until the socket delivers one — so the card shows
  // the real last value outside market hours instead of a permanent skeleton.
  const snap = useQuery({
    queryKey: queryKeys.tick(token),
    queryFn: () => getTick(token),
    refetchInterval: live ? false : 15_000,
  });
  const snapErr = useStickyQueryError(snap);
  const tick = live ?? snap.data;
  const dimmed = socket === "reconnecting" || socket === "down";
  // Index ticks carry high/low = 0 (the feed's LTP mode), so the day's range comes from
  // today's 1-min candles instead.
  const candles = useQuery({
    queryKey: ["spot-day-range", token],
    queryFn: () => getCandles(token, "ONE_MINUTE", 400),
    refetchInterval: 60_000,
  });
  const dayRange = todayRange(candles.data);

  const ltp = typeof tick?.ltp === "number" ? tick.ltp : null;
  // Index ticks in LTP mode carry high/low = 0: show "—", not a zero price.
  const high = typeof tick?.high === "number" && tick.high > 0 ? tick.high : dayRange?.high ?? null;
  const low  = typeof tick?.low === "number" && tick.low > 0 ? tick.low : dayRange?.low ?? null;
  const prevClose = typeof tick?.close === "number" && tick.close > 0 ? tick.close : null;
  const change = ltp !== null && prevClose !== null ? ltp - prevClose : null;
  const pct = change !== null && prevClose ? (change / prevClose) * 100 : null;

  return (
    <section
      aria-label={`${label} spot`}
      data-testid={`spot-${token}`}
      data-socket={socket}
      className={cn("panel flex min-w-[260px] flex-1 items-center gap-5 px-4 py-3 sm:flex-none", dimmed && "opacity-60")}
    >
      <div className="min-w-0">
        <div className="text-micro font-semibold uppercase tracking-wider text-text-3">{label}</div>
        <div aria-live="polite" aria-atomic="true" className="mt-0.5">
          {!hydrated || (ltp === null && !snapErr.isError) ? (
            <div role="status" className="shimmer h-7 w-32"><span className="sr-only">Loading {label}…</span></div>
          ) : ltp === null ? (
            <ErrorState compact error={snapErr.error} onRetry={() => void snap.refetch()} />
          ) : (
            <PriceText value={ltp} className={cn("text-hero-lg font-bold leading-none", change !== null && change < 0 ? "text-down" : change !== null && change > 0 ? "text-up" : "text-text-1")} />
          )}
        </div>
      </div>

      {hydrated && ltp !== null && (
        <div className="ml-auto text-right">
          <div className="flex items-center justify-end gap-2 text-body font-semibold">
            <DeltaText value={change} />
            <DeltaText value={pct} suffix="%" noGlyph />
          </div>
          <div className="mt-0.5 font-mono text-micro text-text-3">
            H <PriceText value={high} className="text-text-2" /> · L <PriceText value={low} className="text-text-2" />
          </div>
          <div className="mt-0.5 text-micro text-text-3">
            {live || dimmed ? SOCKET_TEXT[socket] : `last snapshot${tick?.ts ? ` ${formatIst(utcIso(tick.ts))}` : ""}`}
          </div>
        </div>
      )}
    </section>
  );
}

const FEED_CHIP: Record<FeedStatusKind, { status: ConnectionStatus; label: string }> = {
  unknown:      { status: "polling",      label: "ticks —" },
  connecting:   { status: "reconnecting", label: "ticks connecting" },
  live:         { status: "live",         label: "ticks live" },
  reconnecting: { status: "reconnecting", label: "ticks reconnecting" },
  down:         { status: "error",        label: "feed disconnected" },
};

export function SpotTicker() {
  const feedStatus = useMarketStore((s) => s.feedStatus);
  const hydrated = useHydrated();
  const chip = FEED_CHIP[hydrated ? feedStatus : "unknown"];
  return (
    <div className="flex flex-wrap items-center gap-3">
      {SPOTS.map((s) => <SpotCard key={s.token} token={s.token} label={s.label} />)}
      <div className="ml-auto" data-testid="tick-feed-chip" data-feed-status={hydrated ? feedStatus : "unknown"}>
        <ConnectionChip
          status={chip.status}
          label={chip.label}
          title="Browser tick sockets to the backend (/market/ws). Prices dim when disconnected."
        />
      </div>
    </div>
  );
}
