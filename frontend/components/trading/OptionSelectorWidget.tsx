"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { selectOption } from "@/lib/tradingApi";
import { useMarketStore } from "@/lib/store";
import { formatPrice, formatNumber, cn } from "@/lib/utils";
import type { OptionCandidate } from "@/types/trading";

export function OptionSelectorWidget() {
  const { selectedUnderlying, ticks } = useMarketStore();
  const [direction, setDirection] = useState<"BUY" | "SELL">("BUY");
  const [results, setResults] = useState<OptionCandidate[]>([]);

  const spotToken = selectedUnderlying === "NIFTY" ? "26000" : "26009";
  const spotLtp   = ticks[spotToken]?.ltp || 0;
  const optionType = direction === "BUY" ? "CE" : "PE";

  const query = useMutation({
    mutationFn: () => selectOption({ direction, underlying: selectedUnderlying, spot_price: spotLtp }),
    onSuccess: (data) => setResults(data),
  });

  return (
    <div className="bg-surface border border-border rounded-xl p-4 space-y-3">
      <div>
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
            Option Selector Preview
          </h3>
          <span className="text-[10px] text-gray-600 bg-gray-800 px-2 py-0.5 rounded">
            Research only
          </span>
        </div>
        <p className="text-[10px] text-gray-600 leading-relaxed">
          Strategies auto-select the best option when they fire a signal.
          Use this to preview scoring. No trade is placed here.
        </p>
      </div>

      <div>
        <div className="text-[10px] text-gray-500 mb-1.5">Simulate signal direction:</div>
        <div className="grid grid-cols-2 gap-2">
          {(["BUY", "SELL"] as const).map(d => (
            <button key={d} onClick={() => { setDirection(d); setResults([]); }}
              className={cn("py-2 rounded-lg text-xs font-bold border transition-colors",
                direction === d
                  ? d === "BUY" ? "bg-green-500/20 text-green-400 border-green-500/40"
                                : "bg-red-500/20 text-red-400 border-red-500/40"
                  : "bg-transparent text-gray-500 border-border hover:border-gray-600"
              )}>
              <div className="font-bold">{d === "BUY" ? "NIFTY UP" : "NIFTY DOWN"}</div>
              <div className="text-[10px] font-normal opacity-70 mt-0.5">
                Best {d === "BUY" ? "CE" : "PE"} auto-selected
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-500">{selectedUnderlying} spot</span>
        <span className={cn("font-mono font-bold", spotLtp ? "text-white" : "text-gray-600")}>
          {spotLtp ? formatPrice(spotLtp) : "No live data"}
        </span>
      </div>

      <button onClick={() => query.mutate()} disabled={!spotLtp || query.isPending}
        className="w-full py-2.5 rounded-lg text-xs font-semibold transition-colors
                   bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30
                   disabled:opacity-40 disabled:cursor-not-allowed">
        {query.isPending ? "Scoring candidates..." : `Preview best ${optionType} for ${selectedUnderlying}`}
      </button>

      {results.length > 0 && (
        <div className="space-y-2 pt-1">
          <div className="text-[10px] text-gray-500">
            {direction === "BUY" ? "NIFTY UP" : "NIFTY DOWN"} signal would select:
          </div>
          {results.map((o, i) => (
            <div key={o.token} className={cn("rounded-lg border p-3 text-xs space-y-2",
              i === 0 ? "border-yellow-500/30 bg-yellow-500/5" : "border-border bg-background/40 opacity-70")}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {i === 0
                    ? <span className="text-[10px] font-bold text-yellow-400 bg-yellow-500/10 border border-yellow-500/20 px-1.5 py-0.5 rounded">AUTO-SELECT</span>
                    : <span className="text-[10px] text-gray-600">Alt {i + 1}</span>}
                  <span className="font-bold font-mono text-white">{o.strike} {o.option_type}</span>
                </div>
                <div className="text-right">
                  <div className={cn("text-sm font-bold font-mono",
                    o.score >= 80 ? "text-green-400" : o.score >= 60 ? "text-yellow-400" : "text-red-400")}>
                    {o.score.toFixed(0)}/100
                  </div>
                  <div className="text-[10px] text-gray-600">score</div>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-x-3 gap-y-1 text-[10px]">
                {[
                  { label: "LTP",    val: formatPrice(o.ltp),         cls: "text-white font-medium" },
                  { label: "Delta",  val: o.delta?.toFixed(3),        cls: o.delta >= 0.35 && o.delta <= 0.50 ? "text-green-400" : "text-yellow-400" },
                  { label: "IV",     val: `${o.iv?.toFixed(1)}%`,     cls: "text-blue-400" },
                  { label: "OI",     val: formatNumber(o.oi),         cls: "text-gray-300" },
                  { label: "Vol",    val: formatNumber(o.volume),     cls: "text-gray-300" },
                  { label: "Spread", val: `${o.spread_pct?.toFixed(1)}%`, cls: (o.spread_pct || 0) > 2 ? "text-red-400" : "text-green-400" },
                ].map(({ label, val, cls }) => (
                  <div key={label}>
                    <span className="text-gray-500">{label} </span>
                    <span className={cn("font-mono", cls)}>{val}</span>
                  </div>
                ))}
              </div>
              {i === 0 && (
                <div className="flex flex-wrap gap-1 pt-1 border-t border-border/30">
                  {[
                    { label: "Spread ≤2%", ok: (o.spread_pct || 0) <= 2 },
                    { label: "OI ≥1L",     ok: (o.oi || 0) >= 100_000 },
                    { label: "Vol ≥5k",    ok: (o.volume || 0) >= 5_000 },
                    { label: "LTP ok",     ok: (o.ltp || 0) >= 15 && (o.ltp || 0) <= 600 },
                  ].map(f => (
                    <span key={f.label} className={cn("text-[9px] px-1.5 py-0.5 rounded border",
                      f.ok ? "text-green-400 border-green-500/20 bg-green-500/5"
                           : "text-red-400 border-red-500/20 bg-red-500/5")}>
                      {f.ok ? "v" : "x"} {f.label}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
          <p className="text-[10px] text-gray-600 pt-1">
            This selection happens automatically when a strategy signal fires. You do not need to click anything.
          </p>
        </div>
      )}
    </div>
  );
}
