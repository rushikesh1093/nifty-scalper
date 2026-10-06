"use client";
import { useEffect, useRef, useState } from "react";
import { createChart, ColorType, IChartApi, ISeriesApi, CandlestickData, Time } from "lightweight-charts";
import { useQuery } from "@tanstack/react-query";
import { getCandles } from "@/lib/api";
import { useMarketStore } from "@/lib/store";
import type { Candle } from "@/types/market";

const INTERVALS = ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"] as const;
type Interval = typeof INTERVALS[number];
const IV_LABEL: Record<Interval, string> = {
  ONE_MINUTE: "1m", THREE_MINUTE: "3m", FIVE_MINUTE: "5m", FIFTEEN_MINUTE: "15m",
};

function toCandle(c: Candle): CandlestickData {
  return { time: (new Date(c.timestamp).getTime() / 1000) as Time, open: c.open, high: c.high, low: c.low, close: c.close };
}

export function CandleChart({ token, symbol }: { token?: string | null; symbol?: string }) {
  const selectedToken = useMarketStore((s) => s.selectedToken);
  const activeToken   = token ?? selectedToken;
  const [interval, setInterval] = useState<Interval>("ONE_MINUTE");

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef     = useRef<IChartApi | null>(null);
  const seriesRef    = useRef<ISeriesApi<"Candlestick"> | null>(null);

  const { data: candles, isLoading } = useQuery({
    queryKey:        ["candles", activeToken, interval],
    queryFn:         () => getCandles(activeToken!, interval, 300),
    enabled:         !!activeToken,
    refetchInterval: interval === "ONE_MINUTE" ? 10000 : 30000,
    staleTime:       5000,
  });

  // Init chart once
  useEffect(() => {
    if (!containerRef.current) return;
    chartRef.current = createChart(containerRef.current, {
      layout:   { background: { type: ColorType.Solid, color: "#0e1118" }, textColor: "#9aa3b8" },
      grid:     { vertLines: { color: "#1b1f2e" }, horzLines: { color: "#1b1f2e" } },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#232635" },
      timeScale: { borderColor: "#232635", timeVisible: true, secondsVisible: false },
      width:  containerRef.current.clientWidth,
      height: 340,
    });
    seriesRef.current = chartRef.current.addCandlestickSeries({
      upColor: "#22d3a0", downColor: "#f43f5e",
      borderUpColor: "#22d3a0", borderDownColor: "#f43f5e",
      wickUpColor:   "#22d3a0", wickDownColor:   "#f43f5e",
    });
    const ro = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current)
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
    });
    ro.observe(containerRef.current);
    return () => { ro.disconnect(); chartRef.current?.remove(); };
  }, []);

  useEffect(() => {
    if (!candles || !seriesRef.current) return;
    const data = candles.map(toCandle).sort((a, b) => (a.time as number) - (b.time as number));
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  // Keep a ref to the latest candles so the tick effect always sees fresh data
  // without needing candles in its dep array (which would cause excessive re-runs)
  const candlesRef = useRef(candles);
  useEffect(() => { candlesRef.current = candles; }, [candles]);

  // Subscribe to only the active token's tick — not the whole ticks map.
  // This prevents the chart from re-rendering on unrelated ticks.
  const activeTick = useMarketStore((s) => activeToken ? s.ticks[activeToken] : null);
  useEffect(() => {
    if (!activeToken || !seriesRef.current || !candlesRef.current?.length) return;
    const tick = activeTick;
    if (!tick?.ltp) return;
    const last   = candlesRef.current[candlesRef.current.length - 1];
    // Use the current wall-clock minute bucket so that a new candle is created
    // at the correct time instead of always overwriting the last candle's bar.
    const nowSec     = Math.floor(Date.now() / 1000);
    const bucketSec  = interval === "ONE_MINUTE"      ? 60
                     : interval === "THREE_MINUTE"    ? 180
                     : interval === "FIVE_MINUTE"     ? 300
                     : 900;  // FIFTEEN_MINUTE
    const currentBucket = Math.floor(nowSec / bucketSec) * bucketSec as Time;
    const lastTs = Math.floor(new Date(last.timestamp).getTime() / 1000);
    // If the current bucket is newer than the last candle, create a new bar
    const barTime = (currentBucket as number) > lastTs ? currentBucket : lastTs as Time;
    seriesRef.current.update({
      time:  barTime,
      open:  (currentBucket as number) > lastTs ? tick.ltp : last.open,
      high:  (currentBucket as number) > lastTs ? tick.ltp : Math.max(last.high, tick.ltp),
      low:   (currentBucket as number) > lastTs ? tick.ltp : Math.min(last.low,  tick.ltp),
      close: tick.ltp,
    });
  }, [activeTick, activeToken, interval]);

  if (!activeToken) {
    return (
      <div className="rounded-xl flex items-center justify-center h-[380px] text-[11px]"
        style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-3)" }}>
        Click a strike to view its chart
      </div>
    );
  }

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: "var(--surface)", border: "1px solid var(--border)" }}>
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
        <span className="font-mono text-[12px] font-semibold" style={{ color: "var(--text-1)" }}>
          {symbol ?? activeToken}
        </span>
        {/* Interval selector */}
        <div className="flex gap-1 ml-auto p-0.5 rounded-lg" style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          {INTERVALS.map(iv => (
            <button key={iv} onClick={() => setInterval(iv)}
              className="px-2.5 py-1 rounded-md text-[10px] font-semibold transition-all"
              style={{
                background: interval === iv ? "var(--accent)" : "transparent",
                color:      interval === iv ? "#fff" : "var(--text-3)",
              }}>
              {IV_LABEL[iv]}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center h-[340px] text-[11px]" style={{ color: "var(--text-3)" }}>
          Loading chart…
        </div>
      ) : (
        <div ref={containerRef} className="w-full" />
      )}
    </div>
  );
}
