"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createChart, ColorType, CrosshairMode, LineStyle, TickMarkType,
  type CandlestickData, type CreatePriceLineOptions, type HistogramData, type IChartApi, type IPriceLine,
  type ISeriesApi, type LineWidth, type MouseEventParams, type Time, type UTCTimestamp,
} from "lightweight-charts";
import { useQuery } from "@tanstack/react-query";
import { getCandles } from "@/lib/api";
import { getPositions } from "@/lib/tradingApi";
import { queryKeys } from "@/lib/queryKeys";
import { useMarketStore, selectTick } from "@/lib/store";
import { getChartTheme, themeColor } from "@/lib/chartTheme";
import { formatIst } from "@/lib/time";
import { cn, formatPrice } from "@/lib/utils";
import { useTickFeed } from "@/hooks/useTickFeed";
import { useLevels } from "@/hooks/useLevels";
import { useStickyQueryError } from "@/hooks/useStickyQueryError";
import { useHydrated } from "@/hooks/useHydrated";
import { Panel } from "@/components/ui/Panel";
import { ChartSkeleton } from "@/components/ui/Skeleton";
import { ErrorState } from "@/components/ui/ErrorState";
import { EmptyState } from "@/components/ui/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { Freshness } from "@/components/ui/Freshness";
import { PriceText } from "@/components/ui/PriceText";
import { ChartToolbar, TIMEFRAME_LABEL } from "@/components/charts/ChartToolbar";
import { CrosshairReadout, type ReadoutBar } from "@/components/charts/CrosshairReadout";
import type { Candle, CandleInterval } from "@/types/market";
import type { PriceLevel } from "@/types/levels";
import type { Position } from "@/types/trading";

// Candlestick chart on lightweight-charts 4.2.3 — v4 API only (design §6.6). Levels drawn here come
// exclusively from GET /market/levels (useLevels); nothing in this file computes a price level.

export const INDEX_TOKEN: Record<"NIFTY" | "BANKNIFTY", string> = { NIFTY: "26000", BANKNIFTY: "26009" };
const INDEX_LABEL: Record<"NIFTY" | "BANKNIFTY", string> = { NIFTY: "NIFTY 50", BANKNIFTY: "BANK NIFTY" };

/** One bucket-size map for live-tick folding (replaces the nested ternary). */
const INTERVAL_SECONDS: Record<CandleInterval, number> = {
  ONE_MINUTE: 60, THREE_MINUTE: 180, FIVE_MINUTE: 300, FIFTEEN_MINUTE: 900,
};
const CANDLE_COUNT = 300;
const CHART_HEIGHT = 400;
const MIN_BAR_SPACING = 2;
const MAX_BAR_SPACING = 50;

interface LineHandle { line: IPriceLine; sig: string; title: string }
interface LineSpec { key: string; options: CreatePriceLineOptions }

interface SeriesData {
  bars:      CandlestickData<UTCTimestamp>[];
  volumes:   HistogramData<UTCTimestamp>[];
  hasVolume: boolean;
}

/** Ascending, de-duplicated by time (lightweight-charts throws on out-of-order/duplicate times). */
function toSeries(candles: Candle[], upColor: string, downColor: string): SeriesData {
  const byTime = new Map<number, Candle>();
  for (const c of Array.isArray(candles) ? candles : []) {
    const ms = Date.parse(c.timestamp);
    if (!Number.isFinite(ms)) continue;
    if (![c.open, c.high, c.low, c.close].every((v) => typeof v === "number" && Number.isFinite(v))) continue;
    byTime.set(Math.floor(ms / 1000), c); // later duplicate wins
  }
  const times = Array.from(byTime.keys()).sort((a, b) => a - b);
  const bars: CandlestickData<UTCTimestamp>[] = [];
  const volumes: HistogramData<UTCTimestamp>[] = [];
  let hasVolume = false;
  for (const t of times) {
    const c = byTime.get(t) as Candle;
    const time = t as UTCTimestamp;
    bars.push({ time, open: c.open, high: c.high, low: c.low, close: c.close });
    const v = typeof c.volume === "number" && Number.isFinite(c.volume) ? c.volume : 0;
    if (v > 0) hasVolume = true;
    volumes.push({ time, value: v, color: c.close >= c.open ? upColor : downColor });
  }
  return { bars, volumes, hasVolume };
}

const timeSec = (t: Time): number => (typeof t === "number" ? t : NaN);

function istTick(time: Time, type: TickMarkType): string {
  const s = timeSec(time);
  if (!Number.isFinite(s)) return "";
  const full = formatIst(s * 1000, { seconds: false, withDate: true }); // "06 Oct 14:20"
  return type === TickMarkType.Time || type === TickMarkType.TimeWithSeconds ? full.slice(7) : full.slice(0, 6);
}

const clampStrength = (s: number) => Math.min(5, Math.max(1, Math.round(Number.isFinite(s) ? s : 1)));

/** Visual encoding of a backend level (kind → colour family, strength → width + opacity, source → style). */
function levelLine(l: PriceLevel): LineSpec {
  const s = clampStrength(l.strength);
  const token = l.kind === "RESISTANCE" ? "red" : l.kind === "SUPPORT" ? "green" : "yellow";
  const lineWidth: LineWidth = s >= 5 ? 3 : s >= 3 ? 2 : 1;
  return {
    key: `${l.source}|${l.price}`,
    options: {
      price: l.price,
      color: themeColor(token, 0.4 + 0.15 * (s - 1)),
      lineWidth,
      lineStyle: l.source === "SWING" ? LineStyle.Dashed : LineStyle.Solid,
      axisLabelVisible: true,
      axisLabelColor: themeColor(token),
      axisLabelTextColor: themeColor("bg"),
      title: `${l.label} ${formatPrice(l.price)} ·${s}`,
    },
  };
}

function positionLines(positions: Position[]): LineSpec[] {
  const many = positions.length > 1;
  const out: LineSpec[] = [];
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? v : null);
  for (const p of positions) {
    const sfx = many ? ` #${p.id}` : "";
    const add = (kind: string, price: number | null, color: string, style: LineStyle, title: string) => {
      if (price === null) return;
      out.push({
        key: `pos:${p.id}:${kind}`,
        options: { price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title: `${title}${sfx}` },
      });
    };
    add("entry", num(p.entry), themeColor("text-2"), LineStyle.Solid, "ENTRY");
    add("sl", num(p.sl), themeColor("red"), LineStyle.Dashed, "SL");
    add("target", num(p.target), themeColor("green"), LineStyle.Dashed, "TGT");
    add("tsl", num(p.trailing_sl), themeColor("red"), LineStyle.Dotted, "TSL");
  }
  return out;
}

/** Diff price lines against the wanted set: remove vanished, add new, re-style changed. Never rebuild all. */
function syncLines(series: ISeriesApi<"Candlestick">, store: Map<string, LineHandle>, specs: LineSpec[]) {
  const wanted = new Map(specs.map((s) => [s.key, s]));
  store.forEach((h, key) => {
    if (!wanted.has(key)) {
      series.removePriceLine(h.line);
      store.delete(key);
    }
  });
  wanted.forEach((spec, key) => {
    const sig = JSON.stringify(spec.options);
    const title = spec.options.title ?? "";
    const have = store.get(key);
    if (!have) store.set(key, { line: series.createPriceLine(spec.options), sig, title });
    else if (have.sig !== sig) {
      have.line.applyOptions(spec.options);
      have.sig = sig;
      have.title = title;
    }
  });
}

export function CandleChart() {
  const underlying       = useMarketStore((s) => s.selectedUnderlying);
  const selectedToken    = useMarketStore((s) => s.selectedToken);
  const setSelectedToken = useMarketStore((s) => s.setSelectedToken);
  const timeframe        = useMarketStore((s) => s.selectedInterval);
  const setTimeframe     = useMarketStore((s) => s.setSelectedInterval);
  const optionChain      = useMarketStore((s) => s.optionChain);
  const hydrated = useHydrated();

  const spotToken    = INDEX_TOKEN[underlying];
  const displayToken = selectedToken ?? spotToken;
  const isIndex      = displayToken === spotToken;
  const seriesKey    = `${displayToken}|${timeframe}`;
  const strike       = isIndex ? undefined : optionChain?.strikes.find((s) => s.token === displayToken);
  const title        = isIndex ? `${INDEX_LABEL[underlying]} · spot` : strike?.symbol ?? `Token ${displayToken}`;

  const [showLevels, setShowLevels] = useState(true);

  // ── Data ────────────────────────────────────────────────────────────────────
  const candlesQ = useQuery({
    queryKey: queryKeys.candles(displayToken, timeframe, CANDLE_COUNT),
    queryFn:  () => getCandles(displayToken, timeframe, CANDLE_COUNT),
    refetchInterval: timeframe === "ONE_MINUTE" ? 10_000 : 30_000,
  });
  const candlesErr = useStickyQueryError(candlesQ);
  const levelsQ = useLevels(underlying, timeframe);
  const positionsParams = { is_open: true, limit: 100 };
  const positionsQ = useQuery({
    queryKey: queryKeys.positions(positionsParams),
    queryFn:  () => getPositions(positionsParams),
    refetchInterval: 5_000,
  });
  useTickFeed(displayToken);
  const liveTick = useMarketStore(selectTick(displayToken));

  // ── Chart objects (refs; nulled in cleanup, guarded by `disposed`) ──────────
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef     = useRef<IChartApi | null>(null);
  const candleRef    = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef    = useRef<ISeriesApi<"Histogram"> | null>(null);
  const disposedRef  = useRef(false);
  const srLinesRef   = useRef(new Map<string, LineHandle>());
  const spotLineRef  = useRef(new Map<string, LineHandle>());
  const posLinesRef  = useRef(new Map<string, LineHandle>());
  const barsRef      = useRef<CandlestickData<UTCTimestamp>[]>([]);
  const indexRef     = useRef(new Map<number, number>());
  const lastBarRef   = useRef<ReadoutBar | null>(null);
  const activeKeyRef = useRef<string | null>(null);
  const fittedRef    = useRef(false);

  const [chartEpoch, setChartEpoch] = useState(0);
  const [barCount, setBarCount]     = useState(0);
  const [hasVolume, setHasVolume]   = useState(false);
  const [lastBar, setLastBar]       = useState<ReadoutBar | null>(null);
  const [hoverBar, setHoverBar]     = useState<ReadoutBar | null>(null);
  const [renderError, setRenderError] = useState<unknown>(null);
  const [lineTitles, setLineTitles] = useState<string[]>([]);

  const publishTitles = useCallback(() => {
    const titles: string[] = [];
    for (const m of [srLinesRef.current, spotLineRef.current, posLinesRef.current]) m.forEach((h) => titles.push(h.title));
    setLineTitles((prev) => (prev.join("\u0000") === titles.join("\u0000") ? prev : titles));
  }, []);

  // Mount: create the chart once the container has a width (StrictMode-safe).
  useEffect(() => {
    disposedRef.current = false;
    const el = containerRef.current;
    if (!el) return;
    let chart: IChartApi | null = null;
    let warned = false;

    const onCrosshair = (p: MouseEventParams) => {
      const candle = candleRef.current;
      if (disposedRef.current || !candle) return;
      const raw = p.time !== undefined ? p.seriesData.get(candle) : undefined;
      if (!raw || !("open" in raw)) { setHoverBar(null); return; }
      const d = raw as CandlestickData;
      const t = timeSec(d.time);
      const idx = indexRef.current.get(t);
      const prev = idx !== undefined && idx > 0 ? barsRef.current[idx - 1] : idx === undefined ? barsRef.current.at(-1) : undefined;
      const vol = volumeRef.current ? (p.seriesData.get(volumeRef.current) as HistogramData | undefined) : undefined;
      setHoverBar({
        time: t, open: d.open, high: d.high, low: d.low, close: d.close,
        volume: vol && typeof vol.value === "number" ? vol.value : null,
        prevClose: prev ? prev.close : null,
      });
    };

    const init = () => {
      if (chart || disposedRef.current) return;
      if (el.clientWidth === 0) {
        if (!warned) { warned = true; console.warn("[chart] container has no width yet; waiting for resize"); }
        return;
      }
      const theme = getChartTheme();
      const lw = theme.lightweight;
      chart = createChart(el, {
        width:  el.clientWidth,
        height: el.clientHeight || CHART_HEIGHT,
        layout: { ...lw.layout, background: { type: ColorType.Solid, color: lw.layout.background.color } },
        grid: lw.grid,
        rightPriceScale: { ...lw.rightPriceScale, scaleMargins: { top: 0.08, bottom: 0.2 } },
        timeScale: { ...lw.timeScale, timeVisible: true, secondsVisible: false, rightOffset: 4, tickMarkFormatter: istTick },
        crosshair: { mode: CrosshairMode.Normal, ...lw.crosshair },
        localization: { timeFormatter: (t: Time) => formatIst(timeSec(t) * 1000, { seconds: false, withDate: true }) },
      });
      const up = theme.color("up"), down = theme.color("down");
      candleRef.current = chart.addCandlestickSeries({
        upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
        priceLineVisible: false,
      });
      volumeRef.current = chart.addHistogramSeries({
        priceScaleId: "volume", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
      });
      chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      chart.subscribeCrosshairMove(onCrosshair);
      chartRef.current = chart;
      setChartEpoch((n) => n + 1);
    };

    const ro = new ResizeObserver(() => {
      if (disposedRef.current) return;
      if (!chart) init();
      else chart.applyOptions({ width: el.clientWidth, height: el.clientHeight || CHART_HEIGHT });
    });
    ro.observe(el);
    init();

    const srLines = srLinesRef.current, spotLine = spotLineRef.current, posLines = posLinesRef.current;
    return () => {
      disposedRef.current = true;
      ro.disconnect();
      if (chart) {
        chart.unsubscribeCrosshairMove(onCrosshair);
        chart.remove();
      }
      chart = null;
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      srLines.clear();
      spotLine.clear();
      posLines.clear();
      barsRef.current = [];
      indexRef.current = new Map();
      lastBarRef.current = null;
      activeKeyRef.current = null;
      fittedRef.current = false;
    };
  }, []);

  // Candles → series. A token/timeframe change resets the series (never appends across scales).
  const candles = candlesQ.data;
  useEffect(() => {
    const chart = chartRef.current, candle = candleRef.current, volume = volumeRef.current;
    if (disposedRef.current || !chart || !candle || !volume) return;
    if (activeKeyRef.current !== seriesKey) {
      candle.setData([]);
      volume.setData([]);
      barsRef.current = [];
      indexRef.current = new Map();
      lastBarRef.current = null;
      activeKeyRef.current = seriesKey;
      fittedRef.current = false;
      setLastBar(null);
      setHoverBar(null);
      setBarCount(0);
      setHasVolume(false);
      setRenderError(null);
    }
    if (!candles) return;
    try {
      const { bars, volumes, hasVolume: hv } = toSeries(candles, themeColor("up", 0.45), themeColor("down", 0.45));
      candle.setData(bars);
      volume.setData(hv ? volumes : []);
      barsRef.current = bars;
      indexRef.current = new Map(bars.map((b, i) => [b.time as number, i]));
      const last = bars.at(-1);
      const lastVol = volumes.at(-1);
      const rb: ReadoutBar | null = last
        ? {
            time: last.time as number, open: last.open, high: last.high, low: last.low, close: last.close,
            volume: hv && lastVol ? lastVol.value : null, prevClose: bars.length > 1 ? bars[bars.length - 2].close : null,
          }
        : null;
      lastBarRef.current = rb;
      setLastBar(rb);
      setBarCount(bars.length);
      setHasVolume(hv);
      setRenderError(null);
      if (!fittedRef.current && bars.length) {
        chart.timeScale().fitContent();
        fittedRef.current = true;
      }
    } catch (e) {
      console.error("[chart] setData failed", e);
      candle.setData([]);
      volume.setData([]);
      barsRef.current = [];
      lastBarRef.current = null;
      setBarCount(0);
      setRenderError(e);
    }
  }, [candles, seriesKey, chartEpoch]);

  // Live tick → fold into the forming bar; clamped so update() never goes behind the last bar.
  useEffect(() => {
    const candle = candleRef.current, last = lastBarRef.current;
    if (disposedRef.current || !candle || !last || activeKeyRef.current !== seriesKey) return;
    const ltp = liveTick?.ltp;
    if (typeof ltp !== "number" || !Number.isFinite(ltp) || ltp <= 0) return;
    const step = INTERVAL_SECONDS[timeframe];
    const bucket = Math.floor(Date.now() / 1000 / step) * step;
    if (bucket < last.time) return;
    const next: ReadoutBar = bucket === last.time
      ? { ...last, high: Math.max(last.high, ltp), low: Math.min(last.low, ltp), close: ltp }
      : { time: bucket, open: ltp, high: ltp, low: ltp, close: ltp, volume: null, prevClose: last.close };
    try {
      candle.update({ time: next.time as UTCTimestamp, open: next.open, high: next.high, low: next.low, close: next.close });
      lastBarRef.current = next;
      setLastBar(next);
    } catch (e) {
      console.error("[chart] live update rejected", e);
    }
  }, [liveTick, seriesKey, timeframe, chartEpoch]);

  // Backend S/R levels → price lines, diffed by source|price. Index chart only.
  const levels = levelsQ.data;
  useEffect(() => {
    const candle = candleRef.current;
    if (disposedRef.current || !candle) return;
    const ok = isIndex && showLevels && levels && levels.token === spotToken && levels.interval === timeframe;
    syncLines(candle, srLinesRef.current, ok ? levels.levels.map(levelLine) : []);
    publishTitles();
  }, [levels, isIndex, showLevels, spotToken, timeframe, chartEpoch, publishTitles]);

  // Spot line (index chart only), updated in place from the live tick, else the levels payload's spot.
  const spotPrice = isIndex ? (liveTick?.ltp ?? (levels?.token === spotToken ? levels.spot : null)) : null;
  useEffect(() => {
    const candle = candleRef.current;
    if (disposedRef.current || !candle) return;
    const specs: LineSpec[] = typeof spotPrice === "number" && spotPrice > 0
      ? [{
          key: "spot",
          options: {
            price: spotPrice, color: themeColor("accent"), lineWidth: 1, lineStyle: LineStyle.LargeDashed,
            axisLabelVisible: true, axisLabelColor: themeColor("accent"), axisLabelTextColor: themeColor("text-1"), title: "SPOT",
          },
        }]
      : [];
    syncLines(candle, spotLineRef.current, specs);
    publishTitles();
  }, [spotPrice, chartEpoch, publishTitles]);

  // Open positions on the displayed contract → entry / SL / TGT / TSL lines.
  const positions = Array.isArray(positionsQ.data) ? positionsQ.data : null;
  const onChart = positions ? positions.filter((p) => p.token === displayToken) : [];
  const offChart = positions ? positions.filter((p) => p.token !== displayToken) : [];
  const onChartKey = onChart.map((p) => `${p.id}:${p.entry}:${p.sl}:${p.target}:${p.trailing_sl}`).join("|");
  const onChartRef = useRef(onChart);
  onChartRef.current = onChart;
  useEffect(() => {
    const candle = candleRef.current;
    if (disposedRef.current || !candle) return;
    syncLines(candle, posLinesRef.current, positionLines(onChartRef.current));
    publishTitles();
  }, [onChartKey, chartEpoch, publishTitles]);

  // ── Controls ────────────────────────────────────────────────────────────────
  const zoom = (factor: number) => {
    const ts = chartRef.current?.timeScale();
    if (!ts || disposedRef.current) return;
    const cur = ts.options().barSpacing;
    ts.applyOptions({ barSpacing: Math.min(MAX_BAR_SPACING, Math.max(MIN_BAR_SPACING, cur * factor)) });
  };
  const fit = () => { if (!disposedRef.current) chartRef.current?.timeScale().fitContent(); };
  const realtime = () => { if (!disposedRef.current) chartRef.current?.timeScale().scrollToRealTime(); };

  // ── Overlay state ───────────────────────────────────────────────────────────
  const hasData = candles !== undefined;
  const state: "loading" | "error" | "empty" | "ready" =
    !hydrated ? "loading"
    : renderError ? "error"
    : candlesErr.isError && !hasData ? "error"
    : !hasData ? "loading"
    : !Array.isArray(candles) || candles.length === 0 ? "empty"
    : "ready";
  const retry = () => { setRenderError(null); void candlesQ.refetch(); };

  return (
    <Panel
      title="Chart"
      meta={
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-table font-semibold text-text-1" data-testid="chart-instrument">{title}</span>
          <Badge intent="neutral">{TIMEFRAME_LABEL[timeframe]}</Badge>
          {!isIndex && (
            <button
              type="button"
              onClick={() => setSelectedToken(null)}
              className="rounded border border-border-2 px-2 py-0.5 text-micro font-semibold text-text-2 hover:text-text-1"
            >
              Back to {underlying}
            </button>
          )}
        </span>
      }
      actions={hydrated && hasData ? <Freshness dataUpdatedAt={candlesQ.dataUpdatedAt} isStale={candlesErr.isError} isFetching={candlesQ.isFetching} /> : undefined}
      flush
    >
      <div
        data-testid="candle-chart"
        data-token={displayToken}
        data-timeframe={timeframe}
        data-state={state}
        data-bars={barCount}
        data-last-bar={lastBar ? new Date(lastBar.time * 1000).toISOString() : ""}
        data-price-lines={JSON.stringify(lineTitles)}
        className="space-y-2 p-3"
      >
        <ChartToolbar
          timeframe={timeframe}
          onTimeframe={setTimeframe}
          onFit={fit}
          onRealtime={realtime}
          onZoomIn={() => zoom(1.25)}
          onZoomOut={() => zoom(0.8)}
          showLevels={showLevels}
          onToggleLevels={() => setShowLevels((v) => !v)}
          levelsAvailable={isIndex}
          disabled={state !== "ready"}
        />
        <CrosshairReadout bar={hoverBar ?? lastBar} hovering={hoverBar !== null} showVolume={hasVolume} />

        {hydrated && candlesErr.isError && hasData && (
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded-md border border-yellow/30 bg-yellow/10 px-3 py-1.5 text-table text-yellow">
            <span>Stale — last updated {formatIst(candlesQ.dataUpdatedAt)}, refresh failing</span>
            <button type="button" onClick={retry} className="ml-auto rounded border border-yellow/40 px-2 py-0.5 font-semibold hover:bg-yellow/10">
              Retry
            </button>
          </div>
        )}

        {/* The container is always mounted so the mount effect can create the chart; states overlay it. */}
        <div className="relative" style={{ height: CHART_HEIGHT }}>
          <div ref={containerRef} className="absolute inset-0" aria-hidden="true" />
          {state === "loading" && (
            <div className="absolute inset-0 z-10 bg-surface">
              <ChartSkeleton height={CHART_HEIGHT} />
            </div>
          )}
          {state === "error" && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-surface p-6">
              <ErrorState
                error={renderError ?? candlesErr.error}
                title="Chart data unavailable"
                onRetry={retry}
                retrying={candlesQ.isFetching}
                className="w-full max-w-lg"
              />
            </div>
          )}
          {state === "empty" && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-surface">
              <EmptyState title="No candles for this instrument yet" hint="Candles appear once the feed has recorded bars at this timeframe." />
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 text-micro text-text-3">
          <span>
            {state === "ready" && !hasVolume && (isIndex ? "Volume not published for index instruments." : "No volume reported for these bars.")}
          </span>
          <span>
            {isIndex
              ? "S/R lines: red resistance · green support · amber pivot; solid structural, dashed swing; width = strength."
              : "S/R levels are index levels — listed beside the chart, not drawn on the option's price scale."}
          </span>
        </div>

        {hydrated && offChart.length > 0 && (
          <div className="rounded-md border border-border bg-surface-2 p-2" data-testid="offchart-positions">
            <div className="mb-1 text-micro font-semibold uppercase tracking-wider text-text-3">
              Open positions on other contracts (levels on their own price scale)
            </div>
            <ul className="space-y-1">
              {offChart.map((p) => (
                <li key={p.id} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-table">
                  <span className="font-mono text-text-1">{p.symbol}</span>
                  <span className="text-text-3">Entry <PriceText value={p.entry} className="text-text-1" /></span>
                  <span className="text-text-3">SL <PriceText value={p.sl} className="text-red" /></span>
                  <span className="text-text-3">TGT <PriceText value={p.target} className="text-green" /></span>
                  {p.trailing_sl != null && <span className="text-text-3">TSL <PriceText value={p.trailing_sl} className="text-red" /></span>}
                  {p.token && (
                    <button
                      type="button"
                      onClick={() => setSelectedToken(p.token)}
                      className={cn("ml-auto rounded border border-accent/40 px-2 py-0.5 text-micro font-semibold text-accent hover:bg-accent/15")}
                    >
                      Chart it
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  );
}
