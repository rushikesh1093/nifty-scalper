/**
 * @deprecated Use CapitalWidget instead — it provides the same functionality
 * with a richer UI and is the component actually mounted on the trading page.
 * This file is kept for reference only and is not rendered anywhere.
 */
"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getConfig, setTradeAmount, setCapital } from "@/lib/tradingApi";
import { cn } from "@/lib/utils";
import toast from "react-hot-toast";
import type { TradePreviewRow } from "@/types/trading";

const PRESET_AMOUNTS = [5000, 10000, 15000, 20000, 25000, 50000];

function fmt(n: number) {
  return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

export function TradeAmountSetter() {
  const qc = useQueryClient();
  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: getConfig, refetchInterval: 15000 });

  const [tradeAmt,  setTradeAmt]  = useState<string>("");
  const [capitalAmt, setCapitalAmt] = useState<string>("");
  const [preview,   setPreview]   = useState<TradePreviewRow[]>([]);
  const [showPreview, setShowPreview] = useState(false);

  const mutate = useMutation({
    mutationFn: ({ ta, cap }: { ta: number; cap?: number }) =>
      setTradeAmount(ta, cap),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["capital"] });
      setPreview(data.preview || []);
      setShowPreview(true);
      toast.success(`Trade amount set to ₹${data.trade_amount.toLocaleString("en-IN")}`);
    },
    onError: (e: any) => toast.error(e.response?.data?.detail || "Failed to update"),
  });

  const capitalMutation = useMutation({
    mutationFn: (cap: number) => setCapital(cap),
    onSuccess: (data: any) => {
      qc.invalidateQueries({ queryKey: ["config"] });
      qc.invalidateQueries({ queryKey: ["capital"] });
      toast.success(`Capital set to ₹${data.capital?.toLocaleString("en-IN")}`);
      setCapitalAmt("");
    },
    onError: (e: any) => toast.error(e.response?.data?.detail || "Failed"),
  });

  const handleSet = () => {
    const ta = parseFloat(tradeAmt);
    if (!ta || ta <= 0) { toast.error("Enter a valid amount"); return; }
    const cap = capitalAmt ? parseFloat(capitalAmt) : undefined;
    mutate.mutate({ ta, cap });
  };

  const handlePreset = (amt: number) => {
    setTradeAmt(String(amt));
    mutate.mutate({ ta: amt });
  };

  const handleCapitalSet = () => {
    const c = parseFloat(capitalAmt);
    if (!c || c <= 0) { toast.error("Enter a valid capital amount"); return; }
    capitalMutation.mutate(c);
  };

  return (
    <div className="bg-surface border border-border rounded-xl p-4 space-y-4">
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Trade Sizing
      </h3>

      {/* Current values */}
      {cfg && (
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="bg-background rounded-lg p-2">
            <div className="text-gray-500 mb-0.5">Current per-trade</div>
            <div className="text-accent font-bold font-mono text-sm">
              {fmt(cfg.trade_amount)}
            </div>
          </div>
          <div className="bg-background rounded-lg p-2">
            <div className="text-gray-500 mb-0.5">Total capital</div>
            <div className="text-white font-bold font-mono text-sm">
              {fmt(cfg.capital)}
            </div>
          </div>
        </div>
      )}

      {/* Preset buttons */}
      <div>
        <div className="text-[10px] text-gray-500 mb-1.5">Quick set trade amount</div>
        <div className="grid grid-cols-3 gap-1.5">
          {PRESET_AMOUNTS.map(amt => (
            <button
              key={amt}
              onClick={() => handlePreset(amt)}
              className={cn(
                "py-1.5 rounded-lg text-xs font-medium border transition-colors",
                cfg?.trade_amount === amt
                  ? "bg-accent/20 border-accent text-accent"
                  : "bg-background border-border text-gray-400 hover:border-gray-500 hover:text-white"
              )}
            >
              {fmt(amt)}
            </button>
          ))}
        </div>
      </div>

      {/* Custom trade amount input */}
      <div className="space-y-2">
        <div className="text-[10px] text-gray-500">Custom trade amount (₹)</div>
        <div className="flex gap-2">
          <input
            type="number"
            value={tradeAmt}
            onChange={e => setTradeAmt(e.target.value)}
            placeholder="e.g. 12500"
            className="flex-1 bg-background border border-border rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:border-accent focus:outline-none font-mono"
          />
          <button
            onClick={handleSet}
            disabled={mutate.isPending}
            className="px-4 py-2 bg-accent hover:bg-blue-600 text-white rounded-lg text-sm font-medium disabled:opacity-50 transition-colors"
          >
            Set
          </button>
        </div>
      </div>

      {/* Capital setter */}
      <div className="space-y-2 pt-2 border-t border-border/40">
        <div className="text-[10px] text-gray-500">Update total capital (₹)</div>
        <div className="flex gap-2">
          <input
            type="number"
            value={capitalAmt}
            onChange={e => setCapitalAmt(e.target.value)}
            placeholder={cfg ? String(cfg.capital) : "e.g. 100000"}
            className="flex-1 bg-background border border-border rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:border-accent focus:outline-none font-mono"
          />
          <button
            onClick={handleCapitalSet}
            disabled={capitalMutation.isPending}
            className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg text-sm font-medium disabled:opacity-50 transition-colors"
          >
            Update
          </button>
        </div>
        <p className="text-[10px] text-gray-600">
          ⚠️ Updating capital resets used_capital to 0
        </p>
      </div>

      {/* Preview table */}
      {showPreview && preview.length > 0 && (
        <div className="space-y-1.5 pt-2 border-t border-border/40">
          <div className="flex items-center justify-between">
            <div className="text-[10px] text-gray-500 uppercase tracking-wider">
              Lot preview at current trade amount
            </div>
            <button onClick={() => setShowPreview(false)} className="text-[10px] text-gray-600 hover:text-gray-400">
              hide
            </button>
          </div>
          <div className="overflow-auto max-h-40">
            <table className="w-full text-[10px]">
              <thead>
                <tr className="text-gray-600 uppercase">
                  <th className="py-1 text-left">Option LTP</th>
                  <th className="py-1 text-left">Lot Size</th>
                  <th className="py-1 text-center">Lots</th>
                  <th className="py-1 text-right">Cost/Lot</th>
                  <th className="py-1 text-right">Deployed</th>
                </tr>
              </thead>
              <tbody>
                {preview
                  .filter(r => r.lot_size === 50)   /* show Nifty only in compact view */
                  .map((r, i) => (
                    <tr key={i} className="border-t border-border/20">
                      <td className="py-1 font-mono text-white">₹{r.ltp}</td>
                      <td className="py-1 text-gray-400">{r.lot_size}</td>
                      <td className="py-1 text-center font-bold text-accent">{r.lots}</td>
                      <td className="py-1 text-right font-mono text-gray-400">{fmt(r.capital_needed)}</td>
                      <td className="py-1 text-right font-mono text-yellow-400">{fmt(r.capital_used)}</td>
                    </tr>
                  ))
                }
              </tbody>
            </table>
          </div>
          <p className="text-[10px] text-gray-600">
            Lots = floor(trade_amount ÷ ltp × lot_size), min 1
          </p>
        </div>
      )}
    </div>
  );
}
